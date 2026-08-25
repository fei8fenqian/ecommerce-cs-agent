"""一次性初始化本地演示用内部账号。

用法：
    INTERNAL_USER_PASSWORD='仅用于本地演示的密码' \
    python scripts/init_internal_users.py

默认创建四个互不影响业务角色的演示账号：
    admin-demo、agent-demo、operator-demo、finance-demo

可通过 DEMO_ADMIN_USERNAME、DEMO_AGENT_USERNAME、DEMO_OPERATOR_USERNAME、
DEMO_FINANCE_USERNAME 覆盖用户名。命令假定数据库表已经由 Alembic 创建，
不会自动建表，也不会覆盖同名账号的密码。
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from infra.db_pool import close_pool, init_pool  # noqa: E402
from store.user_store import create_initial_internal_user  # noqa: E402

_DEMO_USERS = (
    ("admin", "DEMO_ADMIN_USERNAME", "admin-demo"),
    ("agent", "DEMO_AGENT_USERNAME", "agent-demo"),
    ("operator", "DEMO_OPERATOR_USERNAME", "operator-demo"),
    ("finance", "DEMO_FINANCE_USERNAME", "finance-demo"),
)


async def _run() -> int:
    """从环境变量读取演示密码并初始化内部角色账号。"""
    password = os.environ.get("INTERNAL_USER_PASSWORD", "")
    if not password:
        print("必须通过 INTERNAL_USER_PASSWORD 提供演示账号密码", file=sys.stderr)
        return 2

    users = [(role, os.environ.get(env_name, default_username)) for role, env_name, default_username in _DEMO_USERS]

    await init_pool(minconn=1, maxconn=1)
    try:
        results = [
            (
                role,
                username,
                await create_initial_internal_user(username, password, role),
            )
            for role, username in users
        ]
    finally:
        await close_pool()

    for role, username, created in results:
        state = "初始化成功" if created else "已存在，原密码未修改"
        print(f"{role} 账号 {username!r}{state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
