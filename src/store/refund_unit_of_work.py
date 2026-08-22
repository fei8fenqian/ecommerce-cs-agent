"""退款领域的单连接、单事务 Unit of Work。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Protocol

from infra.db_pool import get_connection, put_connection
from store.after_sale_store import AfterSaleRepository, PsycopgAfterSaleRepository
from store.audit_store import AuditRepository, PsycopgAuditRepository
from store.outbox_store import OutboxRepository, PsycopgOutboxRepository
from store.refund_store import PsycopgRefundRepository, RefundRepository
from store.refund_store_types import AsyncConnection


class RefundUnitOfWorkProtocol(Protocol):
    after_sales: AfterSaleRepository
    refunds: RefundRepository
    audits: AuditRepository
    outbox: OutboxRepository

    async def commit(self) -> None:
        """提交当前四个 Repository 共用的事务。"""

    async def rollback(self) -> None:
        """回滚当前四个 Repository 共用的事务。"""


class RefundUnitOfWork:
    """把四个 Repository 绑定到同一连接和事务。"""

    def __init__(self, connection: AsyncConnection):
        self._connection = connection
        self._finished = False
        self.after_sales = PsycopgAfterSaleRepository(connection)
        self.refunds = PsycopgRefundRepository(connection)
        self.audits = PsycopgAuditRepository(connection)
        self.outbox = PsycopgOutboxRepository(connection)

    async def __aenter__(self) -> "RefundUnitOfWork":
        """开始事务并返回绑定好的四个 Repository。

        Returns:
            当前连接上的 RefundUnitOfWork。
        """
        await self._connection.execute("BEGIN")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """未显式结束时根据异常自动回滚或提交。

        Args:
            exc_type: 上下文中的异常类型；无异常时为 None。
            exc_value: 上下文中的异常实例；无异常时为 None。
            traceback: 上下文中的 traceback；无异常时为 None。
        """
        if self._finished:
            return
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()

    async def commit(self) -> None:
        """提交当前事务；重复提交不会再次操作连接。"""
        if not self._finished:
            await self._connection.commit()
            self._finished = True

    async def rollback(self) -> None:
        """回滚当前事务；重复回滚不会再次操作连接。"""
        if not self._finished:
            await self._connection.rollback()
            self._finished = True


class RefundUnitOfWorkFactory:
    """从连接池借出连接并创建 RefundUnitOfWork。"""

    def __init__(
        self,
        connection_factory: Callable[[], Awaitable[AsyncConnection]] = get_connection,
        connection_releaser: Callable[[AsyncConnection], Awaitable[None]] = put_connection,
    ):
        self._connection_factory = connection_factory
        self._connection_releaser = connection_releaser

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[RefundUnitOfWork]:
        """借出一个连接并在退出时释放。

        Yields:
            已绑定同一连接的四个 Repository。

        Raises:
            Exception: 连接获取或事务操作失败时向调用方传播。
        """
        connection = await self._connection_factory()
        try:
            async with RefundUnitOfWork(connection) as unit_of_work:
                yield unit_of_work
        finally:
            await self._connection_releaser(connection)
