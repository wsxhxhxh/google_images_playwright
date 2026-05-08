# browser_ruyipage.py
# -*- coding: utf-8 -*-
"""
谷歌图片搜索脚本 —— ruyiPage 版本
将原 Playwright 异步实现迁移到 ruyiPage (FirefoxPage) 同步实现。

多 Worker 并发方案：
    - 每个 worker 通过 worker_id 获得独立端口（BASE_PORT + worker_id）
    - 每个 worker 拥有独立的 user_dir，彻底隔离 Firefox 实例
    - SOCKS5/HTTP 代理通过向 user_dir 写入 user.js 的方式注入
    - 启动时按 worker_id * STAGGER_SEC 错开，避免 OS 资源竞争
    - 以上措施合并彻底解决 FirefoxPage 单例共享浏览器的问题

依赖安装：
    pip install ruyipage aiohttp aiofiles

使用方式：
    直接运行 main.py，本模块作为浏览器封装层被调用。
"""

import io
import os
import sys
import json
import time
import shutil
import random
import datetime
from urllib.parse import urlparse

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
        pass

sys.path.insert(0, r"C:\Users\XXX\Desktop\mypy\ruyipage")

# ★ 改用 launch()，不再导入 FirefoxPage / FirefoxOptions
from ruyipage import launch, Keys

from config import Config, logger, special_logger
from deal_product_func_async import deal_info, deal_shopify_product_info
from parsel_json_str import demo_with_real_data, get_related_search, get_related_items
from platform_api import send_items_to_api, send_shopify_product_to_api, send_success_task, fetch_tasks_from_api, \
    send_err_task



# ──────────────────────────────────────────────────────────────
# 多 Worker 并发配置
# ──────────────────────────────────────────────────────────────

# 每个 worker 占用 BASE_PORT + worker_id 端口
# 例：worker_id=0 → 9300，worker_id=1 → 9301，worker_id=2 → 9302
BASE_PORT = 9300

# 每个 worker 的 user_dir 根目录（子目录按 worker_id 命名）
# 例：C:\ruyipage_workers\worker_0\，worker_1\，…
USER_DIR_ROOT = r"C:\ruyipage_workers"
TARGET_PREFIX = "https://www.google.com/search?vet="
# 启动错开延迟（秒）：worker_id * STAGGER_SEC
# 防止多个 Firefox 同时启动时抢占 OS 资源导致初始化失败
# 调小可加快启动速度，调大更稳定
STAGGER_SEC = 2.0

# ──────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────

def random_sleep(min_s: float = 0.5, max_s: float = 1.2):
    """随机等待，模拟真人节奏"""
    time.sleep(random.uniform(min_s, max_s))


def _get_worker_user_dir(worker_id: int) -> str:
    """
    返回该 worker 专属的 Firefox profile 目录路径。
    目录结构：USER_DIR_ROOT / worker_{worker_id}
    """
    path = os.path.join(USER_DIR_ROOT, f"worker_{worker_id}")
    os.makedirs(path, exist_ok=True)
    return path


def _write_proxy_to_user_dir(user_dir: str, proxy_server: str) -> None:
    """
    将代理配置写入 user_dir/user.js。
    Firefox 每次启动时都会读取 user.js 并将其中的设置覆盖到 prefs.js，
    这样无需依赖 FirefoxOptions.set_proxy()，launch() 也能使用代理。

    支持格式：
        socks5://host:port
        socks5://user:pass@host:port
        http://host:port
    """
    if not proxy_server:
        return

    p = urlparse(proxy_server)
    scheme = (p.scheme or "").lower()
    host   = p.hostname or ""
    port   = p.port or 1080

    lines = []

    if scheme == "socks5":
        lines = [
            'user_pref("network.proxy.type", 1);',
            'user_pref("permissions.default.image", 2);',
            f'user_pref("network.proxy.socks", "{host}");',
            f'user_pref("network.proxy.socks_port", {port});',
            'user_pref("network.proxy.socks_version", 5);',
            # DNS 查询也走代理，防止 DNS 泄漏
            'user_pref("network.proxy.socks_remote_dns", true);',
        ]
        # SOCKS5 带认证
        if p.username:
            lines += [
                f'user_pref("network.proxy.socks_username", "{p.username}");',
                f'user_pref("network.proxy.socks_password", "{p.password or ""}");',
            ]

    elif scheme == "http":
        lines = [
            'user_pref("network.proxy.type", 1);',
            f'user_pref("network.proxy.http", "{host}");',
            f'user_pref("network.proxy.http_port", {port});',
            f'user_pref("network.proxy.ssl", "{host}");',
            f'user_pref("network.proxy.ssl_port", {port});',
        ]
    else:
        logger.warning(f"[_write_proxy] 不支持的代理协议: {scheme!r}，跳过写入")
        return

    user_js_path = os.path.join(user_dir, "user.js")
    with open(user_js_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    logger.info(f"[_write_proxy] 代理已写入 {user_js_path} → {proxy_server}")


def _clear_proxy_from_user_dir(user_dir: str) -> None:
    """清除 user.js 中的代理配置（直连时调用）"""
    user_js_path = os.path.join(user_dir, "user.js")
    # 写入"无代理"配置
    with open(user_js_path, "w", encoding="utf-8") as f:
        f.write('user_pref("network.proxy.type", 0);\n')


# ──────────────────────────────────────────────────────────────
# 浏览器封装
# ──────────────────────────────────────────────────────────────

class RuyiPageBrowser:
    """
    对 ruyiPage launch() 的封装，提供与原 PlaywrightBrowser 相同的对外接口。

    多 Worker 隔离方案：
        - 每个 worker_id 对应独立端口（BASE_PORT + worker_id）
        - 每个 worker_id 对应独立 user_dir（USER_DIR_ROOT/worker_{worker_id}）
        - 代理通过 user.js 注入，与 launch() 完全兼容
        - 启动时按 worker_id * STAGGER_SEC 错开，避免资源竞争

    ruyiPage 是同步库，因此所有方法均为普通函数（非 async）。
    """

    def __init__(
            self,
            language_code: str = "en-US",
            proxies: dict = None,
            headless: bool = False,
            firefox_path: str = None,
            worker_id: int = 0,
    ):
        self.language_code  = language_code
        self.proxies        = proxies or {}      # {"server": "socks5://host:port"}
        self.headless       = headless
        self.firefox_path   = firefox_path
        self.worker_id      = worker_id
        self.page           = None               # launch() 返回的 FirefoxPage 对象
        self._port          = BASE_PORT + worker_id
        self._user_dir      = _get_worker_user_dir(worker_id)

    # ── 初始化 ────────────────────────────────────────────────
    def initialize(self):
        """
        启动 Firefox，创建独立的 FirefoxPage 实例。

        改动要点（对比原版）：
          1. 不再使用 FirefoxPage(opts)（单例，多 worker 会共享同一浏览器）
          2. 改用 launch(port=..., user_dir=...)，每个 worker 独占独立进程
          3. 代理通过写入 user.js 实现，launch() 不需要传 proxy 参数
          4. worker_id * STAGGER_SEC 延迟错开启动，防止同时初始化冲突
        """
        proxy_server = self.proxies.get("server", "")

        # 步骤1：按 worker_id 错开启动时间
        if self.worker_id > 0:
            stagger = self.worker_id * STAGGER_SEC
            logger.info(
                f"[Worker-{self.worker_id}] 错开延迟 {stagger}s，"
                f"防止多 worker 同时初始化冲突"
            )
            time.sleep(stagger)

        # 步骤2：将代理写入该 worker 专属的 user.js
        if proxy_server:
            _write_proxy_to_user_dir(self._user_dir, proxy_server)
        else:
            _clear_proxy_from_user_dir(self._user_dir)

        # 步骤3：launch() 启动独立 Firefox 进程
        #   - port     : 每个 worker 独占一个端口，互不干扰
        #   - user_dir : 每个 worker 独占一个 profile 目录
        #   - headless : 根据配置决定是否无界面
        #   - browser_path: 可选，指定非默认 Firefox 路径
        logger.info(
            f"[Worker-{self.worker_id}] 启动 Firefox | "
            f"port={self._port} | user_dir={self._user_dir} | "
            f"proxy={proxy_server or '直连'}"
        )
        self.page = launch(
            headless=self.headless,
            port=self._port,
            user_dir=self._user_dir,
            browser_path=self.firefox_path,   # None 时 launch() 自动查找 Firefox
        )
        self.page.run_js("""
        document.documentElement.style.scrollBehavior = 'auto';
        document.body.style.scrollBehavior = 'auto';
        """)
        self.page.listen.start(TARGET_PREFIX)
        logger.info(f"[Worker-{self.worker_id}] Firefox 启动成功")

    # ── 导航 ──────────────────────────────────────────────────
    def goto(self, url: str, timeout: int = 30):
        self._require_page()
        self.page.get(url, timeout=timeout)

    # ── Cookie 弹窗处理 ───────────────────────────────────────
    def handle_cookie_consent(self, timeout: float = 5.0) -> bool:
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
                    random_sleep(0.3, 0.7)
                    btn.click()
                    logger.info(f"[Worker-{self.worker_id}] 已点击 Cookie 同意按钮: {sel}")
                    random_sleep(0.3, 0.6)
                    return True
            except Exception:
                continue
        logger.info(f"[Worker-{self.worker_id}] 未检测到 Cookie 弹窗")
        return False

    # ── 搜索输入 ──────────────────────────────────────────────
    def human_type_and_submit(self, keyword_item: dict, timeout: float = 10.0):
        self._require_page()
        keyword = keyword_item["name"]

        textarea = self.page.ele("css:textarea.gLFyf", timeout=timeout)
        if not textarea:
            raise RuntimeError(f"[Worker-{self.worker_id}] 找不到搜索框，关键词: {keyword}")

        textarea.click()
        random_sleep(0.1, 0.2)

        textarea.clear()
        self.page.run_js(
            f"document.querySelector('textarea.gLFyf').value = {json.dumps(keyword)};"
        )

        self.page.actions.key_down(Keys.ENTER).key_up(Keys.ENTER).perform()
        logger.info(f"[Worker-{self.worker_id}] 已提交搜索: {keyword}")


    # ── 搜索主流程 ────────────────────────────────────────────
    def search_and_get_html(self, keyword_item: dict, params, first_run: bool = False) -> dict | None:
        self._require_page()
        keyword = keyword_item["name"]

        # 只有第一次才打开 Google 图片首页
        if first_run:
            self.goto(
                f"https://www.google.com/imghp?hl={params.language_code}&authuser=0&ogbl"
            )
            random_sleep(0.4, 0.8)

            current_url = self.page.url
            if "/sorry/" in current_url or "sorry" in current_url:
                logger.warning(f"[Worker-{self.worker_id}] 检测到验证页面: {current_url}")
                return None

            self.handle_cookie_consent()

        # 每次都检查是否已经跳验证码
        current_url = self.page.url
        if "/sorry/" in current_url or "sorry" in current_url:
            logger.warning(f"[Worker-{self.worker_id}] 当前已进入验证页面: {current_url}")
            return None

        # 优先使用 name=q，更稳定
        textarea = (
                self.page.ele("css:textarea.gLFyf", timeout=3)
                or self.page.ele("css:input[name='q']", timeout=3)
        )

        if not textarea:
            raise RuntimeError(
                f"[Worker-{self.worker_id}] 找不到搜索框，关键词: {keyword}"
            )

        # 点击输入框
        textarea.click()
        random_sleep(0.1, 0.2)

        # 清空输入框（最稳）
        try:
            textarea.clear()
        except:
            # fallback
            self.page.run_js("""
                let el = document.querySelector('textarea.gLFyf') || document.querySelector('input[name="q"]');
                if (el) el.value = '';
            """)
        random_sleep(0.1, 0.2)
        # 输入关键词（ruyipage原生）
        textarea.input(keyword)
        random_sleep(0.1, 0.2)
        # 回车搜索
        self.page.actions.key_down(Keys.ENTER).key_up(Keys.ENTER).perform()
        random_sleep(0.1, 0.2)

        # 搜索后再检查验证码
        current_url = self.page.url
        if "/sorry/" in current_url or "sorry" in current_url:
            logger.warning(f"[Worker-{self.worker_id}] 搜索后检测到验证页面: {current_url}")
            return None

        html = self.get_rendered_html()

        if not html:
            logger.warning(f"[Worker-{self.worker_id}] 未获取到页面 HTML，关键词: {keyword}")
            return {
                "html": "",
                "new_datas": [],
                "domains": [],
                "related_search": [],
                "related_items": [],
            }

        new_datas = []
        domains = []

        logger.info(f"[Worker-{self.worker_id}] 已获取渲染后 HTML，长度: {len(html)}")
        result = demo_with_real_data(html)

        for item in result:
            if item.get("site", ".jp").endswith(".jp"):
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
                "parent": params.task_id,
                "stat": -1,
                "createdAt": str(datetime.datetime.now(datetime.timezone.utc)),
            }

            new_datas.append(new_data)
            domains.append(item.get("site"))

        related_search = get_related_search(html)
        related_items = get_related_items(html)



        print("[3] 向下滚动，尝试触发更多 /search 请求...")
        for i in range(5):
            self.page.run_js("window.scrollBy(0, 12000)")
            packet = self.page.listen.wait(timeout=1)
            if not packet:
                print(f"   - 第 {i + 1} 次滚动后未捕获新包")
                continue

            print(f"   - 第 {i + 1} 次滚动命中: [{packet.status}] {packet.url}")
            text = packet.text
            if text:
                result = demo_with_real_data(text)
                for item in result:
                    if item.get("site", ".jp").endswith(".jp"):
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
                        "parent": params.task_id,
                        "stat": -1,
                        "createdAt": str(datetime.datetime.now(datetime.timezone.utc)),
                    }

                    new_datas.append(new_data)
                    domains.append(item.get("site"))
                break
            print("     该包无可读文本，继续滚动...")

        return {
            "html": html,
            "new_datas": new_datas,
            "domains": domains,
            "related_search": related_search,
            "related_items": related_items,
        }

    def get_rendered_html(self) -> str:
        """获取当前页面渲染完成后的 HTML"""
        self._require_page()
        try:
            html = self.page.html
            if callable(html):
                html = html()
            return html or ""
        except Exception:
            try:
                return self.page.run_js("return document.documentElement.outerHTML;") or ""
            except Exception:
                return ""

    # ── 关闭 ──────────────────────────────────────────────────
    def close(self):
        """安全关闭浏览器（不删除 user_dir，下次启动可复用 profile）"""
        if self.page:
            try:
                self.page.quit()
                logger.info(f"[Worker-{self.worker_id}] 浏览器已关闭")
            except Exception as e:
                logger.warning(f"[Worker-{self.worker_id}] 关闭浏览器时出错: {e}")
            finally:
                self.page = None

    def close_and_clean(self):
        """
        关闭浏览器并彻底删除该 worker 的 user_dir。
        适合需要完全重置 profile 的场景（如验证码频繁触发）。
        """
        self.close()
        try:
            shutil.rmtree(self._user_dir, ignore_errors=True)
            logger.info(f"[Worker-{self.worker_id}] user_dir 已清理: {self._user_dir}")
        except Exception as e:
            logger.warning(f"[Worker-{self.worker_id}] 清理 user_dir 失败: {e}")

    # ── 内部工具 ──────────────────────────────────────────────
    def _require_page(self):
        if not self.page:
            raise RuntimeError(
                f"[Worker-{self.worker_id}] 浏览器未初始化，请先调用 initialize()"
            )


# ──────────────────────────────────────────────────────────────
# 单关键词搜索
# ──────────────────────────────────────────────────────────────
def search_single_keyword(
    browser: RuyiPageBrowser,
    keyword_item: dict,
    params,
    first_run: bool = False,
    max_retries: int = 2,
):
    keyword = keyword_item["name"]
    keyid = keyword_item["id"]
    proxy_server = (params.proxies or {}).get("server", "")

    for attempt in range(max_retries):
        try:
            logger.info(
                f"[Worker-{params.worker_id}][{keyword}] "
                f"开始搜索（尝试 {attempt + 1}/{max_retries}）"
            )

            aggregated_data = browser.search_and_get_html(
                keyword_item,
                params,
                first_run=first_run,
            )

            if aggregated_data is None:
                params.app.set_fail(params.atm, params.proxies)
                special_logger.info(
                    f"[work-{params.worker_id}][{params.task_id}][{keyword}] "
                    f"{params.proxies['server']} Verification code"
                )
                return None

            if aggregated_data["new_datas"]:
                logger.info(
                    f"[Worker-{params.worker_id}][{keyword}] "
                    f"处理 {len(aggregated_data['new_datas'])} 条数据"
                )

                products = deal_info(aggregated_data["new_datas"], params)
                shopify_products = deal_shopify_product_info(params, products)

                google_item = {
                    "id": keyid,
                    "use_proxy_ip": proxy_server,
                    "from": proxy_server.replace("socks5://", "").split(":")[0],
                    "word": keyword,
                    "script": "",
                    "domains": json.dumps(aggregated_data["domains"]),
                    "related": json.dumps(aggregated_data["related_search"]),
                    "items": json.dumps(aggregated_data["related_items"]),
                    "products": json.dumps(products),
                }

                if products:
                    send_items_to_api(params, google_item)
                    special_logger.info(f"[work-{params.worker_id}][{params.task_id}][{keyword}] {params.proxies['server']} success")
                else:
                    send_success_task(params, [keyword_item])
                if shopify_products:
                    send_shopify_product_to_api(params, shopify_products)

            params.app.set_success(params.atm, params.proxies)
            return True

        except Exception as e:
            logger.exception(f"搜索异常: {e}")

            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                return False

    return False

# ──────────────────────────────────────────────────────────────
# 批量搜索
# ──────────────────────────────────────────────────────────────

def search_keyword_batch(params):
    """
    批量搜索关键词。

    每次调用从 SQLite 最多取 datanum 条任务，在同一个浏览器里跑完后关闭。
    每个 worker_id 对应独立的 Firefox 进程（独立端口 + 独立 user_dir），
    多个 worker 可以安全地并发调用本函数。
    """

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
        headless=Config.HEADLESS,
        worker_id=params.worker_id,
        # firefox_path=r"D:\Firefox\firefox.exe",  # 非默认路径时取消注释
    )

    try:
        logger.info(
            f"[Worker-{params.worker_id}] 初始化浏览器 | "
            f"port={BASE_PORT + params.worker_id} | "
            f"proxy={params.proxies['server']}"
        )
        browser.initialize()

        success_count = 0
        fail_count    = 0
        captcha_hit   = False
        processed     = 0
        tasks = fetch_tasks_from_api(params)
        err_tasks = []
        first_run = True
        while tasks:
            keyword_item_str = tasks.pop(0)

            if type(keyword_item_str) == str:
                keyword_item = json.loads(keyword_item_str)
            else:
                keyword_item = keyword_item_str

            logger.info(f"[Worker-{params.worker_id}] 开始搜索: {keyword_item['name']}")

            success = search_single_keyword(
                browser,
                keyword_item,
                params,
                first_run=first_run,
            )

            # 第一次执行完后，以后都复用页面
            first_run = False

            processed += 1
            if success is True:
                success_count += 1
            elif success is None:
                err_tasks.append(keyword_item)
                logger.warning(
                    f"[Worker-{params.worker_id}] 验证码或代理失败，立即关闭浏览器"
                )
                captcha_hit = True
                try:
                    browser.close()
                except Exception:
                    pass
                break

            else:
                err_tasks.append(keyword_item)
                fail_count += 1
        if err_tasks:
            send_err_task(params, err_tasks)
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
            # 正常关闭保留 user_dir（下次启动可复用 Cookie/缓存）
            # 若要彻底重置 profile 请改为 browser.close_and_clean()
            browser.close()
        except Exception as e:
            logger.warning(f"[Worker-{params.worker_id}] 关闭浏览器失败: {e}")

