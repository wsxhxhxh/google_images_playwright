"""
Playwright 异步 Page 池
- 一个 Browser，多个 Context，每个 Context 一个 Page
- 每个 Context 绑定独立代理
- 支持动态增删 Context / 代理故障处理
"""

import asyncio
import random
from playwright.async_api import async_playwright

from worker import Worker

from config import logger

class PlaywrightPool:
    def __init__(self, proxies, max_use=50):
        self.proxies = proxies

        self.use_proxy = set()
        self.max_use = max_use
        self.queue = asyncio.Queue()
        self.workers = []
        self.playwright = None
        self.browser = None

    async def init(self):
        self.playwright = await async_playwright().start()

        self.browser = await self.playwright.chromium.launch(
            headless=False
        )

        for proxy in self.proxies:
            worker = Worker(self.browser, proxy, self.max_use)
            self.use_proxy.add(proxy)
            await worker.init()
            self.workers.append(worker)
            await self.queue.put(worker)

        asyncio.create_task(self._health_monitor())

    async def acquire(self):
        return await self.queue.get()

    async def release(self, worker):
        await worker.release()
        await self.queue.put(worker)

    async def _health_monitor(self):
        while True:
            await asyncio.sleep(30)
            for worker in self.workers:
                await worker.health_check()

    async def close(self):
        for worker in self.workers:
            await worker.context.close()
        await self.browser.close()
        await self.playwright.stop()

async def t1st():
    async def search(keyword, pool: PlaywrightPool):
        worker = await pool.acquire()
        page = await worker.acquire()

        try:
            await page.goto("https://www.google.com/imghp?hl=zh-CN&tab=ri&authuser=0&ogbl")
            await page.fill('xpath=//textarea[@class="gLFyf"]', keyword)
            logger.info(keyword)
            print(keyword)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(random.randint(800, 1500))
            logger.info(page.url)
            print(page.url)
        except Exception as e:
            logger.exception(111)
            await worker.rebuild()
        finally:
            await pool.release(worker)

    proxies = [
        None,
        None,
        None,
    ]

    pool = PlaywrightPool(proxies, max_use=40)
    await pool.init()

    tasks = []
    for kw in ["Nike Air Max", "Nike Air Max90"]:
        tasks.append(search(kw, pool))

    await asyncio.gather(*tasks)

    await pool.close()

if __name__ == '__main__':
    asyncio.run(t1st())