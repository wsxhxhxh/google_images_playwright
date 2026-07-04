# platform_api.py
import json
import threading
import time
import ssl
import requests
import asyncio
import traceback
from typing import Optional, Dict
import aiohttp


from config import logger, Config


_TLS_VERIFY = False  # 等效原代码 ssl=False
_TLS_TIMEOUT = 10
_SSL_CONTEXT = ssl._create_unverified_context()
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
        if Config.USE_PROXY:
            logger.info(f"send proxy success: {proxy['server']}")
            self.set_proxy_status(atm, proxy, 1)

    def set_fail(self, atm, proxy: Dict) -> None:
        if Config.USE_PROXY:
            logger.info(f"send proxy failed: {proxy['server']}")
            self.set_proxy_status(atm, proxy, 2)


async def get_task_info(atm, session):
    """获取任务信息"""
    token = await atm.get_token()
    url = "https://seosystem.top/prod/api/v1/tasks?platform_id=1&token=" + token
    headers = {"Authorization": "Bearer " + token}
    for attempt in range(10):
        try:
            async with session.get(url, headers=headers,timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
                if resp.status == 200:
                    data = json.loads(await resp.text())
                    res = data["data"]
                    if type(res) == list:
                        return res[0]
                    return res

        except Exception as e:
            logger.error(f"获取任务信息失败 (尝试 {attempt + 1}): {e}")
            await asyncio.sleep(3)

    raise Exception("获取任务信息失败，已重试10次")

async def fetch_tasks_from_api(session, dbname, datanum, binddomain):
    """从 API 获取关键词列表"""
    try:
        api_url = f"https://{binddomain}/page_data_api.php?datatype=getwordsV1&d={dbname}&datanum={datanum}"

        logger.info(f"获取关键词: {api_url}")

        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
            if resp.status == 200:
                task_data = json.loads(await resp.text())
                tasks = task_data.get('data', [])
                logger.info(f"获取到 {len(tasks)} 个关键词")
                return tasks
    except Exception as e:
        logger.error(f"获取关键词失败: {e}")

    return []

async def send_shopify_product_to_api(session, params, item):
    """异步发送Shopify产品数据到API"""
    start_time = time.time()
    api_url = "https://downloadtemp.flsxxsmode.xyz/2026_api_importshopifydomain.php"
    try:
        # 使用GET请求，将JSON数据作为请求体
        async with session.post(api_url, json=item, ssl=False) as response:
            text = await response.text()
            if response.status == 200:
                logger.info(f"send items result: {text}")
            else:
                raise Exception(f"status not 200: {text}")
    except Exception as e:
        traceback_details = traceback.format_exc()
        logger.error(f"send_shopify_product_to_api Exception occurred:\n{traceback_details}")
        raise Exception(f"Exception send items shopify product to API Failed: {e}")

    end_time = time.time()
    logger.info(
        f"send items shopify product to API use {end_time - start_time:.2f} seconds")

async def send_items_to_api(session, params, item):
    """异步发送产品数据到API"""
    start_time = time.time()
    try:
        items_backup = [item]
        data_to_send = json.dumps({'param': [dict(item) for item in items_backup]})

        # 使用异步POST请求
        async with session.post(
                f"https://{params.binddomain}/page_data_api.php?datatype=setwordsV1&d={params.dbname}",
                data=data_to_send,
                ssl=False,
                headers={'Content-Type': 'application/json'}
        ) as response:
            text = await response.text()
            if response.status == 200 and json.loads(text)['stat'] == 1:
                logger.info(f"send items result: {text}")
            else:
                raise Exception(f"stat not 1: {text}")

    except Exception as e:
        traceback_details = traceback.format_exc()
        logger.error(f"send_items_to_api Exception occurred:\n{traceback_details}")
        raise Exception(f"Exception send items {params.dbname} to API Failed: {e}")

    end_time = time.time()
    logger.info(f"send items {params.dbname} to API use {end_time - start_time:.2f} seconds")

async def send_err_task(params, tasks):

    if not tasks:
        logger.info(f"[Work-{params.worker_id}] 没有错误任务需要发送")
        return


    ids = []
    for task in tasks:
        t = json.loads(task)
        ids.append(t['id'])

    data = {
        "keyword_ids": ids,
        "status": 0
    }

    domain = params.binddomain
    headers = {
        'User-Agent': 'Apifox/1.0.0 (https://apifox.com)',
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'Host': domain,
        'Connection': 'keep-alive'
    }
    url = f"https://{domain}/page_data_api.php?datatype=update_keyword_status&d={params.dbname}"
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.post(url, json=data, ssl=False) as response:
                text = await response.text()
                logger.info(f"send tasks result: {text}")
    except Exception as e:
        logger.exception(f"[Work-{params.worker_id}] 发送错误任务异常: {e}")
        return False


async def update_task_status(atm, session, task_id):
    token = await atm.get_token()
    url = f"https://seosystem.top/prod/api/v1/tasks/{task_id}/status"
    headers = {"Authorization": "Bearer " + token}
    data = {"status": 2, "token": token}
    timeout = aiohttp.ClientTimeout(total=10)
    async with session.post(url, headers=headers, data=data, timeout=timeout, ssl=False) as resp:
        text = await resp.text()
        logger.info(f"update tasks result: {text}")


async def testapp():
    """使用示例"""
    # 1. 创建代理池实例（不包含异步操作）
    proxy_pool = AsyncProxyPool()

    # 2. 初始化代理池（包含异步操作）
    # 注意：这里需要替换为实际的代理API URL
    await proxy_pool.init_proxy_pool()

    # 3. 获取随机代理
    proxy = await proxy_pool.get_random_proxy()
    if proxy:
        logger.info(f"获取到代理: {proxy}")

        # 4. 模拟使用代理（假设失败）
        await proxy_pool.set_fail(proxy)
        await proxy_pool.set_fail(proxy)

        # 5. 再次获取代理
        proxy2 = await proxy_pool.get_random_proxy()
        logger.info(f"第二次获取代理: {proxy2}")

        # 6. 模拟使用成功
        if proxy2:
            await proxy_pool.set_success(proxy2)


    # 7. 查看代理池状态
    status = await proxy_pool.get_pool_status()
    logger.info("\n代理池状态:")
    logger.info(f"总代理数: {status['total_proxies']}")
    logger.info(f"可用代理: {status['available_proxies']}")
    logger.info(f"冷却中代理: {status['cooling_proxies']}")


    await proxy_pool.set_success(proxy)
    status = await proxy_pool.get_pool_status()
    logger.info("\n代理池状态:")
    logger.info(f"总代理数: {status['total_proxies']}")
    logger.info(f"可用代理: {status['available_proxies']}")
    logger.info(f"冷却中代理: {status['cooling_proxies']}")


async def main():
    atm = AsyncTokenManager()

    async with aiohttp.ClientSession() as session:
        await update_task_status(atm, session, 78)

if __name__ == '__main__':
    # app = AsyncProxyPool()
    asyncio.run(main())

