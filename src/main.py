from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent.loop import AgentLoop
from agent.session import SessionManager
from agent.tools import (
    check_stock,
    compare_products,
    create_ticket,
    search_product,
    track_order,
)
from agent.tools_registry import ToolRegistry
from api.chat import router
from api.middleware import RequestIDMiddleware
from config import settings
from core.db_pool import close_pool, init_pool
from core.llm_client import LLMClient
from log_config import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    setup_logging()
    init_pool()
    llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    registry = ToolRegistry()
    registry.register(search_product.SearchProduct())
    registry.register(check_stock.CheckStock())
    registry.register(track_order.TrackOrder())
    registry.register(create_ticket.CreateTicket())
    registry.register(compare_products.CompareProducts())
    agent = AgentLoop(llm, registry, max_steps=10)
    session = SessionManager()

    app.state.llm_client = llm
    app.state.registry = registry
    app.state.agent = agent
    app.state.session = session

    yield

    # shutdown
    close_pool()


app = FastAPI(title="极客数码 AI 客服", version="0.1.0", lifespan=lifespan)
app.include_router(router)

app.add_middleware(RequestIDMiddleware)


@app.get("/health")
async def health():
    return {"status": "OK"}
