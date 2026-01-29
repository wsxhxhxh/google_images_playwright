import json
import asyncio
from dataclasses import dataclass
from typing import Dict, List

import aiohttp

from playwright_async_fixed import search_keyword_batch
from config import logger, Config
from platform_api import (AsyncTokenManager, AsyncProxyPool, get_task_info,
                          fetch_tasks_from_api)



@dataclass
class SearchTaskParams:
    """搜索任务参数类"""
    worker_id: int
    tasks: List
    dbname: str
    binddomain: str
    language_code: str
    usenum: int
    desimagenum: int
    languageid: int
    jxycategory_id: str
    proxies: Dict[str, str]
    collect_platform_type: List[str]

async def worker(worker_id: int):
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                work_info = await get_task_info(atm, session)
                dbname = work_info.get("product_db_name")
                datanum = work_info.get("keyword_count")
                binddomain = work_info.get("server_main_domain")
                usenum = work_info.get("product_count")
                jxycategory_id = work_info.get("category_id")
                desimagenum = work_info.get("image_count")
                task_name = work_info.get("task_name")
                collect_platform_type = work_info.get("collect_platform_type")
                language_id = work_info.get("language_id")
                language_code = Config.LANGUAGE_CODE_MAP.get(work_info.get("language_code"), "en-US")
                logger.info(f"Worker ID: {worker_id} get work info: {task_name}")

                tasks = await fetch_tasks_from_api(session, dbname, datanum, binddomain)
                logger.info(f"Worker ID: {worker_id} fetch task num: {len(tasks)} {tasks[:3]}...")
                proxy = await app.get_random_proxy()
                proxies = {"server": proxy}

                params = SearchTaskParams(
                    worker_id=worker_id,
                    tasks=tasks,
                    dbname=dbname,
                    binddomain=binddomain,
                    language_code=language_code,
                    usenum=usenum,
                    desimagenum=desimagenum,
                    jxycategory_id=jxycategory_id,
                    proxies=proxies,
                    languageid=language_id,
                    collect_platform_type=collect_platform_type,
                )

                await search_keyword_batch(params)
        except Exception as e:
            logger.exception(e)

async def main():
    """主函数"""
    try:
        # 初始化代理池
        logger.info("开始初始化代理池...")
        await asyncio.wait_for(app.init_proxy_pool(), timeout=60.0)
        logger.info(f"代理池初始化完成：共 {len(app.proxy_pool)} 个代理")
        logger.info("开始获取平台Token...")
        await asyncio.wait_for(atm.refresh_token(), timeout=60.0)
        token = await atm.get_token()
        logger.info(f"获取到平台Token: {token}")

        if len(app.proxy_pool) == 0:
            logger.error("代理池为空，程序退出")
            return

        # 创建任务
        tasks = []
        for worker_id in range(Config.TASK_NUM):
            task = asyncio.create_task(worker(worker_id + 1))
            tasks.append(task)

        logger.info(f"创建了 {len(tasks)} 个 Worker 任务")
        await asyncio.gather(*tasks)

    except asyncio.TimeoutError:
        logger.error("代理池初始化超时")
    except Exception as e:
        logger.exception(f"主函数异常: {e}")


if __name__ == '__main__':
    data = []
    try:
        app = AsyncProxyPool()
        atm = AsyncTokenManager()
        asyncio.run(main())

    except FileNotFoundError:
        logger.error("找不到 scratch_3.json 文件")
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析错误: {e}")
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.exception(f"程序异常退出: {e}")
    finally:
        logger.info("程序结束")
