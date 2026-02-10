# platform_api.py
import json
import time
import random
import asyncio
import traceback
from typing import Optional, Dict, List
import aiohttp
from datetime import datetime, timedelta
from aiohttp_socks import ProxyConnector

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
    """异步代理池管理类，支持代理获取、状态管理和冷却机制"""

    def __init__(self):
        """初始化代理池（不包含异步操作）"""
        self.proxies: List[Dict] = []  # 原始代理列表
        self.proxy_status: Dict[str, Dict] = {}  # 代理状态字典
        self._lock = None  # 异步锁，在init_proxy_pool中初始化
        self._initialized = False

    async def init_proxy_pool(self):
        """
        初始化代理池（包含异步操作）

        Args:
            proxy_url: 代理API的URL，返回格式为包含字典的列表的JSON字符串
        """
        # 初始化异步锁
        self._lock = asyncio.Lock()
        proxy_url = f"https://yoyoproxy.flsxxsmode.xyz/proxy_api7.php?key={Config.PROXY_KEY}"
        # 从API获取代理列表
        async with aiohttp.ClientSession() as session:
            async with session.get(proxy_url, ssl=False) as response:
                if response.status == 200:
                    text = await response.text()
                    self.proxies = json.loads(text)
                else:
                    raise Exception(f"Failed to fetch proxies: HTTP {response.status}")

        # 初始化所有代理的状态
        for proxy in self.proxies:
            proxy_key = f"socks5://{proxy['ip']}:{proxy['port']}"
            self.proxy_status[proxy_key] = {
                'available': True,
                'cooldown_until': None,
                'fail_count': 0,  # 连续失败次数
                'total_success': 0,
                'total_fail': 0
            }

        self._initialized = True
        logger.info(f"代理池初始化完成，共加载 {len(self.proxies)} 个代理")

    async def get_random_proxy(self) -> Optional[str]:
        """
        获取一个随机可用的代理

        Returns:
            代理字符串（格式：socks5://ip:port）或None（如果没有可用代理）
        """
        if not self._initialized:
            raise RuntimeError("代理池未初始化，请先调用 init_proxy_pool()")

        async with self._lock:
            current_time = datetime.now()
            available_proxies = []

            # 筛选可用代理
            for proxy in self.proxies:
                proxy_key = f"socks5://{proxy['ip']}:{proxy['port']}"
                status = self.proxy_status[proxy_key]

                # 检查冷却时间是否已过
                if status['cooldown_until']:
                    if current_time >= status['cooldown_until']:
                        # 冷却期结束，恢复可用状态
                        status['available'] = True
                        status['cooldown_until'] = None

                # 收集可用代理
                if status['available']:
                    available_proxies.append(proxy_key)

            # 返回随机代理
            if available_proxies:
                return random.choice(available_proxies)
            else:
                return None

    async def set_success(self, proxy: str):
        """
        标记代理使用成功

        Args:
            proxy: 代理字符串（格式：socks5://ip:port）
        """
        if not self._initialized:
            raise RuntimeError("代理池未初始化，请先调用 init_proxy_pool()")

        async with self._lock:
            if proxy in self.proxy_status:
                status = self.proxy_status[proxy]
                status['available'] = True
                status['cooldown_until'] = None
                status['fail_count'] = 0  # 重置连续失败次数
                status['total_success'] += 1
                logger.info(f"代理 {proxy} 标记为成功，状态已恢复")

    async def set_fail(self, proxy: str):
        """
        标记代理使用失败，并设置冷却期

        规则：
        - 第一次失败：5分钟冷却期
        - 第二次及以后失败：30分钟冷却期

        Args:
            proxy: 代理字符串（格式：socks5://ip:port）
        """
        if not self._initialized:
            raise RuntimeError("代理池未初始化，请先调用 init_proxy_pool()")

        async with self._lock:
            if proxy in self.proxy_status:
                status = self.proxy_status[proxy]
                status['available'] = False
                status['fail_count'] += 1
                status['total_fail'] += 1

                current_time = datetime.now()

                # 根据失败次数设置不同的冷却时间
                if status['fail_count'] == 1:
                    # 第一次失败：5分钟冷却
                    cooldown_minutes = 5
                    status['cooldown_until'] = current_time + timedelta(minutes=5)
                else:
                    # 第二次及以后失败：30分钟冷却
                    cooldown_minutes = 30
                    status['cooldown_until'] = current_time + timedelta(minutes=30)

                logger.info(f"代理 {proxy} 标记为失败（第{status['fail_count']}次），"
                      f"冷却{cooldown_minutes}分钟至 {status['cooldown_until'].strftime('%H:%M:%S')}")

    async def get_pool_status(self) -> Dict:
        """
        获取代理池的整体状态信息

        Returns:
            包含代理池统计信息的字典
        """
        if not self._initialized:
            raise RuntimeError("代理池未初始化，请先调用 init_proxy_pool()")

        async with self._lock:
            current_time = datetime.now()
            total_proxies = len(self.proxies)
            available_count = 0
            cooling_count = 0

            for proxy in self.proxies:
                proxy_key = f"socks5://{proxy['ip']}:{proxy['port']}"
                status = self.proxy_status[proxy_key]

                # 检查是否在冷却中
                if status['cooldown_until'] and current_time < status['cooldown_until']:
                    cooling_count += 1
                elif status['available']:
                    available_count += 1

            return {
                'total_proxies': total_proxies,
                'available_proxies': available_count,
                'cooling_proxies': cooling_count,
                'unavailable_proxies': total_proxies - available_count - cooling_count,
                'timestamp': current_time.strftime('%Y-%m-%d %H:%M:%S'),
                'detailed_status': {
                    proxy_key: {
                        'available': status['available'],
                        'fail_count': status['fail_count'],
                        'total_success': status['total_success'],
                        'total_fail': status['total_fail'],
                        'cooldown_until': status['cooldown_until'].strftime('%Y-%m-%d %H:%M:%S')
                        if status['cooldown_until'] else None
                    }
                    for proxy_key, status in self.proxy_status.items()
                }
            }



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

async def send_err_task(params, tasks):

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
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.post(url, json=data, ssl=False) as response:
            text = await response.text()
            logger.info(f"send tasks result: {text}")



if __name__ == '__main__':
    # app = AsyncProxyPool()
    asyncio.run(testapp())

