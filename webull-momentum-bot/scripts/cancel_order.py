#!/usr/bin/env python3
"""
Cancel one or more resting orders by ID -- e.g. a stop/target bracket leg
left over from scripts/verify_bracket_orders.py, or (once broker-side
position management is live) any resting order that needs manual cleanup.

Accepts either a leg's own client_order_id/broker_order_id, or a combo-level
client_combo_order_id/combo_order_id -- try the combo ID first if you have
it (cancelling the whole bracket in one call would be convenient if Webull
supports it), and fall back to cancelling each leg's own ID individually if
that doesn't work. This script doesn't know which kind of ID it was given;
it just calls cancel_order and reports whatever Webull says.

Usage: python scripts/cancel_order.py ORDER_ID [ORDER_ID2 ...]
"""
import sys

from _rate_limit import call_with_retry

from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    order_ids = sys.argv[1:]

    settings = get_settings()
    broker = get_broker_client(settings)
    broker.connect()
    print(f"trading_mode={settings.trading_mode.value}\n")

    for order_id in order_ids:
        print(f"Cancelling {order_id} ...")
        try:
            call_with_retry(lambda oid=order_id: broker.cancel_order(oid), label=order_id)
            print("  cancelled (or already inactive -- Webull's cancel is idempotent for a")
            print("  non-open order, so a clean return here doesn't guarantee it was live)")
        except Exception as exc:
            print(f"  cancel_order raised: {exc!r}")

    print("\nCheck scripts/list_and_close_positions.py afterward to confirm the position")
    print("is now free to close (no more OAUTH_OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION).")


if __name__ == "__main__":
    main()
