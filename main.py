import aiohttp
import asyncio
from palt_api import fetch_domain_by_task_id, get_task_info, send_result_batch, send_task_status
from platform_api import AsyncTokenManager, AsyncProxyPool
from get_res import process_site, make_link_session
from playwright_async_fixed import search_keyword_batch
from dataclasses import dataclass
from typing import Dict, List


async def is_ok_site(session, domain):

    if not domain.startswith(('http://', 'https://')):
        url_with_protocol = 'https://' + domain
        domain = url_with_protocol

    try:
        async with session.get(domain, timeout=10, ssl=False) as resp:
            return resp.status
    except Exception as e:
        print(e)
        return 0



async def domain_work(domain_info, session, link_session):
    res = {
        "id": domain_info["id"],
        "domain": domain_info["domain"],
    }
    domain = domain_info["domain"]

    # 获取网站状态
    site_status = await is_ok_site(session, domain)
    res["http_code"] = site_status
    if not (200 <= site_status < 300):
        res["status"] = 0
        return res


    # 查询link114.cn信息
    tmp = await process_site(link_session, domain)



    res["da"] = tmp.get("moz_da")
    res["pa"] = tmp.get("moz_pa")
    res["query_result"] = {"create": tmp.get("create")}
    res["domain_title"] = tmp.get("title")
    res["server_ip"] = tmp.get("ip")

    res["country"] = tmp.get("location")
    res["status"] = 2

    if not tmp.get("moz_da") or not tmp.get("moz_pa"):
        res["status"] = 0
        return res

    return res


@dataclass
class SearchTaskParams:
    worker_id: int
    language_code: str
    tasks: List
    proxies: Dict | None
    app: AsyncProxyPool
    atm: AsyncTokenManager

async def main():
    atm = AsyncTokenManager()
    app = AsyncProxyPool()
    await atm.refresh_token()
    link_session = await make_link_session()

    async with aiohttp.ClientSession() as session:
        while True:
            task_info_list = await get_task_info(atm, session)
            print(task_info_list)
            if not task_info_list:
                print("not task sleep 30s")
                await asyncio.sleep(30)
                continue

            task_info = task_info_list[0]
            task_id = task_info["id"]
            while True:
                domain_info_list = await fetch_domain_by_task_id(atm, session, task_id)
                if not domain_info_list:
                    print("not domain sleep 30s")
                    break
                tasks = [
                    asyncio.create_task(domain_work(domain_info, session, link_session))
                    for domain_info in domain_info_list
                ]
                site_result = await asyncio.gather(*tasks)
                print(site_result)
                failed_result = [s for s in site_result if s["status"] == 0]
                success_result = [s for s in site_result if s["status"] == 2]
                await send_result_batch(atm, session, failed_result)
                params = SearchTaskParams(
                    worker_id=1,
                    tasks=success_result,
                    proxies=None,
                    app=app,
                    atm=atm,
                    language_code='en-US',
                )
                await search_keyword_batch(params)

                await send_task_status(atm, session, task_id, 4)



if __name__ == '__main__':
    asyncio.run(main())