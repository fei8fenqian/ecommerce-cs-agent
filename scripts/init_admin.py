"""一次性创建首个管理员。

用法：
    ADMIN_USERNAME=bootstrap-admin \
    ADMIN_PASSWORD='由部署系统注入的强密码' \
    python scripts/init_admin.py

该命令假定数据库表已经由 Alembic 创建，不会自动建表，也不会覆盖已有管理员密码。
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from infra.db_pool import close_pool, init_pool  # noqa: E402
from store.user_store import create_initial_admin  # noqa: E402


async def _run() -> int:
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not username or not password:
        print("必须通过 ADMIN_USERNAME 和 ADMIN_PASSWORD 提供管理员凭据", file=sys.stderr)
        return 2

    await init_pool(minconn=1, maxconn=1)
    try:
        created = await create_initial_admin(username, password)
    finally:
        await close_pool()

    if created:
        print(f"管理员 {username!r} 初始化成功")
    else:
        print(f"管理员 {username!r} 已存在，原密码未修改")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
