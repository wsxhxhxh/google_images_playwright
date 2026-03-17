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


db = DbManager(db_path="tasks.db")



@dataclass
class SearchTaskParams:
    """搜索任务参数类"""
    worker_id: int
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

async def worker(worker_id: int):
    while True:
        session = None
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


            params = SearchTaskParams(
                worker_id=worker_id,
                tasks=[],
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

        except IndexError as e:
            logger.info("not task sleep 60s")
            await asyncio.sleep(60)

        except Exception as e:
            logger.exception(e)
        finally:
            # ⭐ 关闭session
            if session:
                try:
                    await session.close()
                except Exception as e:
                    logger.warning(f"关闭session失败: {e}")

async def main():
    """主函数"""
    try:
        logger.info("开始获取平台Token...")
        await asyncio.wait_for(atm.refresh_token(), timeout=60.0)
        token = await atm.get_token()
        logger.info(f"获取到平台Token: {token}")

        logger.info(f"初始化sqlite")
        await db.init()

        # 创建任务
        tasks = []
        for worker_id in range(Config.TASK_NUM):
            task = asyncio.create_task(worker(worker_id + 1), name=f"Work-{worker_id}")
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
