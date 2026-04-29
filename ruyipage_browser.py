import os
import json
import time
import shutil
import random
from config import Config, logger
from urllib.parse import urlparse

import requests
from ruyipage import launch, Keys


# ──────────────────────────────────────────────────────────────
# 多 Worker 并发配置
# ──────────────────────────────────────────────────────────────

# 每个 worker 占用 BASE_PORT + worker_id 端口
# 例：worker_id=0 → 9300，worker_id=1 → 9301，worker_id=2 → 9302
BASE_PORT = 9300

# 每个 worker 的 user_dir 根目录（子目录按 worker_id 命名）
# 例：C:\ruyipage_workers\worker_0\，worker_1\，…
USER_DIR_ROOT = r"C:\ruyipage_workers"

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


def contains_japanese_kana(text):
    import re
    return bool(re.search(r'[\u3040-\u30ff]', text))


def send_result_batch(atm, items):
    token = atm.get_token()
    url = f"https://seosystem.top/prod/api/v1/shell-domain-filter/domains/query-results"
    headers = {"Authorization": "Bearer " + token}
    data = {"items": items, "token": token}
    resp = requests.post(url, headers=headers, json=data)
    if resp.status_code == 200:
        print(resp.text)



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
        logger.info(f"[Worker-{self.worker_id}] Firefox 启动成功")

    # ── 导航 ──────────────────────────────────────────────────
    def goto(self, url: str, timeout: int = 30):
        self._require_page()
        self.page.get(url, timeout=timeout)
        time.sleep(1)


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
                    random_sleep(0.4, 0.9)
                    btn.click()
                    logger.info(f"[Worker-{self.worker_id}] 已点击 Cookie 同意按钮: {sel}")
                    random_sleep(0.3, 0.7)
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
        random_sleep(0.8, 1.5)

        logger.info(f"[Worker-{self.worker_id}] 已提交搜索: {keyword}")

    # ── 滚动 ──────────────────────────────────────────────────
    def human_scroll(self, steps: int = 6):
        self._require_page()
        for i in range(steps):
            prev_height = self.page.run_js("return document.body.scrollHeight;")
            self.page.run_js("window.scrollTo(0, document.body.scrollHeight);")
            random_sleep(0.5, 1.0)

            new_height = self.page.run_js("return document.body.scrollHeight;")
            if new_height == prev_height:
                logger.info(f"[Worker-{self.worker_id}] 已到达页面底部（滚动 {i + 1} 次）")
                break
            else:
                logger.info(f"[Worker-{self.worker_id}] 页面高度: {prev_height} -> {new_height}")

            if random.random() < 0.3:
                back = random.randint(100, 300)
                self.page.run_js(f"window.scrollBy(0, -{back});")
                random_sleep(0.3, 0.6)

    def slight_random_scroll(self):
        self._require_page()
        distance = random.randint(120, 260)
        self.page.run_js(f"window.scrollBy(0, {distance});")
        random_sleep(0.3, 0.8)

    # ── 搜索主流程 ────────────────────────────────────────────
    def search_and_get_html(self, keyword_item: dict, params, first_run: bool = False) -> str | None:
        self._require_page()
        keyword = "site:" + keyword_item["domain"]

        # 只有第一次才打开 Google 图片首页
        if first_run:
            self.goto(f"https://www.google.com/")
            random_sleep(0.5, 1.0)

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
        random_sleep(0.2, 0.4)

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

        self.slight_random_scroll()
        random_sleep(0.5, 1.0)

        html = self.get_rendered_html()

        return html

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


def go_to_page(browser, page_num):
    page = browser.page
    page.run_js("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(0.5)
    items = page.eles("css:td.NKTSme a.fl")

    for item in items:
        if item.text.strip() == str(page_num):
            item.click_self()
            break

    # 等页面真正刷新
    page.ele("css:#search", timeout=10)



# ---------- 单关键词搜索 ----------
def search_single_keyword(browser: RuyiPageBrowser, keyword_item: dict, params, first_run: bool = False, max_retries=2):
    """
    使用 ruyiPage 同步浏览器搜索单个关键词
    """
    keyword = keyword_item["domain"]

    for attempt in range(max_retries):
        try:
            # 首次打开 Google 图片搜索
            logger.info(
                f"[Worker-{params.worker_id}][{keyword}] "
                f"开始搜索（尝试 {attempt + 1}/{max_retries}）"
            )

            html = browser.search_and_get_html(
                keyword_item,
                params,
                first_run=first_run,
            )
            time.sleep(random.uniform(0.5, 1.0))

            html_content = browser.get_rendered_html()

            if '- did not match any documents.' in html_content:
                keyword_item["included_count"] = 0
                keyword_item["status"] = 0

            # 判断是否日本相关
            is_jp = contains_japanese_kana(html_content)
            if is_jp:
                keyword_item["domain_type"] = 2

            # 尝试获取收录数
            try:
                text = browser.page.ele("css:#result-stats").text
                text = text.replace("About", " ").replace("results", " ").strip()
                keyword_item["included_count"] = int(text.split()[0].replace(",", ""))
                keyword_item["status"] = 2
            except Exception as e:
                print(e)
                keyword_item["included_count"] = 0
                keyword_item["status"] = 0

            if keyword_item["included_count"] < 4:
                keyword_item["status"] = 0
                if keyword_item.get("query_result"):
                    keyword_item["query_result"]["err_msg"] = "谷歌收录小于4"
                else:
                    keyword_item["query_result"] = {"err_msg": "谷歌收录小于4"}

            title = keyword_item.get("domain_title")
            is_jp = contains_japanese_kana(title)

            if not is_jp:
                html_content = browser.get_rendered_html()
                is_jp = contains_japanese_kana(html_content)

            if not is_jp:
                go_to_page(browser, 5)
                html_content = browser.get_rendered_html()
                is_jp = contains_japanese_kana(html_content)

            if not is_jp:
                go_to_page(browser, 10)
                html_content = browser.get_rendered_html()
                is_jp = contains_japanese_kana(html_content)

            if is_jp:
                keyword_item["domain_type"] = 2

            print(keyword_item)

            logger.info(f"[Success] 完成关键词: {keyword}")
            params.app.set_success(params.atm, params.proxies)
            return True

        except Exception as e:
            logger.exception(f"[{keyword}] 搜索异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                return False
        finally:
            # 发送批量结果
            try:
                send_result_batch(params.atm, [keyword_item])
            except Exception as e:
                logger.warning(f"[{keyword}] 发送结果失败: {e}")

    return False


def init_browse(params):
    while True:
        proxy = params.app.get_random_proxy()
        params.proxies = proxy
        if proxy:
            break
        else:
            time.sleep(30)
    browser = RuyiPageBrowser(
        language_code=params.language_code,
        proxies=params.proxies,
        headless=Config.HEADLESS,
        worker_id=params.worker_id,
        # firefox_path=r"D:\Firefox\firefox.exe",  # 非默认路径时取消注释
    )

    # 初始化浏览器，带超时
    logger.info(
        f"[Worker-{params.worker_id}] 初始化浏览器 | "
        f"port={BASE_PORT + params.worker_id} | "
        f"proxy={params.proxies['server']}"
    )
    browser.initialize()
    return browser


# ---------- 批量搜索 ----------
def search_keyword_batch(params):
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
    first_run = True

    try:
        browser = init_browse(params)

        success_count = 0
        fail_count = 0
        tasks = params.tasks.copy()
        while tasks:
            keyword_item_str = tasks.pop(0)
            keyword_item = keyword_item_str
            logger.info(f"开始搜索: {keyword_item['domain']}")
            success = search_single_keyword(browser, keyword_item, params, first_run)
            first_run = False
            if success:
                success_count += 1
            elif success is None:
                logger.warning(f"检测到验证页面，立即关闭浏览器并退出")
                tasks.insert(0, keyword_item_str)  # 用insert(0)而不是append，保持顺序

                # 先关闭旧浏览器
                old_browser = browser
                browser = None  # ⭐ 先置None，防止异常后使用旧引用

                try:
                    old_browser.close()
                    logger.info("旧浏览器已关闭")
                except Exception as e:
                    logger.error(f"关闭浏览器失败（忽略）: {e}")

                # 单独try新建浏览器，失败就让外层异常处理
                browser = init_browse(params)
                logger.info(f"新浏览器初始化完成")
            else:
                fail_count += 1


        logger.info(f"批次完成 - 成功: {success_count}, 失败: {fail_count}")

    except Exception as e:
        logger.exception(f"批量搜索异常: {e}")
        raise
    finally:
        if browser:
            try:
                browser.close()
            except Exception as e:
                logger.error(f"关闭浏览器失败: {e}")

if __name__ == '__main__':
    from platform_api import ProxyPool, TokenManager

    app = ProxyPool()
    atm = TokenManager()
    atm.refresh_token()

    class A:
        language_code = "en-US"
        proxies = None
        atm = atm
        app = app
        worker_id = 1

    params = A()

    browser = init_browse(params)

    search_single_keyword(browser, {"domain": "baidu.com", "domain_title": "baidu"}, params, True)
    search_single_keyword(browser, {"domain": "bing.com", "domain_title": "bing"}, params, False)
    search_single_keyword(browser, {"domain": "google.com", "domain_title": "google"}, params, False)

