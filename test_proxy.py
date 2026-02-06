import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector


async def test_proxy():
    proxy_url = "socks5://172.96.89.216:1080"

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


async def main():
    await test_proxy()
    await test_no_proxy()


if __name__ == "__main__":
    asyncio.run(main())
