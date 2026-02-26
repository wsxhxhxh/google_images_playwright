import json
import os
import random
import datetime
import asyncio
import aiofiles

import aiohttp
from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeout
from playwright._impl._errors import Error as PlaywrightError
from typing import Optional

from config import Config, logger
from deal_product_func_async import deal_info_by_async, deal_shopify_product_info_async
from parsel_json_str import demo_with_real_data, get_related_search, get_related_items
from platform_api import send_items_to_api, send_shopify_product_to_api, send_err_task
from managed import ManagedPage, ResponseTracker, ThreadSafeAggregator


# ===== 指纹注入 =====

async def inject_fingerprint(context: BrowserContext, ua: str):
    """
    统一的指纹注入入口，修复了：
    - brands 三元组（原来只有一个 Chromium，会被识别）
    - plugins 结构（原来是数字数组，真实是 PluginArray 对象）
    - canvas 噪声
    - webdriver / languages / platform / connection / fonts / userAgentData
    """
    major = ua.split("Chrome/")[1].split(".")[0]

    # --- canvas 噪声 ---
    await context.add_init_script("""
        (() => {
          const original = HTMLCanvasElement.prototype.toDataURL;
          HTMLCanvasElement.prototype.toDataURL = function() {
            const ctx2d = this.getContext('2d');
            if (ctx2d) {
              const imageData = ctx2d.getImageData(0, 0, 1, 1);
              imageData.data[0] = imageData.data[0] ^ (Math.floor(Math.random() * 3));
              ctx2d.putImageData(imageData, 0, 0);
            }
            return original.apply(this, arguments);
          };
        })();
    """)

    # --- webdriver ---
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)

    # --- platform ---
    await context.add_init_script("""
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    """)

    # --- connection ---
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

    # --- fonts ---
    await context.add_init_script("""
        Object.defineProperty(document, 'fonts', {
          value: { check: () => true }
        });
    """)

    # --- userAgentData：brands 必须是三元组 ---
    await context.add_init_script(f"""
        Object.defineProperty(navigator, 'userAgentData', {{
          get: () => ({{
            brands: [
              {{ brand: "Not_A Brand",   version: "8"    }},
              {{ brand: "Chromium",      version: "{major}" }},
              {{ brand: "Google Chrome", version: "{major}" }}
            ],
            mobile: false,
            platform: "Windows"
          }})
        }});
    """)

    # --- plugins：必须是 PluginArray 结构，不能是数字数组 ---
    await context.add_init_script("""
        (() => {
          const makePlugin = (name, filename, description, mimeTypes) => {
            const plugin = Object.create(Plugin.prototype);
            Object.defineProperties(plugin, {
              name:        { value: name,        enumerable: true },
              filename:    { value: filename,    enumerable: true },
              description: { value: description, enumerable: true },
              length:      { value: mimeTypes.length, enumerable: true },
            });
            mimeTypes.forEach((mt, i) => {
              plugin[i] = mt;
            });
            return plugin;
          };

          const mimeType = (type, suffixes, description, plugin) => {
            const mt = Object.create(MimeType.prototype);
            Object.defineProperties(mt, {
              type:        { value: type,        enumerable: true },
              suffixes:    { value: suffixes,    enumerable: true },
              description: { value: description, enumerable: true },
              enabledPlugin: { value: plugin,   enumerable: true },
            });
            return mt;
          };

          const pdfPlugin = makePlugin(
            'PDF Viewer', 'internal-pdf-viewer',
            'Portable Document Format', []
          );
          const chromePdf = makePlugin(
            'Chrome PDF Viewer', 'internal-pdf-viewer',
            'Portable Document Format', []
          );
          const nativePdf = makePlugin(
            'Chromium PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
            'Portable Document Format', []
          );
          const nacl = makePlugin(
            'Native Client', 'internal-nacl-plugin',
            '', []
          );
          const widevine = makePlugin(
            'Widevine Content Decryption Module',
            'widevinecdmadapter.dll',
            'Enables Widevine licenses for playback of HTML audio/video content.', []
          );

          const plugins = [pdfPlugin, chromePdf, nativePdf, nacl, widevine];

          Object.defineProperty(navigator, 'plugins', {
            get: () => {
              const arr = Object.create(PluginArray.prototype);
              Object.defineProperty(arr, 'length', { value: plugins.length });
              plugins.forEach((p, i) => { arr[i] = p; });
              arr.item = (i) => plugins[i];
              arr.namedItem = (name) => plugins.find(p => p.name === name) || null;
              arr.refresh = () => {};
              return arr;
            }
          });
        })();
    """)

    # --- speechSynthesis：真实浏览器有语音列表，空列表很异常 ---
    await context.add_init_script("""
        (() => {
          const voices = [
            { default: true,  lang: 'en-US', localService: true,
              name: 'Microsoft David - English (United States)',
              voiceURI: 'Microsoft David - English (United States)' },
            { default: false, lang: 'en-US', localService: true,
              name: 'Microsoft Zira - English (United States)',
              voiceURI: 'Microsoft Zira - English (United States)' },
            { default: false, lang: 'en-GB', localService: true,
              name: 'Microsoft Hazel - English (United Kingdom)',
              voiceURI: 'Microsoft Hazel - English (United Kingdom)' },
          ];
          if (window.speechSynthesis) {
            window.speechSynthesis.getVoices = () => voices;
          }
        })();
    """)


# ===== 间隔函数 =====

async def human_like_sleep():
    """
    长尾分布的关键词间隔，模拟真实用户看结果的时间：
    - 75%：8-25秒（正常浏览结果）
    - 17%：30-60秒（停下来多看几眼）
    - 8%： 60-120秒（去倒水/厕所）
    """
    r = random.random()
    if r < 0.75:
        t = random.uniform(8, 25)
    elif r < 0.92:
        t = random.uniform(30, 60)
    else:
        t = random.uniform(60, 120)
    logger.info(f"关键词间隔 {t:.1f}s")
    await asyncio.sleep(t)


# ===== ContextWrapper =====

class ContextWrapper:
    """
    封装一个 BrowserContext + Page，支持多关键词复用。

    生命周期：
      创建 → 处理 N 个关键词 → retire()
      N = max_keywords（随机 5-15），到了就退休，不强制杀掉
    """

    def __init__(self, browser_wrapper: "BrowserWrapper", context: BrowserContext,
                 proxy: dict, language_code: str):
        self.browser_wrapper = browser_wrapper
        self.context = context
        self.proxy = proxy
        self.language_code = language_code

        # 寿命：随机 5-15 个关键词
        self.max_keywords = random.randint(5, 15)
        self.keyword_count = 0          # 已处理关键词数

        self.fail_count = 0             # 普通失败次数
        self.consecutive_sorry = 0      # 连续触发 sorry 次数

        self.page: Optional[Page] = None
        self.closed = False
        self.in_use = False

    @property
    def should_retire(self) -> bool:
        """是否应该退休（寿命到了，或者连续 sorry ≥ 2）"""
        return self.keyword_count >= self.max_keywords or self.consecutive_sorry >= 2

    async def ensure_page(self):
        """确保 page 存在且未关闭"""
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
            await self.page.route("**/*", block_images)
        return self.page

    async def retire(self):
        """退休：关闭 context，从 browser_wrapper 中移除自己"""
        if self.closed:
            return
        self.closed = True
        try:
            await self.context.close()
        except Exception:
            pass
        self.browser_wrapper.contexts.discard(self)
        logger.info(f"Context 退休 (proxy={self.proxy.get('server')}, "
                    f"keywords={self.keyword_count}/{self.max_keywords}, "
                    f"sorry={self.consecutive_sorry})")


# ===== BrowserWrapper =====

class BrowserWrapper:
    def __init__(self, playwright, chrome_path: str):
        self.playwright = playwright
        self.chrome_path = chrome_path
        self.browser = None
        self.fail_count = 0
        self.contexts: set[ContextWrapper] = set()
        self.lock = asyncio.Lock()

    async def start(self):
        self.browser = await self.playwright.chromium.launch(
            executable_path=self.chrome_path,
            headless=Config.HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-default-apps",
                "--disable-extensions",
                "--mute-audio",
                "--use-gl=desktop",
                "--enable-features=NetworkService,NetworkServiceInProcess",
                "--max-connections-per-host=6",
            ],
        )

    async def new_context(self, proxy: dict, language_code: str) -> ContextWrapper:
        async with self.lock:
            time_zone = random.choice(Config.FINGERPRINT_REGIONS.get(language_code))
            dpr_setting = random.choice(Config.DPR_SETTING)
            ua = random.choice(Config.USER_AGENT)

            context = await self.browser.new_context(
                proxy=proxy,
                locale=time_zone["locale"],
                screen=dpr_setting["screen"],
                viewport=dpr_setting["viewport"],
                user_agent=ua,
                device_scale_factor=dpr_setting["dpr"],
                timezone_id=time_zone["timezone"],
                extra_http_headers={"Accept-Language": time_zone["accept_language"]}
            )

            # 注入语言
            await context.add_init_script("""
                Object.defineProperty(navigator, 'languages', {{
                  get: () => ['{lc}', '{l}']
                }});
            """.format(lc=language_code, l=language_code.split("-")[0]))

            # 注入所有指纹
            await inject_fingerprint(context, ua)

            # Google cookie（跳过同意弹窗）
            await context.add_cookies([
                {
                    'name': 'CONSENT',
                    'value': 'YES+srp.gws-20260211-0-RC2.en+FX+111',
                    'domain': '.google.com', 'path': '/',
                    'secure': True, 'sameSite': 'Lax'
                },
                {
                    'name': 'SOCS',
                    'value': 'CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg',
                    'domain': '.google.com', 'path': '/',
                    'secure': True, 'sameSite': 'Lax'
                },
                {
                    'name': 'NID',
                    'value': '511=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
                    'domain': '.google.com', 'path': '/',
                    'secure': True, 'httpOnly': True, 'sameSite': 'None'
                }
            ])

            wrapper = ContextWrapper(self, context, proxy, language_code)
            self.contexts.add(wrapper)
            return wrapper

    async def restart(self):
        try:
            await self.browser.close()
        except Exception:
            pass
        await self.start()
        self.fail_count = 0
        logger.info("Browser 熔断重启完成")


# ===== BrowserPool =====

class BrowserPool:
    """
    Context 复用池。

    核心语义变化（对比旧版）：
      旧：acquire=新建context，release=销毁context
      新：acquire=从idle队列取，release=判断是否退休，没退休放回队列

    idle_queue 里存放的都是空闲的、未退休的 ContextWrapper。
    退休或出错时，pool 自动异步补充一个新的进来，保持池子大小稳定。
    """

    def __init__(
        self,
        chrome_path: str,
        max_browser: int = 4,
        max_context_per_browser: int = 2,
        browser_fail_limit: int = 3,
        startup_jitter: float = 30.0,   # 启动错峰最大秒数
    ):
        self.chrome_path = chrome_path
        self.max_browser = max_browser
        self.max_context_per_browser = max_context_per_browser
        self.browser_fail_limit = browser_fail_limit
        self.startup_jitter = startup_jitter

        self.total_slots = max_browser * max_context_per_browser

        self.playwright = None
        self.browsers: list[BrowserWrapper] = []

        # 空闲队列：所有可用的 ContextWrapper
        self._idle_queue: asyncio.Queue[ContextWrapper] = asyncio.Queue()

        # 补充锁：防止并发触发多次补充
        self._replenish_lock = asyncio.Lock()

        self.total_success = 0  # 累计成功关键词数
        self.total_sorry = 0  # 累计 sorry 次数
        self.total_retired = 0  # 累计退休 context 数

    async def start(self, initial_proxies: list[dict], language_code: str):
        """
        启动浏览器，预热所有 context。
        启动时加随机错峰延迟，避免200个代理同时打 Google。
        """
        self.playwright = await async_playwright().start()

        for _ in range(self.max_browser):
            bw = BrowserWrapper(self.playwright, self.chrome_path)
            await bw.start()
            self.browsers.append(bw)

        logger.info(f"启动 {self.max_browser} 个 Browser，预热 {self.total_slots} 个 Context...")

        proxy_iter = iter(initial_proxies)
        tasks = []
        for i in range(self.total_slots):
            proxy = next(proxy_iter, None)
            if proxy is None:
                break
            # 错峰延迟：每个 context 随机延迟 0 ~ startup_jitter 秒
            jitter = random.uniform(0, self.startup_jitter)
            tasks.append(self._delayed_create_context(jitter, proxy, language_code))

        await asyncio.gather(*tasks)
        logger.info(f"BrowserPool 预热完成，idle={self._idle_queue.qsize()}")

    async def _delayed_create_context(self, delay: float, proxy: dict, language_code: str):
        await asyncio.sleep(delay)
        await self._create_and_enqueue(proxy, language_code)

    async def _create_and_enqueue(self, proxy: dict, language_code: str):
        """创建一个新的 ContextWrapper 并放入空闲队列"""
        browser = self._select_browser()
        try:
            ctx = await browser.new_context(proxy, language_code)
            await self._idle_queue.put(ctx)
            logger.debug(f"新 Context 入队 (proxy={proxy.get('server')}, idle={self._idle_queue.qsize()})")
        except Exception as e:
            logger.error(f"创建 Context 失败 (proxy={proxy.get('server')}): {e}")
            browser.fail_count += 1
            if browser.fail_count >= self.browser_fail_limit:
                logger.warning("Browser 熔断重启")
                await browser.restart()

    def _select_browser(self) -> BrowserWrapper:
        return min(self.browsers, key=lambda b: len(b.contexts))

    async def acquire(self, timeout: float = 60.0) -> ContextWrapper:
        """
        从空闲队列取一个 ContextWrapper。
        如果队列暂时为空（全部在用中），等待直到有归还的。
        """
        try:
            ctx = await asyncio.wait_for(self._idle_queue.get(), timeout=timeout)
            ctx.in_use = True
            return ctx
        except asyncio.TimeoutError:
            raise TimeoutError(f"BrowserPool.acquire 超时 ({timeout}s)，当前 idle={self._idle_queue.qsize()}")

    async def release(self, ctx: ContextWrapper, proxy: dict, language_code: str, success: bool = True):
        """
        归还 ContextWrapper。

        - 如果 should_retire → 退休，异步补充一个新的
        - 如果 success=False → fail_count++，连续 sorry 由调用方自行维护
        - 否则放回队列继续用
        """
        ctx.in_use = False

        if not success:
            ctx.fail_count += 1

        if ctx.should_retire or ctx.closed:
            # 退休
            await ctx.retire()
            # 异步补充，不阻塞调用方
            asyncio.create_task(
                self._replenish(proxy, language_code),
                name="pool-replenish"
            )
        else:
            # 放回队列
            await self._idle_queue.put(ctx)
            logger.debug(f"Context 归还队列 (keywords={ctx.keyword_count}/{ctx.max_keywords}, "
                         f"idle={self._idle_queue.qsize()})")

    async def _replenish(self, proxy: dict, language_code: str):
        """补充一个新 Context 到池子"""
        async with self._replenish_lock:
            # 稍微错开一下，避免退休潮同时补充
            await asyncio.sleep(random.uniform(1, 5))
            await self._create_and_enqueue(proxy, language_code)

    async def shutdown(self):
        for b in self.browsers:
            try:
                await b.browser.close()
            except Exception:
                pass
        await self.playwright.stop()
        logger.info("BrowserPool shutdown 完成")


# ===== 工具函数 =====

async def block_images(route):
    url = route.request.url.lower()
    rtype = route.request.resource_type
    if rtype == "image" or url.endswith(Config.IMAGE_EXTENSIONS):
        await route.abort()
    else:
        await route.continue_()


def create_child_task(coro, *, name=None, suffix=None):
    parent = asyncio.current_task()
    parent_name = parent.get_name() if parent else "Main"
    if name:
        task_name = name
    elif suffix:
        task_name = f"{parent_name}/{suffix}"
    else:
        task_name = parent_name
    return asyncio.create_task(coro, name=task_name)


async def save_text(path: str, content: str, mode: str = "w"):
    async with aiofiles.open(path, mode=mode, encoding="utf-8") as f:
        await f.write(content)


# ===== Cookie / 导航工具 =====

async def handle_cookie_consent(page, timeout=5000):
    selectors = [
        'button#L2AGLb',
        'button[aria-label*="Accept"]',
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        'div[role="button"]:has-text("Accept")',
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=timeout):
                await asyncio.sleep(random.uniform(0.5, 1.0))
                await button.click()
                logger.info("✅ 已点击 Cookie 同意按钮")
                await asyncio.sleep(random.uniform(0.3, 0.8))
                return True
        except Exception:
            continue
    return False


def is_sorry_url(url: str) -> bool:
    return '/sorry/' in url or url.startswith('https://sorry.google.com')


# ===== 鼠标 / 滚动 =====

async def human_mouse_move(page, start, end, steps=30):
    for i in range(steps):
        t = i / steps
        x = start[0] + (end[0] - start[0]) * t + random.uniform(-2, 2)
        y = start[1] + (end[1] - start[1]) * t + random.uniform(-2, 2)
        await page.mouse.move(x, y)
    await page.wait_for_timeout(random.randint(5, 20))


async def human_scroll(page, steps=6, wait_for_load=True):
    for i in range(steps):
        prev_height = await page.evaluate("() => document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(0.5, 1.0))
        if wait_for_load:
            new_height = await page.evaluate("() => document.body.scrollHeight")
            if new_height == prev_height:
                logger.info(f"已到达页面底部 (滚动 {i + 1} 次)")
                break
            else:
                logger.info(f"页面高度: {prev_height} -> {new_height}")
        if random.random() < 0.3:
            back_distance = random.randint(100, 300)
            await page.evaluate(f"window.scrollBy(0, -{back_distance})")
            await asyncio.sleep(random.uniform(0.3, 0.6))


# ===== 响应消费者 =====

async def response_consumer(queue, task_id, params, aggregated):
    while True:
        response = await queue.get()
        if response is None:
            queue.task_done()
            break
        try:
            body = await asyncio.wait_for(response.text(), timeout=15.0)
            result = await demo_with_real_data(body)
            for item in result:
                if item.get("site", ".jp").endswith('.jp'):
                    continue
                new_data = {
                    "index": item.get("id"),
                    "word": item.get("title"),
                    "domain": item.get("site"),
                    "link": item.get("url"),
                    "image": item.get("image"),
                    "info": {
                        "desc": item.get("desc"),
                        "brand": item.get("brand"),
                        "price": item.get("price"),
                        "currency": item.get("currency"),
                        "score": item.get("score"),
                        "review": item.get("review"),
                    },
                    "parent": task_id,
                    "stat": -1,
                    "createdAt": str(datetime.datetime.now(datetime.timezone.utc))
                }
                await aggregated.add_data(new_data)
                await aggregated.add_domain(item.get("site"))
            related_search = await get_related_search(body)
            await aggregated.add_related_search(related_search)
            related_items = await get_related_items(body)
            await aggregated.add_related_items(related_items)
            logger.info(f"[Work-{params.worker_id}] 处理完成，数据: {len(result)}")
        except asyncio.TimeoutError:
            logger.warning(f"[Work-{params.worker_id}] response.text() 超时，跳过")
        except Exception as e:
            if "Target page, context or browser has been closed" in str(e):
                logger.warning(f"[Work-{params.worker_id}] 页面已关闭")
            else:
                logger.exception(f"[Work-{params.worker_id}] 消费异常: {e}")
        finally:
            queue.task_done()


async def wait_queue_safe(queue, consumer_task, params, timeout=120):
    try:
        await asyncio.wait_for(asyncio.shield(queue.join()), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"[Work-{params.worker_id}] queue.join 超时，强制跳出")
    if consumer_task.done():
        exc = consumer_task.exception()
        if exc:
            logger.error(f"[Work-{params.worker_id}] consumer_task 异常退出: {exc}")


# ===== 输入 =====

async def human_type_and_submit(page, keyword_item, timeout=10000):
    keyword = keyword_item["name"]
    try:
        await page.wait_for_selector("textarea.gLFyf", timeout=timeout)
        textarea = page.locator("textarea.gLFyf")
        box = await textarea.bounding_box()
        if not box:
            raise RuntimeError("Cannot get textarea bounding box")
        start = (random.randint(0, 200), random.randint(0, 200))
        end = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await human_mouse_move(page, start, end, steps=random.randint(25, 40))
        await textarea.click(delay=random.randint(50, 120))
        await page.wait_for_timeout(random.randint(200, 500))
        await page.keyboard.down("Control")
        await page.keyboard.press("KeyA")
        await page.keyboard.up("Control")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(random.randint(100, 300))
        await page.evaluate(f"""
            document.querySelector('textarea.gLFyf').value = {json.dumps(keyword)};
        """)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(random.randint(200, 300))
    except PlaywrightTimeout as e:
        logger.error(f"人类输入超时: {e}")
        raise
    except Exception as e:
        logger.exception(f"人类输入异常: {e}")
        raise


# ===== 单关键词搜索 =====

async def search_single_keyword_with_page(page, keyword_item, params, max_retries=2):
    """
    搜索单个关键词。

    返回值：
      True   → 成功
      False  → 普通失败（可重试）
      None   → 代理失败（需要换代理 / 退休 context）
      "sorry"→ 触发了 sorry 页面
    """
    keyword = keyword_item["name"]
    keyid = keyword_item["id"]

    response_queue = asyncio.Queue()
    aggregated = ThreadSafeAggregator()

    for attempt in range(max_retries):
        try:
            async def handle_response(response):
                url = response.url
                if "google.com/search" not in url:
                    return
                if "tbm=isch" not in url and "q=" not in url:
                    return
                if response.status in [301, 302]:
                    return
                logger.info(f"[Work-{params.worker_id}] 捕获响应: {url}")
                await response_queue.put(response)

            page.on('response', handle_response)
            consumer_task = create_child_task(
                response_consumer(response_queue, keyid, params, aggregated),
                name=f"Work-{params.worker_id}"
            )

            # --- goto ---
            logger.info(f"[{keyword}] 打开 Google 图片搜索 (尝试 {attempt + 1}/{max_retries})")
            task = None
            try:
                task = create_child_task(
                    page.goto(
                        f"https://www.google.com/imghp?hl={params.language_code}&authuser=0&ogbl",
                        wait_until="domcontentloaded",
                        timeout=30000
                    )
                )
                await asyncio.wait_for(task, timeout=40.0)

                # 前置检查：刚打开就 sorry，直接返回
                if is_sorry_url(page.url):
                    logger.warning(f"[{keyword}] goto 后立即触发 sorry: {page.url}")
                    return "sorry"

                await asyncio.sleep(0.5)
                await handle_cookie_consent(page, timeout=3000)

            except (PlaywrightError, asyncio.TimeoutError) as e:
                error_msg = str(e)
                proxy_errors = [
                    "ERR_PROXY_CONNECTION_FAILED",
                    "ERR_TUNNEL_CONNECTION_FAILED",
                    "ERR_SOCKS_CONNECTION_FAILED",
                    "ERR_CONNECTION_REFUSED",
                    "ERR_CONNECTION_TIMED_OUT",
                    "net::ERR_",
                ]
                if any(err in error_msg for err in proxy_errors):
                    logger.error(f"[{keyword}] 代理/网络错误: {error_msg}")
                    return None  # 代理失败
                elif isinstance(e, asyncio.TimeoutError):
                    if task:
                        task.cancel()
                    logger.error(f"[{keyword}] 页面加载超时 (尝试 {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(3)
                        continue
                    return False
                else:
                    logger.exception(f"[{keyword}] 导航失败: {e}")
                    raise

            # --- 输入关键词 ---
            logger.info(f"[{keyword}] 开始输入关键词")
            task = create_child_task(human_type_and_submit(page, keyword_item))
            await asyncio.wait_for(task, timeout=20.0)

            await asyncio.sleep(1)

            # 输入后检查 sorry
            if is_sorry_url(page.url):
                logger.warning(f"[{keyword}] 输入后触发 sorry: {page.url}")
                return "sorry"

            # --- 滚动 ---
            logger.info(f"[{keyword}] 开始滚动")
            task = create_child_task(human_scroll(page, 3))
            await asyncio.wait_for(task, timeout=60.0)

            # --- 等待响应队列 ---
            logger.info(f"[{keyword}] 等待响应队列处理...")
            await wait_queue_safe(response_queue, consumer_task, params, timeout=120)

            # 停止 consumer
            if not consumer_task.done():
                await response_queue.put(None)
                try:
                    await asyncio.wait_for(consumer_task, timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(f"[Work-{params.worker_id}] consumer_task 停止超时，强制取消")
                    consumer_task.cancel()
                    try:
                        await consumer_task
                    except asyncio.CancelledError:
                        pass
            else:
                exc = consumer_task.exception()
                if exc:
                    logger.error(f"[Work-{params.worker_id}] consumer_task 异常退出: {exc}")

            # --- 数据处理 ---
            aggregated_data = await aggregated.get_all()
            logger.info(f"[{keyword}] 聚合数据: {len(aggregated_data['new_datas'])} 条")

            if aggregated_data['new_datas']:
                unique_domains = list(set(aggregated_data['domains']))
                unique_related_search = list(set(aggregated_data['related_search'])) if aggregated_data['related_search'] else []
                unique_related_items = list(set(aggregated_data['related_items'])) if aggregated_data['related_items'] else []

                products = await deal_info_by_async(aggregated_data['new_datas'], params)
                shopify_products = await deal_shopify_product_info_async(params, products)

                google_item = {
                    'id': keyid,
                    'use_proxy_ip': params.proxies.get("server"),
                    'from': params.proxies.get("server").replace("socks5://", "").split(":")[0],
                    'word': keyword,
                    'script': "",
                    'domains': json.dumps(unique_domains),
                    'related': json.dumps(unique_related_search),
                    'items': json.dumps(unique_related_items),
                    'products': json.dumps(products)
                }

                if products or shopify_products:
                    async with aiohttp.ClientSession() as session:
                        if products:
                            await send_items_to_api(session, params, google_item)
                        if shopify_products:
                            await send_shopify_product_to_api(session, params, shopify_products)

                logger.info(f"[{keyword}] 数据处理完成")
            else:
                logger.warning(f"[{keyword}] 没有收集到数据")

            # 最终 sorry 检查
            if is_sorry_url(page.url):
                logger.warning(f"[{keyword}] 最终检查触发 sorry: {page.url}")
                return "sorry"

            logger.info(f"[Success] 完成关键词: {keyword}")
            return True

        except Exception as e:
            logger.exception(f"[{keyword}] 搜索异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
            else:
                logger.error(f"[{keyword}] 已达最大重试次数，跳过")
                return False

    return False


# ===== 批量搜索（核心改动） =====

async def search_keyword_batch(params, pool: BrowserPool, language_code: str):
    """
    核心改动：一个 context 连续处理多个关键词，不再每词新建/销毁。

    流程：
      1. acquire 一个 context（可能已有历史 cookie）
      2. 循环处理关键词，每词之间 human_like_sleep
      3. context 达到寿命 / 连续 sorry ≥ 2 → release(success=False) 触发退休
      4. 普通成功 → release(success=True) 放回队列
    """
    success_count = 0
    fail_count = 0
    tasks = params.tasks.copy()
    err_tasks = []

    while tasks:
        # 每次 acquire 一个 context，连续处理若干关键词
        proxy = await params.app.get_random_proxy()
        params.proxies = proxy

        try:
            ctx = await pool.acquire(timeout=60.0)
        except TimeoutError as e:
            logger.error(f"[Work-{params.worker_id}] acquire 超时: {e}")
            await asyncio.sleep(5)
            continue

        page = await ctx.ensure_page()
        ctx_success = True  # 这个 context 整体是否正常

        # 在这个 context 的剩余寿命内循环处理关键词
        while tasks and not ctx.should_retire:
            keyword_item_str = tasks.pop(0)
            keyword_item = json.loads(keyword_item_str)

            result = await search_single_keyword_with_page(page, keyword_item, params)
            ctx.keyword_count += 1

            if result is True:
                ctx.consecutive_sorry = 0   # 重置连续 sorry
                success_count += 1
                pool.total_success += 1
                await params.app.set_success(params.atm, proxy)

            elif result == "sorry":
                ctx.consecutive_sorry += 1
                pool.total_sorry += 1
                err_tasks.append(keyword_item_str)
                fail_count += 1
                await params.app.set_fail(params.atm, proxy)
                logger.warning(f"[Work-{params.worker_id}] 连续 sorry = {ctx.consecutive_sorry}")
                if ctx.consecutive_sorry >= 2:
                    # 代理被封，强制退休这个 context
                    ctx_success = False
                    break

                # 单次 sorry 先等一会再试（可能是临时的）
                await asyncio.sleep(random.uniform(30, 60))

            elif result is None:
                # 代理连接失败
                ctx_success = False
                err_tasks.append(keyword_item_str)
                fail_count += 1
                await params.app.set_fail(params.atm, proxy)
                break

            else:
                # result is False：普通失败
                err_tasks.append(keyword_item_str)
                fail_count += 1
                ctx.fail_count += 1

            # 关键词间隔（长尾分布）
            if tasks and not ctx.should_retire:
                await human_like_sleep()

        # 归还 context
        await pool.release(ctx, proxy=proxy, language_code=language_code, success=ctx_success)

    if err_tasks:
        await send_err_task(params, err_tasks)

    logger.info(f"[Work-{params.worker_id}] 批次完成 - 成功: {success_count}, 失败: {fail_count}")