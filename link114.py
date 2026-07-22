import asyncio

from urllib.parse import urlparse
from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeout

lock = asyncio.Lock()

def create_response_handler(data):
    async def handle_response(response):
        url = response.url
        if "www.link114.cn/get.php" not in url:
            return

        jjjj = await response.json()
        print(jjjj)
        if jjjj["status"] != '1':
            return
        query = urlparse(url).query
        params = {q.split("=")[0]: q.split("=")[1] for q in query.split('&')}
        site = params["site"][::-1]
        async with lock:
            if not data.get(site):
                data[site] = {}

        if params["func"] == "moz_da":
            data[site]["moz_da"] = jjjj["result"]["da"]
            data[site]["moz_pa"] = jjjj["result"]["pa"]


    return handle_response


async def get_link_114_info(domains):

    if type(domains) == list:
        domains_str = "\n".join(domains)
        domains_list = domains
    elif type(domains) == str:
        domains_str = domains
        domains_list = domains.split('\n')
    res_data = {}
    pw = await async_playwright().start()
    browse = await pw.chromium.launch(headless=False)
    context = await browse.new_context(
        screen={"width": 1920, "height": 1080},
        viewport={"width": 1900, "height": 940},
    )

    await context.add_cookies([{
        "name": "preference",
        "value": "moz_da",
        "domain": ".link114.cn",  # 注意域名格式
        "path": "/",
        "httpOnly": False,
        "secure": True
    }])

    page = await context.new_page()
    page.on('response', create_response_handler(res_data))

    for _ in range(3):
        try:
            await page.goto("https://www.link114.cn/do.php?type=login")
            break
        except Exception as e:
            print(e)
        finally:
            await asyncio.sleep(0.5)

    await page.locator('xpath=//input[@name="username"]').fill('xingji199')
    await page.locator('xpath=//input[@name="passwd"]').fill('Yooo775885@#')
    await page.locator('#do_submit').click()
    await asyncio.sleep(0.5)


    for _ in range(3):
        try:
            await page.goto("https://www.link114.cn")
            break
        except Exception as e:
            print(e)
        finally:
            await asyncio.sleep(0.5)

    await page.locator('#ip_websites').fill(domains_str)

    await asyncio.sleep(0.5)
    await page.locator('#tj').click()

    for i in range(len(domains_list) * 2 + 10):
        await asyncio.sleep(1)

    try:
        await page.close()
        await context.close()
        await browse.close()
        await pw.stop()
    except:
        pass


    return res_data

if __name__ == '__main__':
    print(asyncio.run(get_link_114_info(["github.com", "deepseek.com"])))
