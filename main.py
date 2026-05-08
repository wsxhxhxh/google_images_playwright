# main.py
import signal
import threading
import time
from dataclasses import dataclass
from typing import Dict, List

from browser_ruyipage import search_keyword_batch
from config import logger, Config
from platform_api import TokenManager, ProxyPool, get_task_info


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



def worker(worker_id: int, stop_event: threading.Event, atm: TokenManager, app: ProxyPool):
    logger.info(f"[Worker-{worker_id}] 启动")

    try:
        while not stop_event.is_set():

            task_info = get_task_info(atm)
            if not task_info:
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
                )

                search_keyword_batch(params)
            except Exception as exc:
                logger.exception(f"[Worker-{worker_id}] 异常: {exc}")
    finally:
        logger.info(f"[Worker-{worker_id}] 已停止")

def main():
    stop_event = threading.Event()
    worker_threads = []
    try:
        logger.info("获取平台 Token...")
        atm.refresh_token()
        token = atm.get_token()
        logger.info(f"Token 获取成功: {token}")

        for wid in range(Config.TASK_NUM):
            thread = threading.Thread(
                target=worker,
                args=(wid + 1, stop_event, atm, app),
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
        logger.info("全部停止")


if __name__ == '__main__':
    app = ProxyPool()
    atm = TokenManager()
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