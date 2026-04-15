# dblocal.py
import asyncio
import aiosqlite
from typing import List, Dict, Optional, Callable, Awaitable
from config import logger, special_logger


TASK_PENDING = 0
TASK_RUNNING = 1
TASK_SUCCESS = 2
TASK_FAILED = 3

KEYWORD_PENDING = 0
KEYWORD_RUNNING = 1
KEYWORD_SUCCESS = 2
KEYWORD_FAILED = 3


class DbManager:
    def __init__(
        self,
        db_path: str = "tasks.db",
        fetch_func: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

        # 现在 fetch_func 只负责“去远端拿新的 task 信息”
        # 不再承担旧版“低水位补全局关键词池”的职责
        self.fetch_func = fetch_func

        self._transaction_lock = asyncio.Lock()
        self._task_lock = asyncio.Lock()

    async def init(self):
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")

        # task 主表：一个 task 一条记录
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS task_meta (
                task_id              INTEGER PRIMARY KEY,
                language_code        TEXT NOT NULL,
                keyword_table        TEXT NOT NULL,
                task_status          INTEGER DEFAULT 0,
                empty_fetch_count    INTEGER DEFAULT 0,
                create_time          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                update_time          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_meta_status
            ON task_meta(task_status, update_time)
        """)

        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

    # ============================================================
    # task_meta 管理
    # ============================================================

    async def ensure_task_table(self, task_id: int) -> str:
        """
        为 task 创建独立关键词表
        """
        table_name = f"task_keywords_{task_id}"

        await self.db.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword     TEXT,
                keyword_id  INTEGER,
                status      INTEGER DEFAULT 0,
                err_num     INTEGER DEFAULT 0,
                create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(keyword, keyword_id)
            )
        """)

        await self.db.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_status
            ON {table_name}(status)
        """)

        await self.db.commit()
        return table_name

    async def upsert_task_meta(self, task_id: int, language_code: str) -> Dict:
        """
        写入或更新 task_meta
        """
        table_name = await self.ensure_task_table(task_id)

        await self.db.execute("""
            INSERT INTO task_meta (
                task_id, language_code, keyword_table, task_status, empty_fetch_count
            )
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(task_id) DO UPDATE SET
                language_code = excluded.language_code,
                keyword_table = excluded.keyword_table,
                update_time   = CURRENT_TIMESTAMP
        """, (task_id, language_code, table_name, TASK_PENDING))
        await self.db.commit()

        return {
            "task_id": task_id,
            "language_code": language_code,
            "keyword_table": table_name,
            "task_status": TASK_PENDING,
            "empty_fetch_count": 0,
        }

    async def get_unfinished_task(self) -> Optional[Dict]:
        """
        先取本地未完成的 task
        """
        async with self.db.execute("""
            SELECT task_id, language_code, keyword_table, task_status, empty_fetch_count
            FROM task_meta
            WHERE task_status IN (?, ?)
            ORDER BY update_time ASC, create_time ASC
            LIMIT 1
        """, (TASK_PENDING, TASK_RUNNING)) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        return {
            "task_id": row[0],
            "language_code": row[1],
            "keyword_table": row[2],
            "task_status": row[3],
            "empty_fetch_count": row[4],
        }

    async def get_task_meta(self, task_id: int) -> Optional[Dict]:
        async with self.db.execute("""
            SELECT task_id, language_code, keyword_table, task_status, empty_fetch_count
            FROM task_meta
            WHERE task_id = ?
            LIMIT 1
        """, (task_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        return {
            "task_id": row[0],
            "language_code": row[1],
            "keyword_table": row[2],
            "task_status": row[3],
            "empty_fetch_count": row[4],
        }

    async def update_task_meta_status(self, task_id: int, task_status: int):
        await self.db.execute("""
            UPDATE task_meta
            SET task_status = ?, update_time = CURRENT_TIMESTAMP
            WHERE task_id = ?
        """, (task_status, task_id))
        await self.db.commit()

    async def increase_empty_fetch_count(self, task_id: int) -> int:
        await self.db.execute("""
            UPDATE task_meta
            SET empty_fetch_count = empty_fetch_count + 1,
                update_time = CURRENT_TIMESTAMP
            WHERE task_id = ?
        """, (task_id,))
        await self.db.commit()

        async with self.db.execute("""
            SELECT empty_fetch_count
            FROM task_meta
            WHERE task_id = ?
        """, (task_id,)) as cursor:
            row = await cursor.fetchone()

        return row[0] if row else 0

    async def reset_empty_fetch_count(self, task_id: int):
        await self.db.execute("""
            UPDATE task_meta
            SET empty_fetch_count = 0,
                update_time = CURRENT_TIMESTAMP
            WHERE task_id = ?
        """, (task_id,))
        await self.db.commit()

    # ============================================================
    # 关键词表操作
    # ============================================================

    async def refresh_keywords(self, keyword_table: str, task_list: List[Dict]):
        """
        批量插入关键词（已存在则忽略）
        """
        sql = f"""
            INSERT OR IGNORE INTO {keyword_table} (keyword, keyword_id, status, err_num)
            VALUES (?, ?, ?, 0)
        """
        await self.db.executemany(sql, [
            (t["keyword"], t["keyword_id"], KEYWORD_PENDING)
            for t in task_list
        ])
        await self.db.commit()

    async def fetch_one_keyword_safe(self, keyword_table: str) -> Optional[Dict]:
        """
        原子取关键词：status 0 -> 1
        """
        async with self._transaction_lock:
            try:
                async with self.db.execute(f"""
                    SELECT id, keyword, keyword_id
                    FROM {keyword_table}
                    WHERE status = ?
                    LIMIT 1
                """, (KEYWORD_PENDING,)) as cursor:
                    row = await cursor.fetchone()

                if not row:
                    return None

                row_id = row[0]
                cursor = await self.db.execute(f"""
                    UPDATE {keyword_table}
                    SET status = ?, update_time = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = ?
                """, (KEYWORD_RUNNING, row_id, KEYWORD_PENDING))
                await self.db.commit()

                # 注意：这里不能再用 total_changes
                if cursor.rowcount == 0:
                    return None

                return {
                    "id": row[0],
                    "keyword": row[1],
                    "keyword_id": row[2],
                }

            except Exception as e:
                await self.db.rollback()
                logger.error(f"[DB] 获取关键词失败: {e}")
                return None

    async def mark_keyword_success(self, keyword_table: str, row_id: int):
        await self.db.execute(f"""
            UPDATE {keyword_table}
            SET status = ?, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (KEYWORD_SUCCESS, row_id))
        await self.db.commit()

    async def mark_keyword_failed(self, keyword_table: str, row_id: int):
        async with self.db.execute(
            f"SELECT err_num FROM {keyword_table} WHERE id = ?",
            (row_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return

        err_num = row[0] + 1
        # 失败不超过 3 次则回到 pending，超过 3 次永久失败
        new_status = KEYWORD_PENDING if err_num < 3 else KEYWORD_FAILED

        await self.db.execute(f"""
            UPDATE {keyword_table}
            SET err_num = ?, status = ?, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (err_num, new_status, row_id))
        await self.db.commit()

    async def get_keyword_pending_count(self, keyword_table: str) -> int:
        async with self.db.execute(
            f"SELECT COUNT(*) FROM {keyword_table} WHERE status = ?",
            (KEYWORD_PENDING,)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0]

    # ============================================================
    # 调试 / 统计
    # ============================================================

    async def get_task_meta_stats(self) -> Dict:
        async with self.db.execute("""
            SELECT task_status, COUNT(*) FROM task_meta GROUP BY task_status
        """) as cursor:
            rows = await cursor.fetchall()

        stats = {"pending": 0, "running": 0, "success": 0, "failed": 0}
        for status, count in rows:
            if status == TASK_PENDING:
                stats["pending"] = count
            elif status == TASK_RUNNING:
                stats["running"] = count
            elif status == TASK_SUCCESS:
                stats["success"] = count
            elif status == TASK_FAILED:
                stats["failed"] = count
        stats["total"] = sum(stats.values())
        return stats

    async def print_task_meta_stats(self):
        stats = await self.get_task_meta_stats()
        msg = (
            f"task_meta => pending:{stats['pending']} | running:{stats['running']} | "
            f"success:{stats['success']} | failed:{stats['failed']} | total:{stats['total']}"
        )
        logger.info(msg)
        special_logger.info(msg)