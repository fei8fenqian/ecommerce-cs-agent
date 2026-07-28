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

    # ---- LLM API（Phase 3 才用，先占位）----
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"

    # ---- 检索参数 ----
    retrieval_top_k: int = Field(default=20, ge=1, le=100, description="粗筛返回条数")
    rerank_top_k: int = Field(default=5, ge=1, le=20, description="精排后保留条数")

    # ---- Agent 参数 ----
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="llm输出温度")
    max_tokens: int = Field(default=1024, ge=0, le=16384, description="上下文最大tokens")
    max_same_tools: int = Field(default=5, ge=0, le=100, description="最大连续调用同一工具次数")
    max_steps: int = Field(default=5, ge=1, le=100, description="llm最大调用轮数")

    # ---- MCP Server 端点 ----
    mcp_servers: list[str] = []  # 如 ["http://localhost:8081/sse"]

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
