# platform_api.py
import json
import ssl
import time
import threading
import traceback
from typing import Dict, Optional
from urllib import error

import requests

from config import logger, Config, data_logger


_SSL_CONTEXT = ssl._create_unverified_context()


_TLS_VERIFY = False  # 等效原代码 ssl=False
_TLS_TIMEOUT = 10


_thread_local = threading.local()


def _get_session() -> requests.Session:
    sess = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        _thread_local.session = sess
    return sess


def _request_text(
    method: str,
    url: str,
    *,
    headers=None,
    data=None,
    json_data=None,
    timeout: int = _TLS_TIMEOUT,
) -> str:
    """
    requests 统一封装：对 data 使用表单编码，对 json_data 使用 json=。
    统一加 headers，解决部分接口对 User-Agent/Content-Type 敏感导致 403 的问题。
    """
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    if headers:
        req_headers.update(headers)

    sess = _get_session()
    try:
        if json_data is not None:
            resp = sess.request(
                method=method.upper(),
                url=url,
                headers=req_headers,
                json=json_data,
                timeout=timeout,
                verify=_TLS_VERIFY,
            )
        else:
            # data 走表单
            resp = sess.request(
                method=method.upper(),
                url=url,
                headers=req_headers,
                data=data,
                timeout=timeout,
                verify=_TLS_VERIFY,
            )
        text = resp.text if resp.text is not None else ""
        if resp.status_code >= 400:
            snippet = text[:200].replace("\n", " ").replace("\r", " ")
            logger.warning(f"[HTTP] {method.upper()} {url} -> {resp.status_code} resp_snippet={snippet!r}")
            raise RuntimeError(f"HTTP {resp.status_code} Forbidden/Request failed for {url}: {snippet}")
        return text
    except requests.RequestException as exc:
        # 兼容上层原逻辑：抛出异常交给调用方处理
        raise


class TokenManager:
    def __init__(self, token_expire_seconds: int = 3600 * 36):
        self._token: Optional[str] = None
        self._expire_time: float = 0
        self._lock = threading.Lock()
        self._token_expire_seconds = token_expire_seconds
        self.apikey = "5a11020697da4aceba7e011fc0370185"
        self._url = "https://seosystem.top/prod/api/v1/token"

    def _fetch_new_token(self) -> str:
        text = _request_text("POST", self._url, data={"apikey": self.apikey}, timeout=10)
        resp_json = json.loads(text)
        return resp_json["data"]["token"]

    def get_token(self) -> str:
        with self._lock:
            now = time.time()
            if not self._token or now >= self._expire_time:
                self._token = self._fetch_new_token()
                self._expire_time = now + self._token_expire_seconds
            return self._token

    def refresh_token(self) -> str:
        with self._lock:
            self._token = self._fetch_new_token()
            self._expire_time = time.time() + self._token_expire_seconds
            return self._token


class ProxyPool:
    def __init__(self):
        self.pool = []
        self.lock = threading.Lock()

    def safe_request(self, method, url, **kwargs):
        try:
            return _request_text(method, url, **kwargs)
        except requests.RequestException as exc:
            logger.warning(f"request error: {exc}, retrying...")
            return _request_text(method, url, **kwargs)

    def refresh_pool(self):
        if not Config.PROXY_URL:
            self.pool = []
            return
        text = self.safe_request("GET", Config.PROXY_URL, timeout=10)
        resp_json = json.loads(text)
        logger.info(f"refresh local proxy pool, num: {len(resp_json)}")
        self.pool = resp_json

    def get_random_proxy(self):
        with self.lock:
            if not self.pool:
                self.refresh_pool()
            if not self.pool:
                return None
            proxy = self.pool.pop()

        proxy["server"] = f"socks5://{proxy['ip']}:{proxy['port']}"
        return proxy

    def set_proxy_status(self, atm, proxy, status):
        url = Config.PROXY_STATUS.format(id=proxy["id"])
        token = atm.get_token()
        data = {"status": status, "token": token}
        headers = {"Authorization": f"Bearer {token}"}
        text = self.safe_request("POST", url, data=data, headers=headers, timeout=10)
        logger.info(text)

    def set_success(self, atm, proxy: Dict):
        logger.info(f"send proxy success: {proxy['server']}")
        self.set_proxy_status(atm, proxy, 1)

    def set_fail(self, atm, proxy: Dict) -> None:
        logger.info(f"send proxy failed: {proxy['server']}")
        self.set_proxy_status(atm, proxy, 2)


def get_task_info(atm, session=None):
    """获取任务信息。"""
    token = atm.get_token()
    url = f"https://seosystem.top/prod/api/v1/tasks?platform_id=1&token={token}"
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(10):
        try:
            text = _request_text("GET", url, headers=headers, timeout=10)
            data = json.loads(text)
            res = data["data"]
            if res and isinstance(res, list):
                return res[0]
            return res
        except Exception as exc:
            logger.error(f"获取任务信息失败 (尝试 {attempt + 1}): {exc}")
            time.sleep(3)
    raise Exception("获取任务信息失败，已重试10次")


def fetch_tasks_from_api(params):
    """从 API 获取关键词列表。"""
    try:
        api_url = (
            f"{params.agent_url}?action=getwordsV1&d={params.dbname}&db_user={params.dbuser}"
            f"&db_pass={params.dbpasswd}&secret_key={params.agent_key}&datanum={params.datanum}"
        )
        logger.info(f"获取关键词: {api_url}")
        text = _request_text("GET", api_url, timeout=10)
        task_data = json.loads(text)
        return task_data.get("data", [])
    except Exception as exc:
        logger.error(f"获取关键词失败: {exc}")
        return []


def send_shopify_product_to_api(*args):
    """发送 Shopify 产品数据到 API。"""
    if len(args) == 2:
        params, item = args
    elif len(args) == 3:
        _, params, item = args
    else:
        raise TypeError("send_shopify_product_to_api expects (params, item) or (session, params, item)")

    start_time = time.time()
    api_url = "https://downloadtemp.flsxxsmode.top/2026_api_importshopifydomain.php"
    try:
        text = _request_text("POST", api_url, json_data=item, timeout=10)
        logger.info(f"send items result: {text}")
    except Exception as exc:
        traceback_details = traceback.format_exc()
        logger.error(f"send_shopify_product_to_api Exception occurred:\n{traceback_details}")
        raise Exception(f"Exception send items shopify product to API Failed: {exc}")

    logger.info(f"send items shopify product to API use {time.time() - start_time:.2f} seconds")


def send_items_to_api(*args):
    """发送产品数据到 API。"""
    if len(args) == 2:
        params, item = args
    elif len(args) == 3:
        _, params, item = args
    else:
        raise TypeError("send_items_to_api expects (params, item) or (session, params, item)")

    start_time = time.time()
    try:
        items_backup = [item]
        data_to_send = {"param": [dict(entry) for entry in items_backup]}
        data_logger.info(f"[{params.worker_id}] {data_to_send}")
        url = (
            f"{params.agent_url}?action=setwordsV1&d={params.dbname}&db_user={params.dbuser}"
            f"&db_pass={params.dbpasswd}&secret_key={params.agent_key}"
        )
        text = _request_text("POST", url, json_data=data_to_send, timeout=10)
        payload = json.loads(text)
        if payload.get("stat") == 1:
            logger.info(f"send items result: {text}")
        else:
            raise Exception(f"stat not 1: {text}")
    except Exception as exc:
        traceback_details = traceback.format_exc()
        logger.error(f"send_items_to_api Exception occurred:\n{traceback_details}")
        raise Exception(f"Exception send items {params.dbname} to API Failed: {exc}")

    logger.info(f"send items {params.dbname} to API use {time.time() - start_time:.2f} seconds")



def send_keyword_status(params, tasks, status):
    if not tasks:
        logger.info(f"[Work-{params.worker_id}] 没有错误任务需要发送")
        return True

    ids = []
    for task in tasks:
        t = json.loads(task)
        ids.append(t["id"])

    data = {"keyword_ids": ids, "status": status}
    headers = {
        "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Host": params.binddomain,
        "Connection": "keep-alive",
    }
    url = (
        f"{params.agent_url}?action=update_keyword_status&d={params.dbname}&db_user={params.dbuser}"
        f"&db_pass={params.dbpasswd}&secret_key={params.agent_key}"
    )
    try:
        text = _request_text("POST", url, headers=headers, json_data=data, timeout=5)
        logger.info(f"send tasks result: {text}")
        return True
    except Exception as exc:
        logger.exception(f"[Work-{params.worker_id}] 发送错误任务异常: {exc}")
        return False

def send_err_task(params, tasks):
    send_keyword_status(params, tasks, 0)

def send_success_task(params, tasks):
    send_keyword_status(params, tasks, 3)

def update_task_status(atm, session, task_id):
    token = atm.get_token()
    url = f"https://seosystem.top/prod/api/v1/tasks/{task_id}/status"
    headers = {"Authorization": f"Bearer {token}"}
    data = {"status": 2, "token": token}
    text = _request_text("POST", url, headers=headers, data=data, timeout=10)
    logger.info(f"update tasks result: {text}")


# 兼容旧名称
AsyncTokenManager = TokenManager
AsyncProxyPool = ProxyPool

