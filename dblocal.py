import asyncio
import aiosqlite
from typing import List, Dict, Optional
from config import logger, special_logger


class DbManager:
    def __init__(self, db_path: str = "tasks.db", low_watermark=20, fetch_func=None):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

        self.low_watermark = low_watermark  # 阈值
        self.fetch_func = fetch_func  # 外部获取函数

        self._refresh_lock = asyncio.Lock()  # 防止并发重复刷新
        self._transaction_lock = asyncio.Lock()  # 新增：控制事务并发

    async def init(self):
        """初始化数据库 + 索引"""
        self.db = await aiosqlite.connect(self.db_path)

        # ⭐ 开启 WAL 模式，允许多个读 + 一个写并发，大幅减少锁冲突
        await self.db.execute("PRAGMA journal_mode=WAL")
        # ⭐ 等锁超时 5 秒再报错，而不是立即抛 OperationalError
        await self.db.execute("PRAGMA busy_timeout=5000")

        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT,
            keyword_id INTEGER,
            task_id INTEGER,
            status INTEGER DEFAULT 0,
            err_num INTEGER DEFAULT 0,
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # 唯一索引（防重复）
        await self.db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_unique
        ON tasks(keyword, keyword_id, task_id)
        """)

        # 状态索引（性能关键）
        await self.db.execute("""
        CREATE INDEX IF NOT EXISTS idx_status
        ON tasks(status)
        """)

        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

    # ===============================
    # 1. 刷新任务（批量插入）
    # ===============================
    async def refresh_tasks(self, task_list: List[Dict]):
        """
        task_list = [
            {"keyword": "...", "keyword_id": "...", "task_id": "..."}
        ]
        """
        sql = """
        INSERT OR IGNORE INTO tasks (keyword, keyword_id, task_id, status, err_num)
        VALUES (?, ?, ?, 0, 0)
        """

        async with self.db.executemany(sql, [
            (t["keyword"], t["keyword_id"], t["task_id"])
            for t in task_list
        ]):
            pass

        await self.db.commit()

    # ===============================
    # 2. 获取一个任务（status=0 -> 1）
    # ===============================
    async def fetch_one_task(self) -> Optional[Dict]:

        await self.auto_refresh_if_needed()

        async with self.db.execute("""
            SELECT id, keyword, keyword_id, task_id
            FROM tasks
            WHERE status = 0
            LIMIT 1
        """) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        task_id = row[0]

        await self.db.execute("""
            UPDATE tasks
            SET status = 1, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (task_id,))
        await self.db.commit()

        return {
            "id": row[0],
            "keyword": row[1],
            "keyword_id": row[2],
            "task_id": row[3],
        }

    # ===============================
    # 3. 标记成功
    # ===============================
    async def mark_success(self, task_id: int):
        await self.db.execute("""
        UPDATE tasks
        SET status = 2, update_time = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (task_id,))
        await self.db.commit()

    # ===============================
    # 4. 标记失败（带重试）
    # ===============================
    async def mark_failed(self, task_id: int):
        async with self.db.execute("""
            SELECT err_num FROM tasks WHERE id = ?
        """, (task_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            return

        err_num = row[0] + 1

        if err_num < 3:
            await self.db.execute("""
            UPDATE tasks
            SET err_num = ?, status = 0, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
            """, (err_num, task_id))
        else:
            # 建议用 status=3 表示失败（更规范）
            await self.db.execute("""
            UPDATE tasks
            SET err_num = ?, status = 2, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
            """, (err_num, task_id))

        await self.db.commit()

    # ===============================
    # 5. 获取待处理数量
    # ===============================
    async def get_pending_count(self) -> int:
        async with self.db.execute("""
            SELECT COUNT(*) FROM tasks WHERE status = 0
        """) as cursor:
            row = await cursor.fetchone()

        return row[0]

    # ===============================
    # ⭐ 可选增强：原子取任务（推荐多worker用）
    # ===============================
    async def fetch_one_task_safe(self) -> Optional[Dict]:
        """
        强一致版本（推荐多协程/多进程）
        利用事务避免重复取任务
        """

        async with self._transaction_lock:
            try:
                # 检查当前是否在事务中
                async with self.db.execute("SELECT 1") as cursor:
                    pass

                # 直接执行查询和更新，让 aiosqlite 自动处理事务
                async with self.db.execute("""
                                           SELECT id, keyword, keyword_id, task_id
                                           FROM tasks
                                           WHERE status = 0 LIMIT 1
                                           """) as cursor:
                    row = await cursor.fetchone()

                if not row:
                    return None

                task_id = row[0]

                # 在同一连接中更新
                await self.db.execute("""
                                      UPDATE tasks
                                      SET status      = 1,
                                          update_time = CURRENT_TIMESTAMP
                                      WHERE id = ?
                                        AND status = 0 -- 增加条件确保原子性
                                      """, (task_id,))

                # 检查是否真的更新了（防止并发冲突）
                if self.db.total_changes == 0:
                    return None  # 被其他worker抢走了

                await self.db.commit()

                return {
                    "id": row[0],
                    "keyword": row[1],
                    "keyword_id": row[2],
                    "task_id": row[3],
                }

            except Exception as e:
                await self.db.rollback()
                logger.error(f"获取任务失败: {e}")
                return None

    async def auto_refresh_if_needed(self):
        """
        如果任务数低于阈值，则自动刷新
        """
        if not self.fetch_func:
            return

        count = await self.get_pending_count()

        if count >= self.low_watermark:
            return

        # 防止多个协程同时触发刷新
        async with self._refresh_lock:
            # 再检查一次（双检锁）
            count = await self.get_pending_count()
            if count >= self.low_watermark:
                return

            logger.info(f"[TaskManager] Tasks insufficient({count})，Start automatic refresh...")

            try:
                new_tasks = await self.fetch_func()
                if new_tasks:
                    await self.refresh_tasks(new_tasks)
                    logger.info(f"[TaskManager] Added tasks {len(new_tasks)}")
            except Exception as e:
                logger.info(f"[TaskManager] automatic refresh Failed:{e}")

    async def get_status_stats(self) -> Dict:
        """
        返回所有状态的统计信息
        """
        async with self.db.execute("""
                                   SELECT status, COUNT(*)
                                   FROM tasks
                                   GROUP BY status
                                   """) as cursor:
            rows = await cursor.fetchall()

        stats = {
            "pending": 0,  # status=0
            "processing": 0,  # status=1
            "success": 0,  # status=2
            "failed": 0  # status=3（如果你以后用）
        }

        for status, count in rows:
            if status == 0:
                stats["pending"] = count
            elif status == 1:
                stats["processing"] = count
            elif status == 2:
                stats["success"] = count
            elif status == 3:
                stats["failed"] = count

        stats["total"] = sum(stats.values())

        return stats

    async def get_status_stats_raw(self):
        async with self.db.execute("""
                                   SELECT status, COUNT(*)
                                   FROM tasks
                                   GROUP BY status
                                   """) as cursor:
            return await cursor.fetchall()

    async def print_stats(self):
        stats = await self.get_status_stats()
        logger.info(
            f"pending:{stats['pending']} | "
            f"processing:{stats['processing']} | "
            f"success:{stats['success']} | "
            f"failed:{stats['failed']} | "
            f"total:{stats['total']}"
        )
        special_logger.info(
            f"pending:{stats['pending']} | "
            f"processing:{stats['processing']} | "
            f"success:{stats['success']} | "
            f"failed:{stats['failed']} | "
            f"total:{stats['total']}"
        )


async def main():
    dm = DbManager()
    await dm.init()

    # 插入任务
    await dm.refresh_tasks([
        {"keyword": "abc", "keyword_id": 1, "task_id": 1},
        {"keyword": "bbc", "keyword_id": 2, "task_id": 1},
    ])

    # 获取任务
    task = await dm.fetch_one_task()
    logger.info(task)

    if task:
        try:
            # 模拟执行
            await dm.mark_success(task["id"])
        except:
            await dm.mark_failed(task["id"])

    count = await dm.get_pending_count()
    logger.info(f"pending:{count}")

    await dm.close()


asyncio.run(main())