import json
import os
import random
import datetime
import asyncio
import aiofiles

import aiohttp
from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeout
from playwright._impl._errors import Error as PlaywrightError
from typing import Optional