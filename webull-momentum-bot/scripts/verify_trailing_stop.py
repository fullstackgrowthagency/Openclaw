#!/usr/bin/env python3
"""
One-off live check for whether Webull's OpenAPI actually supports a native
TRAILING_STOP_LOSS order type for a US-market equity, and whether it can be
combined into an OCO combo with a target LIMIT leg the same way the plain
STOP_LOSS+STOP_PROFIT bracket already confirmed in verify_bracket_orders.py
can.

Why this matters: this bot currently trails a stop by recomputing
position.stop_price in software every tick (PositionManager.
_maybe_update_trailing_stop) and pushing it to the broker via cancel +
place a fresh plain STOP order whenever it moves enough
(TradingLoop._sync_broker_protective_orders) -- see that method's
docstring for why (modify_order's effect on a resting order's price was
inconclusive when live-tested). If Webull will actually let a
TRAILING_STOP_LOSS order sit and trail on its own, that whole
recompute-and-replace loop could be replaced by placing the order ONCE and
letting Webull's own matching engine do the trailing, which is simpler and
can't fall behind between ticks the way this process's own polling cadence
can.

Why this is NOT a safe assumption already: the SDK's own sample code
(samples/trade/trade_client_v3.py) only demonstrates TRAILING_STOP_LOSS
against "market": "HK" (symbol 00700) -- every other order type this bot
already uses (STOP, LIMIT, the OCO combo) is demonstrated there against
"market": "US". That's a real signal this might be region-gated, not
proof either way. Do NOT wire TRAILING_STOP_LOSS into position_manager.py/
trading_loop.py until this script has actually run clean against a real
US equity in this account -- same "don't trust the docs, trust a live
response" rule verify_bracket_orders.py already established.

Must be run somewhere with real network access to Webull's API and
TRADING_MODE=sandbox (or live, with confirmation) -- NOT from a
paper-mode dev container. Must also be run during core trading hours
(9:30am-4:00pm ET, Mon-Fri): every order this codebase sends hardcodes
support_trading_session="CORE" (see WebullBrokerClient._order_payload's
docstring -- "ALL" is confirmed rejected by this account), so running
this outside that window will very likely reject the entry buy in Step 1
before ever reaching the actual question, and any result from that run
would be indistinguishable from "trailing stops aren't supported" without
this caveat -- don't misread a core-hours rejection as a trailing-stop
finding.

Usage: python scripts/verify_trailing_stop.py SYMBOL [--qty N] [--trail-pct P]
  Places a REAL small market buy in whatever TRADING_MODE is configured
  (should be sandbox), then tests a lone TRAILING_STOP_LOSS order and a
  TRAILING_STOP_LOSS + LIMIT OCO combo against it, then reports exactly
  what worked. Defaults to qty=1 and a 3% trail to keep this cheap and
  match this bot's own default trailing_stop_pct.
"""
import sys
import time
import uuid

from _rate_limit import call_with_retry

from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings
from webull_bot.enums import OrderSide, OrderStatus, OrderType
from webull_bot.models import Order

_INTER_STEP_DELAY_SECONDS = 3.0


def _wait_for_terminal(broker, order, label, timeout_s=90):
    terminal = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED}
    deadline = time.time() + timeout_s
    while order.status not in terminal and time.time() < deadline:
        time.sleep(2.0)
        try:
            order = call_with_retry(lambda: broker.get_order_status(order.broker_order_id), label=f"{label} poll")
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
    trail_pct = 3.0
    if "--trail-pct" in sys.argv:
        trail_pct = float(sys.argv[sys.argv.index("--trail-pct") + 1])

    settings = get_settings()
    broker = get_broker_client(settings)
    broker.connect()
    print(f"trading_mode={settings.trading_mode.value}  symbol={symbol}  qty={qty}  trail_pct={trail_pct}\n")

    if settings.trading_mode.value == "live":
        confirm = input("TRADING_MODE=live -- this places REAL orders with real money. Type LIVE to proceed: ")
        if confirm.strip() != "LIVE":
            print("Not confirmed -- aborting.")
            return

    if not hasattr(broker, "_require_trade_client"):
        print(f"{type(broker).__name__} has no _require_trade_client -- this script only works against WebullBrokerClient.")
        sys.exit(1)

    snapshot = call_with_retry(lambda: broker.get_snapshot(symbol), label="get_snapshot")
    print(f"Current price for {symbol}: {snapshot.last_price}\n")

    # -- Step 1: real entry (or reuse an existing position, same pattern as
    # verify_bracket_orders.py) -- everything below tests orders against a
    # real, owned position.
    positions = call_with_retry(lambda: broker.get_positions(), label="get_positions")
    existing_position = next((p for p in positions if p.symbol == symbol), None)
    if existing_position is not None:
        print(f"Step 1: reusing existing position ({existing_position.quantity:g} {symbol} "
              f"@ {existing_position.avg_entry_price}) instead of buying more")
        qty = existing_position.quantity
        fill_price = existing_position.avg_entry_price
    else:
        print(f"Step 1: real MARKET buy of {qty} {symbol}")
        entry = Order(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=qty)
        entry = call_with_retry(lambda: broker.place_order(entry), label="entry place_order")
        entry = _wait_for_terminal(broker, entry, "entry")
        if entry.status != OrderStatus.FILLED:
            print("Entry did not fill -- if this is REJECTED and it's outside 9:30am-4:00pm ET,")
            print("that's the core-hours support_trading_session=CORE issue, not a finding about")
            print("trailing stops. Re-run during core hours.")
            return

        positions = call_with_retry(lambda: broker.get_positions(), label="get_positions")
        live_position = next((p for p in positions if p.symbol == symbol), None)
        fill_price = live_position.avg_entry_price if live_position else snapshot.last_price
    print(f"  using qty={qty:g} @ ~{fill_price}\n")
    time.sleep(_INTER_STEP_DELAY_SECONDS)

    trade_client = broker._require_trade_client()
    account_id = broker.account_id

    # -- Step 2: lone TRAILING_STOP_LOSS order against the owned position.
    # This bypasses WebullBrokerClient.place_order/_order_payload entirely
    # (neither supports trailing_type/trailing_stop_step yet) and builds
    # the payload by hand, same reasoning as verify_bracket_orders.py's OCO
    # step.
    print("Step 2: lone TRAILING_STOP_LOSS order against the owned position")
    trailing_client_order_id = str(uuid.uuid4())
    trailing_payload = {
        "combo_type": "NORMAL",
        "client_order_id": trailing_client_order_id,
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "TRAILING_STOP_LOSS",
        "quantity": str(qty),
        "trailing_stop_step": str(trail_pct),
        "trailing_type": "PERCENTAGE",
        "support_trading_session": "CORE",
        "side": "SELL",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }
    print(f"  client_order_id={trailing_client_order_id} (save for scripts/cancel_order.py if cleanup is needed)")
    trailing_placed = False
    try:
        response = call_with_retry(
            lambda: trade_client.order_v3.place_order(account_id, [trailing_payload]),
            label="TRAILING_STOP_LOSS place_order",
        )
        response.raise_for_status()
        print(f"  ACCEPTED for US market: {response.json()}")
        trailing_placed = True
    except Exception as exc:
        print(f"  REJECTED / raised: {exc!r}")
        print("  (this is the answer we needed either way -- if this failed for a reason other")
        print("   than core hours, TRAILING_STOP_LOSS is not usable for US equities on this account)")

    if trailing_placed:
        time.sleep(2.0)
        try:
            detail = call_with_retry(
                lambda: trade_client.order_v3.get_order_detail(account_id, trailing_client_order_id),
                label="TRAILING_STOP_LOSS get_order_detail",
            )
            print(f"  order detail: {detail.json()}")
        except Exception as exc:
            print(f"  get_order_detail raised: {exc!r}")
        try:
            call_with_retry(
                lambda: trade_client.order_v3.cancel_order(account_id, trailing_client_order_id),
                label="TRAILING_STOP_LOSS cancel",
            )
            print("  cancelled cleanly")
        except Exception as exc:
            print(f"  cancel raised: {exc!r} -- check scripts/list_and_close_positions.py / cancel manually")
    print()
    time.sleep(_INTER_STEP_DELAY_SECONDS)

    # -- Step 3: the actually load-bearing question for this bot's design --
    # can TRAILING_STOP_LOSS be one leg of an OCO combo alongside a target
    # LIMIT leg, the same shape as the plain STOP+LIMIT bracket
    # verify_bracket_orders.py already confirmed works? If this fails but
    # Step 2 (lone trailing stop) succeeded, this bot could still use a
    # trailing stop for the POST-partial-exit leg (which never has a target
    # anymore anyway -- see _attach_broker_bracket's docstring) even if it
    # can't be combined with a target for the pre-partial full bracket.
    print("Step 3: TRAILING_STOP_LOSS + LIMIT target as an OCO combo")
    combo_id = str(uuid.uuid4())
    target_price = round(fill_price * 1.05, 2)
    combo_trailing_id = str(uuid.uuid4())
    combo_target_id = str(uuid.uuid4())
    combo_trailing_payload = dict(trailing_payload)
    combo_trailing_payload["client_order_id"] = combo_trailing_id
    combo_trailing_payload["combo_type"] = "OCO"
    combo_trailing_payload["client_combo_order_id"] = combo_id
    combo_target_payload = {
        "combo_type": "OCO",
        "client_combo_order_id": combo_id,
        "client_order_id": combo_target_id,
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "LIMIT",
        "quantity": str(qty),
        "limit_price": str(target_price),
        "support_trading_session": "CORE",
        "side": "SELL",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }
    print(f"  submitting OCO pair: trailing_stop_step={trail_pct}% target={target_price} combo_id={combo_id}")
    combo_placed = False
    try:
        response = call_with_retry(
            lambda: trade_client.order_v3.place_order(account_id, [combo_trailing_payload, combo_target_payload]),
            label="TRAILING_STOP_LOSS+LIMIT OCO place_order",
        )
        response.raise_for_status()
        print(f"  ACCEPTED: {response.json()}")
        combo_placed = True
    except Exception as exc:
        print(f"  REJECTED / raised: {exc!r}")

    if combo_placed:
        time.sleep(2.0)
        for order_id, label in ((combo_trailing_id, "trailing leg"), (combo_target_id, "target leg")):
            try:
                call_with_retry(
                    lambda oid=order_id: trade_client.order_v3.cancel_order(account_id, oid),
                    label=f"{label} cancel",
                )
                print(f"  {label} cancelled cleanly")
            except Exception as exc:
                print(f"  {label} cancel raised: {exc!r} -- may have already been cancelled by the OCO sibling")

    print("\nDone. If anything above is still open at the broker (check with")
    print("scripts/list_and_close_positions.py), close it manually before leaving this test.")


if __name__ == "__main__":
    main()
