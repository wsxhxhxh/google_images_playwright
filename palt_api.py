import json
import random
import time
import asyncio
import traceback
from typing import Optional, Dict
import aiohttp

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


async def get_task_info(atm, session):
    """获取任务信息"""
    token = await atm.get_token()
    up = "page=1&page_size=50&status=1&task_status=2&token="
    url = "https://seosystem.top/prod/api/v1/shell-domain-filter/tasks?" + up + token
    headers = {"Authorization": "Bearer " + token}
    for attempt in range(10):
        try:
            async with session.get(url, headers=headers,timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
                if resp.status == 200:
                    data = json.loads(await resp.text())
                    res = data["data"]["list"]
                    return res

        except Exception as e:
            print(f"获取任务信息失败 (尝试 {attempt + 1}): {e}")
            await asyncio.sleep(3)

    raise Exception("获取任务信息失败，已重试10次")


async def fetch_domain_by_task_id(atm, session, task_id):
    token = await atm.get_token()
    up = "page=1&page_size=100&status=1&filter_status=1&token="
    url = f"https://seosystem.top/prod/api/v1/shell-domain-filter/tasks/{task_id}/domains?{up}{token}"
    headers = {"Authorization": "Bearer " + token}
    for attempt in range(10):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
                if resp.status == 200:
                    data = json.loads(await resp.text())
                    res = data["data"]["list"]
                    return res

        except Exception as e:
            print(f"task: {task_id} 获取域名信息失败 (尝试 {attempt + 1}): {e}")
            await asyncio.sleep(3)

    raise Exception(f"task: {task_id} 获取任务信息失败，已重试10次")

async def send_result_batch(atm, session, items):
    token = await atm.get_token()
    url = f"https://seosystem.top/prod/api/v1/shell-domain-filter/domains/query-results"
    headers = {"Authorization": "Bearer " + token}
    data = {"items": items, "token": token}
    async with session.post(url, headers=headers, json=data) as resp:
        if resp.status == 200:
            text = await resp.text()
            print(text)

async def send_task_status(atm, session, task_id, status):
    token = await atm.get_token()
    url = f"https://seosystem.top/prod/api/v1/shell-domain-filter/tasks/{task_id}/status"
    headers = {"Authorization": "Bearer " + token}
    data = {"token": token, "status": status}
    async with session.post(url, headers=headers, json=data) as resp:
        if resp.status == 200:
            text = await resp.text()
            print(text)



async def test():
    atm = AsyncTokenManager()
    await atm.refresh_token()
    async with aiohttp.ClientSession() as session:


        # test task

        # task = await get_task_info(atm, session)
        # print(task)
        #
        # await send_task_status(atm, session, 3, 2)
        #
        # task = await get_task_info(atm, session)
        # print(task)




        # test domain

        # items = [{
        #     "id": 64,
        #     "status": 0
        # }]
        # re11 = await send_result_batch(atm, session, items)
        #
        res = await fetch_domain_by_task_id(atm, session, 3)
        print(res)




if __name__ == '__main__':
    asyncio.run(test())
