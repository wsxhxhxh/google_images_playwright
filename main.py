# main.py
import json
import signal
import random
import threading
import time
from dataclasses import dataclass
from typing import Dict, List

from browser_ruyipage import search_keyword_batch
from config import Config
from dblocal import RedisSetReader
from log import logger
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
    rsr: RedisSetReader


def worker(worker_id: int, stop_event: threading.Event, atm: TokenManager, app: ProxyPool):
    logger.info(f"[Worker-{worker_id}] Start")
    print_sleep = True
    try:
        while not stop_event.is_set():

            task_info = get_task_info(atm)
            if not task_info:
                if print_sleep:
                    logger.info(f"not task_info, sleep...")
                print_sleep = False
                delay = random.uniform(4, 6)
                stop_event.wait(delay)
                continue
            cpt = task_info.get("collect_platform_type")
            if cpt and type(cpt) == str:
                cpt = json.loads(cpt)
            print_sleep = True
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
                    collect_platform_type=cpt,
                    app=app,
                    atm=atm,
                    rsr=rsr,
                )

                search_keyword_batch(params)
            except Exception as exc:
                logger.exception(f"[Worker-{worker_id}] Exception: {exc}")
    finally:
        logger.info(f"[Worker-{worker_id}] Stop")

def main():
    stop_event = threading.Event()
    worker_threads = []
    try:
        logger.info("Found Token...")
        atm.refresh_token()
        token = atm.get_token()
        logger.info(f"Token Found Success: {token}")

        for wid in range(Config.TASK_NUM):
            thread = threading.Thread(
                target=worker,
                args=(wid + 1, stop_event, atm, app),
                name=f"Worker-{wid + 1}",
                daemon=True,
            )
            thread.start()
            worker_threads.append(thread)

        logger.info(f"Start {len(worker_threads)} Worker")
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Receive KeyboardInterrupt, Stoping...")
    finally:
        stop_event.set()
        for thread in worker_threads:
            thread.join(timeout=15)
        logger.info("All Stop")


if __name__ == '__main__':
    app = ProxyPool()
    atm = TokenManager()
    rsr = RedisSetReader()
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Force Close...")
    except Exception as exc:
        logger.exception(f"MainQuitException: {exc}")
    finally:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        logger.info("Process Termination")