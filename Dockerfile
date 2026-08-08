# =============================================================================
# Dockerfile — 3C 智能客服 Agent 生产镜像
#
# 构建:  docker build -t cs-agent:latest .
# 运行:  docker-compose up -d
#
# 层缓存策略:
#   pyproject.toml 变动少 → 放前面，改了依赖才重装
#   src/ 变动频繁     → 放后面，改了代码只重建 COPY 层
# =============================================================================

FROM python:3.12-slim

WORKDIR /app

# ---- 1. 系统依赖 ----
RUN apt-get update && rm -rf /var/lib/apt/lists/*

# ---- 2. pip 依赖（这层缓存住，改了代码不会重装）----
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    fastapi>=0.110 \
    "openai>=1.30" \
    "psycopg[binary,pool]>=3.2" \
    "pydantic>=2.5" \
    "pydantic-settings>=2.1" \
    "sentence-transformers>=3.0" \
    "langgraph>=1.0" \
    "mcp>=1.0" \
    "tiktoken>=0.7" \
    "redis>=5.0" \
    uvicorn \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# ---- 3. 代码（最常变，放最后）----
COPY src/ src/

# ---- 4. 启动 ----
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
