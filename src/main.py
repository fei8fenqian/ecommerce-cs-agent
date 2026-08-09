from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent.llm.intent_router import IntentRouter
from agent.llm.llm_client import LLMClient
from agent.loop import AgentLoop
from agent.mcp_tool import MCPClientManager, MCPTool
from agent.plan_execute import PlanAndExecuteAgent
from agent.session import SessionManager
from agent.tools import (
    check_stock,
    compare_products,
    create_ticket,
    search_component,
    search_product,
    track_order,
)
from agent.tools_registry import ToolRegistry
from api.chat import chat_router
from api.health import health_router
from api.middleware import RequestIDMiddleware
from api.session import session_router
from api.tickets import ticket_router
from config import settings
from infra.db_pool import close_pool, init_pool
from log_config import setup_logging
from store.ticket_store import init_table


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    setup_logging()
    await init_pool()
    await init_table()
    llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
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
    await session.health_check()

    mcp_managers: list[MCPClientManager] = []
    for url in settings.mcp_servers:
        manager = MCPClientManager(url)
        await manager.connect()
        for tool_info in await manager.list_tools():
            registry.register(MCPTool(manager, tool_info))
        mcp_managers.append(manager)

    app.state.llm_client = llm
    app.state.intent_router = intent_router
    app.state.registry = registry
    app.state.plan_execute_agent = plan_execute_agent
    app.state.agent = agent
    app.state.session = session
    app.state.mcp_managers = mcp_managers

    yield

    # shutdown
    await session.close()
    await close_pool()
    for manager in mcp_managers:
        await manager.disconnect()


app = FastAPI(title="极客数码 AI 客服", version="0.1.0", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(session_router)
app.include_router(ticket_router)
app.include_router(health_router)

app.add_middleware(RequestIDMiddleware)
