# playwright_async_fixed.py  （只展示 search_keyword_batch，其余函数保持不变）
# ... 保留文件中所有其他函数不动，只替换 search_keyword_batch ...

async def search_keyword_batch(params):
    """
    批量搜索关键词。

    新逻辑：
      - 每次调用从 SQLite 最多取 datanum 条任务，在同一个浏览器里跑完后关闭。
      - 低水线补词由 DbManager.auto_refresh_if_needed 自动触发，
        这里不再主动 fetch_tasks_from_api。
      - 验证码 / 代理失败时立即关闭浏览器并退出本批，由上层 worker 循环重启。
    """
    import json
    import aiohttp
    from config import logger, Config
    from platform_api import send_items_to_api, send_shopify_product_to_api, update_task_status
    from deal_product_func_async import deal_info_by_async, deal_shopify_product_info_async

    db = params.db
    browser = None

    try:
        # ── 1. 获取代理 ───────────────────────────────────────────
        while True:
            proxy = await params.app.get_random_proxy()
            if proxy:
                params.proxies = proxy
                break
            logger.info(f"[Worker-{params.worker_id}] 暂无可用代理，等待 30s")
            await asyncio.sleep(30)

        # ── 2. 启动浏览器 ──────────────────────────────────────────
        browser = PlaywrightBrowser(
            chrome_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            language_code=params.language_code,
            proxies=params.proxies,
            headless=False,
        )
        logger.info(f"[Worker-{params.worker_id}] 初始化浏览器，代理: {params.proxies['server']}")
        await asyncio.wait_for(
            create_child_task(browser.initialize()),
            timeout=30.0,
        )

        # ── 3. 逐词处理（最多 datanum 条）────────────────────────
        success_count = 0
        fail_count    = 0
        captcha_hit   = False
        processed     = 0           # 本批已处理数量

        while processed < params.datanum:
            # ── 3-a. 触发低水线检查（会在后台补词）─────────────────
            await db.auto_refresh_if_needed()

            # ── 3-b. 从 SQLite 取一条任务 ────────────────────────
            db_task = await db.fetch_one_task_safe(task_id=params.task_id)
            if db_task is None:
                logger.info(f"[Worker-{params.worker_id}] SQLite 暂无任务，等待 10s")
                await asyncio.sleep(10)
                # 等待后再试一次，如果还是没有就结束本批（让上层重新进入循环）
                db_task = await db.fetch_one_task_safe(task_id=params.task_id)
                if db_task is None:
                    logger.info(f"[Worker-{params.worker_id}] 等待后仍无任务，结束本批")
                    break

            keyword_item = {
                "id":   db_task["keyword_id"],
                "name": db_task["keyword"],
            }
            logger.info(f"[Worker-{params.worker_id}] 开始搜索: {keyword_item['name']}")

            # ── 3-c. 搜索单词 ─────────────────────────────────────
            success = await search_single_keyword(browser, keyword_item, params)
            processed += 1

            if success is True:
                await db.mark_success(db_task["id"])
                success_count += 1

            elif success is None:
                # 验证码 / 代理失败 → 标记失败，退出本批
                await db.mark_failed(db_task["id"])
                logger.warning(f"[Worker-{params.worker_id}] 验证码或代理失败，结束本批")
                captcha_hit = True
                break

            else:
                await db.mark_failed(db_task["id"])
                fail_count += 1

        # ── 4. 汇报本批结果 ───────────────────────────────────────
        await db.print_stats()
        logger.info(
            f"[Worker-{params.worker_id}] 本批结束 — "
            f"处理: {processed}, 成功: {success_count}, 失败: {fail_count}"
            + (" [验证码/代理中断]" if captcha_hit else "")
        )

    except asyncio.CancelledError:
        logger.info(f"[Worker-{params.worker_id}] search_keyword_batch 被取消")
        raise

    except asyncio.TimeoutError:
        logger.error(f"[Worker-{params.worker_id}] 浏览器初始化超时")
        raise

    except Exception as e:
        logger.exception(f"[Worker-{params.worker_id}] 批量搜索异常: {e}")
        raise

    finally:
        # ── 5. 无论如何关闭浏览器 ─────────────────────────────────
        if browser:
            try:
                await asyncio.wait_for(browser.close(), timeout=10.0)
                logger.info(f"[Worker-{params.worker_id}] 浏览器已关闭")
            except Exception as e:
                logger.error(f"[Worker-{params.worker_id}] 关闭浏览器失败: {e}")
