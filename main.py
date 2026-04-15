# main.py
import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional

import aiohttp

from playwright_async_fixed import search_keyword_batch
from config import logger, Config
from platform_api import AsyncTokenManager, AsyncProxyPool, get_task_info
from dblocal import DbManager


@dataclass
class SearchTaskParams:
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


_fetch_lock = asyncio.Lock()
_current_task_info: Optional[dict] = None


def normalize_task_info(task_info: Optional[dict]) -> Optional[dict]:
    if not task_info:
        return None

    info = dict(task_info)
    info["_normalized_language_code"] = Config.LANGUAGE_CODE_MAP.get(
        info.get("language_code"), info.get("language_code") or "en-US"
    )
    return info


async def fetch_next_task_info(atm: AsyncTokenManager) -> Optional[dict]:
    """
    只负责获取“新 task 信息”，不在 main 里补关键词。
    关键词补充交给 playwright_async_fixed.py 中的任务循环统一处理。
    """
    global _current_task_info

    async with _fetch_lock:
        async with aiohttp.ClientSession() as session:
            task_info = None
            for attempt in range(3):
                try:
                    task_info = await get_task_info(atm, session)
                except Exception as e:
                    logger.error(f"[FetchTask] get_task_info 异常: {e}")
                    task_info = None

                if task_info:
                    break

                logger.info(f"[FetchTask] 暂无 task，等待 10s (attempt {attempt + 1}/3)")
                await asyncio.sleep(10)

            if not task_info:
                logger.warning("[FetchTask] 连续 3 次未获取到 task_info")
                return None

            _current_task_info = normalize_task_info(task_info)
            logger.info(
                f"[FetchTask] 获取到 task: "
                f"id={_current_task_info.get('id')}, "
                f"language_code={_current_task_info.get('_normalized_language_code')}"
            )
            return _current_task_info


async def worker(worker_id: int, stop_event: asyncio.Event, db: DbManager,
                 atm: AsyncTokenManager, app: AsyncProxyPool):
    global _current_task_info

    logger.info(f"[Worker-{worker_id}] 启动")

    while not stop_event.is_set():
        if _current_task_info is None:
            await fetch_next_task_info(atm)
            if _current_task_info is None:
                await asyncio.sleep(5)
                continue

        task_info = dict(_current_task_info)
        session = aiohttp.ClientSession()

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
                language_code=task_info.get("_normalized_language_code", "en-US"),
                usenum=task_info.get("product_count"),
                desimagenum=task_info.get("image_count"),
                languageid=task_info.get("language_id"),
                no_keyword_num=0,
                jxycategory_id=task_info.get("category_id"),
                task_id=task_info.get("id"),
                proxies=None,
                collect_platform_type=task_info.get("collect_platform_type") or [],
                session=session,
                app=app,
                atm=atm,
                db=db,
            )

            await search_keyword_batch(params)
            await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info(f"[Worker-{worker_id}] 被取消，退出")
            break
        except Exception as e:
            logger.exception(f"[Worker-{worker_id}] 异常: {e}")
            await asyncio.sleep(3)
        finally:
            try:
                await session.close()
            except Exception as e:
                logger.warning(f"[Worker-{worker_id}] 关闭 session 失败: {e}")

    logger.info(f"[Worker-{worker_id}] 已停止")


async def main():
    stop_event = asyncio.Event()
    worker_tasks = []

    try:
        logger.info("获取平台 Token...")
        await asyncio.wait_for(atm.refresh_token(), timeout=60.0)
        token = await atm.get_token()
        logger.info(f"Token 获取成功: {token}")

        await db.init()
        logger.info("tasks.db 初始化成功")
        await db.print_task_meta_stats()

        async def _fetch_func():
            await fetch_next_task_info(atm)

        db.fetch_func = _fetch_func

        logger.info("启动前预取一次 task_info...")
        await fetch_next_task_info(atm)

        for wid in range(Config.TASK_NUM):
            t = asyncio.create_task(
                worker(wid + 1, stop_event, db, atm, app),
                name=f"Worker-{wid + 1}",
            )
            worker_tasks.append(t)

        logger.info(f"已启动 {len(worker_tasks)} 个 Worker")
        await asyncio.gather(*worker_tasks)

    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt，准备停止...")
        stop_event.set()
        for t in worker_tasks:
            t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*worker_tasks, return_exceptions=True),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning("部分 Worker 未在 15s 内退出，强制结束")

    except asyncio.TimeoutError:
        logger.error("初始化超时（Token 获取失败）")

    except Exception as e:
        logger.exception(f"Main 异常: {e}")

    finally:
        await db.close()
        logger.info("全部停止")


if __name__ == '__main__':
    app = AsyncProxyPool()
    atm = AsyncTokenManager()
    db = DbManager(db_path="tasks.db")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(main())
    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到退出信号，强制关闭...")
    except Exception as e:
        logger.exception(f"main 异常退出: {e}")
    finally:
        import signal
        import os

        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass

        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()

        if pending:
            async def _wait_cancelled():
                await asyncio.wait(pending, timeout=3.0)

            try:
                loop.run_until_complete(_wait_cancelled())
            except Exception:
                pass

        try:
            loop.close()
        except Exception:
            pass

        logger.info("进程退出")
        os._exit(0)
