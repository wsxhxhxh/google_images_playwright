# pasrsel_json_str.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON 递归提取工具 (异步版本)
支持递归解析嵌套的JSON字符串
"""

import json
import re
import asyncio
from lxml import etree
from typing import List, Any, Tuple, Union
from config import logger


class AsyncRecursiveJSONExtractor:
    """异步递归JSON提取器类"""

    @staticmethod
    async def extract_json_arrays(text: str, recursive: bool = True) -> List[Any]:
        """
        从文本中提取所有有效的JSON数组，支持递归解析

        Args:
            text: 包含JSON字符串的原始文本
            recursive: 是否递归解析内部的JSON字符串

        Returns:
            解析后的JSON对象列表
        """
        results = []
        i = 0

        while i < len(text):
            if text[i] == '[':
                json_str, end_pos = AsyncRecursiveJSONExtractor._extract_single_json(text, i)
                if json_str:
                    try:
                        json_obj = json.loads(json_str)
                        if recursive:
                            json_obj = await AsyncRecursiveJSONExtractor._recursive_parse(json_obj)
                        results.append(json_obj)
                        i = end_pos
                    except json.JSONDecodeError:
                        pass
            i += 1

        return results

    @staticmethod
    async def _recursive_parse(obj: Any) -> Any:
        """
        递归解析对象中的JSON字符串

        Args:
            obj: 要解析的对象

        Returns:
            解析后的对象
        """
        if isinstance(obj, str):
            # 尝试解析字符串是否为JSON
            obj = obj.strip()
            if (obj.startswith('[') or obj.startswith('{')) and len(obj) > 1:
                try:
                    parsed = json.loads(obj)
                    # 递归解析内部内容
                    return await AsyncRecursiveJSONExtractor._recursive_parse(parsed)
                except (json.JSONDecodeError, ValueError):
                    # 不是有效的JSON，返回原字符串
                    return obj
            return obj

        elif isinstance(obj, list):
            # 并发递归处理列表中的每个元素
            tasks = [AsyncRecursiveJSONExtractor._recursive_parse(item) for item in obj]
            return await asyncio.gather(*tasks)

        elif isinstance(obj, dict):
            # 并发递归处理字典中的每个值
            keys = list(obj.keys())
            values = list(obj.values())
            tasks = [AsyncRecursiveJSONExtractor._recursive_parse(value) for value in values]
            parsed_values = await asyncio.gather(*tasks)
            return {key: value for key, value in zip(keys, parsed_values)}

        else:
            # 其他类型直接返回
            return obj

    @staticmethod
    async def extract_json_with_strings(text: str, recursive: bool = True) -> List[Tuple[str, Any]]:
        """
        从文本中提取所有有效的JSON数组，同时返回原始字符串

        Args:
            text: 包含JSON字符串的原始文本
            recursive: 是否递归解析内部的JSON字符串

        Returns:
            (原始JSON字符串, 解析后的对象) 元组列表
        """
        results = []
        i = 0

        while i < len(text):
            if text[i] == '[':
                json_str, end_pos = AsyncRecursiveJSONExtractor._extract_single_json(text, i)
                if json_str:
                    try:
                        json_obj = json.loads(json_str)
                        if recursive:
                            json_obj = await AsyncRecursiveJSONExtractor._recursive_parse(json_obj)
                        results.append((json_str, json_obj))
                        i = end_pos
                    except json.JSONDecodeError:
                        pass
            i += 1

        return results

    @staticmethod
    def _extract_single_json(text: str, start: int) -> Tuple[str, int]:
        """
        从指定位置提取单个JSON数组

        Args:
            text: 文本内容
            start: 开始位置

        Returns:
            (JSON字符串, 结束位置)
        """
        bracket_count = 0
        in_string = False
        escape_next = False

        i = start
        while i < len(text):
            char = text[i]

            # 处理转义字符
            if escape_next:
                escape_next = False
                i += 1
                continue

            if char == '\\' and in_string:
                escape_next = True
                i += 1
                continue

            # 处理字符串
            if char == '"':
                in_string = not in_string

            # 只在字符串外部计数括号
            if not in_string:
                if char in '[{':
                    bracket_count += 1
                elif char in ']}':
                    bracket_count -= 1

                    # 找到匹配的结束括号
                    if bracket_count == 0 and char == ']':
                        return text[start:i + 1], i

            i += 1

        return None, start


async def demo_with_real_data(real_data):
    """使用真实的Google搜索数据演示 (异步版本)"""

    extractor = AsyncRecursiveJSONExtractor()

    # 递归解析
    results = await extractor.extract_json_arrays(real_data, recursive=True)

    # 并发处理所有结果
    tasks = []
    for obj in results:
        if len(obj) == 1 and isinstance(obj[0], list):
            objs = obj[0]
            for sub_obj in objs:
                tasks.append(parse_item(sub_obj))
        else:
            tasks.append(parse_item(obj))

    # 并发执行所有解析任务
    parsed_results = await asyncio.gather(*tasks)

    # 过滤掉None结果
    result_list = [res for res in parsed_results if res]
    # res = dedupe_by_id(result_list)
    res = dedupe_by_image(result_list)
    logger.info(f"找到: {len(res)}个产品 {res[:3]}...")
    return res


def dedupe_by_id(items):
    """去重函数 (同步)"""
    seen_ids = set()
    result = []

    for item in items:
        _id = item.get("id")
        if _id in seen_ids:
            continue
        seen_ids.add(_id)
        result.append(item)

    return result


def dedupe_by_image(items):
    """去重函数 (同步)"""
    seen_ids = set()
    result = []

    for item in items:
        _id = item.get("image")
        if _id in seen_ids:
            continue
        seen_ids.add(_id)
        result.append(item)

    return result

def get_nested(obj, path, default=""):
    """获取嵌套值 (同步)"""
    try:
        for key in path:
            obj = obj[key]
        if obj is None:
            return default
        return obj
    except (KeyError, IndexError, TypeError):
        return default


async def parse_product_info(data: dict) -> dict:
    """
    解析产品信息 (异步版本)
    data = item[9]，也就是包含 2000 / 2003 / 2006 的 dict
    """
    result = {
        "brand": "",
        "desc": "",
        "price": "",
        "currency": "",
        "score": "",
        "review": ""
    }

    item2006 = data.get("2006")
    if not item2006:
        return result

    # 新结构（你这个 Nike 就是这个）
    info = get_nested(item2006, [12])
    if not info:
        return result

    result["brand"] = get_nested(info, [7])
    result["desc"] = get_nested(info, [6])
    result["price"] = get_nested(info, [2, 1])
    result["currency"] = get_nested(info, [2, 0])
    result["score"] = get_nested(info, [3])
    result["review"] = get_nested(info, [8])

    return result


async def parse_basic_info(data: dict) -> dict:
    """解析基础信息 (异步版本)"""
    item2003 = data.get("2003")
    if not item2003:
        return {}

    return {
        "id": get_nested(item2003, [1]),
        "url": get_nested(item2003, [2]),
        "title": get_nested(item2003, [3]),
        "site": get_nested(item2003, [17]),
        "brand_guess": get_nested(item2003, [12]),
    }


async def parse_item(obj):
    """
    解析单个item (异步版本)
    obj 可以是 #63 或 #64
    """
    if obj is None or isinstance(obj, bool):
        return None

    if isinstance(obj, list) and len(obj) == 2 and isinstance(obj[1], list):
        obj = obj[1]

    if isinstance(obj, (list, tuple)) and len(obj) > 1 and isinstance(obj[1], list):
        item = obj[1]  # #63
    else:
        item = obj  # #64

    try:
        meta = item[9]
        if not isinstance(meta, dict):
            return None
    except (KeyError, IndexError, TypeError):
        return None

    # 并发解析基础信息和产品信息
    base_task = parse_basic_info(meta)
    product_task = parse_product_info(meta)
    base, product = await asyncio.gather(base_task, product_task)

    return {
        **base,
        **product,
        "image": get_nested(item, [3, 0]),
        "thumb": get_nested(item, [2, 0]),
    }

async def get_related_search(html_content):
    """获取相关搜索 (异步版本)"""
    # lxml操作是CPU密集型,可以在executor中运行
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_related_search_sync, html_content)


def _get_related_search_sync(html_content):
    """同步版本的相关搜索获取"""
    rtn = []
    parser = etree.HTMLParser()
    tree = etree.fromstring(html_content, parser)
    h2_elements = tree.xpath('//h2/following-sibling::a/div[last()]')

    for h2 in h2_elements:
        rtn.append("".join(h2.xpath('.//text()')))
    return rtn


def decode_escaped_string(s):
    """解码转义字符串 (同步)"""
    return re.sub(r'\\x([0-9A-Fa-f]{2})', lambda x: chr(int(x.group(1), 16)), s)


def find_between(text, str1, str2):
    """查找两个字符串之间的内容 (同步)"""
    pattern = f"{re.escape(str1)}(.*?){re.escape(str2)}"
    matches = re.findall(pattern, text, re.DOTALL)
    return matches


async def get_related_items(html_content):
    """获取相关项目 (异步版本)"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: find_between(decode_escaped_string(html_content), 'jsname="pIvPIe">', '</span>')
    )


async def test():
    """主函数 (异步)"""
    # 异步读取文件
    import aiofiles
    import datetime
    import aiohttp
    from deal_product_func_async import deal_info_by_async
    from platform_api import send_items_to_api

    file_path = r"C:\Users\XXX\Desktop\111\html_temp_9.txt"

    async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
        text = await f.read()

    class P: pass
    params = P()
    params.worker_id = 1
    params.dbname = "t0062-c2-en-usgoimg"
    params.binddomain = "image8xgs.xyz"
    params.usenum = 20
    params.desimagenum = 20
    params.collect_platform_type = None

    result = await demo_with_real_data(text)
    new_datas = []
    for index, item in enumerate(result):
        if item.get("site", ".jp").endswith('.jp'):
            continue
        new_data = {
            "index": item.get("id"),
            "word": item.get("title"),
            "domain": item.get("site"),
            "link": item.get("url"),
            "image": item.get("image"),
            "info": {
                "desc": item.get("desc"),
                "brand": item.get("brand"),
                "price": item.get("price"),
                "currency": item.get("currency"),
                "score": item.get("score"),
                "review": item.get("review"),
            },
            "parent": 1,
            "stat": -1,
            "createdAt": str(datetime.datetime.now(datetime.timezone.utc))
        }
        new_datas.append(new_data)

    print(len(new_datas), new_datas[:1])
    ll = await deal_info_by_async(new_datas, params)
    print(len(ll), ll[:1])


    google_item = {
        'id': "11",
        'use_proxy_ip': "127.0.0.1",
        'from': "127.0.0.1",
        'word': "t1st",
        'script': "",
        'domains': '[]',
        'related': '[]',
        'items': '[]',
        'products': json.dumps(ll)
    }

    async with aiohttp.ClientSession() as session:
        await send_items_to_api(session, params, google_item)
    # print(ll)



if __name__ == '__main__':
    # 运行异步主函数
    asyncio.run(test())
