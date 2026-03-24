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

    res["status"] = 3
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
                site_status_result = await asyncio.gather(*tasks)
                status_not_200 = [s for s in site_status_result if s["status"] == 0]
                status_200 = [s for s in site_status_result if s["status"] == 3]
                if status_not_200:
                    await send_result_batch(atm, session, status_not_200)

                if status_200:
                    await send_result_batch(atm, session, status_200)

                link_data = await get_link_114_info([o["domain"] for o in status_200])

                for res in status_200:
                    domain = res["domain"]
                    if link_data.get(domain):
                        tmp = link_data[domain]
                        res["da"] = tmp.get("moz_da")
                        res["pa"] = tmp.get("moz_pa")
                        res["query_result"] = {"create": tmp.get("create")}
                        res["domain_title"] = tmp.get("title")
                        res["server_ip"] = tmp.get("ip")

                        res["country"] = tmp.get("location")

                        if not tmp.get("moz_da") or not tmp.get("moz_pa"):
                            res["status"] = 0
                    else:
                        res["status"] = 0

                no_da_pa = [s for s in status_200 if s["status"] == 0]
                has_da_pa = [s for s in status_200 if s["status"] == 3]

                if no_da_pa:
                    await send_result_batch(atm, session, no_da_pa)

                if has_da_pa:
                    await send_result_batch(atm, session, has_da_pa)
                    params = SearchTaskParams(
                        worker_id=1,
                        tasks=has_da_pa,
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