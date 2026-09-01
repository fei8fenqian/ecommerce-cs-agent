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
    rag_device: str = "auto"  # auto | cpu | cuda | cuda:0
    embedding_dim: int = Field(
        default=1024,
        frozen=True,
        description="bge-large-zh-v1.5 输出 1024 维，换模型需重建索引",
    )

    # ---- LLM API ----
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    intent_llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = Field(default=10.0, gt=0, description="LLM 单次请求超时(秒)")
    llm_max_attempts: int = Field(default=2, ge=1, description="LLM 整个调用最多尝试次数")
    llm_retry_backoff_seconds: float = Field(default=0.5, ge=0, description="LLM 重试退避基数(秒)")
    llm_sdk_max_retries: int = Field(default=0, ge=0, description="LLM SDK 内部最大重试次数")
    llm_stream_timeout_seconds: float = Field(default=30.0, gt=0, description="LLM 流式调用超时(秒)")
    llm_circuit_failure_threshold: int = Field(default=3, ge=1, description="LLM 熔断连续失败阈值")
    llm_circuit_open_seconds: float = Field(default=30.0, gt=0, description="LLM 熔断冷却时间(秒)")

    # ---- 支付宝沙箱（本地/演示环境） ----
    # 密钥只保存为本机文件路径；不能写入代码、Git 或日志。
    alipay_sandbox_app_id: str = ""
    alipay_sandbox_seller_id: str = ""
    # 支付宝沙箱同时存在新旧网关；旧地址对电脑网站支付兼容性更稳，仍可通过
    # ALIPAY_SANDBOX_GATEWAY 显式切换到新地址进行对照测试。
    alipay_sandbox_gateway: str = "https://openapi.alipaydev.com/gateway.do"
    alipay_sandbox_app_private_key_path: str = ""
    alipay_sandbox_public_key_path: str = ""
    alipay_sandbox_notify_url: str = ""
    alipay_sandbox_return_url: str = ""
    alipay_sandbox_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=60,
        description="支付宝沙箱网关单次请求超时(秒)",
    )

    # ---- UnionPay 5.1.0 U0 protocol + U1 checkout test ----
    # U0 CLI remains independent; U1 checkout uses the same protocol settings.
    unionpay_mer_id: str = ""
    unionpay_sign_cert_path: str = ".secrets/unionpay/acp_test_sign.pfx"
    unionpay_sign_cert_password: SecretStr = SecretStr("")
    unionpay_root_cert_path: str = ".secrets/unionpay/acp_test_root.cer"
    unionpay_middle_cert_path: str = ".secrets/unionpay/acp_test_middle.cer"
    unionpay_encrypt_cert_path: str = ".secrets/unionpay/acp_test_enc.cer"
    # 本地 Vite 使用 127.0.0.1；避免 UnionPay 回跳后因 origin 不同丢失
    # sessionStorage 中的客户登录态。最终回跳仍经过 checkout service 的 allowlist。
    unionpay_front_url: str = "http://127.0.0.1:5173/"
    # U1.1 银联前台回跳只允许配置公网 FastAPI base URL；空值时禁止生成 U1.1 支付表单。
    unionpay_public_base_url: str = ""
    # 银联官方接口说明：不需要后台通知时可固定上送该地址。
    unionpay_back_url: str = "http://www.specialUrl.com"
    # 保持现有本地 .env 的 *_GATEWAY 命名，避免配置升级时把未知键当成错误。
    unionpay_front_gateway: str = "https://gateway.test.95516.com/gateway/api/frontTransReq.do"
    unionpay_query_gateway: str = "https://gateway.test.95516.com/gateway/api/queryTrans.do"
    unionpay_back_gateway: str = "https://gateway.test.95516.com/gateway/api/backTransReq.do"
    unionpay_timeout_seconds: float = Field(default=30.0, gt=0, le=60)

    # ---- 内部 Metrics 端点 ----
    metrics_bearer_token: SecretStr = SecretStr("")

    # ---- 检索参数 ----
    retrieval_top_k: int = Field(default=20, ge=1, le=100, description="粗筛返回条数")
    rerank_top_k: int = Field(default=5, ge=1, le=20, description="精排后保留条数")
    pre_rag_similarity_threshold: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="Pre-RAG 向量相似度最低阈值；低于阈值的知识不注入 Router",
    )

    # ---- Agent 参数 ----
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="llm输出温度")
    max_tokens: int = Field(default=2048, ge=0, le=16384, description="单次模型输出最大 tokens")
    max_same_tools: int = Field(default=10, ge=0, le=100, description="最大连续调用同一工具次数")
    max_steps: int = Field(default=5, ge=1, le=100, description="llm最大调用轮数")

    # ---- 工单 Agent ----
    # 默认关闭，避免旧环境在未明确启用时自动处理历史工单。
    ai_ticket_worker_enabled: bool = False
    ai_ticket_worker_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    ai_ticket_claim_timeout_seconds: int = Field(default=120, ge=30, le=3600)

    # ---- 客服人工升级通知 ----
    # 默认不配置飞书，工单仍可在本系统客服工作台中处理。
    feishu_duty_webhook_url: str = ""
    # 使用 SPA 根入口，避免静态服务器未配置 /after-sales rewrite 时深链 404。
    feishu_workbench_url: str = "http://localhost:5173/?page=login&next=tickets"
    feishu_webhook_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    feishu_escalation_worker_enabled: bool = False
    feishu_escalation_worker_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    feishu_escalation_max_attempts: int = Field(default=5, ge=1, le=20)
    feishu_escalation_claim_timeout_seconds: int = Field(default=60, ge=10, le=3600)

    # ---- 财务异常扫描 ----
    finance_anomaly_timeout_minutes: int = Field(default=30, ge=5, le=1440)

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
    auth_session_ttl_seconds: int = Field(
        default=2_592_000,
        ge=3600,
        le=7_776_000,
        description="浏览器登录态有效期(秒)，默认 30 天",
    )

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

    def validate_required_config(self) -> None:
        """启动时校验。缺必填配置直接退出，不带错误运行。"""
        missing = []
        if not self.pg_password.get_secret_value():
            missing.append("PG_PASSWORD")
        if not self.llm_api_key.get_secret_value():
            missing.append("LLM_API_KEY")

        if missing:
            raise SystemExit(f"缺少必要配置: {','.join(missing)}。当前 ENV={self.env}，请在环境变量或 .env 中设置。")


settings = Settings()
