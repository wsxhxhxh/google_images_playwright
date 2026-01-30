import json
import os
import random
import datetime
import asyncio
import aiofiles


import aiohttp
from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeout
from typing import Optional

from config import Config, logger
from deal_product_func_async import deal_info_by_async, deal_shopify_product_info_async
from parsel_json_str import demo_with_real_data, get_related_search, get_related_items
from platform_api import send_items_to_api, send_shopify_product_to_api


async def block_images(route):
    url = route.request.url.lower()
    rtype = route.request.resource_type

    if rtype == "image" or url.endswith(Config.IMAGE_EXTENSIONS):
        await route.abort()
    else:
        await route.continue_()


async def save_text(path: str, content: str):
    async with aiofiles.open(path, mode="w", encoding="utf-8") as f:
        await f.write(content)


class PlaywrightBrowser:
    def __init__(
            self,
            chrome_path: str,
            language_code: str = "en-US",
            proxies: dict = None,
            headless: bool = False,
    ):
        """
        初始化 Playwright 浏览器配置

        Args:
            chrome_path: Chrome 可执行文件路径
            headless: 是否无头模式
            viewport: 视口大小，默认 1366x768
            locale: 语言设置
            timezone_id: 时区设置
        """
        self.chrome_path = chrome_path
        self.language_code = language_code
        self.proxies = proxies
        self.headless = headless

        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        await self.close()

    async def initialize(self):
        """初始化 Playwright 和浏览器上下文"""
        self.playwright = await async_playwright().start()

        self.browser = await self.playwright.chromium.launch(
            executable_path=self.chrome_path,
            proxy=self.proxies,
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        time_zone = random.choice(Config.FINGERPRINT_REGIONS.get(self.language_code))
        dpr_setting = random.choice(Config.DPR_SETTING)
        ua = random.choice(Config.USER_AGENT)
        major = ua.split("Chrome/")[1].split(".")[0]
        logger.info(str(time_zone))
        logger.info(self.language_code)
        context = await self.browser.new_context(
            locale=time_zone["locale"],
            screen=dpr_setting["screen"],
            viewport=dpr_setting["viewport"],
            user_agent=ua,
            device_scale_factor=dpr_setting["dpr"],
            timezone_id=time_zone["timezone"],
            extra_http_headers={"Accept-Language": time_zone["accept_language"], }
        )

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
        """.format(language_code=self.language_code, language=self.language_code.split("-")[0]))
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
        """.format(major=major))
        await context.add_init_script("""
            Object.defineProperty(navigator, 'plugins', {
              get: () => [1, 2, 3, 4, 5]
            });
        """)

        self.context = context
        self.page = None

    async def _inject_anti_detection_scripts(self):
        """注入反检测脚本"""
        await self.page.add_init_script("""
            // chrome
            window.chrome = {
                runtime: {}
            };
            // permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
        """)

    async def goto(self, url: str, wait_until: str = "domcontentloaded"):
        """
        导航到指定 URL

        Args:
            url: 目标 URL
            wait_until: 等待状态（domcontentloaded, load, networkidle）
        """
        if not self.page:
            raise RuntimeError("Page not created. Call new_page() first.")

        await self.page.goto(url, wait_until=wait_until)

    async def search_google_images(self, keyword: str):
        """
        在 Google 图片搜索关键词

        Args:
            keyword: 搜索关键词
        """
        if not self.page:
            raise RuntimeError("Page not created. Call new_page() first.")

        # 等待搜索框
        await self.page.wait_for_selector("textarea.gLFyf", timeout=5000)
        textarea = self.page.locator("textarea.gLFyf")

        # 模拟真人输入
        await textarea.click()
        await textarea.fill("")
        await textarea.type(keyword, delay=80)
        await textarea.press("Enter")

        await self.page.wait_for_load_state("domcontentloaded")

    async def smooth_scroll(self, scroll_count: int = 5, distance: int = 400, delay: int = 800):
        """
        平滑滚动页面

        Args:
            scroll_count: 滚动次数
            distance: 每次滚动的像素距离
            delay: 每次滚动之间的延迟（毫秒）
        """
        if not self.page:
            raise RuntimeError("Page not created. Call new_page() first.")

        for _ in range(scroll_count):
            await self.page.mouse.wheel(0, distance)
            await self.page.wait_for_timeout(delay)

    async def type_with_human_like_delay(self, selector: str, text: str, delay: int = 80, clear_first: bool = True):
        """
        在指定元素中模拟真人打字

        Args:
            selector: 元素选择器
            text: 要输入的文本
            delay: 每个字符之间的延迟（毫秒）
            clear_first: 是否先清空输入框
        """
        if not self.page:
            raise RuntimeError("Page not created. Call new_page() first.")

        element = self.page.locator(selector)
        await element.click()

        if clear_first:
            await element.fill("")

        await element.type(text, delay=delay)

    async def close(self):
        """关闭浏览器和 Playwright"""
        try:
            if self.page:
                await self.page.close()
        except Exception as e:
            logger.warning(f"关闭页面时出错: {e}")

        try:
            if self.context:
                await self.context.close()
        except Exception as e:
            logger.warning(f"关闭上下文时出错: {e}")

        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.warning(f"关闭浏览器时出错: {e}")

        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.warning(f"停止 Playwright 时出错: {e}")

    async def create_new_page(self) -> Page:
        """创建一个新的独立页面（不覆盖 self.page）"""
        if not self.context:
            raise RuntimeError("Browser context not initialized. Call initialize() first.")

        page = await self.context.new_page()
        await page.route("**/*", block_images)
        # 注入反爬虫脚本
        await self._inject_anti_detection_scripts_for_page(page)

        return page

    async def _inject_anti_detection_scripts_for_page(self, page: Page):
        """为指定页面注入反检测脚本"""
        await page.add_init_script("""
            // chrome
            window.chrome = {
                runtime: {}
            };

            // permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
        """)


async def human_mouse_move(page, start, end, steps=30):
    for i in range(steps):
        t = i / steps
        x = start[0] + (end[0] - start[0]) * t + random.uniform(-2, 2)
        y = start[1] + (end[1] - start[1]) * t + random.uniform(-2, 2)
        await page.mouse.move(x, y)
    await page.wait_for_timeout(random.randint(5, 20))


async def human_scroll(page, steps=6, wait_for_load=True):
    """
    滚动到页面底部触发懒加载

    Args:
        page: Playwright 页面对象
        steps: 最大滚动次数
        wait_for_load: 是否等待新内容加载
    """
    for i in range(steps):
        # 获取当前页面高度
        prev_height = await page.evaluate("() => document.body.scrollHeight")

        # 滚动到底部
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # 等待一段时间，让新内容加载
        await asyncio.sleep(random.uniform(1.0, 2.0))

        if wait_for_load:
            # 获取新的页面高度
            new_height = await page.evaluate("() => document.body.scrollHeight")

            # 如果高度没有变化，说明没有更多内容了
            if new_height == prev_height:
                logger.info(f"已到达页面底部，没有更多内容加载 (滚动 {i + 1} 次)")
                break
            else:
                logger.info(f"检测到新内容加载，页面高度: {prev_height} -> {new_height}")

        # 随机小幅回滚（模拟真人行为）
        if random.random() < 0.3:
            back_distance = random.randint(100, 300)
            await page.evaluate(f"window.scrollBy(0, -{back_distance})")
            await asyncio.sleep(random.uniform(0.3, 0.6))


def make_response_handler(task_id, params, aggregated_data):
    """
    aggregated_data: 共享的数据收集器字典，包含 new_datas, related_search, related_items
    """

    async def handle_response(response):
        url = response.url
        if 'https://www.google.com/search' in url:
            if response.status in [301, 302]: return
            logger.info(f"捕获到搜索请求: {url}")

            try:
                body = await response.text()
                # await save_text(f"./logs/html_temp_{len(os.listdir('./logs')) + 1}.txt", body)

                result = await demo_with_real_data(body)

                # 收集 new_datas
                for index, item in enumerate(result):
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
                    aggregated_data['new_datas'].append(new_data)
                    aggregated_data['domains'].append(item.get("site"))

                # 收集 related_search
                related_search = await get_related_search(body)
                logger.info(
                    f"related search num: {len(related_search)} {related_search[:3]}...")
                aggregated_data['related_search'].extend(related_search)

                # 收集 related_items
                related_items = await get_related_items(body)
                logger.info(
                    f"related item num: {len(related_items)} {related_items[:3]}...")
                aggregated_data['related_items'].extend(related_items)

            except Exception as e:
                logger.exception(f"无法获取响应体: {e}")

    return handle_response

async def human_type_and_submit(page, keyword_item, timeout=10000):
    """
    模拟人类输入并提交搜索

    Args:
        page: Playwright 页面对象
        keyword_item: 关键词字典
        timeout: 超时时间（毫秒）
    """
    keyword = keyword_item["name"]
    try:
        # 等搜索框
        await page.wait_for_selector("textarea.gLFyf", timeout=timeout)
        textarea = page.locator("textarea.gLFyf")

        # 获取元素位置
        box = await textarea.bounding_box()
        if not box:
            raise RuntimeError("Cannot get textarea bounding box")

        # 模拟鼠标移动到输入框中心
        start = (random.randint(0, 200), random.randint(0, 200))
        end = (
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )
        await human_mouse_move(page, start, end, steps=random.randint(25, 40))

        # 点击聚焦
        await textarea.click(delay=random.randint(50, 120))

        # 停顿一下（人会想一想）
        await page.wait_for_timeout(random.randint(200, 500))

        # 清空（Ctrl+A + Backspace，比 fill 更像人）
        await page.keyboard.down("Control")
        await page.keyboard.press("KeyA")
        await page.keyboard.up("Control")
        await page.keyboard.press("Backspace")

        await page.wait_for_timeout(random.randint(100, 300))

        # 真人打字（每个字母不等速）
        for ch in keyword:
            await page.keyboard.type(ch)
            await page.wait_for_timeout(random.randint(30, 80))

        # 打完字，犹豫一下
        await page.wait_for_timeout(random.randint(200, 500))

        # 用「输入换行」提交（不是 press）
        await page.keyboard.type("\n")

        # 提交后停顿
        await page.wait_for_timeout(random.randint(800, 1500))

    except PlaywrightTimeout as e:
        logger.error(f"人类输入超时: {e}")
        raise
    except Exception as e:
        logger.exception(f"人类输入异常: {e}")
        raise


async def human_type_with_suggestion(page, keyword):
    """模拟使用Google搜索建议"""
    input_box = page.locator("textarea.gLFyf")
    await input_box.click()

    typed = ""
    target = keyword.lower()

    for ch in keyword:
        typed += ch
        await input_box.type(ch, delay=random.randint(80, 150))

        # 给 Google 一点反应时间
        await page.wait_for_timeout(random.randint(120, 220))

        suggestions = page.locator("li.sbct span")
        count = await suggestions.count()

        for i in range(count):
            text = (await suggestions.nth(i).inner_text()).lower()

            # ⭐ 完整命中
            if text == target:
                # 用键盘而不是 click（更像人）
                for _ in range(i):
                    await page.keyboard.press("ArrowDown")
                    await page.wait_for_timeout(random.randint(50, 90))

                await page.keyboard.press("Enter")
                return

    # 如果一直没命中,正常 Enter
    await page.keyboard.press("Enter")


async def search_single_keyword(browser, keyword_item, params, max_retries=2):
    """
    搜索单个关键词
    """
    page = None
    keyword = keyword_item["name"]
    keyid = keyword_item["id"]

    # 创建共享的数据收集器
    aggregated_data = {
        'new_datas': [],
        'related_search': [],
        'related_items': [],
        'domains': []
    }

    for attempt in range(max_retries):
        try:
            # 创建新页面
            page = await asyncio.wait_for(
                browser.create_new_page(),
                timeout=30.0
            )
            # 传入共享的数据收集器
            page.on('response', make_response_handler(keyid, params, aggregated_data))

            # 打开 Google 图片搜索
            logger.info(
                f"[{keyword}] 正在打开 Google 图片搜索页面 (尝试 {attempt + 1}/{max_retries})")
            await asyncio.wait_for(
                page.goto(
                    f"https://www.google.com/imghp?hl={params.language_code}&authuser=0&ogbl",
                    wait_until="domcontentloaded",
                    timeout=30000
                ),
                timeout=40.0
            )

            # 搜索关键词
            logger.info(f"[{keyword}] 开始输入关键词")
            await asyncio.wait_for(
                human_type_and_submit(page, keyword_item),
                timeout=20.0
            )

            # 平滑滚动
            logger.info(f"[{keyword}] 开始滚动页面")
            await asyncio.wait_for(
                human_scroll(page, 6),
                timeout=60.0
            )

            await asyncio.sleep(0.5)

            logger.info(f"[Success] 完成关键词: {keyword}")

            # 关闭页面
            if page: await page.close()

            # 在循环结束后统一处理所有收集到的数据
            if aggregated_data['new_datas']:
                logger.info(
                    f"[{keyword}] 开始处理聚合数据，共 {len(aggregated_data['new_datas'])} 条")

                # 去重处理（如果需要）
                unique_domains = list(set(aggregated_data['domains']))
                unique_related_search = list(set(aggregated_data['related_search'])) if aggregated_data['related_search'] else []
                unique_related_items = list(set(aggregated_data['related_items'])) if aggregated_data['related_items'] else []

                # 统一处理所有数据
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

            return True

        except asyncio.TimeoutError:
            logger.error(f"[{keyword}] 搜索超时 (尝试 {attempt + 1}/{max_retries})")
            if page:
                try:
                    await page.close()
                except:
                    pass
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
            else:
                logger.error(f"[{keyword}] 已达最大重试次数，跳过")
                return False

        except PlaywrightTimeout as e:
            logger.error(
                f"[{keyword}] Playwright 超时 (尝试 {attempt + 1}/{max_retries}): {e}")
            if page:
                try:
                    await page.close()
                except:
                    pass
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
            else:
                logger.error(f"[{keyword}] 已达最大重试次数，跳过")
                return False

        except Exception as e:
            logger.exception(
                f"[{keyword}] 搜索异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if page:
                try:
                    await page.close()
                except:
                    pass
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
            else:
                logger.error(f"[{keyword}] 已达最大重试次数，跳过")
                return False

    return False

async def search_keyword_batch(params):
    """
    批量搜索关键词

    Args:
        keyword_str_list: 关键词列表字符形式
        dbname: 数据库名
        binddomain: 绑定域名
        usenum: 使用数量
        desimagenum: 描述图片数量
        languageid: 语言ID
        jxycategory_id: 分类ID
        proxies: 代理配置
    """
    browser = None

    try:
        browser = PlaywrightBrowser(
            chrome_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            language_code=params.language_code,
            proxies=params.proxies,
            headless=False
        )

        # 初始化浏览器，带超时
        logger.info(f"初始化浏览器，代理: {params.proxies['server']}")
        await asyncio.wait_for(
            browser.initialize(),
            timeout=60.0
        )

        # 串行执行
        success_count = 0
        fail_count = 0

        for keyword_item_str in params.tasks:
            keyword_item = json.loads(keyword_item_str)
            logger.info(f"开始搜索: {keyword_item['name']}")
            success = await search_single_keyword(browser, keyword_item, params)

            if success:
                success_count += 1
            else:
                fail_count += 1

            # 每个关键词之间的间隔
            await asyncio.sleep(random.uniform(3, 5))

        logger.info(f"批次完成 - 成功: {success_count}, 失败: {fail_count}")

    except asyncio.TimeoutError:
        logger.error(f"浏览器初始化超时")
        raise
    except Exception as e:
        logger.exception(f"批量搜索异常: {e}")
        raise
    finally:
        if browser:
            logger.info(f"关闭浏览器")

            await browser.close()


# 使用示例
async def test():
    from dataclasses import dataclass
    @dataclass
    class SearchTaskParams:
        """搜索任务参数类"""
        worker_id = 1
        tasks = ['{"id": "92", "name": "air pot planter replacement parts"}']
        dbname = "t0039-c19-de-usgoimg"
        binddomain = "image8xgs.xyz"
        language_code = "en-US"
        usenum = 20
        desimagenum = 20
        languageid = 3
        jxycategory_id = 19
        proxies = {"server": "socks5://172.96.89.145:1080"}
        collect_platform_type = None

    params = SearchTaskParams()
    await search_keyword_batch(params)


if __name__ == "__main__":
    asyncio.run(test())
