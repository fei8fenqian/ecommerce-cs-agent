"""UnionPay 5.1.0 U0 手工联调 CLI；不进入商城运行时。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from infra.unionpay_test import (  # noqa: E402
    SMOKE_AMOUNT_CENTS,
    UnionPayProtocolError,
    UnionPayTestClient,
    new_smoke_transaction,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UnionPay 5.1.0 U0 test-gateway smoke")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create", help="生成一次性银联测试收银台 HTML")
    query = subparsers.add_parser("query", help="验签并查询一笔 U0 测试交易")
    query.add_argument("order_id")
    query.add_argument("txn_time")
    return parser


def _create() -> int:
    client = UnionPayTestClient.from_settings()
    transaction = new_smoke_transaction()
    form = client.build_front_payment_form(
        order_id=transaction.order_id,
        txn_time=transaction.txn_time,
        txn_amt=transaction.txn_amt,
    )
    output_path = Path("/tmp") / f"unionpay-smoke-{transaction.order_id}.html"
    output_path.write_text(form.as_html(), encoding="utf-8")
    print(f"orderId={transaction.order_id}")
    print(f"txnTime={transaction.txn_time}")
    print(f"amount_cents={SMOKE_AMOUNT_CENTS}")
    print(f"html_path={output_path}")
    return 0


async def _query(order_id: str, txn_time: str) -> int:
    client = UnionPayTestClient.from_settings()
    result = await client.query_transaction(order_id=order_id, txn_time=txn_time)
    print(f"signature_verified={str(result.signature_verified).lower()}")
    print(f"respCode={result.resp_code}")
    print(f"origRespCode={result.orig_resp_code}")
    print(f"queryId={result.query_id}")
    print(f"txnAmt={result.txn_amt}")
    print(f"orderId={result.order_id}")
    if result.payment_success:
        print("PAYMENT_SUCCESS")
    else:
        print("PAYMENT_NOT_CONFIRMED")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "create":
            return _create()
        return asyncio.run(_query(args.order_id, args.txn_time))
    except (OSError, UnionPayProtocolError) as exc:
        # 不输出异常链/HTTP body，避免把配置、签名或网关原始字段带到终端。
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
