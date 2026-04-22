# main.py
import json
import signal
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from browser_ruyipage import search_keyword_batch
from config import logger, Config
from platform_api import TokenManager, ProxyPool, get_task_info, fetch_tasks_from_api
from dblocal import DbManager


@dataclass
class SearchTaskParams:
    """搜索任务参数类"""
    worker_id: int
    agent_url: str
    agent_key: str
    dbuser: str
    dbpasswd: str
    task_id: int
    tasks: List
    dbname: str
    binddomain: str
    language_code: str
    usenum: int
    datanum: int
    no_keyword_num: int
    desimagenum: int
    languageid: int
    jxycategory_id: str
    proxies: Dict | None
    collect_platform_type: List[str]
    app: ProxyPool
    atm: TokenManager
    db: DbManager


_fetch_lock = threading.Lock()
_task_info_lock = threading.Lock()
_current_task_info: Optional[dict] = None


def get_current_task_info_snapshot() -> Optional[dict]:
    with _task_info_lock:
        return dict(_current_task_info) if _current_task_info else None


def set_current_task_info(task_info: dict) -> None:
    global _current_task_info
    with _task_info_lock:
        _current_task_info = dict(task_info) if task_info else None

def fetch_and_load_keywords(atm: TokenManager, db: DbManager):
    """拉取 task_info -> 拉关键词 -> 写入 SQLite。"""
    with _fetch_lock:
        task_info = None
        for attempt in range(3):
            task_info = get_task_info(atm)
            if task_info:
                break
            logger.info(f"[FetchKeyword] get_task_info 暂无任务，等待 10s (attempt {attempt + 1}/3)")
            time.sleep(10)

        if not task_info:
            logger.warning("[FetchKeyword] 连续 3 次未获取到 task_info，放弃本次补词")
            return

        set_current_task_info(task_info)

        class _FakeParams:
            pass

        fake = _FakeParams()
        fake.atm = atm
        fake.task_id = task_info.get("id")
        fake.dbname = task_info.get("product_db_name")
        fake.datanum = task_info.get("keyword_count", 50)
        fake.binddomain = task_info.get("server_main_domain")
        fake.agent_url = task_info.get("agent_url")
        fake.agent_key = task_info.get("agent_key")
        fake.dbuser = task_info.get("product_db_user")
        fake.dbpasswd = task_info.get("product_db_password")
        fake.usenum = task_info.get("product_count")
        fake.desimagenum = task_info.get("image_count")
        fake.languageid = task_info.get("language_id")
        fake.language_code = Config.LANGUAGE_CODE_MAP.get(task_info.get("language_code"), "en-US")
        fake.jxycategory_id = task_info.get("category_id")
        fake.collect_platform_type = task_info.get("collect_platform_type")

        raw_tasks = fetch_tasks_from_api(fake)
        if not raw_tasks:
            logger.info("[FetchKeyword] fetch_tasks_from_api 返回空，本次补词结束")
            return

        records = []
        for item_str in raw_tasks:
            try:
                item = json.loads(item_str)
                records.append({
                    "keyword": item["name"],
                    "keyword_id": item["id"],
                    "task_id": task_info.get("id"),
                })
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning(f"[FetchKeyword] 解析任务失败: {item_str!r} — {exc}")

        if records:
            db.refresh_tasks(records)
            logger.info(f"[FetchKeyword] 写入 {len(records)} 条关键词")


def worker(worker_id: int, stop_event: threading.Event, db: DbManager,
           atm: TokenManager, app: ProxyPool):
    logger.info(f"[Worker-{worker_id}] 启动")

    try:
        while not stop_event.is_set():
            pending = db.get_pending_count()
            if pending == 0:
                logger.info(f"[Worker-{worker_id}] SQLite 暂无任务，尝试补词...")
                try:
                    if db.fetch_func:
                        db.fetch_func()
                except Exception as exc:
                    logger.exception(f"[Worker-{worker_id}] 空队列补词失败: {exc}")

                pending = db.get_pending_count()
                if pending == 0:
                    logger.info(f"[Worker-{worker_id}] 补词后仍无任务，等待 10s")
                    stop_event.wait(10)
                continue

            task_info = get_current_task_info_snapshot()
            if task_info is None:
                logger.info(f"task_info 尚未就绪，等待 5s")
                stop_event.wait(5)
                continue

            try:
                params = SearchTaskParams(
                    worker_id=worker_id,
                    tasks=[],
                    agent_url=task_info.get("agent_url"),
                    agent_key=task_info.get("agent_key"),
                    dbuser=task_info.get("product_db_user"),
                    dbpasswd=task_info.get("product_db_password"),
                    dbname=task_info.get("product_db_name"),
                    datanum=task_info.get("keyword_count", 50),
                    binddomain=task_info.get("server_main_domain"),
                    language_code=Config.LANGUAGE_CODE_MAP.get(
                        task_info.get("language_code"), "en-US"
                    ),
                    usenum=task_info.get("product_count"),
                    desimagenum=task_info.get("image_count"),
                    languageid=task_info.get("language_id"),
                    no_keyword_num=0,
                    jxycategory_id=task_info.get("category_id"),
                    task_id=task_info.get("id"),
                    proxies=None,
                    collect_platform_type=task_info.get("collect_platform_type"),
                    app=app,
                    atm=atm,
                    db=db,
                )

                search_keyword_batch(params)
            except Exception as exc:
                logger.exception(f"[Worker-{worker_id}] 异常: {exc}")
    finally:
        db.close()
        logger.info(f"[Worker-{worker_id}] 已停止")

def main():
    stop_event = threading.Event()
    worker_threads = []
    try:
        logger.info("获取平台 Token...")
        atm.refresh_token()
        token = atm.get_token()
        logger.info(f"Token 获取成功: {token}")

        db.init()
        logger.info("tasks.db 初始化成功")
        db.print_stats()

        db.fetch_func = lambda: fetch_and_load_keywords(atm, db)

        logger.info("首次补词...")
        fetch_and_load_keywords(atm, db)

        current_task_info = get_current_task_info_snapshot()
        if current_task_info:
            real_datanum = current_task_info.get("keyword_count", 50)
            db.low_watermark = int(Config.TASK_NUM * real_datanum * 0.3)
            logger.info(f"低水线设置为 {db.low_watermark}")

        db.print_stats()

        for wid in range(Config.TASK_NUM):
            thread = threading.Thread(
                target=worker,
                args=(wid + 1, stop_event, db, atm, app),
                name=f"Worker-{wid + 1}",
                daemon=True,
            )
            thread.start()
            worker_threads.append(thread)

        logger.info(f"已启动 {len(worker_threads)} 个 Worker")
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt，准备停止...")
    finally:
        stop_event.set()
        for thread in worker_threads:
            thread.join(timeout=15)
        db.close()
        logger.info("全部停止")


if __name__ == '__main__':
    app = ProxyPool()
    atm = TokenManager()
    db = DbManager(db_path="tasks.db")
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到退出信号，强制关闭...")
    except Exception as exc:
        logger.exception(f"main 异常退出: {exc}")
    finally:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        logger.info("进程退出")