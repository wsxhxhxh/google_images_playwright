# dblocal.py
import asyncio
import aiosqlite
from typing import List, Dict, Optional, Callable, Awaitable
from config import logger, special_logger


class DbManager:
    def __init__(self, db_path: str = "tasks.db", low_watermark: int = 20,
                 fetch_func: Optional[Callable[[], Awaitable[None]]] = None):
        self.db_path      = db_path
        self.db: Optional[aiosqlite.Connection] = None

        self.low_watermark = low_watermark   # 由 main() 动态更新
        self.fetch_func    = fetch_func      # 由 main() 注入

        self._refresh_lock     = asyncio.Lock()
        self._transaction_lock = asyncio.Lock()

    # ═══════════════════════════════════════════════════════════════
    # 初始化 / 关闭
    # ═══════════════════════════════════════════════════════════════

    async def init(self):
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")

        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword     TEXT,
                keyword_id  INTEGER,
                task_id     INTEGER,
                status      INTEGER DEFAULT 0,
                err_num     INTEGER DEFAULT 0,
                create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_unique
            ON tasks(keyword, keyword_id, task_id)
        """)
        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_status ON tasks(status)
        """)
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

    # ═══════════════════════════════════════════════════════════════
    # 写操作
    # ═══════════════════════════════════════════════════════════════

    async def refresh_tasks(self, task_list: List[Dict]):
        """批量插入（已存在则忽略）"""
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

    async def mark_success(self, task_id: int):
        await self.db.execute("""
            UPDATE tasks SET status = 2, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (task_id,))
        await self.db.commit()

    async def mark_failed(self, task_id: int):
        async with self.db.execute(
            "SELECT err_num FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return

        err_num = row[0] + 1
        # 失败不超过 3 次则重置为 pending，否则永久标记失败（status=3）
        new_status = 0 if err_num < 3 else 3
        await self.db.execute("""
            UPDATE tasks
            SET err_num = ?, status = ?, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (err_num, new_status, task_id))
        await self.db.commit()

    # ═══════════════════════════════════════════════════════════════
    # 读操作
    # ═══════════════════════════════════════════════════════════════

    async def get_pending_count(self) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 0"
        ) as cursor:
            row = await cursor.fetchone()
        return row[0]

    async def fetch_one_task_safe(self, task_id: int) -> Optional[Dict]:
        """
        原子取任务：status 0 → 1。
        修复原版 SQL 语法错误（WHERE status=0, and → WHERE status=0 AND）。
        """
        async with self._transaction_lock:
            try:
                async with self.db.execute("""
                    SELECT id, keyword, keyword_id, task_id
                    FROM tasks
                    WHERE status = 0 AND task_id = ?
                    LIMIT 1
                """, (task_id,)) as cursor:
                    row = await cursor.fetchone()

                if not row:
                    return None

                row_id = row[0]

                await self.db.execute("""
                    UPDATE tasks
                    SET status = 1, update_time = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 0
                """, (row_id,))

                if self.db.total_changes == 0:
                    # 被其他 worker 抢走
                    return None

                await self.db.commit()

                return {
                    "id":         row[0],
                    "keyword":    row[1],
                    "keyword_id": row[2],
                    "task_id":    row[3],
                }

            except Exception as e:
                await self.db.rollback()
                logger.error(f"[DB] 获取任务失败: {e}")
                return None

    # ═══════════════════════════════════════════════════════════════
    # 低水线自动补词
    # ═══════════════════════════════════════════════════════════════

    async def auto_refresh_if_needed(self):
        """
        检查 pending 数量是否低于低水线；若低则调用 fetch_func 补词。
        fetch_func 由 main() 注入，签名为 async () -> None。
        使用双检锁防止多 worker 并发重复触发。
        """
        if not self.fetch_func:
            return

        # 快速路径（不加锁）
        if await self.get_pending_count() >= self.low_watermark:
            return

        async with self._refresh_lock:
            # 加锁后再检查一次
            if await self.get_pending_count() >= self.low_watermark:
                return

            logger.info(
                f"[DB] pending 低于水线 {self.low_watermark}，触发补词..."
            )
            try:
                await self.fetch_func()
            except Exception as e:
                logger.error(f"[DB] 自动补词失败: {e}")

    # ═══════════════════════════════════════════════════════════════
    # 统计 / 日志
    # ═══════════════════════════════════════════════════════════════

    async def get_status_stats(self) -> Dict:
        async with self.db.execute("""
            SELECT status, COUNT(*) FROM tasks GROUP BY status
        """) as cursor:
            rows = await cursor.fetchall()

        stats = {"pending": 0, "processing": 0, "success": 0, "failed": 0}
        for status, count in rows:
            if status == 0: stats["pending"]    = count
            elif status == 1: stats["processing"] = count
            elif status == 2: stats["success"]    = count
            elif status == 3: stats["failed"]     = count
        stats["total"] = sum(stats.values())
        return stats

    async def print_stats(self):
        stats = await self.get_status_stats()
        msg = (
            f"pending:{stats['pending']} | processing:{stats['processing']} | "
            f"success:{stats['success']} | failed:{stats['failed']} | total:{stats['total']}"
        )
        logger.info(msg)
        special_logger.info(msg)
