import aiohttp
import asyncio
from palt_api import get_task_info, send_result_batch, send_task_status, fetch_domain_by_task_id
from platform_api import AsyncTokenManager, AsyncProxyPool
from link114 import get_link_114_info
from playwright_async_fixed import search_keyword_batch
from dataclasses import dataclass
from typing import Dict, List


async def is_ok_site(session, domain):

    if not domain.startswith(('http://', 'https://')):
        url_with_protocol = 'https://' + domain
        domain = url_with_protocol

    try:
        async with session.get(domain, timeout=10, ssl=False) as resp:
            await resp.text()
            return resp.status
    except Exception as e:
        print(e)
        return 0



async def domain_work(domain_info, session):
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

    res["status"] = 1
    return res


@dataclass
class SearchTaskParams:
    worker_id: int
    language_code: str
    tasks: List
    session: aiohttp.ClientSession
    proxies: Dict | None
    app: AsyncProxyPool
    atm: AsyncTokenManager

async def main():
    atm = AsyncTokenManager()
    app = AsyncProxyPool()
    await atm.refresh_token()
    while True:
        async with aiohttp.ClientSession() as session:
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
                    print("not domain break")
                    break
                tasks = [
                    asyncio.create_task(domain_work(domain_info, session))
                    for domain_info in domain_info_list
                ]
                site_result = await asyncio.gather(*tasks)
                failed_result = [s for s in site_result if s["status"] == 0]
                ok_result = [s for s in site_result if s["status"] == 1]
                if failed_result:
                    await send_result_batch(atm, session, failed_result)

                link_data = await get_link_114_info([o["domain"] for o in ok_result])

                for res in ok_result:
                    domain = res["domain"]
                    if link_data.get(domain):
                        tmp = link_data[domain]
                        res["da"] = tmp.get("moz_da")
                        res["pa"] = tmp.get("moz_pa")
                        res["query_result"] = {"create": tmp.get("create")}
                        res["domain_title"] = tmp.get("title")
                        res["server_ip"] = tmp.get("ip")

                        res["country"] = tmp.get("location")
                        res["status"] = 2

                        if not tmp.get("moz_da") or not tmp.get("moz_pa"):
                            res["status"] = 0
                    else:
                        res["status"] = 0

                failed_result = [s for s in ok_result if s["status"] == 0]
                ook_result = [s for s in ok_result if s["status"] == 2]

                if failed_result:
                    await send_result_batch(atm, session, failed_result)

                params = SearchTaskParams(
                    worker_id=1,
                    tasks=ook_result,
                    proxies=None,
                    session=session,
                    app=app,
                    atm=atm,
                    language_code='en-US',
                )
                await search_keyword_batch(params)
            await send_task_status(atm, session, task_id, 4)

if __name__ == '__main__':
    asyncio.run(main())