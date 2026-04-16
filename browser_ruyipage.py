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
import gzip

# Windows 控制台 UTF-8 兼容
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
            )
        if hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer,
                encoding="utf-8",
                errors="replace",
                line_buffering=True,
            )
    except Exception:
        # 某些终端/重定向场景不支持重包裹，忽略并使用默认流
        pass

sys.path.insert(0, r"C:\Users\XXX\Desktop\mypy\ruyipage")

from ruyipage import FirefoxPage, FirefoxOptions, Keys

from config import Config, logger, special_logger
from deal_product_func_async import deal_info, deal_shopify_product_info
from parsel_json_str import demo_with_real_data, get_related_search, get_related_items
from platform_api import send_items_to_api, send_shopify_product_to_api
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
        self._require_page()
        keyword = keyword_item["name"]
        keyid = keyword_item["id"]

        # ── 1. 同时开启 DataCollector 和 listen ──────────────────
        collector = self.page.network.add_data_collector(
            ["responseCompleted"], data_types=["response"]
        )
        self.page.listen.start("google.com/search")
        logger.info(f"[RuyiPageBrowser] 开始监听响应，关键词: {keyword}")

        packets = []
        try:
            self.goto(f"https://www.google.com/imghp?hl={params.language_code}&authuser=0&ogbl")
            random_sleep(0.5, 1.0)

            current_url = self.page.url
            if "/sorry/" in current_url or "sorry" in current_url:
                logger.warning(f"[RuyiPageBrowser] 检测到验证页面: {current_url}")
                return None

            self.human_type_and_submit(keyword_item)
            random_sleep(0.8, 1.2)

            current_url = self.page.url
            if "/sorry/" in current_url or "sorry" in current_url:
                logger.warning(f"[RuyiPageBrowser] 搜索后检测到验证页面: {current_url}")
                return None

            logger.info(f"[RuyiPageBrowser] 等待初始数据包...")
            self._collect_packets(packets, timeout=15)

            logger.info(f"[RuyiPageBrowser] 开始滚动页面")
            self.human_scroll_to_bottom()
            self._collect_packets(packets, timeout=10)

            logger.info(f"[RuyiPageBrowser] 共收到 {len(packets)} 个数据包")

        finally:
            self.page.listen.stop()

        # ── 2. 用 collector 取响应体 ─────────────────────────────
        new_datas = []
        related_search = []
        related_items = []

        for packet in packets:
            try:
                # request_id 在 packet.request["request"] 里
                request_id = packet.request.get("request")
                if not request_id:
                    logger.debug("[RuyiPageBrowser] packet 无 request_id，跳过")
                    continue

                data = collector.get(request_id, data_type="response")
                if not data or not data.has_data:
                    logger.debug(f"[RuyiPageBrowser] collector 无数据, url={packet.url[:80]}")
                    continue

                body_bytes = data.bytes  # bytes 类型
                collector.disown(request_id)  # 释放浏览器内存

                body_text = self._decompress_and_decode(body_bytes, packet)
                if not body_text:
                    continue

                result = demo_with_real_data(body_text)
                rs = get_related_search(body_text)
                ri = get_related_items(body_text)

                for item in result:
                    if item.get("site", ".jp").endswith(".jp"):
                        continue
                    new_datas.append({
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
                        "parent": keyid,
                        "stat": -1,
                        "createdAt": str(datetime.datetime.now(datetime.timezone.utc)),
                    })

                related_search.extend(rs or [])
                related_items.extend(ri or [])

            except Exception as e:
                logger.warning(f"[RuyiPageBrowser] 解析数据包失败: {e}")
                continue

        finally_cleanup = True
        try:
            collector.remove()
        except Exception as e:
            logger.warning(f"[RuyiPageBrowser] collector.remove() 失败: {e}")

        if packets and not new_datas:
            logger.warning(
                f"[RuyiPageBrowser] 收到响应包但未解析出商品数据，包数={len(packets)}"
            )

        logger.info(f"[RuyiPageBrowser] 解析完成，共 {len(new_datas)} 条数据")
        return {
            "new_datas": new_datas,
            "domains": list({d["domain"] for d in new_datas if d.get("domain")}),
            "related_search": list(set(related_search)),
            "related_items": list(set(related_items)),
        }

    def _decompress_and_decode(self, body_bytes: bytes, packet=None) -> str:
        """根据响应头 Content-Encoding 解压并解码响应体"""
        if not body_bytes:
            return ""

        # 从 packet.headers 判断编码方式
        encoding = ""
        if packet:
            headers = getattr(packet, "headers", {}) or {}
            encoding = headers.get("content-encoding", "").lower()

        # 按编码方式解压
        try:
            if encoding == "br":
                import brotli
                body_bytes = brotli.decompress(body_bytes)
            elif encoding in ("gzip", "x-gzip"):
                import gzip
                body_bytes = gzip.decompress(body_bytes)
            elif encoding == "zstd":
                import zstandard as zstd
                body_bytes = zstd.ZstdDecompressor().decompress(body_bytes)
            elif encoding == "deflate":
                import zlib
                try:
                    body_bytes = zlib.decompress(body_bytes)
                except zlib.error:
                    body_bytes = zlib.decompress(body_bytes, -zlib.MAX_WBITS)
            else:
                # 没有明确编码，逐个尝试
                for decompress_fn in [
                    lambda b: __import__('brotli').decompress(b),
                    lambda b: __import__('gzip').decompress(b),
                    lambda b: __import__('zlib').decompress(b),
                ]:
                    try:
                        body_bytes = decompress_fn(body_bytes)
                        break
                    except Exception:
                        continue

        except Exception as e:
            logger.debug(f"[RuyiPageBrowser] 解压失败({encoding}): {e}，尝试直接解码")

        # 解码为字符串
        try:
            return body_bytes.decode("utf-8")
        except Exception:
            return body_bytes.decode("utf-8", errors="ignore")

    def _normalize_response_body(self, body) -> str:
        """
        把响应体统一转成 str，兼容 ruyiPage 返回 bytes 的场景。
        """
        if isinstance(body, str):
            return body

        if isinstance(body, bytes):
            # 先尝试直接按 utf-8 解码（大多数 Google 响应可行）
            try:
                return body.decode("utf-8")
            except UnicodeDecodeError:
                pass

            # 某些响应可能是 gzip 压缩
            try:
                return gzip.decompress(body).decode("utf-8", errors="ignore")
            except Exception:
                pass

            # 最后兜底：宽松解码，尽量保留可解析内容
            return body.decode("utf-8", errors="ignore")

        return str(body)

    def _extract_packet_body(self, packet):
        """
        从 ruyiPage 数据包中提取 body，兼容对象/字典两种结构。
        """

        body = getattr(packet, "body", None)
        if body:
            return body

        response = getattr(packet, "response", None)
        if isinstance(response, dict):
            content = response.get("content")
            if content:
                return content

        # 兜底：response["content"]
        response = getattr(packet, "response", None)
        if isinstance(response, dict):
            return response.get("content") or response.get("body")

        return None

    def _extract_packet_url(self, packet) -> str:
        """
        从 ruyiPage 数据包中提取 url，兼容对象/字典两种结构。
        """
        url = getattr(packet, "url", None)
        if url:
            return str(url)

        # 兜底：response dict
        response = getattr(packet, "response", None)
        if isinstance(response, dict):
            return str(response.get("url", "") or "")

        if isinstance(packet, dict):
            return str(packet.get("url", "") or "")

        return ""

    def _is_target_search_url(self, url: str) -> bool:
        """
        只保留 Google 图片搜索结果相关请求，过滤静态资源和埋点请求。
        """
        if not url or "google.com/search" not in url:
            return False
        return any(token in url for token in ("udm=2", "tbm=isch", "async=", "asearch=arc"))

    def _collect_packets(self, packets: list, timeout: float = 15):
        """
        循环调用 page.listen.wait() 收取所有当前可用的数据包，直到超时为止。

        ruyiPage 的 wait() 签名：
            wait(count=1, timeout=秒) -> DataPacket | None
        每次返回一个包或 None（超时/无包）。
        """
        first_debug = True
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                # 每次等待最多 3s，拿到一个包就存起来继续循环
                packet = self.page.listen.wait(count=1, timeout=min(3.0, remaining))
                if packet is None:
                    # 连续无包，说明当前没有新请求了，提前退出
                    break
                if first_debug:
                    self._debug_packet_structure(packet)
                    first_debug = False

                url = self._extract_packet_url(packet)
                if self._is_target_search_url(url):
                    packets.append(packet)
            except Exception as e:
                logger.debug(f"[RuyiPageBrowser] wait() 异常: {e}")
                break

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

    def _debug_packet_structure(self, packet):
        """临时调试：打印 packet 的完整结构"""
        import inspect
        logger.info(f"=== PACKET TYPE: {type(packet)} ===")
        logger.info(f"PACKET.url = {getattr(packet, 'url', '<<NOT FOUND>>')}")
        logger.info(f"PACKET.body = {repr(getattr(packet, 'body', '<<NOT FOUND>>'))[:200]}")

        response = getattr(packet, 'response', None)
        if isinstance(response, dict):
            content = response.get('content')
            logger.info(f"RESPONSE content type: {type(content)}, value: {repr(content)[:300]}")

        # 如果是对象，打印所有属性
        if not isinstance(packet, dict):
            attrs = [a for a in dir(packet) if not a.startswith('__')]
            logger.info(f"PACKET ATTRS: {attrs}")

            response = getattr(packet, 'response', None)
            if response:
                logger.info(f"RESPONSE TYPE: {type(response)}")
                if not isinstance(response, dict):
                    r_attrs = [a for a in dir(response) if not a.startswith('__')]
                    logger.info(f"RESPONSE ATTRS: {r_attrs}")
                    # 尝试常见属性
                    for k in ('body', 'text', 'content', 'raw', 'url', 'status'):
                        val = getattr(response, k, '<<NOT FOUND>>')
                        logger.info(f"  response.{k} = {repr(val)[:200]}")
                else:
                    logger.info(f"RESPONSE DICT KEYS: {list(response.keys())}")
        else:
            logger.info(f"PACKET DICT KEYS: {list(packet.keys())}")


# ──────────────────────────────────────────────────────────────
# 单关键词搜索
# ──────────────────────────────────────────────────────────────

def search_single_keyword(browser: RuyiPageBrowser, keyword_item: dict, params, max_retries: int = 2):
    """
    搜索单个关键词。

    Returns:
        True  — 成功
        False — 失败（已重试完）
        None  — 代理 / 验证码失败，需要换代理
    """
    keyword = keyword_item["name"]
    keyid   = keyword_item["id"]
    proxy_server = (params.proxies or {}).get("server", "")

    for attempt in range(max_retries):
        try:
            logger.info(f"[{keyword}] 开始搜索 (尝试 {attempt + 1}/{max_retries})")

            aggregated_data = browser.listen_and_collect(keyword_item, params)

            # None 表示验证码 / 代理失败
            if aggregated_data is None:
                special_logger.info(
                    f"[work-{params.worker_id}][{params.task_id}][{keyword}] "
                    f"{params.proxies['server']} Verification code"
                )
                params.app.set_fail(params.atm, params.proxies)
                return None

            # ── 异步：数据处理 + API 上报 ─────────────────────────
            if aggregated_data["new_datas"]:
                logger.info(f"[{keyword}] 处理 {len(aggregated_data['new_datas'])} 条数据")

                products = deal_info(aggregated_data["new_datas"], params)
                shopify_products = deal_shopify_product_info(params, products)

                google_item = {
                    "id":           keyid,
                    "use_proxy_ip": proxy_server,
                    "from":         proxy_server.replace("socks5://", "").split(":")[0],
                    "word":         keyword,
                    "script":       "",
                    "domains":      json.dumps(aggregated_data["domains"]),
                    "related":      json.dumps(aggregated_data["related_search"]),
                    "items":        json.dumps(aggregated_data["related_items"]),
                    "products":     json.dumps(products),
                }

                if products:
                    send_items_to_api(params, google_item)
                if shopify_products:
                    send_shopify_product_to_api(params, shopify_products)

                logger.info(f"[{keyword}] 数据上报完成")
            else:
                logger.warning(f"[{keyword}] 没有收集到任何数据")

            special_logger.info(
                f"[work-{params.worker_id}][{params.task_id}][{keyword}] "
                f"{params.proxies['server']} success"
            )
            params.app.set_success(params.atm, params.proxies)
            return True

        except Exception as e:
            logger.exception(f"[{keyword}] 搜索异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            error_msg = str(e)
            if any(err in error_msg for err in [
                "ERR_PROXY_CONNECTION_FAILED",
                "ERR_TUNNEL_CONNECTION_FAILED",
                "ERR_SOCKS_CONNECTION_FAILED",
                "ERR_CONNECTION_REFUSED",
                "ERR_CONNECTION_TIMED_OUT",
                "net::ERR_",
                "ProxyError",
                "proxy",
            ]):
                special_logger.info(
                    f"[work-{params.worker_id}][{params.task_id}][{keyword}] "
                    f"{proxy_server or 'unknown_proxy'} ERR_PROXY_OR_NETWORK"
                )
                params.app.set_fail(params.atm, params.proxies)
                return None

            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                logger.error(f"[{keyword}] 已达最大重试次数，跳过")
                return False

    return False


# ──────────────────────────────────────────────────────────────
# 批量搜索
# ──────────────────────────────────────────────────────────────

def search_keyword_batch(params):
    """
    批量搜索关键词。
    每次调用从 SQLite 最多取 datanum 条任务，在同一个浏览器里跑完后关闭。

    ruyiPage 的 Firefox 驱动是线程绑定的（内部用 ThreadLocal 管理），
    当前同步版本要求整个 worker 线程独占浏览器对象，因此浏览器操作
    全部直接在当前线程中执行。
    """
    db: DbManager = params.db

    while True:
        proxy = params.app.get_random_proxy()
        if proxy:
            params.proxies = proxy
            break
        logger.info(f"[Worker-{params.worker_id}] 暂无可用代理，等待 30s")
        time.sleep(30)

    browser = RuyiPageBrowser(
        language_code=params.language_code,
        proxies=params.proxies,
        headless=False,
        # firefox_path=r"D:\Firefox\firefox.exe",  # 非默认路径时取消注释
    )

    try:
        logger.info(f"[Worker-{params.worker_id}] 初始化浏览器，代理: {params.proxies['server']}")
        browser.initialize()

        # ── 4. 逐词处理 ───────────────────────────────────────────
        success_count = 0
        fail_count    = 0
        captcha_hit   = False
        processed     = 0

        # while processed < params.datanum:
        while processed < 1: # todo

            db_task = _fetch_task_with_refill(db, params)
            if db_task is None:
                logger.info(f"[Worker-{params.worker_id}] 补词后仍无任务，结束本批")
                break

            keyword_item = {
                "id":   db_task["keyword_id"],
                "name": db_task["keyword"],
            }
            logger.info(f"[Worker-{params.worker_id}] 开始搜索: {keyword_item['name']}")

            success = search_single_keyword(browser, keyword_item, params)
            processed += 1

            if success is True:
                db.mark_success(db_task["id"])
                success_count += 1
            elif success is None:
                db.mark_failed(db_task["id"])
                logger.warning(f"[Worker-{params.worker_id}] 验证码或代理失败，结束本批")
                captcha_hit = True
                break
            else:
                db.mark_failed(db_task["id"])
                fail_count += 1

            db.auto_refresh_if_needed()

        db.print_stats()
        logger.info(
            f"[Worker-{params.worker_id}] 本批结束 — "
            f"处理: {processed}, 成功: {success_count}, 失败: {fail_count}"
            + (" [验证码/代理中断]" if captcha_hit else "")
        )

    except Exception as e:
        logger.exception(f"[Worker-{params.worker_id}] 批量搜索异常: {e}")
        raise

    finally:
        try:
            browser.close()
        except Exception as e:
            logger.warning(f"[Worker-{params.worker_id}] 关闭浏览器失败: {e}")


# ──────────────────────────────────────────────────────────────
# 补词逻辑（与原版完全一致）
# ──────────────────────────────────────────────────────────────

def _fetch_task_with_refill(db: DbManager, params, max_wait_rounds: int = 6) -> dict | None:
    """
    取一条任务。取不到时触发补词并等待，最多等 max_wait_rounds * 10s。
    """
    db_task = db.fetch_one_task_safe(task_id=params.task_id)
    if db_task:
        return db_task

    logger.info(f"[Worker-{params.worker_id}] SQLite 暂无任务，触发补词...")

    for round_i in range(1, max_wait_rounds + 1):
        if db.fetch_func:
            try:
                db.fetch_func()
            except Exception as e:
                logger.error(f"[Worker-{params.worker_id}] 补词异常: {e}")

        from main import get_current_task_info_snapshot
        current_task_info = get_current_task_info_snapshot()
        if current_task_info and current_task_info.get("id") != params.task_id:
            logger.info(
                f"[Worker-{params.worker_id}] task_id 更新: "
                f"{params.task_id} -> {current_task_info.get('id')}"
            )
            params.task_id = current_task_info.get("id")

        db_task = db.fetch_one_task_safe(task_id=params.task_id)
        if db_task:
            logger.info(f"[Worker-{params.worker_id}] 补词后取到任务（第 {round_i} 轮）")
            return db_task

        logger.info(
            f"[Worker-{params.worker_id}] 补词后仍无任务，等待 10s "
            f"({round_i}/{max_wait_rounds})"
        )
        time.sleep(10)

    return None