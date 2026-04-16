# dblocal.py
import sqlite3
import threading
from typing import Callable, Dict, List, Optional
from config import logger, special_logger


class DbManager:
    def __init__(self, db_path: str = "tasks.db", low_watermark: int = 20,
                 fetch_func: Optional[Callable[[], None]] = None):
        self.db_path      = db_path
        self.db: Optional[sqlite3.Connection] = None

        self.low_watermark = low_watermark   # 由 main() 动态更新
        self.fetch_func    = fetch_func      # 由 main() 注入

        self._refresh_lock     = threading.Lock()
        self._transaction_lock = threading.Lock()

    # ═══════════════════════════════════════════════════════════════
    # 初始化 / 关闭
    # ═══════════════════════════════════════════════════════════════

    def init(self):
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")

        self.db.execute("""
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
        self.db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_unique
            ON tasks(keyword, keyword_id, task_id)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_status ON tasks(status)
        """)
        self.db.commit()

    def close(self):
        if self.db:
            self.db.close()

    # ═══════════════════════════════════════════════════════════════
    # 写操作
    # ═══════════════════════════════════════════════════════════════

    def refresh_tasks(self, task_list: List[Dict]):
        """批量插入（已存在则忽略）"""
        sql = """
            INSERT OR IGNORE INTO tasks (keyword, keyword_id, task_id, status, err_num)
            VALUES (?, ?, ?, 0, 0)
        """
        self.db.executemany(sql, [
            (t["keyword"], t["keyword_id"], t["task_id"])
            for t in task_list
        ])
        self.db.commit()

    def mark_success(self, task_id: int):
        self.db.execute("""
            UPDATE tasks SET status = 2, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (task_id,))
        self.db.commit()

    def mark_failed(self, task_id: int):
        cursor = self.db.execute("SELECT err_num FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            return

        err_num = row["err_num"] + 1
        new_status = 0 if err_num < 3 else 3
        self.db.execute("""
            UPDATE tasks
            SET err_num = ?, status = ?, update_time = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (err_num, new_status, task_id))
        self.db.commit()

    # ═══════════════════════════════════════════════════════════════
    # 读操作
    # ═══════════════════════════════════════════════════════════════

    def get_pending_count(self) -> int:
        cursor = self.db.execute("SELECT COUNT(*) FROM tasks WHERE status = 0")
        row = cursor.fetchone()
        return row[0]

    def fetch_one_task_safe(self, task_id: int) -> Optional[Dict]:
        """
        原子取任务：status 0 → 1。
        修复原版 SQL 语法错误（WHERE status=0, and → WHERE status=0 AND）。
        """
        with self._transaction_lock:
            try:
                before_changes = self.db.total_changes
                cursor = self.db.execute("""
                    SELECT id, keyword, keyword_id, task_id
                    FROM tasks
                    WHERE status = 0 AND task_id = ?
                    LIMIT 1
                """, (task_id,))
                row = cursor.fetchone()

                if not row:
                    return None

                row_id = row["id"]

                self.db.execute("""
                    UPDATE tasks
                    SET status = 1, update_time = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 0
                """, (row_id,))

                if self.db.total_changes == before_changes:
                    return None

                self.db.commit()

                return {
                    "id":         row["id"],
                    "keyword":    row["keyword"],
                    "keyword_id": row["keyword_id"],
                    "task_id":    row["task_id"],
                }

            except Exception as e:
                self.db.rollback()
                logger.error(f"[DB] 获取任务失败: {e}")
                return None

    # ═══════════════════════════════════════════════════════════════
    # 低水线自动补词
    # ═══════════════════════════════════════════════════════════════

    def auto_refresh_if_needed(self):
        """
        检查 pending 数量是否低于低水线；若低则调用 fetch_func 补词。
        fetch_func 由 main() 注入，签名为 sync () -> None。
        使用双检锁防止多 worker 并发重复触发。
        """
        if not self.fetch_func:
            return

        if self.get_pending_count() >= self.low_watermark:
            return

        with self._refresh_lock:
            if self.get_pending_count() >= self.low_watermark:
                return

            logger.info(
                f"[DB] pending 低于水线 {self.low_watermark}，触发补词..."
            )
            try:
                self.fetch_func()
            except Exception as e:
                logger.error(f"[DB] 自动补词失败: {e}")

    # ═══════════════════════════════════════════════════════════════
    # 统计 / 日志
    # ═══════════════════════════════════════════════════════════════

    def get_status_stats(self) -> Dict:
        cursor = self.db.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        rows = cursor.fetchall()

        stats = {"pending": 0, "processing": 0, "success": 0, "failed": 0}
        for status, count in rows:
            if status == 0: stats["pending"]    = count
            elif status == 1: stats["processing"] = count
            elif status == 2: stats["success"]    = count
            elif status == 3: stats["failed"]     = count
        stats["total"] = sum(stats.values())
        return stats

    def print_stats(self):
        stats = self.get_status_stats()
        msg = (
            f"pending:{stats['pending']} | processing:{stats['processing']} | "
            f"success:{stats['success']} | failed:{stats['failed']} | total:{stats['total']}"
        )
        logger.info(msg)
        special_logger.info(msg)
