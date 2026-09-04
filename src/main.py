import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.engines.loop import AgentLoop
from agent.engines.plan_execute import PlanAndExecuteAgent
from agent.engines.support_workflow import SupportWorkflowAgent
from agent.llm.intent_router import IntentRouter
from agent.llm.llm_client import LLMClient
from agent.llm.session import SessionManager
from agent.mcp_tool import MCPClientManager, MCPTool
from agent.rag.retrieve import warmup_customer_catalog_retrieval
from agent.ticket_resolution import TicketResolutionAgent, TicketResolutionWorker
from agent.tools import (
    check_after_sales,
    check_payment_status,
    check_refund_eligibility,
    check_stock,
    compare_products,
    create_ticket,
    present_product_candidates,
    query_refund_status,
    search_component,
    search_knowledge,
    search_product,
    track_order,
)
from agent.tools_registry import ToolRegistry
from api.auth import auth_router
from api.cart import cart_router
from api.chat import chat_router
from api.checkout import checkout_router
from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from api.fulfillments import fulfillment_router
from api.health import health_router
from api.metrics import metrics_router
from api.orders import order_router
from api.payments import payment_router
from api.products import product_router
from api.session import session_router
from api.tickets import ticket_router
from config import settings
from exceptions import BaseAppException
from infra.casbin_enforcer import init_casbin
from infra.circuit_breaker import CircuitBreaker
from infra.db_pool import close_pool, init_pool
from infra.feishu_notifier import build_duty_notifier
from infra.redis_client import close_redis, health_check, init_redis
from log_config import setup_logging
from middleware.auth import AuthMiddleware
from middleware.metrics import MetricsMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIDMiddleware
from service.support_case_service import SupportCaseService
from service.ticket_escalation_worker import TicketEscalationNotificationWorker
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
    intent_llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.intent_llm_model,
        timeout=settings.llm_timeout_seconds,
        max_attempts=settings.llm_max_attempts,
        retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        sdk_max_retries=settings.llm_sdk_max_retries,
        stream_timeout=settings.llm_stream_timeout_seconds,
        circuit_breaker=CircuitBreaker(
            failure_threshold=settings.llm_circuit_failure_threshold,
            open_seconds=settings.llm_circuit_open_seconds,
        ),
    )
    intent_router = IntentRouter(intent_llm)
    registry = ToolRegistry()
    registry.register(search_product.SearchProduct())
    registry.register(search_knowledge.SearchKnowledge())
    registry.register(check_stock.CheckStock())
    registry.register(track_order.TrackOrder())
    registry.register(check_payment_status.CheckPaymentStatus())
    registry.register(query_refund_status.QueryRefundStatus())
    registry.register(check_refund_eligibility.CheckRefundEligibility())
    registry.register(check_after_sales.CheckAfterSales())
    registry.register(create_ticket.CreateTicket())
    registry.register(compare_products.CompareProducts())
    registry.register(present_product_candidates.PresentProductCandidates())
    registry.register(search_component.SearchComponent())
    plan_execute_agent = PlanAndExecuteAgent(
        llm,
        registry,
        max_iterations=settings.max_iterations,
    )
    agent = AgentLoop(llm, registry, max_steps=settings.max_steps)
    support_workflow_agent = SupportWorkflowAgent(agent, registry)
    support_case_service = SupportCaseService()
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
    app.state.intent_llm_client = intent_llm
    app.state.intent_router = intent_router
    app.state.registry = registry
    app.state.plan_execute_agent = plan_execute_agent
    app.state.support_workflow_agent = support_workflow_agent
    app.state.support_case_service = support_case_service
    app.state.agent = agent
    app.state.session = session
    app.state.mcp_managers = mcp_managers

    # 把首次商品咨询的模型/索引冷启动移到服务启动期；失败只记录，不阻塞基础服务。
    try:
        await warmup_customer_catalog_retrieval()
    except Exception:
        _logger.warning("customer catalog retrieval warmup failed")

    ticket_worker_task: asyncio.Task[None] | None = None
    escalation_worker_task: asyncio.Task[None] | None = None
    if settings.ai_ticket_worker_enabled:
        ticket_resolution_agent = TicketResolutionAgent(
            llm,
            claim_timeout_seconds=settings.ai_ticket_claim_timeout_seconds,
        )
        ticket_worker = TicketResolutionWorker(
            ticket_resolution_agent,
            interval_seconds=settings.ai_ticket_worker_interval_seconds,
        )
        ticket_worker_task = asyncio.create_task(ticket_worker.run(), name="ai-ticket-worker")
        app.state.ticket_resolution_worker = ticket_worker
        _logger.info("AI ticket worker enabled")

    if settings.feishu_escalation_worker_enabled:
        escalation_worker = TicketEscalationNotificationWorker(
            build_duty_notifier(),
            interval_seconds=settings.feishu_escalation_worker_interval_seconds,
            max_attempts=settings.feishu_escalation_max_attempts,
            claim_timeout_seconds=settings.feishu_escalation_claim_timeout_seconds,
        )
        escalation_worker_task = asyncio.create_task(
            escalation_worker.run(),
            name="ticket-escalation-notification-worker",
        )
        app.state.ticket_escalation_worker = escalation_worker
        _logger.info("ticket escalation notification worker enabled")

    yield

    # shutdown
    if ticket_worker_task is not None:
        ticket_worker_task.cancel()
        try:
            await ticket_worker_task
        except asyncio.CancelledError:
            pass
    if escalation_worker_task is not None:
        escalation_worker_task.cancel()
        try:
            await escalation_worker_task
        except asyncio.CancelledError:
            pass
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
app.include_router(product_router)
app.include_router(checkout_router)
app.include_router(cart_router)
app.include_router(payment_router)
app.include_router(order_router)
app.include_router(fulfillment_router)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(auth_router)


app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(MetricsMiddleware)
