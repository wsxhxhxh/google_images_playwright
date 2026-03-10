# main.py
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, List

import aiohttp

from playwright_async_fixed import search_keyword_batch, BrowserPool
from config import logger, Config
from platform_api import (AsyncTokenManager, AsyncProxyPool, get_task_info,
                          fetch_tasks_from_api, update_task_status)


@dataclass
class SearchTaskParams:
    worker_id: int
    task_id: int
    tasks: List
    dbname: str
    binddomain: str
    language_code: str
    usenum: int
    desimagenum: int
    languageid: int
    jxycategory_id: str
    proxies: Dict
    collect_platform_type: List[str]
    app: AsyncProxyPool
    atm: AsyncTokenManager


async def worker(worker_id: int, pool: BrowserPool):
    while True:
        session = None
        no_keyword_num = 0
        try:
            session = aiohttp.ClientSession()
            work_info = await get_task_info(atm, session)
            dbname = work_info.get("product_db_name")
            datanum = work_info.get("keyword_count")
            binddomain = work_info.get("server_main_domain")
            usenum = work_info.get("product_count")
            jxycategory_id = work_info.get("category_id")
            desimagenum = work_info.get("image_count")
            task_name = work_info.get("task_name")
            task_id = work_info.get("id")
            collect_platform_type = work_info.get("collect_platform_type")
            language_id = work_info.get("language_id")
            language_code = Config.LANGUAGE_CODE_MAP.get(work_info.get("language_code"), "en-US")
            logger.info(f"get work info: {task_name}")

            tasks = await fetch_tasks_from_api(session, dbname, datanum, binddomain)
            if not tasks:
                no_keyword_num += 1
            if no_keyword_num >= 20:
                await update_task_status(atm, session, task_id)
            logger.info(f"fetch task num: {len(tasks)} {tasks[:3]}...")

            params = SearchTaskParams(
                worker_id=worker_id,
                tasks=tasks,
                dbname=dbname,
                binddomain=binddomain,
                language_code=language_code,
                usenum=usenum,
                desimagenum=desimagenum,
                languageid=language_id,
                jxycategory_id=jxycategory_id,
                task_id=task_id,
                proxies=None,
                collect_platform_type=collect_platform_type,
                app=app,
                atm=atm,
            )

            await search_keyword_batch(params, pool, language_code)

        except IndexError:
            logger.info("no task, sleep 60s")
            await asyncio.sleep(60)

        except Exception as e:
            logger.exception(e)

        finally:
            if session:
                try:
                    await session.close()
                except Exception as e:
                    logger.warning(f"close session failed: {e}")

async def monitor_pool(pool: BrowserPool, interval: int = 30):
    """
    每隔 interval 秒打印一次池子状态和速度统计
    """
    last_success = 0
    last_time = asyncio.get_event_loop().time()

    while True:
        await asyncio.sleep(interval)

        now = asyncio.get_event_loop().time()
        elapsed = now - last_time

        current_success = pool.total_success  # 需要在pool里加这个计数器
        delta = current_success - last_success
        speed = delta / elapsed * 60  # 词/分钟

        idle = pool._idle_queue.qsize()
        total = pool.total_slots

        logger.info(
            f"[Monitor] "
            f"速度={speed:.1f}词/min | "
            f"idle={idle}/{total} | "
            f"成功={current_success} | "
            f"sorry累计={pool.total_sorry}"
        )

        last_success = current_success
        last_time = now


async def main():
    try:
        logger.info("get platform Token...")
        await asyncio.wait_for(atm.refresh_token(), timeout=60.0)
        token = await atm.get_token()
        logger.info(f"Token: {token}")

        logger.info(f"pull {Config.TOTAL_SLOTS} proxies init context...")
        initial_proxies = await app.get_random_proxies(Config.TOTAL_SLOTS)

        # language_code 用第一个 worker 的语言即可，后续每个 context 自己的 locale 由指纹随机决定
        # 如果你有多语言需求，可以把 language_code 列表传进来分配
        default_language = "en-US"

        pool = BrowserPool(
            chrome_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            max_browser=Config.MAX_BROWSER,
            max_context_per_browser=Config.MAX_CTX_PER_BROWSER,
            browser_fail_limit=3,
            startup_jitter=30.0,    # 18 个 context 在 0-30 秒内随机错峰启动
        )

        # 新版 start 需要传入代理列表和语言
        await pool.start(initial_proxies=initial_proxies, language_code=default_language)

        worker_tasks = []
        for worker_id in range(Config.TOTAL_SLOTS):
            t = asyncio.create_task(worker(worker_id + 1, pool), name=f"Work-{worker_id}")
            worker_tasks.append(t)

        logger.info(f"Create {len(worker_tasks)} Workers")

        asyncio.create_task(monitor_pool(pool, interval=30), name="monitor")
        await asyncio.gather(*worker_tasks)

    except asyncio.TimeoutError:
        logger.error("Init Timeout")
    except Exception as e:
        logger.exception(f"Main Exception: {e}")



if __name__ == '__main__':
    try:
        app = AsyncProxyPool()
        atm = AsyncTokenManager()
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("User Kill")
    except Exception as e:
        logger.exception(f"Main Exception Quit: {e}")
    finally:
        logger.info("Main Finish")