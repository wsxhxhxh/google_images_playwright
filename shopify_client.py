import json
import random
import asyncio
import aiohttp
import time
from typing import Dict, Any, Optional, List, AsyncGenerator
from config import logger, special_logger
from managed import create_child_task
from platform_api import send_shopify_product_products_to_api


class ShopifySyncException(Exception):
    """基础异常类"""
    pass


class ShopifyAPIException(ShopifySyncException):
    """Shopify API异常"""
    pass


class RetryableException(ShopifySyncException):
    """可重试的异常"""
    pass


class ShopifyClient:
    """Shopify API客户端（异步 + 重试机制）"""

    def __init__(self, shop_url: str):
        self.base_url = shop_url
        self.timeout = aiohttp.ClientTimeout(total=30)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """懒加载 Session（复用连接）"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    'User-Agent': 'Apifox/1.0.0 (https://apifox.com)',
                    'Accept': 'application/json',
                },
                timeout=self.timeout,
            )
        return self._session

    async def _make_request(
            self,
            method: str,
            endpoint: str,
            retries: int = 3,
            backoff_factor: float = 2.0,
            **kwargs
    ) -> Dict[str, Any]:
        """发起异步 HTTP 请求（含手动重试）"""
        url = f"{self.base_url}{endpoint}"
        session = await self._get_session()

        for attempt in range(retries + 1):
            try:
                logger.debug(f"[Attempt {attempt + 1}] {method} {url}")

                async with session.request(method, url, **kwargs) as response:
                    # 检查速率限制头
                    if 'X-Shopify-Shop-Api-Call-Limit' in response.headers:
                        logger.debug(f"API call limit: {response.headers['X-Shopify-Shop-Api-Call-Limit']}")

                    # 429 单独处理：等待 Retry-After 再重试
                    if response.status == 429:
                        retry_after = int(response.headers.get('Retry-After', 5))
                        logger.warning(f"Rate limited. Retrying after {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue

                    # 5xx 按退避重试
                    if response.status >= 500:
                        if attempt < retries:
                            wait = backoff_factor ** attempt
                            logger.warning(f"Server error {response.status}, retrying in {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        raise RetryableException(f"Server error {response.status}: {url}")

                    # 其他 4xx 直接抛出
                    if response.status >= 400:
                        logger.error(f"HTTP error {response.status}: {url}")
                        raise ShopifyAPIException(f"HTTP {response.status}: {url}")

                    return await response.json()

            except (aiohttp.ServerTimeoutError, asyncio.TimeoutError):
                if attempt < retries:
                    wait = backoff_factor ** attempt
                    logger.warning(f"Timeout, retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise RetryableException(f"Request timeout: {url}")

            except aiohttp.ClientConnectionError as e:
                if attempt < retries:
                    wait = backoff_factor ** attempt
                    logger.warning(f"Connection error, retrying in {wait}s: {e}")
                    await asyncio.sleep(wait)
                    continue
                raise RetryableException(f"Connection error: {str(e)}")

            except (ShopifyAPIException, RetryableException):
                raise

            except Exception as e:
                logger.error(f"Unexpected error: {str(e)}")
                raise ShopifyAPIException(f"Unexpected error: {str(e)}")

        raise RetryableException(f"Max retries exceeded: {url}")

    async def get_products(
            self,
            page: int = 1,
            limit: int = 250
    ) -> Dict[str, Any]:
        """获取单页产品列表"""
        endpoint = f"/products.json?page={page}&limit={limit}"
        try:
            data = await self._make_request('GET', endpoint)
            logger.info(f"Fetched {len(data.get('products', []))} products from page {page}")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch products page {page}: {str(e)}")
            raise

    async def get_all_products(
            self,
            limit: int = 250,
            start_page: int = 1,
            delay: float = 1.0,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        自动翻页异步生成器，逐个 yield 产品

        Args:
            limit:      每页数量，最大 250
            start_page: 起始页码
            delay:      每页请求间隔（秒），避免触发速率限制

        Examples:
            async for product in client.get_all_products():
                await process(product)
        """
        total = 0
        logger.info(f"Start fetching all products: limit={limit}, start_page={start_page}")

        for page in range(start_page, 251):
            try:
                data = await self.get_products(page=page, limit=limit)

                if 'errors' in data:
                    err = data['errors']
                    if 'exceeds the 25000 limit' in str(err):
                        logger.warning(f"Reached Shopify 25000 limit at page {page}")
                        break
                    raise ShopifyAPIException(f"API error: {err}")

                products = data.get('products', [])
                if not products:
                    logger.info(f"No more products at page {page}. Total: {total}")
                    break

                for product in products:
                    total += 1
                    yield product

                logger.info(f"Page {page} done, +{len(products)} products, total={total}")
                await asyncio.sleep(delay)

            except RetryableException as e:
                logger.warning(f"Retryable error on page {page}: {e}, skipping page")
                continue

            except ShopifyAPIException:
                raise

            except Exception as e:
                logger.error(f"Unexpected error on page {page}: {e}")
                raise

    async def get_all_products_as_list(
            self,
            limit: int = 250,
            start_page: int = 1,
            max_products: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """收集所有产品并返回列表"""
        products = []
        async for product in self.get_all_products(limit=limit, start_page=start_page):
            products.append(product)
            if max_products and len(products) >= max_products:
                logger.info(f"Reached max_products limit: {max_products}")
                break
        logger.info(f"Total products collected: {len(products)}")
        return products

    async def get_product(self, product_id: int) -> Dict[str, Any]:
        """获取单个产品"""
        try:
            data = await self._make_request('GET', f"/products/{product_id}.json")
            logger.info(f"Fetched product {product_id}")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch product {product_id}: {str(e)}")
            raise

    async def close(self):
        """关闭 Session"""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("Shopify client session closed")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


async def fetch_shopify_products(shopify_domain: str, group_id) -> List[Dict[str, Any]]:
    if not shopify_domain.startswith(("https://", "http://")):
        shopify_domain = "https://" + shopify_domain

    products = []
    async with ShopifyClient(shop_url=shopify_domain) as client:
        async for product in client.get_all_products():
            products.append(product)

    for product in products:
        product["groupId"] = group_id

    return products

async def get_and_send_shopify_products(domains: List | str, params) -> bool:
    background_tasks = []
    if type(domains) == str:
        domains = json.loads(domains)

    # 分成每组4个域名
    domain_groups = []
    for i in range(0, len(domains), 4):
        domain_groups.append({
            "group_id": int(time.time() * 100000) + i + random.randint(100, 999),
            "domains": domains[i:i + 4]
        })

    for group in domain_groups:
        special_logger.info(f"[{params.worker_id}] group id: {group['group_id']} domains: {group['domains']}")
        # 并发抓取整组所有域名的产品
        tasks = [
            fetch_shopify_products(domain, group["group_id"])
            for domain in group["domains"]
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 合并整组产品，跳过失败的任务
        all_products = []
        for domain, result in zip(group["domains"], results):
            if isinstance(result, Exception):
                logger.error(f"Failed to fetch {domain}: {result}")
                continue
            all_products.extend(result)

        logger.info(f"Group {group['group_id']}: collected {len(all_products)} products")

        # 按50个一批写入
        # batches = [all_products[i:i + 50] for i in range(0, len(all_products), 50)]
        # for batch in batches:
        #     await send_shopify_product_products_to_api(batch, params)  # 你的写入函数占位

        for product in all_products:
            task = create_child_task(send_shopify_product_products_to_api(product, params))
            background_tasks.append(task)

    await asyncio.gather(*background_tasks, return_exceptions=True)
    return True