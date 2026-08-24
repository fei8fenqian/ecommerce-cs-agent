"""应用自有订单的确定性履约命令服务。"""

from dataclasses import dataclass

from store.checkout_store import OperatorFulfillment, mark_fulfillment_shipped


class FulfillmentUnavailableError(ValueError):
    """订单当前不能进入指定的履约状态。"""


@dataclass(frozen=True)
class ShipOrderCommand:
    """运营发货命令；不接受模型或客户端提供的任意状态。"""

    order_no: str
    carrier: str
    tracking_number: str


async def ship_order(command: ShipOrderCommand) -> OperatorFulfillment:
    """以固定状态迁移将待发货订单标记为已发货。

    Args:
        command: 已在 API 边界校验格式的订单号、承运商和运单号。

    Returns:
        已发货订单的履约摘要。

    Raises:
        FulfillmentUnavailableError: 订单不存在、尚未付款或已被处理。
    """
    result = await mark_fulfillment_shipped(
        order_no=command.order_no,
        carrier=command.carrier,
        tracking_number=command.tracking_number,
    )
    if result is None:
        raise FulfillmentUnavailableError("fulfillment unavailable")
    return result
