"""
src/config.py — 全局配置中心。

所有环境相关的配置（数据库、模型、API Key）都在这一个文件里，
其他地方不写死连接串和路径。

使用方式：
    from config import settings
    model = SentenceTransformer(settings.embedding_model)
"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ---- 运行环境 ----
    env: str = "dev"  # dev | staging | prod

    # ---- 开发/测试 demo 账号 ----
    # 默认关闭；生产环境禁止开启。密码只能通过环境变量注入。
    seed_demo_users: bool = False
    demo_admin_password: SecretStr = SecretStr("")
    demo_agent_password: SecretStr = SecretStr("")
    demo_operator_password: SecretStr = SecretStr("")
    demo_customer_password: SecretStr = SecretStr("")

    # ---- PostgreSQL / pgvector ----
    pg_host: str = "localhost"
    pg_port: int = 5433
    pg_user: str = "postgres"
    pg_password: SecretStr = SecretStr("")
    pg_dbname: str = "postgres"

    # ---- Embedding 模型 ----
    embedding_model: str = "BAAI/bge-large-zh-v1.5"
    embedding_dim: int = Field(
        default=1024,
        frozen=True,
        description="bge-large-zh-v1.5 输出 1024 维，换模型需重建索引",
    )

    # ---- LLM API ----
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = Field(default=10.0, gt=0, description="LLM 单次请求超时(秒)")
    llm_max_attempts: int = Field(default=2, ge=1, description="LLM 整个调用最多尝试次数")
    llm_retry_backoff_seconds: float = Field(default=0.5, ge=0, description="LLM 重试退避基数(秒)")
    llm_sdk_max_retries: int = Field(default=0, ge=0, description="LLM SDK 内部最大重试次数")
    llm_stream_timeout_seconds: float = Field(default=30.0, gt=0, description="LLM 流式调用超时(秒)")
    llm_circuit_failure_threshold: int = Field(default=3, ge=1, description="LLM 熔断连续失败阈值")
    llm_circuit_open_seconds: float = Field(default=30.0, gt=0, description="LLM 熔断冷却时间(秒)")

    # ---- 内部 Metrics 端点 ----
    metrics_bearer_token: SecretStr = SecretStr("")

    # ---- 检索参数 ----
    retrieval_top_k: int = Field(default=20, ge=1, le=100, description="粗筛返回条数")
    rerank_top_k: int = Field(default=5, ge=1, le=20, description="精排后保留条数")

    # ---- Agent 参数 ----
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="llm输出温度")
    max_tokens: int = Field(default=1024, ge=0, le=16384, description="上下文最大tokens")
    max_same_tools: int = Field(default=10, ge=0, le=100, description="最大连续调用同一工具次数")
    max_steps: int = Field(default=5, ge=1, le=100, description="llm最大调用轮数")

    # ---- MCP Server 端点 ----
    mcp_servers: list[str] = []  # 如 ["http://localhost:8081/sse"]
    mcp_connect_timeout_seconds: float = Field(default=5.0, gt=0, description="MCP 连接超时(秒)")
    mcp_list_tools_timeout_seconds: float = Field(default=5.0, gt=0, description="MCP 工具发现超时(秒)")
    mcp_call_timeout_seconds: float = Field(default=10.0, gt=0, description="MCP 工具调用超时(秒)")
    mcp_circuit_failure_threshold: int = Field(default=3, ge=1, description="MCP 熔断连续失败阈值")
    mcp_circuit_open_seconds: float = Field(default=30.0, gt=0, description="MCP 熔断冷却时间(秒)")

    # ---- PLAN and EXECUTE 参数 ----
    max_iterations: int = Field(default=3, ge=1, le=20, description="judge失败重试的最大次数")
    history_max_tokens: int = Field(default=100000, ge=1000, le=500000, description="对话历史截断阈值(token)")

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"
    session_ttl: int = Field(default=86400, ge=3600, le=2592000, description="会话过期时间(秒)，默认24小时")

    # ---- Rate limit ----
    rate_limit_login_per_minute: int = Field(default=5, ge=1, description="登录接口每 IP 每分钟最大请求数")
    rate_limit_chat_per_minute: int = Field(default=20, ge=1, description="普通聊天每用户/IP 每分钟最大请求数")
    rate_limit_chat_stream_per_minute: int = Field(
        default=10,
        ge=1,
        description="流式聊天每用户/IP 每分钟最大请求数",
    )

    class Config:
        # 从项目根目录的 .env 文件读取（环境变量优先级更高）
        env_file = str(Path(__file__).parent.parent / ".env")
        env_file_encoding = "utf-8"

    def validate(self) -> None:
        """启动时校验。缺必填配置直接退出，不带错误运行。"""
        missing = []
        if not self.pg_password.get_secret_value():
            missing.append("PG_PASSWORD")
        if not self.llm_api_key.get_secret_value():
            missing.append("LLM_API_KEY")

        if missing:
            raise SystemExit(f"缺少必要配置: {','.join(missing)}。当前 ENV={self.env}，请在环境变量或 .env 中设置。")


settings = Settings()
