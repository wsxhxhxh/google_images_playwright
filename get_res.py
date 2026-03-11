import ssl
import asyncio
from typing import Any
from contextlib import asynccontextmanager

from config import Config
import aiohttp

from reproduce_r_async import generate_r

DEFAULT_HOST = "www.link114.cn"


def build_ssl_context() -> ssl.SSLContext:
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    # Allow older/broader cipher suites the server may require
    ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    return ssl_ctx

def build_headers() -> dict:
    return {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7,en-GB;q=0.6",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "referer": "https://www.link114.cn/",
        "connection": "keep-alive",
        "charset": "utf-8",
        "content-type": "text/html",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def build_cookies() -> dict:
    return {
        "latestversion": Config.LATEST_VERSION,
        "linkatb": Config.LINK_ATB,
        "login_id": Config.LOGGING_ID,
        "logincookie": Config.LOGGING_COOKIE,
        "preference": "whois_days|moz_da|moz_pa|ip|title",
    }


async def post_start(session: aiohttp.ClientSession, sites: list[str]) -> None:
    url = "https://www.link114.cn/multi.php"
    data = {
        "func": "whois_days|moz_da|moz_pa|ip|title",
        "websites": "|".join(sites),
    }
    async with session.post(url, data=data) as resp:
        text = await resp.text()
        print(text)


async def get_site_func(session: aiohttp.ClientSession, site: str, func: str) -> dict | None:
    r = generate_r(site, func=func)
    req_url = f"https://www.link114.cn/get.php?func={func}&site={site[::-1]}&r={r}"
    print(req_url)
    try:
        async with session.get(req_url) as resp:
            data = await resp.json(content_type=None)
            print(data)
            return data
    except Exception as exc:
        print(exc)
        return None


async def process_site(session: aiohttp.ClientSession, site: str) -> dict:
    tmp: dict[str, Any] = {"site": site}
    pass_ip = False
    pass_title = False

    r1 = await get_site_func(session, site, "whois_days")
    if r1 and r1.get("status") == "1":
        if r1["result"]["create"] == "0":
            tmp["create"] = "UNREGISTERED"
            tmp["ip"] = "NO_IP"
            tmp["title"] = "NO_IP"
            pass_ip = True
            pass_title = True
        else:
            tmp["create"] = r1["result"]["create"]
            tmp["days"] = r1["result"]["days"]

    r2 = await get_site_func(session, site, "moz_da")
    if r2 and r2.get("status") == "1":
        tmp["moz_da"] = r2["result"]["da"]
        tmp["moz_pa"] = r2["result"]["pa"]

    if not pass_ip:
        r4 = await get_site_func(session, site, "ip")
        if r4 and r4.get("status") == "1":
            if r4["result"]["data"] == "0":
                tmp["ip"] = "NO_IP"
            else:
                tmp["ip"] = r4["result"]["data"]
            tmp["location"] = r4["result"].get("location")

    if not pass_title:
        r5 = await get_site_func(session, site, "title")
        if r5 and r5.get("status") == "1":
            if r5["result"].get("title"):
                tmp["title"] = r5["result"]["title"]
                tmp["keywords"] = r5["result"]["keywords"]
                tmp["description"] = r5["result"]["description"]
            else:
                tmp["title"] = "NO_IP"

    return tmp


async def make_link_session():
    headers = build_headers()
    cookies = build_cookies()

    timeout = aiohttp.ClientTimeout(total=30)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    connector = aiohttp.TCPConnector(limit=10, ssl=ssl_ctx)
    return aiohttp.ClientSession(headers=headers, cookies=cookies, timeout=timeout, connector=connector)


async def main_async() -> None:
    sites = ["muhammedadnansami.com", "leiderschapinverandering.com"]

    headers = build_headers()
    cookies = build_cookies()
    timeout = aiohttp.ClientTimeout(total=30)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    ssl_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    connector = aiohttp.TCPConnector(limit=10, ssl=ssl_ctx)

    async with aiohttp.ClientSession(headers=headers, cookies=cookies, timeout=timeout, connector=connector) as session:
        await post_start(session, sites)
        await asyncio.sleep(0.1)
        results = await asyncio.gather(*(process_site(session, site) for site in sites))
        for item in results:
            print(item)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
