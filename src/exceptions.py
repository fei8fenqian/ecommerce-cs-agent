"""
src/exceptions.py — 异常继承体系

整个项目的所有异常都从这里派生，不裸抛 Exception。

为什么需要这个文件：
1. 全局异常处理器（后面的 middleware）能精确识别异常类型，返回不同 HTTP 状态码
2. Agent Loop 里可以按异常类型决策——LLM 挂了重试、Tool 挂了降级、检索挂了兜底
3. 日志里一眼看到异常类型就知道是哪个模块出的问题

继承结构：
BaseAppException          — 所有异常的根，带 error_code + message
├── ConfigError           — 配置缺失（启动时校验用）
├── RetrievalError        — 检索失败（DB 挂了 / 结果为空 / 查询非法）
├── LLMError              — LLM API 调用失败（带重试次数，方便 loop 里判断）
├── ToolExecutionError    — Agent 工具执行失败（tool_name + 原始错误）
└── AgentLoopError        — Agent 循环异常（死循环 / 超步数 / 解析失败）

用法：
raise LLMError("DeepSeek API 返回 500", retry_count=3)
raise ToolExecutionError("check_stock", "数据库连接超时")
"""

from typing import Any


# 根异常
class BaseAppException(Exception):
    """
    所有业务异常的基类。

    属性：
    message:   给人看的错误描述（展示给前端 / 写进日志）
    error_code: 机器读的状态码（"RETRIEVAL_ERROR" / "LLM_ERROR" 等）
    detail:    可选的额外上下文，字典形式 {"table": "xxx", "query": "xxx"}
    """

    def __init__(
        self,
        message: str,
        error_code: str = "INTERNAL_ERROR",
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.detail = detail or {}

    def __str__(self):
        return f"{type(self).__name__} [{self.error_code}]: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON，给 FastAPI exception handler 用"""
        return {
            "error": self.error_code,
            "message": self.message,
            "detail": self.detail,
        }


# 配置异常
class ConfigError(BaseAppException):
    """
    .env 缺少必填项、环境变量格式错误、路径不存在。
    启动时抛出，直接退出——配置不对跑起来也没意义。
    """

    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message, error_code="CONFIG_ERROR", detail=detail)


# 检索异常
class RetrievalError(BaseAppException):
    """
    检索引擎返回空结果、查询语法错误、pgvector 不可用。

    Phase 3 的 RAG 引擎捕获此异常后：
        - recoverable=True  → 降级兜底（换检索策略再试）
        - recoverable=False → 硬错误（比如 pgvector 扩展没装），直接 503
    """

    def __init__(
        self,
        message: str,
        detail: dict[str, Any] | None = None,
        recoverable: bool = True,
    ):
        super().__init__(message, error_code="RETRIEVAL_ERROR", detail=detail)
        self.recoverable = recoverable


# LLM 异常
class LLMError(BaseAppException):
    """
    LLM API 调用失败。

    Agent Loop 根据状态码和重试次数决策：
        - status_code 401/403 → 鉴权问题，不重试
        - 其他错误 + retry_count < 3 → 等一等再试（exponential backoff）
        - retry_count >= 3 → 放弃，返回降级回答（直接给检索原文）

    属性：
        retry_count:  已重试次数
        status_code:  HTTP 状态码
        last_response: 最后一次 API 返回的原始错误
    """

    def __init__(
        self,
        message: str,
        retry_count: int = 0,
        status_code: int | None = None,
        last_response: str = "",
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message, error_code="LLM_ERROR", detail=detail)
        self.retry_count = retry_count
        self.status_code = status_code
        self.last_response = last_response

    @property
    def can_retry(self) -> bool:
        if self.status_code in (401, 403):
            return False
        return self.retry_count < 3


# 工具执行异常
class ToolExecutionError(BaseAppException):
    """
    Agent 工具调用时出错了。

    属性：
    tool_name: 哪个工具出的问题（"check_stock" / "search_order"）
    original_error: 原始异常，保留完整 traceback 用于排查
    """

    def __init__(
        self,
        tool_name: str,
        message: str,
        original_error: Exception | None = None,
        detail: dict[str, Any] | None = None,
    ):
        error_code = f"TOOL_ERROR/{tool_name.upper()}"
        super().__init__(f"[{tool_name}] {message}", error_code=error_code, detail=detail)
        self.tool_name = tool_name
        self.original_error = original_error


# Agent 循环异常
class AgentLoopError(BaseAppException):
    """
    Agent ReAct 循环自身的异常——不是工具挂了也不是 LLM 挂了，是循环逻辑出问题。

    场景：
    - max_steps 到了还没结束（任务太难）
    - LLM 输出的 Action 格式无法解析
    - 同一个 Tool 连续调了 3 次（幻觉循环）

    属性：
    step_count:  到了第几步出的问题
    last_action: 出问题前最后一次解析出的 Action
    reason:      原因标签 "max_steps" / "parse_error" / "tool_loop"
    """

    def __init__(
        self,
        message: str,
        step_count: int = 0,
        last_action: str = "",
        reason: str = "unknown",
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message, error_code="AGENT_LOOP_ERROR", detail=detail)
        self.step_count = step_count
        self.last_action = last_action
        self.reason = reason
