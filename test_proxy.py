import os
import json
import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector
from dotenv import load_dotenv

load_dotenv()

async def test_proxy(proxy_url):
    # proxy_url = "socks5://158.62.210.138:1080"

    connector = ProxyConnector.from_url(proxy_url)

    timeout = aiohttp.ClientTimeout(total=5)

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout
    ) as session:
        try:
            async with session.get("https://ipinfo.io/ip", ssl=False) as resp:
                text = await resp.text()
                print("proxy:", text.strip())
        except Exception as e:
            print("proxy error:", e)


async def test_no_proxy():
    timeout = aiohttp.ClientTimeout(total=5)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get("https://ipinfo.io/ip", ssl=False) as resp:
                text = await resp.text()
                print("no proxy:", text.strip())
        except Exception as e:
            print("no proxy error:", e)


async def get_proxy(PROXY_URL):
    timeout = aiohttp.ClientTimeout(total=5)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(PROXY_URL) as resp:
            text = json.loads(await resp.text())
            return text



async def main():
    PROXY_URL: str = os.getenv("PROXY_URL", "")
    proxy_list = await get_proxy(PROXY_URL)
    for ppp in proxy_list:
        await test_proxy(f"socks5://{ppp['ip']}:{ppp['port']}")
    await test_no_proxy()


if __name__ == "__main__":
    asyncio.run(main())
