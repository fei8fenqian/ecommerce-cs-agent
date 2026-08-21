import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.engines.loop import AgentLoop
from agent.engines.plan_execute import PlanAndExecuteAgent
from agent.llm.intent_router import IntentRouter
from agent.llm.llm_client import LLMClient
from agent.llm.session import SessionManager
from agent.mcp_tool import MCPClientManager, MCPTool
from agent.tools import (
    check_stock,
    compare_products,
    create_ticket,
    search_component,
    search_product,
    track_order,
)
from agent.tools_registry import ToolRegistry
from api.auth import auth_router
from api.chat import chat_router
from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from api.health import health_router
from api.metrics import metrics_router
from api.session import session_router
from api.tickets import ticket_router
from config import settings
from exceptions import BaseAppException
from infra.casbin_enforcer import init_casbin
from infra.circuit_breaker import CircuitBreaker
from infra.db_pool import close_pool, init_pool
from infra.redis_client import close_redis, health_check, init_redis
from log_config import setup_logging
from middleware.auth import AuthMiddleware
from middleware.metrics import MetricsMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIDMiddleware
from store.user_store import seed_users

_logger = logging.getLogger(__name__)


async def _seed_demo_users_if_enabled() -> None:
    """仅在显式开启且非生产环境时插入 demo 用户。"""
    if not settings.seed_demo_users:
        return
    if settings.env.lower() == "prod":
        raise RuntimeError("生产环境禁止开启 SEED_DEMO_USERS")

    await seed_users(
        (
            ("admin", settings.demo_admin_password.get_secret_value(), "admin"),
            ("agent", settings.demo_agent_password.get_secret_value(), "agent"),
            (
                "operator",
                settings.demo_operator_password.get_secret_value(),
                "operator",
            ),
            (
                "customer",
                settings.demo_customer_password.get_secret_value(),
                "customer",
            ),
        )
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    setup_logging()
    await init_pool()
    init_redis()
    await health_check()
    await _seed_demo_users_if_enabled()
    init_casbin()
    llm_circuit_breaker = CircuitBreaker(
        failure_threshold=settings.llm_circuit_failure_threshold,
        open_seconds=settings.llm_circuit_open_seconds,
    )
    llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_attempts=settings.llm_max_attempts,
        retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        sdk_max_retries=settings.llm_sdk_max_retries,
        stream_timeout=settings.llm_stream_timeout_seconds,
        circuit_breaker=llm_circuit_breaker,
    )
    intent_router = IntentRouter(llm)
    registry = ToolRegistry()
    registry.register(search_product.SearchProduct())
    registry.register(check_stock.CheckStock())
    registry.register(track_order.TrackOrder())
    registry.register(create_ticket.CreateTicket())
    registry.register(compare_products.CompareProducts())
    registry.register(search_component.SearchComponent())
    plan_execute_agent = PlanAndExecuteAgent(
        llm,
        registry,
        max_iterations=settings.max_iterations,
    )
    agent = AgentLoop(llm, registry, max_steps=settings.max_steps)
    session = SessionManager()

    mcp_managers: list[MCPClientManager] = []
    for url in settings.mcp_servers:
        manager = MCPClientManager(
            url,
            connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
            list_tools_timeout_seconds=settings.mcp_list_tools_timeout_seconds,
            call_timeout_seconds=settings.mcp_call_timeout_seconds,
            circuit_breaker=CircuitBreaker(
                failure_threshold=settings.mcp_circuit_failure_threshold,
                open_seconds=settings.mcp_circuit_open_seconds,
            ),
        )
        try:
            await manager.connect()
            for tool_info in await manager.list_tools():
                registry.register(MCPTool(manager, tool_info))
            mcp_managers.append(manager)
        except Exception:
            # 单个 MCP 不可用时跳过它，不能阻塞整个 HTTP 服务启动。
            _logger.warning("MCP server unavailable during startup", extra={"reason": "startup_failed"})
            await manager.disconnect()

    app.state.llm_client = llm
    app.state.intent_router = intent_router
    app.state.registry = registry
    app.state.plan_execute_agent = plan_execute_agent
    app.state.agent = agent
    app.state.session = session
    app.state.mcp_managers = mcp_managers

    yield

    # shutdown
    await close_pool()
    await close_redis()
    for manager in mcp_managers:
        await manager.disconnect()


app = FastAPI(title="极客数码 AI 客服", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
app.add_exception_handler(RequestValidationError, handle_validation_error)
app.add_exception_handler(BaseAppException, handle_app_exception)
app.add_exception_handler(Exception, handle_unexpected_exception)
app.include_router(chat_router)
app.include_router(session_router)
app.include_router(ticket_router)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(auth_router)


app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(MetricsMiddleware)
