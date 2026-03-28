# main.py
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, List

import aiohttp

from playwright_async_fixed import search_keyword_batch
from config import logger, Config
from platform_api import (AsyncTokenManager, AsyncProxyPool, get_task_info)
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
    session: aiohttp.ClientSession
    app: AsyncProxyPool
    atm: AsyncTokenManager
    db: DbManager

async def worker(worker_id: int, stop_event: asyncio.Event):
    while not stop_event.is_set():
        session = None
        try:
            session = aiohttp.ClientSession()
            work_info = await get_task_info(atm, session)
            if not work_info:
                logger.info("not task sleep 10s")
                await asyncio.sleep(10)
                continue

            dbname = work_info.get("product_db_name")
            agent_url = work_info.get("agent_url")
            agent_key = work_info.get("agent_key")
            dbuser = work_info.get("product_db_user")
            dbpasswd = work_info.get("product_db_password")

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


            params = SearchTaskParams(
                worker_id=worker_id,
                tasks=[],
                agent_url=agent_url,
                agent_key=agent_key,
                dbuser=dbuser,
                dbpasswd=dbpasswd,
                dbname=dbname,
                datanum=datanum,
                binddomain=binddomain,
                language_code=language_code,
                usenum=usenum,
                desimagenum=desimagenum,
                languageid=language_id,
                no_keyword_num=0,
                jxycategory_id=jxycategory_id,
                task_id=task_id,
                proxies=None,
                collect_platform_type=collect_platform_type,
                session=session,
                app=app,
                atm=atm,
                db=db,
            )

            await search_keyword_batch(params)

        except asyncio.CancelledError:
            logger.info(f"worker {worker_id} cancelled")
            break

        except IndexError:
            logger.info("not task sleep 10s")
            await asyncio.sleep(10)

        except Exception as e:
            logger.exception(e)
        finally:
            # ⭐ 关闭session
            if session:
                try:
                    await session.close()
                except Exception as e:
                    logger.warning(f"close session failed: {e}")

async def main():
    """主函数"""
    stop_event = asyncio.Event()
    tasks = []

    try:
        logger.info("start get platform Token...")
        await asyncio.wait_for(atm.refresh_token(), timeout=60.0)
        token = await atm.get_token()
        logger.info(f"get platform success Token: {token}")

        await db.init()
        logger.info(f"tasks.db init success")
        await db.print_stats()

        # 创建任务
        for worker_id in range(Config.TASK_NUM):
            task = asyncio.create_task(worker(worker_id + 1, stop_event), name=f"Work-{worker_id}")
            tasks.append(task)

        logger.info(f"craet {len(tasks)} 个 Worker task")
        await asyncio.gather(*tasks)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, stopping...")

        # ⭐ 通知 worker 停止
        stop_event.set()

        # ⭐ 强制取消所有任务
        for task in tasks:
            task.cancel()

        # ⭐ 等待任务结束
        await asyncio.gather(*tasks, return_exceptions=True)


    except asyncio.TimeoutError:
        logger.error("init proxies pool failed!")
    except Exception as e:
        logger.exception(f"Main Exception: {e}")

    finally:
        await db.close()
        logger.info("All stopped")


if __name__ == '__main__':
    data = []
    app = AsyncProxyPool()
    atm = AsyncTokenManager()
    db = DbManager(db_path="tasks.db")
    asyncio.run(main())

