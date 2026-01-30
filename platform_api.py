import json
import time
import random
import asyncio
import traceback
from typing import Optional
import aiohttp
from aiohttp_socks import ProxyConnector

from config import logger, Config


class AsyncTokenManager:
    def __init__(self, token_expire_seconds: int = 3600 * 36):
        """
        token_expire_seconds: token 的有效期，单位秒
        """
        self._token: Optional[str] = None
        self._expire_time: float = 0
        self._lock = asyncio.Lock()  # 避免并发刷新token
        self._token_expire_seconds = token_expire_seconds
        self.apikey = "5a11020697da4aceba7e011fc0370185"
        self._url = "https://seosystem.top/prod/api/v1/token"
        self._connector = None
        self._session = None

    async def _ensure_session(self):
        """
        确保 aiohttp session 只在 event loop 中创建
        """
        if self._session is None:
            self._connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=20,
                ssl=False
            )
            self._session = aiohttp.ClientSession(
                connector=self._connector
            )

    async def _fetch_new_token(self) -> str:
        """
        异步获取新token
        """
        await self._ensure_session()
        async with aiohttp.ClientSession(connector=self._connector) as session:
            data = {"apikey": self.apikey}
            async with session.post(self._url, data=data, ssl=False) as resp:
                text = await resp.text()
                resp_json = json.loads(text)
                token = resp_json["data"]["token"]
                return token

    async def get_token(self) -> str:
        """
        获取 token，如果过期则刷新
        """
        async with self._lock:
            now = time.time()
            if not self._token or now >= self._expire_time:
                self._token = await self._fetch_new_token()
                self._expire_time = now + self._token_expire_seconds
            return self._token

    async def refresh_token(self) -> str:
        """
        主动刷新 token
        """
        async with self._lock:
            self._token = await self._fetch_new_token()
            self._expire_time = time.time() + self._token_expire_seconds
            return self._token

    async def close(self):
        """
        程序退出时调用，释放连接
        """
        if self._session:
            await self._session.close()


class AsyncProxyPool:
    """异步代理池（带冷却 + 健康检测）"""

    def __init__(self):
        self.proxy_pool = []   # list[dict]
        self._lock = asyncio.Lock()

    async def init_proxy_pool(self):
        async with aiohttp.ClientSession() as session:

            async with self._lock:
                for i in range(Config.MAX_RETRIES):
                    result = await self._fetch_proxy(session)
                    if isinstance(result, list):
                        for proxy in result:
                            self.proxy_pool.append({
                                "proxy": f"socks5://{proxy['ip']}:{proxy['port']}",
                                "cooldown_until": 0,
                                "fail_count": 0,
                                "last_check": 0
                            })
                        break

        logger.info(f"代理池初始化完成，共 {len(self.proxy_pool)} 个代理")

    async def _fetch_proxy(self, session):
        try:
            url = f"https://yoyoproxy.flsxxsmode.xyz/proxy_api7.php?key={Config.PROXY_KEY}"
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return json.loads(text)
        except Exception as e:
            logger.warning(f"代理获取失败: {e}")
        return []

    def _in_cooldown(self, proxy_info):
        return time.time() < proxy_info["cooldown_until"]

    def _set_cooldown(self, proxy_info):
        cooldown = random.randint(Config.COOLDOWN_MIN, Config.COOLDOWN_MAX)
        proxy_info["cooldown_until"] = time.time() + cooldown

    async def _check_proxy(self, proxy):
        """用 Bing 检测代理是否可用"""
        return True

    async def get_random_proxy(self):
        async with self._lock:
            available = [
                p for p in self.proxy_pool
                if not self._in_cooldown(p)
            ]

        if not available:
            logger.warning("所有代理都在冷却中")
            return None

        random.shuffle(available)

        for proxy_info in available:
            proxy = proxy_info["proxy"]

            ok = await self._check_proxy(proxy)

            async with self._lock:
                proxy_info["last_check"] = time.time()

                if ok:
                    proxy_info["fail_count"] = 0
                    self._set_cooldown(proxy_info)
                    logger.debug(f"代理可用: {proxy}")
                    return proxy
                else:
                    proxy_info["fail_count"] += 1
                    self._set_cooldown(proxy_info)
                    logger.debug(f"代理不可用，进入冷却: {proxy}")

        return None


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
    api_url = "https://downloadtemp.flsxxsmode.xyz/api_product_storage.php"
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


async def test_app(app):
    await app.init_proxy_pool()
    proxy = await app.get_random_proxy()
    print(proxy)
    connector = ProxyConnector.from_url(proxy)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get("https://ipinfo.io/json") as resp:
            text = await resp.text()
            print(text)



if __name__ == '__main__':
    app = AsyncProxyPool()
    asyncio.run(test_app(app))

