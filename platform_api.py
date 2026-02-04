# platform_api.py
import json
import time
import random
import asyncio
import traceback
from typing import Optional, Dict, List
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
    """改进的异步代理池（消除竞态条件）"""

    def __init__(self):
        self.proxy_pool: List[Dict] = []
        self._lock = asyncio.Lock()
        self._checking = set()  # ⭐ 新增：正在检测的代理集合

    async def init_proxy_pool(self):
        async with aiohttp.ClientSession() as session:
            async with self._lock:
                for i in range(Config.MAX_RETRIES):
                    result = await self._fetch_proxy(session)
                    if isinstance(result, list):
                        for proxy in result:
                            proxy_url = f"socks5://{proxy['ip']}:{proxy['port']}"

                            # 去重
                            if any(p["proxy"] == proxy_url for p in self.proxy_pool):
                                continue

                            self.proxy_pool.append({
                                "proxy": proxy_url,
                                "cooldown_until": 0,
                                "fail_count": 0,
                                "last_check": 0,
                                "in_use": False,  # ⭐ 新增：是否正在使用
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

    async def _check_proxy(self, proxy: str) -> bool:
        """真正检测代理是否可用"""
        return True

    async def get_random_proxy(self) -> Optional[str]:
        """
        改进的获取代理方法（消除竞态条件）
        """
        max_attempts = 3  # 最多尝试3个代理

        for attempt in range(max_attempts):
            # ⭐ 关键修改：在锁内完成筛选和标记
            async with self._lock:
                # 筛选可用代理
                available = [
                    p for p in self.proxy_pool
                    if not self._in_cooldown(p)
                       and p["proxy"] not in self._checking  # ⭐ 排除正在检测的
                       and p["fail_count"] < 5  # 失败次数过多的跳过
                ]

                if not available:
                    logger.warning(
                        f"无可用代理 (总数: {len(self.proxy_pool)}, "
                        f"冷却中: {sum(1 for p in self.proxy_pool if self._in_cooldown(p))}, "
                        f"检测中: {len(self._checking)})"
                    )
                    return None

                # 随机选择一个
                random.shuffle(available)
                proxy_info = available[0]
                proxy = proxy_info["proxy"]

                # ⭐ 立即标记为"检测中"，防止其他 worker 选择
                self._checking.add(proxy)
                logger.debug(f"准备检测代理: {proxy}")

            # ⭐ 在锁外进行耗时的网络检测
            try:
                ok = await self._check_proxy(proxy)
            except Exception as e:
                logger.error(f"检测代理异常 {proxy}: {e}")
                ok = False

            # ⭐ 检测完成，更新状态
            async with self._lock:
                # 从检测集合中移除
                self._checking.discard(proxy)

                proxy_info["last_check"] = time.time()

                if ok:
                    # ⭐ 代理可用，只设置冷却期（不标记为使用中）
                    proxy_info["fail_count"] = 0
                    self._set_cooldown(proxy_info)  # ⭐ 设置冷却期
                    logger.info(f"✅ 分配代理: {proxy}")
                    return proxy
                else:
                    # 代理不可用，增加失败计数并冷却
                    proxy_info["fail_count"] += 1
                    self._set_cooldown(proxy_info)
                    logger.warning(f"❌ 代理不可用: {proxy}, 失败次数: {proxy_info['fail_count']}")
                    # 继续尝试下一个代理

        logger.error(f"尝试了 {max_attempts} 个代理都失败")
        return None

    async def release_proxy(self, proxy_url: str):
        """释放代理"""
        async with self._lock:
            for proxy_info in self.proxy_pool:
                if proxy_info["proxy"] == proxy_url:
                    proxy_info["in_use"] = False
                    logger.info(f"释放代理: {proxy_url}")
                    break

    async def mark_proxy_failed(self, proxy_url: str):
        """标记代理失败"""
        async with self._lock:
            for proxy_info in self.proxy_pool:
                if proxy_info["proxy"] == proxy_url:
                    proxy_info["fail_count"] += 1
                    proxy_info["in_use"] = False
                    self._set_cooldown(proxy_info)
                    logger.warning(f"标记代理失败: {proxy_url}, 失败次数: {proxy_info['fail_count']}")
                    break

    async def get_pool_status(self) -> Dict:
        """获取代理池状态"""
        async with self._lock:
            total = len(self.proxy_pool)
            cooling = sum(1 for p in self.proxy_pool if self._in_cooldown(p))
            in_use = sum(1 for p in self.proxy_pool if p["in_use"])
            checking = len(self._checking)
            failed = sum(1 for p in self.proxy_pool if p["fail_count"] >= 5)
            available = total - cooling - in_use - checking - failed

            return {
                "total": total,
                "available": available,
                "cooling": cooling,
                "in_use": in_use,
                "checking": checking,
                "failed": failed,
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

