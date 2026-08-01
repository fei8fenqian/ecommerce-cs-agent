"""
结构化日志模块
==============
把日志从"给人看的多行文本"变成"给机器看的单行 JSON"。
每条日志带 request_id，出问题时能把一次请求的所有日志串起来。
"""

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

# ContextVar：每个 asyncio Task 有独立的副本，请求间互不干扰
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> None:
    """middleware 调这个，把 request_id 注入当前协程"""
    _request_id_var.set(request_id)


def get_request_id() -> str:
    """任何下游代码调这个，拿到当前请求的 request_id"""
    return _request_id_var.get()


# Filter：每条日志输出前自动把 contextvar 里的 request_id 塞到 record 上
class _RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


# JSON Formatter：把日志格式化成单行 JSON
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "func": record.funcName,
            "logger": record.name,
            "rid": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exc"] = self.formatException(record.exc_info)

        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """应用启动时调用，配置全局 JSON 日志。"""

    root = logging.getLogger()
    root.setLevel(level)

    # 清掉旧 handler，避免 uvicorn reload 时重复输出
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(_RequestIDFilter())
    root.addHandler(handler)

    # 抑制第三方库的 DEBUG 日志噪音
    for noisy in (
        "chromadb",
        "httpx",
        "sentence_transformers",
        "urllib3",
        "psycopg2",
        "watchfiles",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
