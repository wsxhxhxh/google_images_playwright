import os
import time
import logging
import threading
from dotenv import load_dotenv
from logging.handlers import TimedRotatingFileHandler

load_dotenv()

from contextlib import contextmanager
class TaskNameFilter(logging.Filter):
    def filter(self, record):
        thread_name = threading.current_thread().name
        record.task_name = thread_name if thread_name else "Main"
        return True


formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(task_name)s] %(message)s')
directory_path = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(directory_path, "logs"), exist_ok=True)
file_handler = TimedRotatingFileHandler(
    filename=os.path.join(directory_path, 'logs/crawl_google_us_async.log'),
    when='midnight',
    interval=1,
    backupCount=99
)
file_handler.setFormatter(formatter)
try:
    if file_handler.stream and hasattr(file_handler.stream, "reconfigure"):
        file_handler.stream.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
try:
    if stream_handler.stream and hasattr(stream_handler.stream, "reconfigure"):
        stream_handler.stream.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logger = logging.getLogger()
logger.addFilter(TaskNameFilter())
logger.setLevel(logging.INFO)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)



special_logger = logging.getLogger("special_log")
special_logger.setLevel(logging.INFO)

special_handler = logging.FileHandler(
    os.path.join(directory_path, "logs/special.log"),
    encoding="utf-8"
)

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

special_handler.setFormatter(formatter)

special_logger.addHandler(special_handler)

# 关键：禁止传播到 root logger
special_logger.propagate = False


data_logger = logging.getLogger("data_log")
data_logger.setLevel(logging.INFO)

data_handler = logging.FileHandler(
    os.path.join(directory_path, "logs/send_data.log"),
    encoding="utf-8"
)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

data_handler.setFormatter(formatter)

data_logger.addHandler(data_handler)

# 关键：禁止传播到 root logger
data_logger.propagate = False


@contextmanager
def log_timing(worker_id: int, action: str):
    start = time.time()
    logger.info(f"[Worker-{worker_id}] START {action}")

    try:
        yield
    finally:
        cost = round(time.time() - start, 2)
        logger.info(f"[Worker-{worker_id}] END {action} | cost={cost}s")