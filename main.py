import time
import aiohttp
import asyncio
from palt_api import get_task_info, send_result_batch, send_task_status, fetch_domain_by_task_id
from platform_api import TokenManager, ProxyPool
from link114 import get_link_114_info
from playwright_async_fixed import search_keyword_batch
from dataclasses import dataclass
from typing import Dict, List
import requests

def is_ok_site(domain):

    if not domain.startswith(('http://', 'https://')):
        url_with_protocol = 'https://' + domain
        domain = url_with_protocol

    try:
        resp = requests.get(domain, timeout=10, verify=False)
        text = resp.text

        if '/wp-content/' in text:
            system_info = 'wordpress'
        elif 'joomla' in text:
            system_info = 'Joomla'
        elif '<div data-mage-init' in text:
            system_info = 'magento'
        elif '/catalog/view/' in text:
            system_info = 'opencart'
        else:
            system_info = 'other'


        return resp.status_code, system_info
    except Exception as e:
        print(e)
        return 0, None



def domain_work(domain_info):
    res = {
        "id": domain_info["id"],
        "domain": domain_info["domain"],
    }
    domain = domain_info["domain"]

    # 获取网站状态
    site_status, system_info = is_ok_site(domain)
    res["http_code"] = site_status
    if not (200 <= site_status < 300):
        res["status"] = 0
        return res
    res["system_info"] = system_info

    res["status"] = 3
    return res


@dataclass
class SearchTaskParams:
    worker_id: int
    language_code: str
    tasks: List
    session: aiohttp.ClientSession | None
    proxies: Dict | None
    app: ProxyPool
    atm: TokenManager

def main():
    atm = TokenManager()
    app = ProxyPool()
    atm.refresh_token()


    while True:
        task_info_list = get_task_info(atm)
        print(task_info_list)

        if not task_info_list:
            print("not task sleep 30s")
            time.sleep(30)
            continue

        task_info = task_info_list[0]
        task_id = task_info["id"]

        while True:
            domain_info_list = fetch_domain_by_task_id(atm, task_id)
            if not domain_info_list:
                print("not domain break")
                break

            site_status_result = [
                domain_work(domain_info)
                for domain_info in domain_info_list
            ]

            status_not_200 = [s for s in site_status_result if s["status"] == 0]
            status_200 = [s for s in site_status_result if s["status"] == 3]


            if status_not_200:
                send_result_batch(atm, status_not_200)

            if status_200:
                send_result_batch(atm, status_200)

            def run_async(coro):
                return asyncio.run(coro)

            link_data = run_async(
                get_link_114_info([o["domain"] for o in status_200])
            )

            for res in status_200:
                domain = res["domain"]
                if link_data.get(domain):
                    tmp = link_data[domain]
                    res["da"] = tmp.get("moz_da")
                    res["pa"] = tmp.get("moz_pa")
                    res["domain_title"] = tmp.get("title")
                    res["server_ip"] = tmp.get("ip")
                    res["country"] = tmp.get("location")

                    if not res.get("query_result"):
                        res["query_result"] = {}

                    res["query_result"]["create"] = tmp.get("create")

                    if not tmp.get("title"):
                        res["status"] = 0
                        res["query_result"]["err_msg"] = "not title"

                    if (not tmp.get("moz_da")) or (not tmp.get("moz_pa")):
                        res["status"] = 0
                        res["query_result"]["err_msg"] = "not da or not pa"
                else:
                    res["status"] = 0

            no_da_pa = [s for s in status_200 if s["status"] == 0]
            has_da_pa = [s for s in status_200 if s["status"] == 3]

            if no_da_pa:
                send_result_batch(atm, no_da_pa)

            if has_da_pa:
                send_result_batch(atm, has_da_pa)
                params = SearchTaskParams(
                    worker_id=1,
                    tasks=has_da_pa,
                    proxies=None,
                    session=None,
                    app=app,
                    atm=atm,
                    language_code='en-US',
                )
                search_keyword_batch(params)
        send_task_status(atm, task_id, 4)

if __name__ == '__main__':
    main()