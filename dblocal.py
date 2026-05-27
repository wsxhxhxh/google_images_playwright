# dblocal.py
import sqlite3
import threading
from typing import Callable, Dict, List, Optional

import redis

from log import logger, special_logger

class DbManager:
    def __init__(
        self,
        db_path: str = "tasks.db",
        low_watermark: int = 20,
        fetch_func: Optional[Callable[[], None]] = None,
    ):
        self.db_path = db_path
        self.low_watermark = low_watermark
        self.fetch_func = fetch_func

        # 只保留补词锁，避免多线程重复补词
        self._refresh_lock = threading.Lock()

        # 每个线程独立连接
        self._local = threading.local()

    # ═══════════════════════════════════════════════════════════════
    # 连接管理
    # ═══════════════════════════════════════════════════════════════

    def _get_conn(self) -> sqlite3.Connection:
        """
        每个线程独立一个 SQLite 连接，避免共享连接带来的串行化和线程竞争。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30,
                isolation_level=None,   # 手动控制事务
                check_same_thread=True, # 每个线程只用自己的连接
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    # ═══════════════════════════════════════════════════════════════
    # 初始化 / 关闭
    # ═══════════════════════════════════════════════════════════════

    def init(self):
        """
        初始化表结构。主线程调用一次即可。
        """
        conn = self._get_conn()

        conn.execute("""
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
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_unique
            ON tasks(keyword, keyword_id, task_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_status_task
            ON tasks(status, task_id, id)
        """)

    def close(self):
        """
        关闭当前线程自己的连接。
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            finally:
                self._local.conn = None

    # ═══════════════════════════════════════════════════════════════
    # 写操作
    # ═══════════════════════════════════════════════════════════════

    def refresh_tasks(self, task_list: List[Dict]):
        """批量插入（已存在则忽略）"""
        if not task_list:
            return

        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            sql = """
                INSERT OR IGNORE INTO tasks (keyword, keyword_id, task_id, status, err_num)
                VALUES (?, ?, ?, 0, 0)
            """
            conn.executemany(sql, [
                (t["keyword"], t["keyword_id"], t["task_id"])
                for t in task_list
            ])
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def mark_success(self, task_id: int):
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                UPDATE tasks
                SET status = 2, update_time = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (task_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def mark_failed(self, task_id: int):
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "SELECT err_num FROM tasks WHERE id = ?",
                (task_id,)
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return

            err_num = row["err_num"] + 1
            new_status = 0 if err_num < 3 else 3

            conn.execute("""
                UPDATE tasks
                SET err_num = ?, status = ?, update_time = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (err_num, new_status, task_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ═══════════════════════════════════════════════════════════════
    # 读操作
    # ═══════════════════════════════════════════════════════════════

    def get_pending_count(self) -> int:
        conn = self._get_conn()
        cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 0")
        row = cursor.fetchone()
        return row[0] if row else 0

    def fetch_one_task_safe(self, task_id: int) -> Optional[Dict]:
        """
        原子抢任务：status=0 -> status=1
        不再使用 Python 锁，而是依赖 SQLite 事务。
        """
        conn = self._get_conn()

        try:
            # 先抢写锁，保证这段事务内的 select + update 原子
            conn.execute("BEGIN IMMEDIATE")

            cursor = conn.execute("""
                SELECT id, keyword, keyword_id, task_id
                FROM tasks
                WHERE status = 0 AND task_id = ?
                ORDER BY id
                LIMIT 1
            """, (task_id,))
            row = cursor.fetchone()

            if not row:
                conn.rollback()
                return None

            result = conn.execute("""
                UPDATE tasks
                SET status = 1, update_time = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 0
            """, (row["id"],))

            if result.rowcount != 1:
                conn.rollback()
                return None

            conn.commit()

            return {
                "id": row["id"],
                "keyword": row["keyword"],
                "keyword_id": row["keyword_id"],
                "task_id": row["task_id"],
            }

        except sqlite3.OperationalError as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(f"[DB] 获取任务冲突/锁等待失败: {e}")
            return None

        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"[DB] 获取任务失败: {e}")
            return None

    # ═══════════════════════════════════════════════════════════════
    # 低水线自动补词
    # ═══════════════════════════════════════════════════════════════

    def auto_refresh_if_needed(self):
        """
        检查 pending 数量是否低于低水线；若低则调用 fetch_func 补词。
        使用双检锁避免多 worker 重复触发。
        """
        if not self.fetch_func:
            return

        if self.get_pending_count() >= self.low_watermark:
            return

        with self._refresh_lock:
            if self.get_pending_count() >= self.low_watermark:
                return

            logger.info(f"[DB] pending 低于水线 {self.low_watermark}，触发补词...")
            try:
                self.fetch_func()
            except Exception as e:
                logger.error(f"[DB] 自动补词失败: {e}")

    # ═══════════════════════════════════════════════════════════════
    # 统计 / 日志
    # ═══════════════════════════════════════════════════════════════

    def get_status_stats(self) -> Dict:
        conn = self._get_conn()
        cursor = conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        rows = cursor.fetchall()

        stats = {"pending": 0, "processing": 0, "success": 0, "failed": 0}
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

    def print_stats(self):
        stats = self.get_status_stats()
        msg = (
            f"pending:{stats['pending']} | processing:{stats['processing']} | "
            f"success:{stats['success']} | failed:{stats['failed']} | total:{stats['total']}"
        )
        logger.info(msg)
        special_logger.info(msg)


class RedisSetReader:
    def __init__(
            self,
            host: str = "127.0.0.1",
            port: int = 6379,
            password: str = "",
            db: int = 0,
            expire_minutes: int = 10  # 10分钟过期
    ):
        # 线程安全连接池
        self.redis_pool = redis.ConnectionPool(
            host=host,
            port=port,
            password=password,
            db=db,
            decode_responses=True  # 自动返回字符串
        )
        self.expire_seconds = expire_minutes * 60
        self.lock = threading.Lock()

    def _get_conn(self):
        # 获取线程安全连接
        return redis.Redis(connection_pool=self.redis_pool)

    def _get_key(self, task_id: str| int) -> str:
        # 每个 task_id 一个独立集合
        return f"task:set:{task_id}"

    # ====================== 添加数据（你写入时用） ======================
    def add(self, task_id: str| int, value: str) -> bool:
        """添加字符串，自动10分钟过期"""
        r = self._get_conn()
        key = self._get_key(task_id)
        with self.lock:
            r.sadd(key, value)
            r.expire(key, self.expire_seconds)
        return True

    def add_batch(self, task_id: str| int, values: List[str]) -> int:
        """批量添加"""
        if not values:
            return 0
        r = self._get_conn()
        key = self._get_key(task_id)
        with self.lock:
            cnt = r.sadd(key, *values)
            r.expire(key, self.expire_seconds)
        return cnt

    # ====================== ✅ 核心：随机读取 N 条（不删除！） ======================
    def random_get(self, task_id: str| int, count: int) -> List[str]:
        """
        随机获取 count 条数据
        🔥 不会删除原数据！！！
        """
        if count <= 0:
            return []

        r = self._get_conn()
        key = self._get_key(task_id)

        with self.lock:
            # 随机取，不删除（关键！）
            data = r.srandmember(key, count)

        return list(data)

    # ====================== 工具方法 ======================
    def count(self, task_id: str| int) -> int:
        """获取集合大小"""
        return self._get_conn().scard(self._get_key(task_id))


if __name__ == '__main__':
    rsssss = RedisSetReader()
    print("总数量：", rsssss.count(12))
    rsssss.add(12, "hello")
    print("总数量：", rsssss.count(12))
    rsssss.add_batch(12, ["hi", "lihua", "kangkang"])

    result = rsssss.random_get(12, 3)
    print("随机读取到：", result)

    result2 = rsssss.random_get(12, 5)
    print("再读一次：", result2)

    print("总数量：", rsssss.count(12))


