import asyncio
import time
from playwright.async_api import async_playwright


class ContextWrapper:
    def __init__(self, browser_wrapper, context, proxy):
        self.browser_wrapper = browser_wrapper
        self.context = context
        self.proxy = proxy

        self.use_count = 0
        self.fail_count = 0
        self.last_used = time.time()
        self.closed = False

    async def new_page(self):
        self.use_count += 1
        self.last_used = time.time()
        return await self.context.new_page()

    async def close(self):
        if not self.closed:
            try:
                await self.context.close()
            except:
                pass
            self.closed = True


class BrowserWrapper:
    def __init__(self, playwright, chrome_path):
        self.playwright = playwright
        self.chrome_path = chrome_path
        self.browser = None

        self.fail_count = 0
        self.contexts = set()
        self.lock = asyncio.Lock()

    async def start(self):
        self.browser = await self.playwright.chromium.launch(
            executable_path=self.chrome_path,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

    async def new_context(self, proxy):
        async with self.lock:
            context = await self.browser.new_context(proxy=proxy)
            wrapper = ContextWrapper(self, context, proxy)
            self.contexts.add(wrapper)
            return wrapper

    async def remove_context(self, ctx_wrapper):
        async with self.lock:
            await ctx_wrapper.close()
            self.contexts.discard(ctx_wrapper)

    async def restart(self):
        try:
            await self.browser.close()
        except:
            pass

        await self.start()
        self.fail_count = 0


class BrowserPool:
    def __init__(
        self,
        chrome_path,
        max_browser=4,
        max_context_per_browser=2,
        max_context_use=20,
        browser_fail_limit=3
    ):
        self.chrome_path = chrome_path
        self.max_browser = max_browser
        self.max_context_per_browser = max_context_per_browser
        self.max_context_use = max_context_use
        self.browser_fail_limit = browser_fail_limit

        self.playwright = None
        self.browsers = []
        self.semaphore = asyncio.Semaphore(max_browser * max_context_per_browser)

        self.total_success = 0  # 累计成功关键词数
        self.total_sorry = 0  # 累计 sorry 次数
        self.total_retired = 0  # 累计退休 context 数

    async def start(self):
        self.playwright = await async_playwright().start()

        for _ in range(self.max_browser):
            bw = BrowserWrapper(self.playwright, self.chrome_path)
            await bw.start()
            self.browsers.append(bw)

    def _select_browser(self):
        return min(self.browsers, key=lambda b: len(b.contexts))

    async def acquire(self, proxy):
        await self.semaphore.acquire()
        browser = self._select_browser()
        ctx = await browser.new_context(proxy)
        return ctx

    async def release(self, ctx_wrapper, success=True):
        browser = ctx_wrapper.browser_wrapper

        if not success:
            browser.fail_count += 1

        # context 超过使用次数就销毁
        if ctx_wrapper.use_count >= self.max_context_use or not success:
            await browser.remove_context(ctx_wrapper)
        else:
            await browser.remove_context(ctx_wrapper)

        # browser 熔断
        if browser.fail_count >= self.browser_fail_limit:
            print("Browser 熔断重启")
            await browser.restart()

        self.semaphore.release()

    async def shutdown(self):
        for b in self.browsers:
            try:
                await b.browser.close()
            except:
                pass

        await self.playwright.stop()