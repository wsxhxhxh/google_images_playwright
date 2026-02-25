"""
使用示例：展示 PagePool 的各种场景
"""

import asyncio
import logging
from page_pool import PagePool, ProxyConfig, create_pool

logging.basicConfig(level=logging.INFO)


# ------------------------------------------------------------------ #
#  示例 1：基础用法
# ------------------------------------------------------------------ #

async def example_basic():
    proxies = [
        # ProxyConfig(server="http://proxy1.example.com:8080", username="user", password="pass"),
        # ProxyConfig(server="http://proxy2.example.com:8080"),
        # ProxyConfig(server="http://proxy3.example.com:8080"),
        None,
        None,
        None,
    ]

    async with create_pool(
        initial_proxies=proxies,
        max_size=10,
        browser_type="chromium",
        launch_options={"headless": False},
        context_options={
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
        },
    ) as pool:

        # 并发抓取多个 URL
        urls = [
            "https://httpbin.org/ip",
            "https://httpbin.org/headers",
            "https://httpbin.org/user-agent",
        ]

        async def fetch(url: str):
            async with pool.acquire(timeout=30) as (slot_id, page):
                print(f"[{slot_id}] fetching {url}")
                await page.goto(url, wait_until="domcontentloaded")
                content = await page.content()
                print(f"[{slot_id}] done, content length={len(content)}")
                return content

        results = await asyncio.gather(*[fetch(url) for url in urls])
        print(results)
        print(f"Pool stats: {pool.stats()}")


# ------------------------------------------------------------------ #
#  示例 2：代理出错，动态移除 + 替换
# ------------------------------------------------------------------ #

async def example_proxy_error_handling():
    pool = PagePool(max_size=5, launch_options={"headless": True})
    await pool.start(initial_proxies=[
        ProxyConfig(server="http://good-proxy.example.com:8080"),
        ProxyConfig(server="http://bad-proxy.example.com:8080"),   # 这个会出错
    ])

    bad_slot_id = None

    try:
        async with pool.acquire() as (slot_id, page):
            try:
                await page.goto("https://example.com", timeout=10_000)
            except Exception as e:
                print(f"Slot {slot_id} proxy error: {e}")
                bad_slot_id = slot_id
                # 不要 raise，先记录，让 finally 正常归还

    finally:
        pass

    # 出了 acquire 上下文之后，slot 已归还，可以安全移除
    if bad_slot_id:
        await pool.remove_slot(bad_slot_id, reason="proxy_error")
        print(f"Removed bad slot: {bad_slot_id}")
        print(f"Pool stats after remove: {pool.stats()}")

        # 换一个新代理加进来
        new_slot_id = await pool.add_slot(
            ProxyConfig(server="http://new-proxy.example.com:8080")
        )
        print(f"Added new slot: {new_slot_id}")
        print(f"Pool stats after add: {pool.stats()}")

    await pool.close()


# ------------------------------------------------------------------ #
#  示例 3：replace_slot 一步到位
# ------------------------------------------------------------------ #

async def example_replace_slot():
    pool = PagePool(max_size=5, launch_options={"headless": True})
    await pool.start(initial_proxies=[
        ProxyConfig(server="http://proxy1.example.com:8080"),
    ])

    old_id = pool.slot_ids()[0]
    print(f"Old slot: {old_id}, slots={pool.slot_ids()}")

    new_id = await pool.replace_slot(
        old_id,
        new_proxy=ProxyConfig(server="http://proxy-new.example.com:8080"),
    )
    print(f"New slot: {new_id}, slots={pool.slot_ids()}")

    await pool.close()


# ------------------------------------------------------------------ #
#  示例 4：动态扩缩容
# ------------------------------------------------------------------ #

async def example_dynamic_scaling():
    pool = PagePool(max_size=10, launch_options={"headless": True})
    await pool.start()  # 空池启动

    # 动态按需加入
    for i in range(3):
        sid = await pool.add_slot(ProxyConfig(server=f"http://proxy{i}.example.com:8080"))
        print(f"Added {sid}")

    print(f"Stats: {pool.stats()}")

    # 移除特定 slot
    target = pool.slot_ids()[1]
    await pool.remove_slot(target, reason="quota_exceeded")
    print(f"Stats after remove: {pool.stats()}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(example_basic())
