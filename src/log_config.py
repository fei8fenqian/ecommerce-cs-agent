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

# 第 1 步：声明"协程安全的变量"
# 每个请求（asyncio Task）有自己独立的副本
# 请求 A 设 "abc123"，请求 B 设 "xyz789"，互不干扰
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> None:
    """middleware 调这个，把 request_id 注入当前协程"""
    _request_id_var.set(request_id)


def get_request_id() -> str:
    """任何下游代码调这个，拿到当前请求的 request_id"""
    return _request_id_var.get()


# 第 2 步：Filter —— 自动给每条日志打上 request_id
# logging.Filter 是"拦截器"：每条日志输出前都会调 filter(record)
# 这里做的事：把 contextvar 里的 request_id 塞到 record 上
class _RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


# 第 3 步：重写 Formatter —— 决定日志长什么样
# 默认 formatter 输出多行文本，格式化成单行 JSON
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),  # 什么时候
            "level": record.levelname,  # 什么级别
            "func": record.funcName,  # 那个函数
            "logger": record.name,  # 哪个模块
            "rid": getattr(record, "request_id", "-"),  # 哪个请求
            "msg": record.getMessage(),  # 拿 msg 模板和 args 实参自动拼好完整信息
        }
        # 如果有异常，也放进 JSON
        # record.exc_info — 元组 (异常类型, 异常实例, traceback对象)
        if record.exc_info and record.exc_info[0]:
            entry["exc"] = self.formatException(record.exc_info)

        # ensure_ascii=False → 中文不转义，直接输出汉字
        return json.dumps(entry, ensure_ascii=False)


# 第 4 步：启动时配置
def setup_logging(level: int = logging.INFO) -> None:
    """应用启动时调一次，配置全局日志"""

    root = logging.getLogger()  # 根 Logger
    root.setLevel(level)

    # 清掉旧 handler（uvicorn reload 会重复创建，不清就输出多次）
    for h in root.handlers[:]:
        root.removeHandler(h)

    # Handler 决定日志写到哪（这里是 stdout，uvicorn 能捕获）
    # Formatter 决定日志长什么样（你的 JSONFormatter 把日志变成单行 JSON）
    # Filter 是拦截器，每条日志输出前自动把 contextvar 里的 request_id 塞进去
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(_RequestIDFilter())
    root.addHandler(handler)

    # 压噪音（chromadb 一次查询打十几行 DEBUG 日志，太吵）
    for noisy in (
        "chromadb",
        "httpx",
        "sentence_transformers",
        "urllib3",
        "psycopg2",
        "watchfiles",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
