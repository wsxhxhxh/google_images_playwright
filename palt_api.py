import json
import random
import time
import asyncio
import traceback
import ssl
import requests
import threading
from typing import Optional, Dict
import aiohttp
from config import logger, Config

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
        """异步获取新token - 每次创建新 session"""

        # ✅ 创建临时 session，用完自动关闭

        data = {"apikey": self.apikey}
        text = _request_text("POST", self._url, data=data, timeout=10)
        resp_json = json.loads(text)
        token = resp_json["data"]["token"]
        return token

    def get_token(self) -> str:
        """获取 token，如果过期则刷新"""
        with self._lock:
            now = time.time()
            if not self._token or now >= self._expire_time:
                self._token = self._fetch_new_token()
                self._expire_time = now + self._token_expire_seconds
            return self._token

    def refresh_token(self) -> str:
        """主动刷新 token"""
        with self._lock:
            self._token = self._fetch_new_token()
            self._expire_time = time.time() + self._token_expire_seconds
            return self._token


def get_task_info(atm):
    """获取任务信息"""
    token = atm.get_token()
    up = "page=1&page_size=50&status=1&task_status=2&token="
    url = "https://seosystem.top/prod/api/v1/shell-domain-filter/tasks?" + up + token
    headers = {"Authorization": "Bearer " + token}
    for attempt in range(10):
        try:
            resp = _request_text("GET", url, headers=headers, timeout=10)
            data = json.loads(resp)
            res = data["data"]["list"]
            return res

        except Exception as e:
            print(f"获取任务信息失败 (尝试 {attempt + 1}): {e}")
            time.sleep(3)

    raise Exception("获取任务信息失败，已重试10次")


def fetch_domain_by_task_id(page=1, page_size=500):
    url = f"https://seosystem.top/prod/api/v1/shell-site-info/domains?page={page}&page_size={page_size}&token=7ee9a43ea2d8c11268dc95e2e298a183"
    headers = {
        "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Authorization": f"Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpYXQiOjE3NzAxNzI5NDMsIm5iZiI6MTc3MDE3Mjk0MywiZXhwIjoxNzcwNzc3NzQzLCJqdGkiOiIxIn0.AYlsEFbLYDrHsJv01BXWDoFYgtEujoqCNoS_H6ZHHYI"
    }
    for attempt in range(10):
        try:
            resp = _request_text("GET", url, headers=headers, timeout=10)
            data = json.loads(resp)
            res = data["data"]["list"]
            return res

        except Exception as e:
            print(f"task获取域名信息失败 (尝试 {attempt + 1}): {e}")
            time.sleep(3)

    raise Exception(f"task 获取任务信息失败，已重试10次")

def send_result_batch(items):
    url = f"https://seosystem.top/prod/api/v1/shell-site-info/batch?token=7ee9a43ea2d8c11268dc95e2e298a183"
    headers = {
        "User-Agent": "Apifox/1.0.0 (https://apifox.com)",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Authorization": f"Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpYXQiOjE3NzAxNzI5NDMsIm5iZiI6MTc3MDE3Mjk0MywiZXhwIjoxNzcwNzc3NzQzLCJqdGkiOiIxIn0.AYlsEFbLYDrHsJv01BXWDoFYgtEujoqCNoS_H6ZHHYI"
    }
    data = {"items": items}
    resp = _request_text("POST", url, json_data=data, headers=headers, timeout=10)
    print(resp)


if __name__ == '__main__':

    send_result_batch([
        {
            "shell_id": 35,
            "index_count": 0,
            "is_penalized": 1,
            "da": 5,
            "hellowrd":1,
            "id": 35
        }
    ])
    print(fetch_domain_by_task_id())