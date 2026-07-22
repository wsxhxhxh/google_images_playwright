import time
import aiohttp
import asyncio
from palt_api import send_result_batch, fetch_domain_by_task_id
from platform_api import TokenManager, ProxyPool
from link114 import get_link_114_info
from ruyipage_browser import search_keyword_batch
from dataclasses import dataclass
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def is_ok_site(domain):
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
        'accept': '*/*',
        'accept-encoding': 'html',
        'accept-language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7,en-GB;q=0.6',
    }
    if not domain.startswith(('http://', 'https://')):
        url_with_protocol = 'https://' + domain
        domain = url_with_protocol

    try:
        resp = requests.get(domain, timeout=10, headers=headers, verify=False)
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
        if not res.get("query_result"):
            res["query_result"] = {}
        res["query_result"]["err_msg"] = "response status not 200"
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


def split_by_group(lst, group_size=5):
    for i in range(0, len(lst), group_size):
        yield lst[i:i + group_size]


def ruyi_work(params):
    search_keyword_batch(params)


def main():
    atm = TokenManager()
    app = ProxyPool()
    atm.refresh_token()

    while True:
        for i in range(1, 5):
            domain_info_list = fetch_domain_by_task_id(i, 100)
            if not domain_info_list:
                print("not domain break")
                break

            search_da = [s for s in domain_info_list if not s["da"]]
            no_search = [s for s in domain_info_list if s["da"]]

            print()
            print("search_da", search_da)
            print("no_search", no_search)

            if search_da:
                def run_async(coro):
                    return asyncio.run(coro)

                link_data = run_async(get_link_114_info([o["domain_name"] for o in search_da]))

                for res in search_da:
                    domain = res["domain_name"]
                    if link_data.get(domain):
                        tmp = link_data[domain]
                        res["da"] = tmp.get("moz_da")

                no_search.extend(search_da)
            print(no_search)
            dp_groups = split_by_group(no_search)
            with ThreadPoolExecutor(max_workers=8) as executor:
                for wi, dp_group in enumerate(dp_groups):
                    params = SearchTaskParams(
                        worker_id=wi + 1,
                        tasks=dp_group,
                        proxies={},
                        session=None,
                        app=app,
                        atm=atm,
                        language_code='en-US',
                    )
                    executor.submit(ruyi_work, params)


if __name__ == '__main__':
    main()
