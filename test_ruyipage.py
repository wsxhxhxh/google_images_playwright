import json
from types import SimpleNamespace

from browser_ruyipage import RuyiPageBrowser


# 固定参数：你可以按需直接改这里
PROXY_SERVER = "socks5://167.160.76.190:1080"
KEYWORD_ITEM = {"name": "t1st", "id": "11"}
LANGUAGE_CODE = "en-US"


def _to_text(body) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="ignore")
    return str(body)


def _extract_response(packet):
    """兼容 ruyiPage 监听包的不同结构。"""
    if isinstance(packet, dict):
        if isinstance(packet.get("response"), dict):
            return packet["response"]
        return packet

    response = getattr(packet, "response", None)
    if isinstance(response, dict):
        return response
    if response is not None:
        return {
            "url": getattr(response, "url", ""),
            "status": getattr(response, "status", ""),
            "body": getattr(response, "body", None),
        }

    return {
        "url": getattr(packet, "url", ""),
        "status": getattr(packet, "status", ""),
        "body": getattr(packet, "body", None),
    }


def _packet_keys(packet):
    if isinstance(packet, dict):
        return list(packet.keys())
    if hasattr(packet, "__dict__"):
        return list(packet.__dict__.keys())
    return []


def main():
    browser = RuyiPageBrowser(
        language_code=LANGUAGE_CODE,
        proxies={"server": PROXY_SERVER},
        headless=False,
        # firefox_path=r"D:\Firefox\firefox.exe",
    )

    # 仅满足 listen_and_collect 所需字段
    params = SimpleNamespace(language_code=LANGUAGE_CODE)

    try:
        print(f"[TEST] 启动浏览器, 代理: {PROXY_SERVER}")
        browser.initialize()

        print(f"[TEST] 开始监听并搜索关键词: {KEYWORD_ITEM['name']}")
        result = browser.listen_and_collect(KEYWORD_ITEM, params)
        print(f"[TEST] listen_and_collect 返回 new_datas 数量: {len(result.get('new_datas', []))}")
        print(f"[TEST] domains: {result.get('domains', [])[:5]}")
        print(f"[TEST] related_search: {result.get('related_search', [])[:5]}")

        packets = []
        browser.page.listen.start("google.com")
        browser.goto(f"https://www.google.com/imghp?hl={LANGUAGE_CODE}&authuser=0&ogbl")
        browser.human_type_and_submit(KEYWORD_ITEM)
        browser.human_scroll_to_bottom()

        # 连续取若干包，用于观察监听结构
        for _ in range(10):
            pkt = browser.page.listen.wait(count=1, timeout=2.5)
            if not pkt:
                break
            packets.append(pkt)
        browser.page.listen.stop()

        print(f"[TEST] 额外监听到的数据包数量: {len(packets)}")
        for idx, pkt in enumerate(packets, start=1):
            resp = _extract_response(pkt)
            url = resp.get("url", "")
            status = resp.get("status", "")
            body_text = _to_text(resp.get("body"))
            preview = body_text[:220].replace("\n", " ").replace("\r", " ")
            pkt_keys = _packet_keys(pkt)
            resp_keys = list(resp.keys()) if isinstance(resp, dict) else []
            print(f"\n[PACKET-{idx}] status={status}")
            print(f"[PACKET-{idx}] url={url}")
            print(f"[PACKET-{idx}] packet_keys={pkt_keys}")
            print(f"[PACKET-{idx}] response_keys={resp_keys}")
            print(f"[PACKET-{idx}] body_len={len(body_text)}")
            print(f"[PACKET-{idx}] body_preview={preview}")

    finally:
        print("[TEST] 关闭浏览器")
        browser.close()


if __name__ == "__main__":
    main()
