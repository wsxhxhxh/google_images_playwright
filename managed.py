import asyncio

from config import logger




class ManagedPage:
    """页面管理器，确保页面总是被关闭"""

    def __init__(self, browser, keyword):
        self.browser = browser
        self.keyword = keyword
        self.page = None

    async def __aenter__(self):
        self.page = await asyncio.wait_for(
            self.browser.create_new_page(),
            timeout=30.0
        )
        logger.info(f"[{self.keyword}] 页面创建成功")
        return self.page

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.page:
            try:
                await asyncio.sleep(0.3)
                await asyncio.wait_for(self.page.close(), timeout=5.0)
                logger.info(f"[{self.keyword}] 页面已关闭")
            except Exception as e:
                logger.error(f"[{self.keyword}] 关闭页面失败: {e}")


class ResponseTracker:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pending = 0
        self.event = asyncio.Event()
        self.event.set()  # 初始状态为完成

    async def start(self):
        async with self.lock:
            self.pending += 1
            self.event.clear()

    async def finish(self):
        async with self.lock:
            self.pending = max(0, self.pending - 1)
            if self.pending == 0:
                self.event.set()

    async def wait_all(self, timeout=10):
        """等待所有响应处理完成"""
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout)
            logger.info(f"所有响应处理完成")
        except asyncio.TimeoutError:
            async with self.lock:
                if self.pending > 0:
                    logger.warning(f"等待响应处理超时，还有 {self.pending} 个未完成")
                    # ⭐ 关键：超时后再等待一段时间让 finally 执行完
                    await asyncio.sleep(1)

class ThreadSafeAggregator:
    """线程安全的数据聚合器"""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.new_datas = []
        self.related_search = []
        self.related_items = []
        self.domains = []

    async def add_data(self, new_data):
        async with self.lock:
            self.new_datas.append(new_data)

    async def add_domain(self, domain):
        async with self.lock:
            self.domains.append(domain)

    async def add_related_search(self, items):
        async with self.lock:
            self.related_search.extend(items)

    async def add_related_items(self, items):
        async with self.lock:
            self.related_items.extend(items)

    async def get_all(self):
        async with self.lock:
            return {
                'new_datas': self.new_datas.copy(),
                'related_search': self.related_search.copy(),
                'related_items': self.related_items.copy(),
                'domains': self.domains.copy()
            }
