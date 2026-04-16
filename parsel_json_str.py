# pasrsel_json_str.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON 递归提取工具（同步版本）。
"""

import json
import re
from lxml import etree
from typing import Any, List, Tuple
from config import logger


class RecursiveJSONExtractor:
    """同步递归 JSON 提取器。"""

    @staticmethod
    def extract_json_arrays(text: str, recursive: bool = True) -> List[Any]:
        results = []
        i = 0

        while i < len(text):
            if text[i] == '[':
                json_str, end_pos = RecursiveJSONExtractor._extract_single_json(text, i)
                if json_str:
                    try:
                        json_obj = json.loads(json_str)
                        if recursive:
                            json_obj = RecursiveJSONExtractor._recursive_parse(json_obj)
                        results.append(json_obj)
                        i = end_pos
                    except json.JSONDecodeError:
                        pass
            i += 1

        return results

    @staticmethod
    def _recursive_parse(obj: Any) -> Any:
        if isinstance(obj, str):
            obj = obj.strip()
            if (obj.startswith('[') or obj.startswith('{')) and len(obj) > 1:
                try:
                    parsed = json.loads(obj)
                    return RecursiveJSONExtractor._recursive_parse(parsed)
                except (json.JSONDecodeError, ValueError):
                    return obj
            return obj

        if isinstance(obj, list):
            return [RecursiveJSONExtractor._recursive_parse(item) for item in obj]

        if isinstance(obj, dict):
            return {key: RecursiveJSONExtractor._recursive_parse(value) for key, value in obj.items()}

        return obj

    @staticmethod
    def extract_json_with_strings(text: str, recursive: bool = True) -> List[Tuple[str, Any]]:
        results = []
        i = 0

        while i < len(text):
            if text[i] == '[':
                json_str, end_pos = RecursiveJSONExtractor._extract_single_json(text, i)
                if json_str:
                    try:
                        json_obj = json.loads(json_str)
                        if recursive:
                            json_obj = RecursiveJSONExtractor._recursive_parse(json_obj)
                        results.append((json_str, json_obj))
                        i = end_pos
                    except json.JSONDecodeError:
                        pass
            i += 1

        return results

    @staticmethod
    def _extract_single_json(text: str, start: int) -> Tuple[str, int]:
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


def demo_with_real_data(real_data):
    """使用真实 Google 搜索数据演示（同步版本）。"""
    extractor = RecursiveJSONExtractor()
    results = extractor.extract_json_arrays(real_data, recursive=True)

    parsed_results = []
    for obj in results:
        if len(obj) == 1 and isinstance(obj[0], list):
            objs = obj[0]
            for sub_obj in objs:
                parsed_results.append(parse_item(sub_obj))
        else:
            parsed_results.append(parse_item(obj))

    result_list = [res for res in parsed_results if res]
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


def parse_product_info(data: dict) -> dict:
    """解析产品信息。"""
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


def parse_basic_info(data: dict) -> dict:
    """解析基础信息。"""
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


def parse_item(obj):
    """解析单个 item。"""
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

    base = parse_basic_info(meta)
    product = parse_product_info(meta)

    return {
        **base,
        **product,
        "image": get_nested(item, [3, 0]),
        "thumb": get_nested(item, [2, 0]),
    }

def _get_related_search_sync(html_content):
    """同步版本的相关搜索获取。"""
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


def get_related_search(html_content):
    return _get_related_search_sync(html_content)


def get_related_items(html_content):
    return find_between(decode_escaped_string(html_content), 'jsname="pIvPIe">', '</span>')


AsyncRecursiveJSONExtractor = RecursiveJSONExtractor


def test():
    """主函数（同步）。"""
    import datetime
    from deal_product_func_async import deal_info
    from platform_api import send_items_to_api

    file_path = r"C:\Users\XXX\Desktop\111\html_temp_9.txt"

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    class P: pass
    params = P()
    params.worker_id = 1
    params.dbname = "t0062-c2-en-usgoimg"
    params.binddomain = "image8xgs.xyz"
    params.usenum = 20
    params.desimagenum = 20
    params.collect_platform_type = None
    params.worker_id = 1
    params.agent_url = ""
    params.dbuser = ""
    params.dbpasswd = ""
    params.agent_key = ""

    result = demo_with_real_data(text)
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
    ll = deal_info(new_datas, params)
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

    send_items_to_api(params, google_item)

if __name__ == '__main__':
    test()
