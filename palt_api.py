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


def fetch_domain_by_task_id(atm, task_id):
    token = atm.get_token()
    up = "page=1&page_size=100&status=1&filter_status=1&token="
    url = f"https://seosystem.top/prod/api/v1/shell-domain-filter/tasks/{task_id}/domains?{up}{token}"
    headers = {"Authorization": "Bearer " + token}
    for attempt in range(10):
        try:
            resp = _request_text("GET", url, headers=headers, timeout=10)
            data = json.loads(resp)
            res = data["data"]["list"]
            return res

        except Exception as e:
            print(f"task: {task_id} 获取域名信息失败 (尝试 {attempt + 1}): {e}")
            time.sleep(3)

    raise Exception(f"task: {task_id} 获取任务信息失败，已重试10次")

def send_result_batch(atm, items):
    token = atm.get_token()
    url = f"https://seosystem.top/prod/api/v1/shell-domain-filter/domains/query-results"
    headers = {"Authorization": "Bearer " + token}
    data = {"items": items, "token": token}
    resp = _request_text("POST", url, json_data=data, headers=headers, timeout=10)
    print(resp)

def send_task_status(atm, task_id, status):
    token = atm.get_token()
    url = f"https://seosystem.top/prod/api/v1/shell-domain-filter/tasks/{task_id}/status"
    headers = {"Authorization": "Bearer " + token}
    data = {"token": token, "status": status}
    resp = _request_text("POST", url, json_data=data, headers=headers, timeout=10)
    print(resp)



def test():
    atm = TokenManager()
    atm.refresh_token()


    # test task

    task = get_task_info(atm)
    print(task)
    #
    # await send_task_status(atm, session, 12, 2)
    #
    # task = await get_task_info(atm, session)
    # print(task)




    # test domain

    # items = [{
    #     "id": i,
    #     "status": 1
    # } for i in range(300, 400)]
    items = [{
        "id": 1399,
        "status": 1,
        "query_result": {
            # "create": None,
            # "err_msg": "谷歌收录小于4"
        }
    }]
    send_result_batch(atm, items)
    #
    res = fetch_domain_by_task_id(atm, 30)
    print(res)




if __name__ == '__main__':
    test()
