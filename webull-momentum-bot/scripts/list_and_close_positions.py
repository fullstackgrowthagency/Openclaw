#!/usr/bin/env python3
"""
One-off operational tool for a real incident: entries filled at the broker
without the bot ever recording them locally (see
TradingLoop._confirm_entry_filled and WebullBrokerClient.get_positions'
docstrings for the root cause and the fix already shipped for it). This
script talks to the broker directly, bypassing the bot's own
(previously-untrustworthy, now-hardened) internal position tracking
entirely, so it works whether or not this specific incident's positions
would even be visible to the running bot process.

Two modes:
  - `python scripts/list_and_close_positions.py` (default): lists every
    position get_positions() returns, plus the RAW pre-parse JSON row for
    each one -- print that raw output if any position looks wrong or
    missing, since it's exactly the data needed to fix
    _position_from_dict's still-unverified field-name guesses. Read-only,
    safe to run any time.
  - `python scripts/list_and_close_positions.py --close`: after listing,
    prompts for a typed "CLOSE" confirmation, then submits a MARKET order
    to fully close every position found (SELL for a long, BUY_TO_COVER for
    a short), polling each order for a fill/terminal status and reporting
    the outcome. This places real orders against whatever account
    WEBULL_ACCOUNT_ID points to (sandbox or live, per TRADING_MODE/
    .env) -- there is no dry-run for --close beyond the confirmation
    prompt, so double check TRADING_MODE before typing CLOSE.

Needs the same env vars the bot itself runs with (WEBULL_APP_KEY/SECRET/
ACCOUNT_ID, TRADING_MODE) -- run this on the VPS, in the same environment
the systemd service uses.
"""
import sys
import time

from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings
from webull_bot.enums import OrderSide, OrderStatus, OrderType
from webull_bot.models import Order

_CLOSE_SIDE = {
    OrderSide.BUY: OrderSide.SELL,
    OrderSide.SELL_SHORT: OrderSide.BUY_TO_COVER,
}

_TERMINAL_STATUSES = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED}


def _fetch_raw_positions(client) -> list:
    """Bypasses _position_from_dict entirely to show exactly what Webull
    returned, in case a row fails to parse (or parses but looks wrong)."""
    response = client._require_trade_client().account_v2.get_account_position(client.account_id)
    response.raise_for_status()
    return response.json()


def main() -> None:
    close = "--close" in sys.argv[1:]

    settings = get_settings()
    broker = get_broker_client(settings)
    broker.connect()

    print(f"trading_mode={settings.trading_mode.value}  account_id={settings.webull.account_id}\n")

    raw_rows = _fetch_raw_positions(broker) if hasattr(broker, "_require_trade_client") else []
    positions = broker.get_positions()

    if not positions:
        print("No open positions found via get_positions().")
        if raw_rows:
            print(f"\nBut the broker returned {len(raw_rows)} raw row(s) that didn't parse -- "
                  f"see the logs from this run above, or the raw rows below:")
            for row in raw_rows:
                print(f"  RAW: {row}")
        return

    print(f"{len(positions)} open position(s):\n")
    for p in positions:
        print(f"  {p.symbol:8s}  side={p.side.value:10s}  qty={p.quantity:g}  "
              f"avg_entry_price={p.avg_entry_price:.4f}  opened_at={p.opened_at}")

    if raw_rows:
        print("\nRaw rows (for verifying/fixing _position_from_dict's field names):")
        for row in raw_rows:
            print(f"  RAW: {row}")

    if not close:
        print("\nDry run only -- re-run with --close to actually close these positions.")
        return

    print(f"\nAbout to submit a MARKET order to fully close all {len(positions)} position(s) above,")
    print(f"against account {settings.webull.account_id} in {settings.trading_mode.value} mode.")
    confirmation = input("Type CLOSE to confirm: ")
    if confirmation.strip() != "CLOSE":
        print("Not confirmed -- aborting, nothing was closed.")
        return

    for p in positions:
        close_side = _CLOSE_SIDE.get(p.side)
        if close_side is None:
            print(f"  {p.symbol}: unrecognized position side {p.side!r}, skipping -- close manually.")
            continue

        order = Order(
            symbol=p.symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            quantity=p.quantity,
        )
        try:
            order = broker.place_order(order)
        except Exception as exc:
            print(f"  {p.symbol}: place_order raised: {exc!r}")
            continue

        print(f"  {p.symbol}: submitted ({close_side.value} {p.quantity:g}), status={order.status.value}")

        for _ in range(10):
            if order.status in _TERMINAL_STATUSES:
                break
            time.sleep(1.0)
            try:
                order = broker.get_order_status(order.broker_order_id)
            except Exception as exc:
                print(f"  {p.symbol}: get_order_status raised: {exc!r}")
                break

        print(f"  {p.symbol}: final status={order.status.value}")


if __name__ == "__main__":
    main()
