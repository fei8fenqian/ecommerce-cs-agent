import logging

from casbin import Enforcer  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)

e: Enforcer | None = None


def init_casbin():
    """初始化RBAC"""
    global e
    e = Enforcer("casbin/model.conf", "casbin/policy.csv")
    logger.info("RBAC 已初始化")


def enforce(role, path, method):
    """鉴定权限"""
    if e is None:
        raise ValueError("先调用 init_casbin 初始化 RBAC")
    return e.enforce(role, path, method)
