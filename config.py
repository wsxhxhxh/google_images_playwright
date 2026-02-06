# config.py
import os
import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler

class TaskNameFilter(logging.Filter):
    def filter(self, record):
        try:
            task = asyncio.current_task()
            record.task_name = task.get_name() if task else "Main"
        except RuntimeError:
            record.task_name = "Main"
        return True


formatter = logging.Formatter('"%(asctime)s [%(levelname)s] [%(task_name)s] %(message)s"')
directory_path = os.path.dirname(os.path.abspath(__file__))
file_handler = TimedRotatingFileHandler(
    filename=os.path.join(directory_path, 'logs/crawl_google_us_async.log'),
    when='midnight',
    interval=1,
    backupCount=99
)
file_handler.setFormatter(formatter)
file_handler.stream.reconfigure(encoding='utf-8')

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
stream_handler.stream.reconfigure(encoding='utf-8')

logger = logging.getLogger()
logger.addFilter(TaskNameFilter())
logger.setLevel(logging.INFO)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)


class Config:
    # 请求控制
    MAX_IMAGES_PER_SESSION = 50  # 每会话最大图片数
    REQUEST_DELAY = (1.0, 3.0)  # 请求延迟范围(秒)
    SESSION_DURATION = 1800  # 会话最长持续时间(秒)

    # 浏览器设置
    VIEWPORTS = [

        {'width': 1366, 'height': 768},
        {'width': 1536, 'height': 864}
    ]
    DPR_SETTING = [
        {
            "screen": {"width": 1920, "height": 1080},
            "viewport": {"width": 1900, "height": 940},
            "dpr": 1
        },
        {
            "screen": {"width": 1920, "height": 1080},
            "viewport": {"width": 1536, "height": 864},
            "dpr": 1.25
        },
        {
            "screen": {"width": 1920, "height": 1080},
            "viewport": {"width": 1280, "height": 720},
            "dpr": 1.5
        },
        {
            "screen": {"width": 1366, "height": 768},
            "viewport": {"width": 1366, "height": 728},
            "dpr": 1
        },
        {
            "screen": {"width": 1366, "height": 768},
            "viewport": {"width": 1093, "height": 614},
            "dpr": 1.25
        },
        {
            "screen": {"width": 1536, "height": 864},
            "viewport": {"width": 1510, "height": 820},
            "dpr": 1
        },
        {
            "screen": {"width": 1920, "height": 1080},
            "viewport": {"width": 1536, "height": 864},
            "dpr": 1.25
        }
    ]



    # 重试策略
    MAX_RETRIES = 3  # 最大重试次数
    RETRY_DELAY = 5  # 重试延迟(秒)

    FINGERPRINT_REGIONS = {
        "en-US": [
            {"locale": "en-US", "accept_language": "en-US,en;q=0.9", "timezone": "America/New_York"},
            {"locale": "en-US", "accept_language": "en-US,en;q=0.9", "timezone": "America/Los_Angeles"}
        ],
        "en-CA": [
            {"locale": "en-CA", "accept_language": "en-CA,en;q=0.9", "timezone": "America/Toronto"}
        ],
        "en-GB": [
            {"locale": "en-GB", "accept_language": "en-GB,en;q=0.9", "timezone": "Europe/London"}
        ],
        "de-DE": [
            {"locale": "de-DE", "accept_language": "de-DE,de;q=0.9,en;q=0.8", "timezone": "Europe/Berlin"}
        ],
        "fr-FR": [
            {"locale": "fr-FR", "accept_language": "fr-FR,fr;q=0.9,en;q=0.8", "timezone": "Europe/Paris"}
        ],
        "es-ES": [
            {"locale": "es-ES", "accept_language": "es-ES,es;q=0.9,en;q=0.8", "timezone": "Europe/Madrid"}
        ],
        "en-SG": [
            {"locale": "en-SG", "accept_language": "en-SG,en;q=0.,en;q=0.8", "timezone": "Asia/Singapore"}
        ],
        "en-AU": [
            {"locale": "en-AU", "accept_language": "en-AU,en;q=0.9,en;q=0.8", "timezone": "Australia/Sydney"}
        ],
        "ja-JP": [
            {"locale": "ja-JP", "accept_language": "ja-JP,ja;q=0.9,en;q=0.8", "timezone": "Asia/Tokyo"}
        ],
        "nl-NL": [
            {"locale": "nl-NL", "accept_language": "nl-NL,nl;q=0.9,en;q=0.8", "timezone": "Europe/Amsterdam"}
        ],
        "it-IT": [
            {"locale": "it-IT", "accept_language": "it-IT,it;q=0.9,en;q=0.8", "timezone": "Europe/Rome"}
        ],
        "nb-NO": [
            {"locale": "nb-NO", "accept_language": "nb-NO,nb;q=0.9,en;q=0.8", "timezone": "Europe/Oslo"}
        ],
        "da-DK": [
            {"locale": "da-DK", "accept_language": "da-DK,da;q=0.9,en;q=0.8", "timezone": "Europe/Copenhagen"}
        ],
        "sv-SE": [
            {"locale": "sv-SE", "accept_language": "sv-SE,sv;q=0.9,en;q=0.8", "timezone": "Europe/Stockholm"}
        ],
        "pl-PL": [
            {"locale": "pl-PL", "accept_language": "pl-PL,pl;q=0.9,en;q=0.8", "timezone": "Europe/Warsaw"}
        ],
        "fi-FI": [
            {"locale": "fi-FI", "accept_language": "fi-FI,fi;q=0.9,en;q=0.8", "timezone": "Europe/Helsinki"}
        ],
        "pt-PT": [
            {"locale": "pt-PT", "accept_language": "pt-PT,pt;q=0.9,en;q=0.8", "timezone": "Europe/Lisbon"}
        ],
        "tr-TR": [
            {"locale": "tr-TR", "accept_language": "tr-TR,tr;q=0.9,en;q=0.8", "timezone": "Europe/Istanbul"}
        ],
        "ro-RO": [
            {"locale": "ro-RO", "accept_language": "ro-RO,ro;q=0.9,en;q=0.8", "timezone": "Europe/Bucharest"}
        ]

    }

    USER_AGENT = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
    ]

    LANGUAGE_CODE_MAP = {
        "en": "en-US", "en-US": "en-US",
        "de": "de-DE", "de-DE": "de-DE",
        "fr": "fr-FR", "fr-FR": "fr-FR",
        "nl": "nl-NL", "nl-NL": "nl-NL",
        "es": "es-ES", "es-ES": "es-ES",
        "it": "it-IT", "it-IT": "it-IT",
        "nb": "nb-NO", "nb-NO": "nb-NO",
        "da": "da-DK", "da-DK": "da-DK",
        "sv": "sv-SE", "sv-SE": "sv-SE",
        "pl": "pl-PL", "pl-PL": "pl-PL",
        "fi": "fi-FI", "fi-FI": "fi-FI",
        "pt": "pt-PT", "pt-PT": "pt-PT",
        "tr": "tr-TR", "tr-TR": "tr-TR",
        "ro": "ro-RO", "ro-RO": "ro-RO",
    }
    IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif")

    PROXY_MAX_RETRIES = 3
    PROXY_KEY = "e038ffb988344353c4765d252c9cbd39"
    COOLDOWN_MIN = 300  # 5 分钟
    COOLDOWN_MAX = 600  # 10 分钟

    TASK_NUM = 20