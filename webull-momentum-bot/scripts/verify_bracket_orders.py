#!/usr/bin/env python3
"""
One-off live check for whether Webull's combo-order API can attach a
protective OCO bracket (STOP_LOSS + STOP_PROFIT) to a position AFTER it's
already open -- as opposed to submitting the entry itself as part of the
combo (the only shape Webull's own docs show an example of, via a MASTER
leg). This matters because this bot's entry-fill pipeline (TradingLoop.
_submit_entry -> _poll_pending_entry -> _confirm_entry_filled) already
places the entry as a plain MARKET order and confirms the fill separately
-- rearchitecting that to submit entry+bracket atomically would mean
touching code that's had several real production bugs fixed in it today.
Attaching the bracket as a second call, right after fill confirmation, is
much lower-risk IF Webull actually allows it.

Also verifies, separately: a lone STOP_LOSS order (no combo at all) --
_order_payload has carried a `stop_price` field for a while but it's never
been exercised live (see its comment in brokers/webull/client.py).

Do NOT trust the docs' claims here without this actually running clean --
support_trading_session: "ALL" was documented and then flatly rejected
live earlier in this project's life; treat every claim below the same way
until this script proves it.

Usage: python scripts/verify_bracket_orders.py SYMBOL [--qty N]
  Places a REAL small market buy in whatever TRADING_MODE is configured
  (should be sandbox), then tries to attach a stop+target OCO bracket to
  it, then cancels everything and reports exactly what worked. Pick a
  liquid, cheap symbol you don't mind a few real (sandbox) shares of --
  defaults to a small qty (1) to keep this cheap regardless.
"""
import sys
import time

from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings
from webull_bot.enums import OrderSide, OrderStatus, OrderType
from webull_bot.models import Order


def _wait_for_terminal(broker, order, label, timeout_s=30):
    terminal = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED}
    deadline = time.time() + timeout_s
    while order.status not in terminal and time.time() < deadline:
        time.sleep(2.0)
        try:
            order = broker.get_order_status(order.broker_order_id)
        except Exception as exc:
            print(f"  {label}: get_order_status raised: {exc!r}")
            break
    print(f"  {label}: final status={order.status.value}")
    return order


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    symbol = sys.argv[1].upper()
    qty = 1
    if "--qty" in sys.argv:
        qty = int(sys.argv[sys.argv.index("--qty") + 1])

    settings = get_settings()
    broker = get_broker_client(settings)
    broker.connect()
    print(f"trading_mode={settings.trading_mode.value}  symbol={symbol}  qty={qty}\n")

    if settings.trading_mode.value == "live":
        confirm = input("TRADING_MODE=live -- this places REAL orders with real money. Type LIVE to proceed: ")
        if confirm.strip() != "LIVE":
            print("Not confirmed -- aborting.")
            return

    # -- Step 1: lone STOP_LOSS order (no combo), cancelled immediately after
    # confirming it was accepted -- just checking the field name/order type
    # is accepted at all, not that it ever fills.
    snapshot = broker.get_snapshot(symbol)
    print(f"Current price for {symbol}: {snapshot.last_price}\n")

    print("Step 1: lone STOP_LOSS order (never meant to fill -- placed far from price, then cancelled)")
    far_stop_price = round(snapshot.last_price * 0.5, 2)  # deliberately unreachable
    lone_stop = Order(
        symbol=symbol, side=OrderSide.SELL, order_type=OrderType.STOP,
        quantity=qty, stop_price=far_stop_price,
    )
    try:
        lone_stop = broker.place_order(lone_stop)
        print(f"  submitted: status={lone_stop.status.value}")
        if lone_stop.status not in (OrderStatus.REJECTED,):
            time.sleep(2.0)
            broker.cancel_order(lone_stop.broker_order_id)
            print("  cancelled cleanly")
    except Exception as exc:
        print(f"  place_order raised: {exc!r}")
    print()

    # -- Step 2: real entry, then attach an OCO stop+target bracket AFTER
    # the fact -- this is the actual question this script exists to answer.
    print(f"Step 2: real MARKET buy of {qty} {symbol}, then attach an OCO stop+target bracket")
    entry = Order(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=qty)
    entry = broker.place_order(entry)
    entry = _wait_for_terminal(broker, entry, "entry")
    if entry.status != OrderStatus.FILLED:
        print("Entry did not fill -- stopping here, nothing to bracket.")
        return

    positions = broker.get_positions()
    live_position = next((p for p in positions if p.symbol == symbol), None)
    fill_price = live_position.avg_entry_price if live_position else snapshot.last_price
    print(f"  entry filled at ~{fill_price}")

    stop_price = round(fill_price * 0.95, 2)
    target_price = round(fill_price * 1.05, 2)

    stop_leg = Order(
        symbol=symbol, side=OrderSide.SELL, order_type=OrderType.STOP,
        quantity=qty, stop_price=stop_price,
    )
    target_leg = Order(
        symbol=symbol, side=OrderSide.SELL, order_type=OrderType.LIMIT,
        quantity=qty, limit_price=target_price,
    )

    # NOTE: place_order/_order_payload don't currently support combo_type
    # or client_combo_order_id at all -- this script calls the trade
    # client's raw order_v3.place_order directly with a hand-built combo
    # payload instead of going through WebullBrokerClient.place_order, so
    # this is intentionally bypassing the normal client for this one probe.
    if hasattr(broker, "_require_trade_client"):
        import uuid

        combo_id = str(uuid.uuid4())
        stop_payload = broker._order_payload(stop_leg)
        stop_payload["combo_type"] = "OCO"
        stop_payload["client_combo_order_id"] = combo_id
        target_payload = broker._order_payload(target_leg)
        target_payload["combo_type"] = "OCO"
        target_payload["client_combo_order_id"] = combo_id

        print(f"  submitting OCO pair: stop={stop_price} target={target_price} combo_id={combo_id}")
        oco_placed = False
        try:
            response = broker._require_trade_client().order_v3.place_order(
                broker.account_id, [stop_payload, target_payload]
            )
            response.raise_for_status()
            print(f"  OCO combo accepted: {response.json()}")
            oco_placed = True
        except Exception as exc:
            print(f"  OCO combo place_order raised: {exc!r}")
            print("  (this is the answer we needed either way -- if this failed, attaching")
            print("   a bracket after the fact isn't supported this way; a MASTER-anchored")
            print("   combo submitted atomically with the entry may be required instead)")

        # -- Step 3: modify the resting stop leg's price -- the mechanism
        # breakeven/trailing updates would rely on. WebullBrokerClient.
        # modify_order's request shape is UNVERIFIED (no live order existed
        # to test a replace against when it was written) -- this is exactly
        # what this step exists to pin down, not something to assume works
        # just because "Replace" is a named lifecycle stage in the docs.
        if oco_placed:
            print("\nStep 3: modify the resting stop leg's stop_price (simulates a breakeven move)")
            new_stop_price = round(fill_price * 0.99, 2)
            stop_broker_order_id = stop_payload["client_order_id"]
            try:
                modified = broker.modify_order(stop_broker_order_id, stop_price=str(new_stop_price))
                print(f"  modify_order accepted: new status={modified.status.value}")
                # Confirm the price actually changed, not just that the call
                # didn't error -- a 2xx with no real effect would be a false
                # positive for what this step is trying to prove.
                confirm_order = broker.get_order_status(stop_broker_order_id)
                print(f"  post-modify order detail: status={confirm_order.status.value}, "
                      f"stop_price={confirm_order.stop_price}")
            except Exception as exc:
                print(f"  modify_order raised: {exc!r}")

    print("\nDone. If anything above is still open at the broker (check with")
    print("scripts/list_and_close_positions.py), close it manually before leaving this test.")


if __name__ == "__main__":
    main()
