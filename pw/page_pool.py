"""
Playwright 异步 Page 池
- 一个 Browser，多个 Context，每个 Context 一个 Page
- 每个 Context 绑定独立代理
- 支持动态增删 Context / 代理故障处理
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)


@dataclass
class ProxyConfig:
    server: str                        # e.g. "http://host:port"
    username: Optional[str] = None
    password: Optional[str] = None

    def to_playwright_proxy(self) -> dict:
        proxy = {"server": self.server}
        if self.username:
            proxy["username"] = self.username
        if self.password:
            proxy["password"] = self.password
        return proxy


@dataclass
class PoolSlot:
    slot_id: str
    context: BrowserContext
    page: Page
    proxy: Optional[ProxyConfig]
    in_use: bool = False
    error_count: int = 0


class PagePool:
    def __init__(
        self,
        max_size: int = 10,
        browser_type: str = "chromium",           # chromium / firefox / webkit
        launch_options: Optional[dict] = None,
        context_options: Optional[dict] = None,   # 全局 context 默认选项
    ):
        self.max_size = max_size
        self.browser_type = browser_type
        self.launch_options = launch_options or {}
        self.context_options = context_options or {}

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._slots: dict[str, PoolSlot] = {}          # slot_id -> PoolSlot
        self._lock = asyncio.Lock()
        self._available = asyncio.Semaphore(0)          # 控制可用 slot 数量
        self._slot_counter = 0
        self._closed = False

    # ------------------------------------------------------------------ #
    #  生命周期                                                            #
    # ------------------------------------------------------------------ #

    async def start(self, initial_proxies: Optional[list[ProxyConfig]] = None):
        """启动浏览器并预热初始 slot"""
        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self.browser_type)
        self._browser = await launcher.launch(**self.launch_options, timeout=30000,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",

                # ⭐ 安全的性能优化
                "--disable-background-networking",
                "--disable-sync",
                "--disable-default-apps",
                "--disable-extensions",
                "--mute-audio",

                # ⭐ 保留 GPU，但降低占用
                "--use-gl=desktop",  # 使用桌面 GL
                "--enable-features=NetworkService,NetworkServiceInProcess",

                # 网络优化
                "--max-connections-per-host=6",
            ],)
        logger.info(f"Browser({self.browser_type}) launched")

        if initial_proxies:
            for proxy in initial_proxies:
                await self.add_slot(proxy)

    async def close(self):
        """关闭所有资源"""
        self._closed = True
        async with self._lock:
            for slot in list(self._slots.values()):
                await self._destroy_slot(slot)
            self._slots.clear()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("PagePool closed")

    # ------------------------------------------------------------------ #
    #  Slot 管理（公开 API）                                               #
    # ------------------------------------------------------------------ #

    async def add_slot(
        self,
        proxy: Optional[ProxyConfig] = None,
        extra_context_options: Optional[dict] = None,
    ) -> str:
        """
        往池子里加一个新 slot（context + page）
        返回 slot_id，可用于后续精确操作
        """
        async with self._lock:
            if len(self._slots) >= self.max_size:
                raise RuntimeError(f"Pool is full (max_size={self.max_size})")

            slot_id = self._next_slot_id()
            slot = await self._create_slot(slot_id, proxy, extra_context_options)
            self._slots[slot_id] = slot
            self._available.release()   # 通知有新 slot 可用
            logger.info(f"Slot {slot_id} added (proxy={proxy.server if proxy else None})")
            return slot_id

    async def remove_slot(self, slot_id: str, reason: str = "manual"):
        """
        移除并关闭指定 slot（代理出错时调用此函数）
        如果 slot 正在使用中会等待它归还后再销毁
        """
        async with self._lock:
            slot = self._slots.pop(slot_id, None)
            if slot is None:
                logger.warning(f"Slot {slot_id} not found")
                return
            # 如果正在使用，page/context 关闭后使用方会收到异常，属于预期行为
            await self._destroy_slot(slot)
            logger.info(f"Slot {slot_id} removed (reason={reason})")

    async def replace_slot(
        self,
        slot_id: str,
        new_proxy: Optional[ProxyConfig] = None,
        extra_context_options: Optional[dict] = None,
    ) -> str:
        """
        快捷方法：移除旧 slot，立刻加入一个新 slot
        返回新的 slot_id
        """
        await self.remove_slot(slot_id, reason="replace")
        return await self.add_slot(new_proxy, extra_context_options)

    def slot_ids(self) -> list[str]:
        return list(self._slots.keys())

    def stats(self) -> dict:
        total = len(self._slots)
        in_use = sum(1 for s in self._slots.values() if s.in_use)
        return {
            "total": total,
            "in_use": in_use,
            "idle": total - in_use,
            "max_size": self.max_size,
        }

    # ------------------------------------------------------------------ #
    #  获取 / 归还 Page（上下文管理器）                                    #
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def acquire(self, timeout: float = 30.0):
        """
        异步上下文管理器，获取一个空闲 Page：

            async with pool.acquire() as (slot_id, page):
                await page.goto("https://example.com")
        """
        slot = await self._acquire_slot(timeout)
        try:
            yield slot.slot_id, slot.page
        except Exception:
            slot.error_count += 1
            raise
        finally:
            async with self._lock:
                # slot 可能已被 remove_slot 清除
                if slot.slot_id in self._slots:
                    slot.in_use = False
                    self._available.release()

    async def _acquire_slot(self, timeout: float) -> PoolSlot:
        try:
            await asyncio.wait_for(self._available.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"No idle page available within {timeout}s")

        async with self._lock:
            for slot in self._slots.values():
                if not slot.in_use:
                    slot.in_use = True
                    return slot

        # 极端情况（slot 刚被 remove）：归还信号量并重试
        self._available.release()
        raise RuntimeError("Acquired semaphore but no idle slot found — retry")

    # ------------------------------------------------------------------ #
    #  内部工具                                                            #
    # ------------------------------------------------------------------ #

    async def _create_slot(
        self,
        slot_id: str,
        proxy: Optional[ProxyConfig],
        extra_context_options: Optional[dict],
    ) -> PoolSlot:
        options = {**self.context_options}
        if proxy:
            options["proxy"] = proxy.to_playwright_proxy()
        if extra_context_options:
            options.update(extra_context_options)

        context: BrowserContext = await self._browser.new_context(**options)

        await context.add_init_script("""
                    (() => {
                      const original = HTMLCanvasElement.prototype.toDataURL;
                      HTMLCanvasElement.prototype.toDataURL = function () {
                        const ctx = this.getContext("2d");
                        const shift = Math.floor(Math.random() * 10);
                        ctx.fillStyle = "rgba(0,0,0,0.01)";
                        ctx.fillRect(shift, shift, 1, 1);
                        return original.apply(this, arguments);
                      };
                    })();
                """)
        await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                      get: () => undefined
                    });
                """)
        await context.add_init_script("""
                    Object.defineProperty(navigator, 'languages', {{
                      get: () => ['{language_code}', '{language}']
                    }});
                """.format(language_code=extra_context_options["language_code"], language=extra_context_options["language_code"].split("-")[0]))
        await context.add_init_script("""
                    Object.defineProperty(document, 'fonts', {
                      value: {
                        check: () => true
                      }
                    });
                """)
        await context.add_init_script("""
                    Object.defineProperty(navigator, 'connection', {
                      get: () => ({
                        effectiveType: '4g',
                        rtt: 50 + Math.floor(Math.random() * 30),
                        downlink: 5 + Math.random() * 2,
                        saveData: false
                      })
                    });
                """)
        await context.add_init_script("""
                            Object.defineProperty(navigator, 'platform', {
                              get: () => 'Win32'
                            });
                """)
        await context.add_init_script("""
                    Object.defineProperty(navigator, 'userAgentData', {{
                      get: () => ({{
                        brands: [{{ brand: "Chromium", version: "{major}" }}],
                        mobile: false,
                        platform: "Windows"
                      }})
                    }});
                """.format(major=extra_context_options["major"]))
        await context.add_init_script("""
                    Object.defineProperty(navigator, 'plugins', {
                      get: () => [1, 2, 3, 4, 5]
                    });
                """)

        await context.add_cookies([
            {
                'name': 'CONSENT',
                'value': 'YES+srp.gws-20260211-0-RC2.en+FX+111',
                'domain': '.google.com',
                'path': '/',
                'secure': True,
                'sameSite': 'Lax'
            },
            {
                'name': 'SOCS',
                'value': 'CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg',
                'domain': '.google.com',
                'path': '/',
                'secure': True,
                'sameSite': 'Lax'
            },
            {
                'name': 'NID',
                'value': '511=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
                'domain': '.google.com',
                'path': '/',
                'secure': True,
                'httpOnly': True,
                'sameSite': 'None'
            }
        ])

        page: Page = await context.new_page()
        # 拦截无用资源，降低检测风险 & 加速
        await page.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2}",
            lambda route: route.abort(),
        )

        guard_page = await context.new_page()
        await guard_page.goto("about:blank")

        return PoolSlot(
            slot_id=slot_id,
            context=context,
            page=page,
            proxy=proxy,
        )

    async def _destroy_slot(self, slot: PoolSlot):
        try:
            await slot.page.close()
        except Exception:
            pass
        try:
            await slot.context.close()
        except Exception:
            pass

    def _next_slot_id(self) -> str:
        self._slot_counter += 1
        return f"slot-{self._slot_counter:04d}"


# ------------------------------------------------------------------ #
#  便捷工厂：async with 直接使用                                       #
# ------------------------------------------------------------------ #

@asynccontextmanager
async def create_pool(
    initial_proxies: Optional[list[ProxyConfig]] = None,
    max_size: int = 10,
    browser_type: str = "chromium",
    launch_options: Optional[dict] = None,
    context_options: Optional[dict] = None,
):
    pool = PagePool(
        max_size=max_size,
        browser_type=browser_type,
        launch_options=launch_options,
        context_options=context_options,
    )
    await pool.start(initial_proxies)
    try:
        yield pool
    finally:
        await pool.close()
