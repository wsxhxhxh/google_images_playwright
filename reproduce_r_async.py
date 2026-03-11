import argparse
import asyncio
import base64
import hashlib
import math
import random
import re
import time
import urllib.parse
from typing import Iterable, Optional

SEED_CIPHER = "0htWF3Og9kElII2Vjlaw4Dq7lKTvVX7xx26M3"
DEFAULT_HOST = "www.link114.cn"


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def js_round(x: float) -> int:
    return math.floor(x + 0.5)


def str_reverse(text: str) -> str:
    return text[::-1]


def js_to_number(text: str) -> float:
    s = (text or "").strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return float("nan")


def js_parse_int(text: str) -> float:
    m = re.match(r"^[\t\n\r\f\v ]*([+-]?\d+)", text or "")
    if not m:
        return float("nan")
    try:
        return float(int(m.group(1), 10))
    except ValueError:
        return float("nan")


def js_str_decode(seed_cipher: str, host: str) -> str:
    s = seed_cipher.replace("*", "+")
    host_md5 = md5_hex(host or "")
    left = md5_hex(host_md5[:16])
    right = md5_hex(host_md5[16:32])

    prefix = s[:1]
    key = left + md5_hex(left + prefix)

    raw = base64.b64decode(s[1:])
    data = "".join(chr(b) for b in raw)

    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + ord(key[i % len(key)])) % 256
        box[i], box[j] = box[j], box[i]

    i = 0
    j = 0
    out = []
    for ch in data:
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        out.append(chr(ord(ch) ^ box[(box[i] + box[j]) % 256]))
    plain = "".join(out)

    # JS: (prefix == 0 || prefix - time() > 0) && sig_match
    prefix_num = js_to_number(plain[:10])
    valid_prefix = (not math.isnan(prefix_num) and prefix_num == 0) or (
        not math.isnan(prefix_num) and prefix_num - int(time.time()) > 0
    )
    valid_sig = plain[10:26] == md5_hex(plain[26:] + right)[:16]
    return plain[26:] if valid_prefix and valid_sig else ""


def parse_url(query: str, mode: str) -> Optional[str]:
    pairs = {}
    for chunk in query.split("&"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            pairs[k] = v

    if "site" not in pairs:
        return None

    site = urllib.parse.unquote(pairs["site"])
    site = str_reverse(site)

    if mode == "domain":
        m = re.match(
            r"(?:^|http://|https://)((?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,8})(/[\x23-\x7f]+)?$",
            site,
            re.I,
        )
        if not m:
            return None
        return urllib.parse.quote(m.group(1), safe="")

    return urllib.parse.quote(site, safe="")


def domainnum(domain: str, salt: int) -> str:
    if not domain:
        return ""

    parts = domain.split(".")
    if not parts:
        return ""

    parts = parts[-2:]
    normalized = "".join("." + parts[i] for i in range(len(parts) - 1, -1, -1))[1:]
    labels = normalized.split(".")

    out = ""
    dot_code = ord(".")
    for i, label in enumerate(labels):
        acc = sum(ord(c) for c in label)
        if i == len(labels) - 1:
            acc = 1 + acc + ord("e") + salt
        else:
            acc = acc + dot_code + salt
        out += str(acc)
    return out


def is_prime(n: int) -> bool:
    if n <= 3:
        return n > 1
    if n % 2 == 0 or n % 3 == 0:
        return False
    f = 5
    while f * f <= n:
        if n % f == 0 or n % (f + 2) == 0:
            return False
        f += 6
    return True


def build_site_param(site: str) -> str:
    return urllib.parse.quote(str_reverse(site), safe="")


def _next_rand(rand_iter: Optional[Iterable[float]]) -> float:
    if rand_iter is None:
        return random.random()
    return next(rand_iter)


def generate_r(
    site: str,
    func: str = "ip",
    host: str = DEFAULT_HOST,
    rand_values: Optional[Iterable[float]] = None,
) -> str:
    rand_seed = js_str_decode(SEED_CIPHER, host)
    rand_seed_int_float = js_parse_int(rand_seed)
    if math.isnan(rand_seed_int_float):
        raise ValueError("rand_seed NaN")
    rand_seed_int = int(rand_seed_int_float)
    rand_seed_num = js_to_number(rand_seed)

    site_param = build_site_param(site)
    query = f"func={func}&site={site_param}"

    domain = parse_url(query, "domain")
    if not domain:
        raise ValueError("site not None but domain None")

    rands = iter(rand_values) if rand_values is not None else None

    n1 = js_round(_next_rand(rands) * 8 + 1)
    n2 = js_round(_next_rand(rands) * 89 + 10)

    prime_backoff = 0
    for i in range(10):
        if is_prime(n2 - i):
            prime_backoff = i
            break

    n3 = js_round(_next_rand(rands) * 899 + 100)
    tail = domainnum(domain, n3)
    if not math.isnan(rand_seed_num) and n1 >= rand_seed_num:
        seed_delta = n1 - rand_seed_int
    else:
        seed_delta = rand_seed_int - n1

    return f"{n1}{n2}{n3}{prime_backoff}{seed_delta}{tail}"


async def generate_r_async(
    site: str,
    func: str = "ip",
    host: str = DEFAULT_HOST,
    rand_values: Optional[Iterable[float]] = None,
) -> str:
    # CPU-bound but lightweight; keep async signature for orchestration.
    return generate_r(site, func, host, rand_values)


async def generate_r_batch_async(
    sites: Iterable[str],
    func: str = "ip",
    host: str = DEFAULT_HOST,
) -> list[str]:
    return [generate_r(site, func=func, host=host) for site in sites]


def parse_input_url(url: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlsplit(url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    func = qs.get("func", ["ip"])[0] or "ip"
    raw_site = qs.get("site", [""])[0]
    if not raw_site:
        raise ValueError("site parameter is required")

    site = str_reverse(urllib.parse.unquote(raw_site))
    host = parsed.hostname or DEFAULT_HOST
    return site, func, host


async def main_async() -> None:
    site = "psdrobo.com"
    func = "moz_da"
    host = DEFAULT_HOST

    r = await generate_r_async(site, func=func, host=host)
    res = f"https://www.link114.cn/get.php?func={func}&site={site[::-1]}&r={r}"
    print(res)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
