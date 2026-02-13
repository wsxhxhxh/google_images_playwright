# platform_api.py
import json
import time
import asyncio
import traceback
from typing import Optional, Dict
import aiohttp


from config import logger, Config


class AsyncTokenManager:
    def __init__(self, token_expire_seconds: int = 3600 * 36):
        self._token: Optional[str] = None
        self._expire_time: float = 0
        self._lock = asyncio.Lock()
        self._token_expire_seconds = token_expire_seconds
        self.apikey = "5a11020697da4aceba7e011fc0370185"
        self._url = "https://seosystem.top/prod/api/v1/token"

    async def _fetch_new_token(self) -> str:
        """异步获取新token - 每次创建新 session"""
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=20,
            ssl=False
        )

        # ✅ 创建临时 session，用完自动关闭
        async with aiohttp.ClientSession(connector=connector) as session:
            data = {"apikey": self.apikey}
            async with session.post(self._url, data=data, ssl=False) as resp:
                text = await resp.text()
                resp_json = json.loads(text)
                token = resp_json["data"]["token"]
                return token

    async def get_token(self) -> str:
        """获取 token，如果过期则刷新"""
        async with self._lock:
            now = time.time()
            if not self._token or now >= self._expire_time:
                self._token = await self._fetch_new_token()
                self._expire_time = now + self._token_expire_seconds
            return self._token

    async def refresh_token(self) -> str:
        """主动刷新 token"""
        async with self._lock:
            self._token = await self._fetch_new_token()
            self._expire_time = time.time() + self._token_expire_seconds
            return self._token

class AsyncProxyPool:

    def __init__(self):
        self.pool = []
        self.lock = asyncio.Lock()
        self.session = None

    async def close(self):
        await self.session.close()

    async def safe_request(self, method, url, **kwargs):
        if not self.session:
            self.session = aiohttp.ClientSession()
        try:
            async with self.session.request(method, url, **kwargs) as resp:
                return await resp.text()

        except aiohttp.ClientError as e:
            logger.warning(f"request error: {e}, retrying...")

            # 重新创建 session
            await self.session.close()
            self.session = aiohttp.ClientSession()

            async with self.session.request(method, url, **kwargs) as resp:
                return await resp.text()

    async def refresh_pool(self):
        url = Config.PROXY_URL
        text = await self.safe_request("GET", url)
        resp_json = json.loads(text)
        logger.info(f"refresh local proxy pool, num: {len(resp_json)}")
        self.pool = resp_json

    async def get_random_proxy(self):

        async with self.lock:

            if not self.pool:
                await self.refresh_pool()

            if not self.pool:
                return None

            proxy = self.pool.pop()

        proxy["server"] = f"socks5://{proxy['ip']}:{proxy['port']}"
        return proxy

    async def set_proxy_status(self, atm, proxy, status):
        url = Config.PROXY_STATUS.format(id=proxy['id'])
        token = await atm.get_token()
        data = {"status": status, "token": token}
        headers = {"Authorization": f"Bearer {token}"}
        text = await self.safe_request("POST", url, data=data, headers=headers)
        logger.info(text)

    async def set_success(self, atm, proxy: Dict):
        logger.info(f"send proxy success: {proxy['server']}")
        await self.set_proxy_status(atm, proxy, 1)

    async def set_fail(self, atm, proxy: Dict) -> None:
        logger.info(f"send proxy failed: {proxy['server']}")
        await self.set_proxy_status(atm, proxy, 2)

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

