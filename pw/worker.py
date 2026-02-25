import asyncio
import time
import random


class Worker:
    def __init__(self, browser, proxy, max_use=50):
        self.browser = browser
        self.proxy = proxy
        self.max_use = max_use

        self.context = None
        self.page = None

        self.use_count = 0
        self.last_used = time.time()
        self.healthy = True

    async def init(self):
        await self._create()

    async def _create(self):
        self.context = await self.browser.new_context(
            proxy=self.proxy,
            viewport={"width": random.randint(1200, 1600), "height": random.randint(800, 1000)}
        )
        self.page = await self.context.new_page()

    async def acquire(self):
        self.use_count += 1
        self.last_used = time.time()
        return self.page

    async def release(self):
        if self.use_count >= self.max_use:
            await self.rebuild()

    async def rebuild(self):
        try:
            await self.context.close()
        except:
            pass
        self.use_count = 0
        await self._create()

    async def health_check(self):
        try:
            await self.page.evaluate("1+1")
            self.healthy = True
        except:
            self.healthy = False
            await self.rebuild()

