from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from exceptions import BaseAppException
from log_config import get_request_id

HTTP_EXCEPTION_MAP = {
    400: "INVALID_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "FORBIDDEN",
    404: "RESOURCE_NOT_AVAILABLE",
    409: "INVALID_STATE",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "DEPENDENCY_UNAVAILABLE",
}

NOT_FOUND_MESSAGE = "资源不可用或无法核验"
AUTHENTICATION_REQUIRED_MESSAGE = "需要登录后访问"
TOKEN_INVALID_MESSAGE = "登录状态无效，请重新登录"
INTERNAL_ERROR_MESSAGE = "服务器内部错误，请稍后重试"
DEPENDENCY_UNAVAILABLE_MESSAGE = "服务暂时不可用，请稍后重试"

BUSINESS_422_MESSAGE_MAP = {
    "ORDER_NOT_ELIGIBLE": "订单不符合退款条件",
    "EVIDENCE_REQUIRED": "请补充必要证据",
    "QUOTE_EXPIRED": "退款报价已过期",
}

VALIDATION_REASON_MAP = {
    "missing": "缺少必填字段",
    "int_parsing": "必须是整数",
    "string_type": "必须是字符串",
    "json_invalid": "JSON 格式不正确",
    "list_type": "必须是列表",
    "dict_type": "必须是对象",
}

APP_EXCEPTION_STATUS_MAP = {
    "AUTHENTICATION_ERROR": 401,
    "RETRIEVAL_ERROR": 503,
    "LLM_ERROR": 503,
    "TOOL_ERROR": 503,
    "DEPENDENCY_UNAVAILABLE": 503,
    "AGENT_LOOP_ERROR": 500,
    "INTERNAL_ERROR": 500,
}

APP_EXCEPTION_MESSAGE_MAP = {
    "AUTHENTICATION_ERROR": "认证失败，请重新登录",
    "RETRIEVAL_ERROR": "检索服务暂时不可用",
    "LLM_ERROR": "智能服务暂时不可用",
    "TOOL_ERROR": "相关服务暂时不可用",
    "DEPENDENCY_UNAVAILABLE": "依赖服务暂时不可用，请稍后重试",
    "AGENT_LOOP_ERROR": "服务器内部错误，请稍后重试",
    "INTERNAL_ERROR": "服务器内部错误，请稍后重试",
}


def error_response_body(code: str, message: str, details: dict, request_id: str) -> dict[str, Any]:
    """异常统一返回格式"""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


async def handle_http_exceptions(request: Request, exc: HTTPException) -> JSONResponse:
    """http异常处理"""
    request_id = get_request_id()
    status_code = exc.status_code
    code = HTTP_EXCEPTION_MAP.get(exc.status_code, "INTERNAL_ERROR")

    if exc.status_code == 404:
        code = "RESOURCE_NOT_AVAILABLE"
        message = NOT_FOUND_MESSAGE
    elif exc.status_code == 401:
        missing_authorization = isinstance(exc.detail, str) and exc.detail == "缺少 Authorization header"
        if request.url.path == "/api/v1/auth/login" or missing_authorization:
            code = "AUTHENTICATION_REQUIRED"
            message = "账号或密码错误" if request.url.path == "/api/v1/auth/login" else AUTHENTICATION_REQUIRED_MESSAGE
        else:
            code = "TOKEN_INVALID"
            message = TOKEN_INVALID_MESSAGE
    elif exc.status_code == 422:
        business_code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
        if business_code is not None and business_code in BUSINESS_422_MESSAGE_MAP:
            code = business_code
            message = BUSINESS_422_MESSAGE_MAP[business_code]
        else:
            code = "INTERNAL_ERROR"
            message = INTERNAL_ERROR_MESSAGE
            status_code = 500
    elif exc.status_code == 500:
        code = "INTERNAL_ERROR"
        message = INTERNAL_ERROR_MESSAGE
    elif exc.status_code == 503:
        code = "DEPENDENCY_UNAVAILABLE"
        message = DEPENDENCY_UNAVAILABLE_MESSAGE
    elif exc.status_code == 429:
        code = "RATE_LIMITED"
        message = "请求过于频繁，请稍后重试"
    elif isinstance(exc.detail, str) and exc.detail:
        message = exc.detail
    else:
        message = "请求处理失败"

    content = error_response_body(code, message, {}, request_id)
    return JSONResponse(content=content, status_code=status_code)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """校验处理"""
    request_id = get_request_id()
    code = "INVALID_REQUEST"
    message = "请求参数无效"
    details = validation_details(exc)
    content = error_response_body(code, message, details, request_id)
    return JSONResponse(content=content, status_code=400)


def validation_details(
    exc: RequestValidationError,
) -> dict[str, Any]:
    errors = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err.get("loc", ()))
        err_type = err.get("type", "")
        reason = VALIDATION_REASON_MAP.get(err_type, "字段格式不正确")
        errors.append({"field": field, "reason": reason})
    return {"fields": errors}


async def handle_app_exception(request: Request, exc: BaseAppException) -> JSONResponse:
    exception_code = "TOOL_ERROR" if exc.error_code.startswith("TOOL_ERROR/") else exc.error_code
    status_code = APP_EXCEPTION_STATUS_MAP.get(exception_code, 500)
    code = HTTP_EXCEPTION_MAP.get(status_code, "INTERNAL_ERROR")
    message = APP_EXCEPTION_MESSAGE_MAP.get(exception_code, "服务器内部错误，请稍后重试")
    request_id = get_request_id()
    content = error_response_body(code, message, {}, request_id)
    return JSONResponse(content=content, status_code=status_code)


async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    request_id = get_request_id()
    content = error_response_body(
        "INTERNAL_ERROR",
        "服务器内部错误，请稍后重试",
        {},
        request_id,
    )
    return JSONResponse(content=content, status_code=500)
