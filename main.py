# main.py
import json
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List

import aiohttp

from playwright_async_fixed import search_keyword_batch
from config import logger, Config
from platform_api import (AsyncTokenManager, AsyncProxyPool, get_task_info, fetch_tasks_from_api, update_task_status)
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


# ─────────────────────────────────────────────
# 补词协程：由 DbManager 低水线回调触发
# 整个进程只跑一个，用锁防并发
# ─────────────────────────────────────────────
_fetch_lock = asyncio.Lock()          # 全局补词锁
_current_task_info: dict | None = None  # 缓存当前 task_info（所有 worker 共用）


async def fetch_and_load_keywords(atm: AsyncTokenManager, db: DbManager):
    """
    拉取 task_info → fetch_tasks_from_api → 写入 SQLite。
    如果 get_task_info 暂时没有任务则等 10 s 后重试，最多重试 3 次。
    """
    global _current_task_info

    async with _fetch_lock:
        async with aiohttp.ClientSession() as session:
            # ── 1. 获取 task_info（支持重试）──────────────────────
            task_info = None
            for attempt in range(3):
                task_info = await get_task_info(atm, session)
                if task_info:
                    break
                logger.info(f"[FetchKeyword] get_task_info 暂无任务，等待 10s (attempt {attempt+1}/3)")
                await asyncio.sleep(10)

            if not task_info:
                logger.warning("[FetchKeyword] 连续 3 次未获取到 task_info，放弃本次补词")
                return

            _current_task_info = task_info  # 缓存，供 worker 初始化时读取

            # ── 2. 根据 task_info 拉取关键词 ──────────────────────
            # 构造完整的 params-like 对象，字段与 SearchTaskParams 对齐
            class _FakeParams:
                pass

            fake = _FakeParams()
            fake.session      = session
            fake.atm          = atm
            # task_info 字段直接透传，保证 fetch_tasks_from_api 能取到任意字段
            fake.task_id      = task_info.get("id")
            fake.dbname       = task_info.get("product_db_name")
            fake.datanum      = task_info.get("keyword_count", 50)
            fake.binddomain   = task_info.get("server_main_domain")
            fake.agent_url    = task_info.get("agent_url")
            fake.agent_key    = task_info.get("agent_key")
            fake.dbuser       = task_info.get("product_db_user")
            fake.dbpasswd     = task_info.get("product_db_password")
            fake.usenum       = task_info.get("product_count")
            fake.desimagenum  = task_info.get("image_count")
            fake.languageid   = task_info.get("language_id")
            fake.language_code = Config.LANGUAGE_CODE_MAP.get(
                task_info.get("language_code"), "en-US"
            )
            fake.jxycategory_id       = task_info.get("category_id")
            fake.collect_platform_type = task_info.get("collect_platform_type")

            raw_tasks = await fetch_tasks_from_api(fake)
            if not raw_tasks:
                logger.info("[FetchKeyword] fetch_tasks_from_api 返回空，本次补词结束")
                return

            # ── 3. 写入 SQLite ─────────────────────────────────────
            records = []
            for item_str in raw_tasks:
                try:
                    if type(item_str) == str:
                        item = json.loads(item_str)
                    else:
                        item = item_str
                    records.append({
                        "keyword":    item["keyword"],
                        "keyword_id": item["id"],
                        "task_id":    task_info.get("id"),
                    })
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"[FetchKeyword] 解析任务失败: {item_str!r} — {e}")

            if records:
                await db.refresh_tasks(records)
                logger.info(f"[FetchKeyword] 写入 {len(records)} 条关键词")


# ─────────────────────────────────────────────
# Worker
# ─────────────────────────────────────────────
async def worker(worker_id: int, stop_event: asyncio.Event, db: DbManager,
                 atm: AsyncTokenManager, app: AsyncProxyPool):
    """
    每个 worker 独立循环：
      1. 等待 SQLite 中有待处理任务
      2. 从缓存的 task_info 中读取配置，构造 params
      3. 调用 search_keyword_batch（内部跑 datanum 个词后关闭浏览器）
      4. 循环重来
    """
    global _current_task_info

    logger.info(f"启动")

    while not stop_event.is_set():
        # ── 等待 SQLite 中出现任务 ──────────────────────────────
        pending = await db.get_pending_count()
        if pending == 0:
            logger.info(f"SQLite 暂无任务，等待 10s")
            await asyncio.sleep(10)
            continue

        # ── 等待 task_info 就绪（补词协程可能还在跑）──────────────
        if _current_task_info is None:
            logger.info(f"task_info 尚未就绪，等待 5s")
            await asyncio.sleep(5)
            continue

        task_info = _current_task_info  # 快照，线程安全（GIL）

        # ── 构造 params ─────────────────────────────────────────
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
                language_code=Config.LANGUAGE_CODE_MAP.get(
                    task_info.get("language_code"), "en-US"
                ),
                usenum=20,
                desimagenum=task_info.get("image_count"),
                languageid=task_info.get("language_id"),
                no_keyword_num=0,
                jxycategory_id=task_info.get("category_id"),
                task_id=task_info.get("id"),
                proxies=None,
                collect_platform_type=["shopify"],
                session=session,
                app=app,
                atm=atm,
                db=db,
            )

            # ── 运行一轮（datanum 个词 + 浏览器开关）───────────────
            await search_keyword_batch(params)

        except asyncio.CancelledError:
            logger.info(f"[Worker-{worker_id}] 被取消，退出")
            break
        except Exception as e:
            logger.exception(f"[Worker-{worker_id}] 异常: {e}")
        finally:
            try:
                await session.close()
            except Exception as e:
                logger.warning(f"[Worker-{worker_id}] 关闭 session 失败: {e}")

    logger.info(f"[Worker-{worker_id}] 已停止")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
async def main():
    stop_event = asyncio.Event()
    worker_tasks = []

    try:
        # ── 1. 初始化 Token ───────────────────────────────────────
        logger.info("获取平台 Token...")
        await asyncio.wait_for(atm.refresh_token(), timeout=60.0)
        token = await atm.get_token()
        logger.info(f"Token 获取成功: {token}")

        # ── 2. 初始化 SQLite ──────────────────────────────────────
        # 低水线 = TASK_NUM * datanum * 0.3，但 datanum 此时未知；
        # 先用占位值初始化，待第一次补词后 DbManager 会用真实值。
        # 注意：低水线通过 db.low_watermark 属性随时可以更新。
        await db.init()
        logger.info("tasks.db 初始化成功")
        await db.print_stats()

        # ── 3. 注入补词回调到 DbManager ───────────────────────────
        # DbManager.auto_refresh_if_needed 会调用这个 fetch_func
        async def _fetch_func():
            await fetch_and_load_keywords(atm, db)

        db.fetch_func = _fetch_func

        # ── 4. 启动时先做一次补词，确保 task_info 和初始关键词就绪 ─
        logger.info("首次补词...")
        await fetch_and_load_keywords(atm, db)

        # 用真实 datanum 更新低水线
        if _current_task_info:
            real_datanum = _current_task_info.get("keyword_count", 50)
            db.low_watermark = int(Config.TASK_NUM * real_datanum * 0.3)
            logger.info(f"低水线设置为 {db.low_watermark}")

        await db.print_stats()

        # ── 5. 启动 Workers ───────────────────────────────────────
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
    db  = DbManager(db_path="tasks.db")   # low_watermark 会在 main() 里动态设置

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
        # ⭐ 取消所有任务后，用一次性的 run_until_complete 等它们结束
        # 同时屏蔽第二次 Ctrl+C 导致的 KeyboardInterrupt
        import signal, os

        # Windows 下屏蔽后续 Ctrl+C
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass

        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()

        if pending:
            # run_until_complete 最多跑 3 秒，超时直接放弃
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
        # 兜底：强杀进程，防止任何残留线程继续阻塞
        os._exit(0)