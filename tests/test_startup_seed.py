"""生产启动与 demo 用户 seed 边界测试。"""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

import main


@pytest.mark.asyncio
async def test_demo_seed_is_not_called_when_disabled(monkeypatch):
    monkeypatch.setattr(main.settings, "seed_demo_users", False)
    seed = AsyncMock()

    with patch("main.seed_users", new=seed):
        await main._seed_demo_users_if_enabled()

    seed.assert_not_awaited()


@pytest.mark.asyncio
async def test_production_cannot_enable_demo_seed(monkeypatch):
    monkeypatch.setattr(main.settings, "env", "prod")
    monkeypatch.setattr(main.settings, "seed_demo_users", True)

    with pytest.raises(RuntimeError, match="禁止开启"):
        await main._seed_demo_users_if_enabled()


@pytest.mark.asyncio
async def test_non_production_demo_seed_requires_injected_passwords(monkeypatch):
    monkeypatch.setattr(main.settings, "env", "dev")
    monkeypatch.setattr(main.settings, "seed_demo_users", True)
    monkeypatch.setattr(main.settings, "demo_admin_password", SecretStr("dev-admin-password"))
    monkeypatch.setattr(main.settings, "demo_agent_password", SecretStr("dev-agent-password"))
    monkeypatch.setattr(main.settings, "demo_operator_password", SecretStr("dev-operator-password"))
    monkeypatch.setattr(main.settings, "demo_customer_password", SecretStr("dev-customer-password"))
    seed = AsyncMock()

    with patch("main.seed_users", new=seed):
        await main._seed_demo_users_if_enabled()

    seed.assert_awaited_once_with(
        (
            ("admin", "dev-admin-password", "admin"),
            ("agent", "dev-agent-password", "agent"),
            ("operator", "dev-operator-password", "operator"),
            ("customer", "dev-customer-password", "customer"),
        )
    )
