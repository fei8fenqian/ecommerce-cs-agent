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

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# ---- 1. 系统依赖 ----
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

# ---- 2. 应用与迁移代码 ----
COPY pyproject.toml .
COPY requirements-demo-cpu.txt .
COPY src/ src/
COPY casbin/ casbin/
COPY alembic/ alembic/
COPY alembic.ini ./
COPY scripts/init_admin.py scripts/init_admin.py

# 从 pyproject.toml 安装唯一事实来源中的运行依赖；不要维护第二份手写依赖列表。
RUN pip install --no-cache-dir --timeout 300 --retries 5 -r requirements-demo-cpu.txt \
    && pip install --no-cache-dir --timeout 300 --retries 5 --no-deps . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app

# JWT 密钥由部署环境以只读 volume 注入，绝不写入镜像或 Git 仓库。
USER appuser

# ---- 3. 启动 ----
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
