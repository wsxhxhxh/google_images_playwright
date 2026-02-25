import asyncio


async def search_single_keyword(page, keyword):
    try:
        await page.goto("https://www.google.com/imghp", timeout=30000)
        await page.wait_for_selector("textarea.gLFyf", timeout=10000)

        await page.fill("textarea.gLFyf", keyword)
        await page.keyboard.press("Enter")

        await asyncio.sleep(2)

        # 验证检测
        current_url = page.url
        if "/sorry/" in current_url or "captcha" in current_url:
            print(f"[{keyword}] 遇到验证")
            return None

        print(f"[{keyword}] 成功")
        return True

    except Exception as e:
        print(f"[{keyword}] 失败: {e}")
        return False