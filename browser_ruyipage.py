# -*- coding: utf-8 -*-
"""
谷歌图片搜索脚本 —— ruyiPage 版本
将原 Playwright 异步实现迁移到 ruyiPage (FirefoxPage) 同步实现。

依赖安装：
    pip install ruyipage aiohttp aiofiles

使用方式：
    直接运行 main.py，本模块作为浏览器封装层被调用。
"""

import io
import sys
import json
import time
import random
import datetime
import asyncio

import aiohttp
import aiofiles

# Windows 控制台 UTF-8 兼容
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

from ruyipage import FirefoxPage, FirefoxOptions, Keys

from config import Config, logger, special_logger
from deal_product_func_async import deal_info_by_async, deal_shopify_product_info_async
from parsel_json_str import demo_with_real_data, get_related_search, get_related_items
from platform_api import send_items_to_api, send_shopify_product_to_api
from managed import ThreadSafeAggregator
from dblocal import DbManager


# ──────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────

def random_sleep(min_s: float = 0.5, max_s: float = 1.2):
    """随机等待，模拟真人节奏"""
    time.sleep(random.uniform(min_s, max_s))


# ──────────────────────────────────────────────────────────────
# 浏览器封装
# ──────────────────────────────────────────────────────────────

class RuyiPageBrowser:
    """
    对 ruyiPage FirefoxPage 的封装，提供与原 PlaywrightBrowser 相同的对外接口。

    ruyiPage 是同步库，因此所有方法均为普通函数（非 async）。
    """

    def __init__(
            self,
            language_code: str = "en-US",
            proxies: dict = None,
            headless: bool = False,
            firefox_path: str = None,
    ):
        self.language_code = language_code
        # proxies 格式: {"server": "socks5://host:port"}
        self.proxies = proxies or {}
        self.headless = headless
        self.firefox_path = firefox_path

        self.page: FirefoxPage | None = None

    # ── 初始化 ────────────────────────────────────────────────

    def initialize(self):
        """启动 Firefox，创建 FirefoxPage 实例。"""
        opts = FirefoxOptions()
        opts.headless(self.headless)

        # 代理设置
        proxy_server = self.proxies.get("server", "")
        if proxy_server:
            opts.set_proxy(proxy_server)

        # 自定义 Firefox 路径（可选）
        if self.firefox_path:
            opts.set_browser_path(self.firefox_path)

        self.page = FirefoxPage(opts)
        logger.info(f"[RuyiPageBrowser] Firefox 启动成功，代理: {proxy_server or '无'}")

    # ── 导航 ──────────────────────────────────────────────────

    def goto(self, url: str, timeout: int = 30):
        """
        导航到指定 URL。

        Args:
            url:     目标 URL
            timeout: 等待页面加载的最大秒数（ruyiPage 以秒为单位）
        """
        self._require_page()
        self.page.get(url, timeout=timeout)

    # ── Cookie 弹窗处理 ───────────────────────────────────────

    def handle_cookie_consent(self, timeout: float = 5.0) -> bool:
        """
        处理 Google Cookie 同意弹窗。

        Returns:
            True  — 找到并点击了同意按钮
            False — 未发现弹窗
        """
        self._require_page()
        selectors = [
            "#L2AGLb",
            "button[aria-label*='Accept']",
            "button:contains('Accept all')",
            "button:contains('I agree')",
        ]
        for sel in selectors:
            try:
                btn = self.page.ele(f"css:{sel}", timeout=timeout)
                if btn:
                    random_sleep(0.4, 0.9)
                    btn.click()
                    logger.info(f"[RuyiPageBrowser] 已点击 Cookie 同意按钮: {sel}")
                    random_sleep(0.3, 0.7)
                    return True
            except Exception:
                continue

        logger.info("[RuyiPageBrowser] 未检测到 Cookie 弹窗")
        return False

    # ── 搜索输入 ──────────────────────────────────────────────

    def human_type_and_submit(self, keyword_item: dict, timeout: float = 10.0):
        """
        模拟真人输入关键词并按回车提交。

        Args:
            keyword_item: {"id": ..., "name": "搜索词"}
            timeout:      等待搜索框出现的超时秒数
        """
        self._require_page()
        keyword = keyword_item["name"]

        # 等待搜索框
        textarea = self.page.ele("css:textarea.gLFyf", timeout=timeout)
        if not textarea:
            raise RuntimeError(f"[RuyiPageBrowser] 找不到搜索框，关键词: {keyword}")

        # 点击聚焦
        textarea.click()
        random_sleep(0.1, 0.2)

        # 清空已有内容，再用 JS 直接写入（更快且像真人粘贴）
        textarea.clear()
        self.page.run_js(
            f"document.querySelector('textarea.gLFyf').value = {json.dumps(keyword)};"
        )

        # 按 Enter 提交
        self.page.actions.key_down(Keys.ENTER).key_up(Keys.ENTER).perform()
        random_sleep(0.8, 1.5)

        logger.info(f"[RuyiPageBrowser] 已提交搜索: {keyword}")

    # ── 滚动 ──────────────────────────────────────────────────

    def human_scroll(self, steps: int = 6):
        """
        分步滚动到页面底部，触发懒加载，模拟真人滚动行为。

        Args:
            steps: 最大滚动次数
        """
        self._require_page()
        for i in range(steps):
            prev_height = self.page.run_js("return document.body.scrollHeight;")
            self.page.run_js("window.scrollTo(0, document.body.scrollHeight);")
            random_sleep(0.5, 1.0)

            new_height = self.page.run_js("return document.body.scrollHeight;")
            if new_height == prev_height:
                logger.info(f"[RuyiPageBrowser] 已到达页面底部 (滚动 {i + 1} 次)")
                break
            else:
                logger.info(f"[RuyiPageBrowser] 页面高度: {prev_height} -> {new_height}")

            # 随机小幅回滚，模拟真人
            if random.random() < 0.3:
                back = random.randint(100, 300)
                self.page.run_js(f"window.scrollBy(0, -{back});")
                random_sleep(0.3, 0.6)

    def human_scroll_to_bottom(self):
        """快速滚动到底部（轻量版）"""
        self._require_page()
        self.page.run_js("window.scrollTo(0, document.body.scrollHeight);")
        random_sleep(0.3, 0.6)

    # ── 响应捕获 ──────────────────────────────────────────────

    def listen_and_collect(self, keyword_item: dict, params) -> list[dict]:
        """
        开启 ruyiPage 数据包监听，执行搜索 + 滚动，收集所有命中的响应体，
        解析后返回聚合数据列表。

        ruyiPage 通过 page.listen.start() / page.listen.fetch() 实现响应监听，
        这是同步的数据包拦截机制，与 Playwright 的 page.on('response') 等价。

        Returns:
            解析后的商品数据列表
        """
        self._require_page()
        keyword = keyword_item["name"]
        keyid   = keyword_item["id"]

        aggregated = ThreadSafeAggregator()   # 仍用聚合器做去重

        # ── 1. 开启监听（监听包含 google.com/search 的响应）─────
        self.page.listen.start("google.com/search")
        logger.info(f"[RuyiPageBrowser] 开始监听响应，关键词: {keyword}")

        try:
            # ── 2. 打开谷歌图片首页 ───────────────────────────────
            logger.info(f"[RuyiPageBrowser] 打开谷歌图片首页")
            self.goto(
                f"https://www.google.com/imghp?hl={params.language_code}&authuser=0&ogbl"
            )
            random_sleep(0.5, 1.0)
            self.handle_cookie_consent(timeout=3.0)

            # ── 3. 验证码检测 ─────────────────────────────────────
            current_url = self.page.url
            if "/sorry/" in current_url or "sorry" in current_url:
                logger.warning(f"[RuyiPageBrowser] 检测到验证页面: {current_url}")
                return None  # 代理/验证码失败信号

            # ── 4. 输入关键词并提交 ───────────────────────────────
            self.human_type_and_submit(keyword_item)

            # 再次检测验证码
            random_sleep(0.8, 1.2)
            current_url = self.page.url
            if "/sorry/" in current_url or "sorry" in current_url:
                logger.warning(f"[RuyiPageBrowser] 搜索后检测到验证页面: {current_url}")
                return None

            # ── 5. 滚动触发懒加载 ─────────────────────────────────
            logger.info(f"[RuyiPageBrowser] 开始滚动页面")
            self.human_scroll_to_bottom()

            # ── 6. 收取所有监听到的数据包 ─────────────────────────
            logger.info(f"[RuyiPageBrowser] 收取数据包...")
            packets = self.page.listen.fetch(count=0, timeout=15)  # count=0 表示取全部

        finally:
            # 无论成功失败都停止监听
            self.page.listen.stop()

        # ── 7. 解析数据包 ─────────────────────────────────────────
        new_datas      = []
        related_search = []
        related_items  = []

        for packet in (packets or []):
            try:
                body = packet.response.body
                if not body:
                    continue

                # demo_with_real_data 是同步函数；若原来是 async 请在此处用 asyncio.run
                result = asyncio.get_event_loop().run_until_complete(
                    demo_with_real_data(body)
                )

                for item in result:
                    if item.get("site", ".jp").endswith(".jp"):
                        continue
                    new_datas.append({
                        "index":     item.get("id"),
                        "word":      item.get("title"),
                        "domain":    item.get("site"),
                        "link":      item.get("url"),
                        "image":     item.get("image"),
                        "info": {
                            "desc":     item.get("desc"),
                            "brand":    item.get("brand"),
                            "price":    item.get("price"),
                            "currency": item.get("currency"),
                            "score":    item.get("score"),
                            "review":   item.get("review"),
                        },
                        "parent":    keyid,
                        "stat":      -1,
                        "createdAt": str(datetime.datetime.now(datetime.timezone.utc)),
                    })

                rs = asyncio.get_event_loop().run_until_complete(get_related_search(body))
                ri = asyncio.get_event_loop().run_until_complete(get_related_items(body))
                related_search.extend(rs or [])
                related_items.extend(ri or [])

            except Exception as e:
                logger.warning(f"[RuyiPageBrowser] 解析数据包失败: {e}")
                continue

        logger.info(f"[RuyiPageBrowser] 解析完成，共 {len(new_datas)} 条数据")
        return {
            "new_datas":      new_datas,
            "domains":        list({d["domain"] for d in new_datas if d.get("domain")}),
            "related_search": list(set(related_search)),
            "related_items":  list(set(related_items)),
        }

    # ── 关闭 ──────────────────────────────────────────────────

    def close(self):
        """安全关闭浏览器"""
        if self.page:
            try:
                self.page.quit()
                logger.info("[RuyiPageBrowser] 浏览器已关闭")
            except Exception as e:
                logger.warning(f"[RuyiPageBrowser] 关闭浏览器时出错: {e}")
            finally:
                self.page = None

    # ── 内部工具 ──────────────────────────────────────────────

    def _require_page(self):
        if not self.page:
            raise RuntimeError("[RuyiPageBrowser] 浏览器未初始化，请先调用 initialize()")


# ──────────────────────────────────────────────────────────────
# 单关键词搜索
# ──────────────────────────────────────────────────────────────

async def search_single_keyword(browser: RuyiPageBrowser, keyword_item: dict, params, max_retries: int = 2):
    """
    搜索单个关键词。

    ruyiPage 本身是同步的，因此浏览器操作在同步块中执行；
    网络 I/O（发送数据到 API）仍走 asyncio。

    Returns:
        True  — 成功
        False — 失败（已重试完）
        None  — 代理 / 验证码失败，需要换代理
    """
    keyword = keyword_item["name"]
    keyid   = keyword_item["id"]

    for attempt in range(max_retries):
        try:
            logger.info(f"[{keyword}] 开始搜索 (尝试 {attempt + 1}/{max_retries})")

            # ── 同步：浏览器操作 + 数据包解析 ────────────────────
            # 在线程池里跑同步代码，不阻塞事件循环
            loop = asyncio.get_event_loop()
            aggregated_data = await loop.run_in_executor(
                None,
                browser.listen_and_collect,
                keyword_item,
                params,
            )

            # None 表示验证码 / 代理失败
            if aggregated_data is None:
                special_logger.info(
                    f"[work-{params.worker_id}][{params.task_id}][{keyword}] "
                    f"{params.proxies['server']} Verification code"
                )
                await params.app.set_fail(params.atm, params.proxies)
                return None

            # ── 异步：数据处理 + API 上报 ─────────────────────────
            if aggregated_data["new_datas"]:
                logger.info(f"[{keyword}] 处理 {len(aggregated_data['new_datas'])} 条数据")

                products         = await deal_info_by_async(aggregated_data["new_datas"], params)
                shopify_products = await deal_shopify_product_info_async(params, products)

                google_item = {
                    "id":           keyid,
                    "use_proxy_ip": params.proxies.get("server"),
                    "from":         params.proxies.get("server", "").replace("socks5://", "").split(":")[0],
                    "word":         keyword,
                    "script":       "",
                    "domains":      json.dumps(aggregated_data["domains"]),
                    "related":      json.dumps(aggregated_data["related_search"]),
                    "items":        json.dumps(aggregated_data["related_items"]),
                    "products":     json.dumps(products),
                }

                async with aiohttp.ClientSession() as session:
                    if products:
                        await send_items_to_api(params, google_item)
                    if shopify_products:
                        await send_shopify_product_to_api(session, params, shopify_products)

                logger.info(f"[{keyword}] 数据上报完成")
            else:
                logger.warning(f"[{keyword}] 没有收集到任何数据")

            special_logger.info(
                f"[work-{params.worker_id}][{params.task_id}][{keyword}] "
                f"{params.proxies['server']} success"
            )
            await params.app.set_success(params.atm, params.proxies)
            return True

        except Exception as e:
            logger.exception(f"[{keyword}] 搜索异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
            else:
                logger.error(f"[{keyword}] 已达最大重试次数，跳过")
                return False

    return False


# ──────────────────────────────────────────────────────────────
# 批量搜索
# ──────────────────────────────────────────────────────────────

async def search_keyword_batch(params):
    """
    批量搜索关键词。
    每次调用从 SQLite 最多取 datanum 条任务，在同一个浏览器里跑完后关闭。
    """
    db: DbManager = params.db
    browser: RuyiPageBrowser | None = None

    try:
        # ── 1. 获取代理 ───────────────────────────────────────────
        while True:
            proxy = await params.app.get_random_proxy()
            if proxy:
                params.proxies = proxy
                break
            logger.info(f"[Worker-{params.worker_id}] 暂无可用代理，等待 30s")
            await asyncio.sleep(30)

        # ── 2. 启动浏览器（同步，在线程池中执行）─────────────────
        browser = RuyiPageBrowser(
            language_code=params.language_code,
            proxies=params.proxies,
            headless=False,
            # firefox_path=r"D:\Firefox\firefox.exe",  # 非默认路径时取消注释
        )

        loop = asyncio.get_event_loop()
        logger.info(f"[Worker-{params.worker_id}] 初始化浏览器，代理: {params.proxies['server']}")

        await asyncio.wait_for(
            loop.run_in_executor(None, browser.initialize),
            timeout=30.0,
        )

        # ── 3. 逐词处理 ───────────────────────────────────────────
        success_count = 0
        fail_count    = 0
        captcha_hit   = False
        processed     = 0

        while processed < params.datanum:

            # 3-a. 取任务
            db_task = await _fetch_task_with_refill(db, params)
            if db_task is None:
                logger.info(f"[Worker-{params.worker_id}] 补词后仍无任务，结束本批")
                break

            keyword_item = {
                "id":   db_task["keyword_id"],
                "name": db_task["keyword"],
            }
            logger.info(f"[Worker-{params.worker_id}] 开始搜索: {keyword_item['name']}")

            # 3-b. 搜索单词
            success = await search_single_keyword(browser, keyword_item, params)
            processed += 1

            if success is True:
                await db.mark_success(db_task["id"])
                success_count += 1
            elif success is None:
                await db.mark_failed(db_task["id"])
                logger.warning(f"[Worker-{params.worker_id}] 验证码或代理失败，结束本批")
                captcha_hit = True
                break
            else:
                await db.mark_failed(db_task["id"])
                fail_count += 1

            # 3-c. 异步触发水线检查
            asyncio.create_task(
                db.auto_refresh_if_needed(),
                name=f"Worker-{params.worker_id}/refill",
            )

        await db.print_stats()
        logger.info(
            f"[Worker-{params.worker_id}] 本批结束 — "
            f"处理: {processed}, 成功: {success_count}, 失败: {fail_count}"
            + (" [验证码/代理中断]" if captcha_hit else "")
        )

    except asyncio.CancelledError:
        logger.info(f"[Worker-{params.worker_id}] search_keyword_batch 被取消")
        raise

    except asyncio.TimeoutError:
        logger.error(f"[Worker-{params.worker_id}] 浏览器初始化超时")
        raise

    except Exception as e:
        logger.exception(f"[Worker-{params.worker_id}] 批量搜索异常: {e}")
        raise

    finally:
        if browser:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, browser.close)


# ──────────────────────────────────────────────────────────────
# 补词逻辑（与原版完全一致）
# ──────────────────────────────────────────────────────────────

async def _fetch_task_with_refill(db: DbManager, params, max_wait_rounds: int = 6) -> dict | None:
    """
    取一条任务。取不到时触发补词并等待，最多等 max_wait_rounds * 10s。
    """
    db_task = await db.fetch_one_task_safe(task_id=params.task_id)
    if db_task:
        return db_task

    logger.info(f"[Worker-{params.worker_id}] SQLite 暂无任务，触发补词...")

    for round_i in range(1, max_wait_rounds + 1):
        if db.fetch_func:
            try:
                await db.fetch_func()
            except Exception as e:
                logger.error(f"[Worker-{params.worker_id}] 补词异常: {e}")

        from main import _current_task_info
        if _current_task_info and _current_task_info.get("id") != params.task_id:
            logger.info(
                f"[Worker-{params.worker_id}] task_id 更新: "
                f"{params.task_id} -> {_current_task_info.get('id')}"
            )
            params.task_id = _current_task_info.get("id")

        db_task = await db.fetch_one_task_safe(task_id=params.task_id)
        if db_task:
            logger.info(f"[Worker-{params.worker_id}] 补词后取到任务（第 {round_i} 轮）")
            return db_task

        logger.info(
            f"[Worker-{params.worker_id}] 补词后仍无任务，等待 10s "
            f"({round_i}/{max_wait_rounds})"
        )
        await asyncio.sleep(10)

    return None
