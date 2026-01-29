import re
import json
import random
import urllib
import asyncio


def select_random_elements(input_list, count):
    """随机选择元素（同步函数，不需要改成异步）"""
    if count >= len(input_list):
        shuffled_list = input_list[:]
        random.shuffle(shuffled_list)
        return shuffled_list
    return random.sample(input_list, count)


def remove_last_separator(text, replacement=""):
    """移除最后一个分隔符（同步函数，不需要改成异步）"""
    # 替换最后一个" - "或" | "之后的内容
    pattern = r'( - | \| )(?!.*( - | \| )).*$'
    return re.sub(pattern, replacement, text)


def remove_trailing_dots(text):
    """移除末尾的点（同步函数，不需要改成异步）"""
    return text.rstrip('.')


def deal_product_name(name):
    """处理产品名称（同步函数，不需要改成异步）"""
    if not name:
        return ""
    name = remove_last_separator(name)
    name = remove_trailing_dots(name)
    return name


def get_list(list1, num):
    """获取列表元素（同步函数，不需要改成异步）"""
    return list1[num] if len(list1) >= num + 1 else ""


def get_dic(dic1, key):
    """获取字典值（同步函数，不需要改成异步）"""
    return dic1.get(key, "")


def deal_product_info_desc(info, key):
    """处理产品信息描述（同步函数，不需要改成异步）"""
    if type(info) == str:
        info = json.loads(info)
    return info.get(key)


def deal_product_info_remove_desc(info, key):
    """移除产品信息中的描述（同步函数，不需要改成异步）"""
    if type(info) == str:
        info = json.loads(info)
    if key in info:
        del info[key]
    return json.dumps(info)


def deal_product_info(info, purl, imageurl):
    """处理产品信息（同步函数，不需要改成异步）"""
    res = deal_product_info_remove_desc(info, "desc")
    if type(res) == str:
        res = json.loads(res)
    res["purl"] = purl
    res["type"] = deal_product_platform_type(purl, imageurl)
    return json.dumps(res)


def deal_product_platform_type(purl, imageurl):
    """判断产品平台类型（同步函数，不需要改成异步）"""
    if '/cdn/shop/' in imageurl and '/products/' in purl:
        return 'shopify'
    elif 'amazon.com' in purl:
        return 'amazon'
    elif 'ebay.com' in purl:
        return 'ebay'
    elif 'etsy.com' in purl:
        return 'etsy'
    elif 'asos.com' in purl:
        return 'asos'
    elif 'zalando.co.uk' in purl:
        return 'zalando'
    elif 'shein.com' in purl:
        return 'shein'
    elif 'walmart.com' in purl:
        return 'walmart'
    else:
        return 'other'


def find_between(text, str1, str2):
    """查找两个字符串之间的内容（同步函数，不需要改成异步）"""
    pattern = f"{re.escape(str1)}(.*?){re.escape(str2)}"
    matches = re.findall(pattern, text, re.DOTALL)
    return matches


def deal_num(str1):
    """处理数字（同步函数，不需要改成异步）"""
    # str1 = find_between(str1, 'About', 'results')
    findlist = find_between(str1, 'About', 'results')
    # print("findlist",findlist)
    if len(findlist) > 0:
        rtn = findlist[0].replace(",", "")
    else:
        rtn = extract_number(str1)

    return rtn.strip()


def extract_number(text):
    """提取数字（同步函数，不需要改成异步）"""
    # 使用正则表达式查找数字
    numbers = re.findall(r'\d+', text)
    if numbers:
        return numbers[0]  # 返回第一个匹配的数字
    return 0


async def deal_info_by_async(productlist, params):
    """
    异步处理产品信息

    Args:
        productlist: 产品列表
        usenum: 使用数量
        desimagenum: 描述图片数量

    Returns:
        处理后的产品数据列表
    """
    datas = []
    descriptions = []

    # 第一步：构建描述列表（可以并行处理）
    for item in productlist:
        description = {
            "desc": deal_product_info_desc(get_dic(item, "info"), "desc"),
            "image": get_dic(item, "image"),
            "name": get_dic(item, "word"),
            "purl": get_dic(item, "link"),
            "type": deal_product_platform_type(get_dic(item, "link"), get_dic(item, "image")),
        }
        descriptions.append(description)

    ok_product = 0

    # 第二步：处理产品数据
    for product in productlist:
        if ok_product >= params.usenum:
            continue

        description = json.dumps(select_random_elements(descriptions, params.desimagenum))
        link = get_dic(product, "link")
        info = deal_product_info(get_dic(product, "info"), link, get_dic(product, "image"))

        if params.collect_platform_type:
            cpts = params.collect_platform_type
            if type(cpts) == str:
                cpts = json.loads(cpts)

            for cpt in cpts:
                if cpt in info:
                    ok_product += 1
                    new_data = {
                        "parent": get_dic(product, "parent"),
                        "index": get_dic(product, "index"),
                        "name1": get_dic(product, "word"),
                        "name": deal_product_name(get_dic(product, "word")),
                        "shortdescription": deal_product_info_desc(get_dic(product, "info"), "desc"),
                        "description": description,
                        "domain": get_dic(product, "domain"),
                        "link": link,
                        "image": get_dic(product, "image"),
                        "info": info,
                    }
                    datas.append(new_data)
                    break


        elif "other" not in info:
            ok_product += 1
            new_data = {
                "parent": get_dic(product, "parent"),
                "index": get_dic(product, "index"),
                "name1": get_dic(product, "word"),
                "name": deal_product_name(get_dic(product, "word")),
                "shortdescription": deal_product_info_desc(get_dic(product, "info"), "desc"),
                "description": description,
                "domain": get_dic(product, "domain"),
                "link": link,
                "image": get_dic(product, "image"),
                "info": info,
            }
            datas.append(new_data)

    return datas


async def deal_shopify_product_info_async(params, products):
    """
    异步处理Shopify产品信息

    Args:
        languageid: 语言ID
        jxycategory_id: 分类ID
        products: 产品列表

    Returns:
        处理后的Shopify产品数据列表
    """
    datas = []

    for item in products:
        for description in json.loads(item["description"]):
            purl = description["purl"]
            ptype = description["type"]
            if ptype == "shopify":
                d = {}
                plib = urllib.parse.urlparse(purl)
                d["domain"] = f"{plib.scheme}://{plib.netloc}"
                d["jxycategory_id"] = params.jxycategory_id
                d["language_id"] = params.languageid
                d["status"] = 1
                datas.append(d)

    return datas

# 使用示例
async def main():
    """测试异步函数"""
    # 模拟产品数据
    test_products = [
        {
            "parent": "test-parent",
            "index": 0,
            "word": "Test Product - Brand",
            "domain": "example.com",
            "link": "https://example.com/products/test",
            "image": "https://example.com/cdn/shop/test.jpg",
            "info": {
                "desc": "Test description",
                "brand": "TestBrand",
                "price": "99.99",
                "currency": "USD",
                "score": "4.5",
                "review": "100"
            }
        }
    ]

    # 测试异步函数
    result = await deal_info_by_async(test_products, 20, 5)
    print("异步处理结果:", json.dumps(result, indent=2))

    shopify_result = await deal_shopify_product_info_async(1, 1, result)
    print("Shopify产品信息:", json.dumps(shopify_result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
