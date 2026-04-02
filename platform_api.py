# platform_api.py
import json
import time
import asyncio
import traceback
from typing import Optional, Dict
import aiohttp


from config import logger, Config, data_logger


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
    # token = await atm.get_token()
    url = "https://yingxiao.softwared.top/open/collect/task/list"
    for attempt in range(10):
        try:
            async with session.get(url,timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
                print(await resp.text())
                if resp.status == 200:
                    data = json.loads(await resp.text())
                    res = data["data"]
                    if res and type(res) == list:
                        return res[0]
                    return res

        except Exception as e:
            logger.error(f"获取任务信息失败 (尝试 {attempt + 1}): {e}")
            await asyncio.sleep(3)

    raise Exception("获取任务信息失败，已重试10次")


async def fetch_tasks_from_api(params):
    """从 API 获取关键词列表"""
    try:
        api_url = f"https://yingxiao.softwared.top/open/collect/task/keywords?taskId={params.task_id}"

        logger.info(f"获取关键词: {api_url}")

        async with params.session.get(api_url, timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
            print(await resp.text())
            if resp.status == 200:
                task_data = json.loads(await resp.text())
                tasks = task_data.get('data', [])
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

async def send_items_to_api(params, item):
    """异步发送产品数据到API"""
    start_time = time.time()
    try:
        items_backup = [item]
        data_to_send = {'param': [dict(item) for item in items_backup]}
        data_logger.info(f"[{params.worker_id}] {data_to_send}")
        # 使用异步POST请求
        async with params.session.post(
                f"{params.agent_url}?action=setwordsV1&d={params.dbname}&db_user={params.dbuser}&db_pass={params.dbpasswd}&secret_key={params.agent_key}",
                json=data_to_send,
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
        "status": 0  # (0: 未处理, 1: 已取词， 2: 已完成)
    }

    domain = params.binddomain
    headers = {
        'User-Agent': 'Apifox/1.0.0 (https://apifox.com)',
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'Host': domain,
        'Connection': 'keep-alive'
    }
    url = f"{params.agent_url}?action=update_keyword_status&d={params.dbname}&db_user={params.dbuser}&db_pass={params.dbpasswd}&secret_key={params.agent_key}"
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


async def main():
    atm = AsyncTokenManager()

    async with aiohttp.ClientSession() as session:
        info = await get_task_info(atm, session)
        print(info)


async def send_shopify_product_products_to_api(product, params):
    url = 'https://yingxiao.softwared.top/open/shopify/product/import'
    data = {
        "products": product,
    }
    async with params.session.post(url, json=data, ssl=False) as resp:
        data_logger.info(f"[{params.worker_id}] send shopify product to api: [{product['groupId']}][{product['id']}]")
        await resp.text()



if __name__ == '__main__':
    # app = AsyncProxyPool()
    asyncio.run(main())

