"""tests/test_auth_casbin.py — Casbin RBAC 策略测试"""

import pytest

from infra.casbin_enforcer import enforce, init_casbin


@pytest.fixture(autouse=True)
def _init():
    init_casbin()


class TestCustomer:
    def test_can_chat(self):
        assert enforce("customer", "/api/v1/chat", "POST")

    def test_can_view_products(self):
        assert enforce("customer", "/api/v1/products/123", "GET")

    def test_can_view_own_orders(self):
        assert enforce("customer", "/api/v1/orders/my/456", "GET")

    def test_can_view_own_tickets(self):
        assert enforce("customer", "/api/v1/tickets", "GET")
        assert enforce("customer", "/api/v1/tickets/789", "GET")

    def test_can_update_own_ticket(self):
        assert enforce("customer", "/api/v1/tickets/789", "PATCH")

    def test_cannot_view_all_orders(self):
        assert not enforce("customer", "/api/v1/orders/456", "GET")

    def test_cannot_handle_tickets(self):
        assert not enforce("customer", "/api/v1/tickets/789", "PUT")

    def test_cannot_manage_products(self):
        assert not enforce("customer", "/api/v1/products/123", "POST")
        assert not enforce("customer", "/api/v1/products/123", "DELETE")


class TestAgent:
    def test_can_chat(self):
        assert enforce("agent", "/api/v1/chat", "POST")

    def test_can_view_all_orders(self):
        assert enforce("agent", "/api/v1/orders/123", "GET")

    def test_can_claim_and_update_tickets(self):
        assert enforce("agent", "/api/v1/tickets/789/claim", "POST")
        assert enforce("agent", "/api/v1/tickets/789", "PATCH")

    def test_cannot_process_refund(self):
        assert not enforce("agent", "/api/v1/refunds/ORD-001", "POST")

    def test_cannot_manage_products(self):
        assert not enforce("agent", "/api/v1/products/123", "POST")


class TestOperator:
    def test_can_manage_products(self):
        assert enforce("operator", "/api/v1/products/123", "POST")
        assert enforce("operator", "/api/v1/products/123", "PUT")
        assert enforce("operator", "/api/v1/products/123", "DELETE")

    def test_can_manage_campaigns(self):
        assert enforce("operator", "/api/v1/campaigns/spring", "GET")
        assert enforce("operator", "/api/v1/campaigns/spring", "POST")

    def test_can_view_reports(self):
        assert enforce("operator", "/api/v1/reports/sales", "GET")

    def test_cannot_refund(self):
        assert not enforce("operator", "/api/v1/refunds/ORD-001", "POST")


class TestAdmin:
    def test_can_do_anything(self):
        assert enforce("admin", "/api/v1/anything/at/all", "GET")
        assert enforce("admin", "/api/v1/orders/999", "PATCH")
        assert enforce("admin", "/api/v1/admin/users/42", "DELETE")

    def test_wildcard_works(self):
        assert enforce("admin", "/api/v1", "POST")
        assert enforce("admin", "/deep/nested/path/to/resource", "CUSTOM_METHOD")


class TestEdgeCases:
    def test_nonexistent_role_denied(self):
        assert not enforce("hacker", "/api/v1/chat", "POST")
        assert not enforce("", "/api/v1/chat", "POST")

    def test_unlisted_path_denied_for_non_admin(self):
        assert not enforce("agent", "/api/v1/admin/users/42", "DELETE")
        assert not enforce("operator", "/api/v1/admin/users/42", "DELETE")
