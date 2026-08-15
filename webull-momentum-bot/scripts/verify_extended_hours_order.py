#!/usr/bin/env python3
"""
One-off live check for what value(s) of Webull's `support_trading_session`
order field this account/endpoint actually accepts, run during a real
pre-market or after-hours window.

Why this matters: the user wants the bot to trade pre-market and
after-hours. RiskConfig.allow_extended_hours_trading (dashboard-adjustable,
off by default) already exists to let entry signals THROUGH the risk gate
outside 9:30am-4:00pm ET -- see risk/risk_engine.py. But every order this
bot places still goes through WebullBrokerClient._order_payload, which
sets `support_trading_session` from Settings.webull_support_trading_session
(env var WEBULL_SUPPORT_TRADING_SESSION, default "CORE"). A "CORE"-scoped
order is not expected to fill (or even accept) outside core hours -- that's
the whole reason this script exists.

Why this is NOT a safe assumption either way: confirmed live on 2026-08-10
that this exact account/endpoint REJECTS "ALL" outright with HTTP 417
(OAUTH_OPENAPI_PARAM_ERR, "invalid support_trading_session, value: ALL"),
directly contradicting developer.webull.com's docs, which (re-checked
2026-08-12) list "ALL" as the documented extended-hours value, alongside
"CORE" and "NIGHT". The SDK's own bundled sample scripts never demonstrate
"ALL" either -- only "CORE" and an unexplained "N". Leading, UNVERIFIED
hypothesis: Webull commonly gates extended-hours order entry behind a
separate account-level entitlement/agreement that this account may not
have enabled -- check the Webull app's account/trading-permissions
settings for an extended-hours opt-in BEFORE re-running this, since if
that's the real blocker, re-testing "ALL" without enabling it first will
just reproduce the same 417 again.

Deliberately does NOT take real market exposure: every order this script
places is a LIMIT BUY priced far below the current bid (won't realistically
fill during the test) that gets cancelled immediately after each check, not
a MARKET order. The question being tested is "does the API layer accept
this support_trading_session value at all", not "does an extended-hours
order actually get matched" -- a resting unfilled limit order answers the
first question just as well without risking a fill.

Must be run somewhere with real network access to Webull's API, with
TRADING_MODE=sandbox (or live, with confirmation) -- NOT from a paper-mode
dev container -- and during an ACTUAL pre-market (4:00am-9:30am ET) or
after-hours (4:00pm-8:00pm ET) window, Mon-Fri. Running this during core
hours defeats the purpose: "CORE" will simply succeed (as always) and tell
you nothing new, since core-hours orders never needed "ALL" to begin with.

Usage: python scripts/verify_extended_hours_order.py SYMBOL [--qty N]
  For each candidate support_trading_session value ("ALL", "NIGHT", "CORE"
  as a control), places a resting LIMIT BUY far below market, reports
  whether the API accepted or rejected it (and the exact rejection body if
  any), then cancels it if accepted. Update
  Settings.webull_support_trading_session (env var
  WEBULL_SUPPORT_TRADING_SESSION) to whichever value this confirms works,
  and only then consider raising RiskConfig.allow_extended_hours_trading's
  default -- this script alone does not change any live config.
"""
import sys
import time
import uuid

from _rate_limit import call_with_retry

from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings

_CANDIDATE_SESSIONS = ["ALL", "NIGHT", "CORE"]
_INTER_STEP_DELAY_SECONDS = 2.0


def _try_session_value(trade_client, account_id, symbol, qty, far_below_price, session_value):
    client_order_id = str(uuid.uuid4())
    payload = {
        "combo_type": "NORMAL",
        "client_order_id": client_order_id,
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "LIMIT",
        "quantity": str(qty),
        "limit_price": str(far_below_price),
        "support_trading_session": session_value,
        "side": "BUY",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }
    print(f"  support_trading_session={session_value!r}: submitting resting LIMIT BUY @ {far_below_price} "
          f"(client_order_id={client_order_id})")
    try:
        response = call_with_retry(
            lambda: trade_client.order_v3.place_order(account_id, [payload]),
            label=f"support_trading_session={session_value} place_order",
        )
        response.raise_for_status()
        print(f"    ACCEPTED: {response.json()}")
    except Exception as exc:
        print(f"    REJECTED / raised: {exc!r}")
        return
    time.sleep(_INTER_STEP_DELAY_SECONDS)
    try:
        call_with_retry(
            lambda: trade_client.order_v3.cancel_order(account_id, client_order_id),
            label=f"support_trading_session={session_value} cancel",
        )
        print("    cancelled cleanly")
    except Exception as exc:
        print(f"    cancel raised: {exc!r} -- check scripts/list_and_close_positions.py / cancel manually")


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
        confirm = input("TRADING_MODE=live -- this places REAL (unlikely-to-fill) orders. Type LIVE to proceed: ")
        if confirm.strip() != "LIVE":
            print("Not confirmed -- aborting.")
            return

    if not hasattr(broker, "_require_trade_client"):
        print(f"{type(broker).__name__} has no _require_trade_client -- this script only works against WebullBrokerClient.")
        sys.exit(1)

    snapshot = call_with_retry(lambda: broker.get_snapshot(symbol), label="get_snapshot")
    # 50% below the last price -- comfortably won't fill during the test,
    # even against a fast-moving low-float name.
    far_below_price = round(snapshot.last_price * 0.5, 2)
    print(f"Current price for {symbol}: {snapshot.last_price}  (test limit price: {far_below_price})\n")

    trade_client = broker._require_trade_client()
    account_id = broker.account_id

    print(f"Testing {len(_CANDIDATE_SESSIONS)} support_trading_session values: {_CANDIDATE_SESSIONS}")
    print("(CORE is included as a control -- it's expected to REJECT outside core hours;")
    print(" if it doesn't, this isn't actually running in an extended-hours window.)\n")
    for session_value in _CANDIDATE_SESSIONS:
        _try_session_value(trade_client, account_id, symbol, qty, far_below_price, session_value)
        print()
        time.sleep(_INTER_STEP_DELAY_SECONDS)

    print("Done. If anything above is still open at the broker (check with")
    print("scripts/list_and_close_positions.py), cancel it manually before leaving this test.")
    print("\nIf ALL or NIGHT was ACCEPTED: set WEBULL_SUPPORT_TRADING_SESSION to that value")
    print("in the deployment's .env, restart the dashboard service, and only then consider")
    print("turning on RiskConfig.allow_extended_hours_trading from the dashboard Settings modal.")
    print("If every non-CORE value was REJECTED with the same OAUTH_OPENAPI_PARAM_ERR seen on")
    print("2026-08-10: check the Webull app's account/trading-permissions settings for an")
    print("extended-hours trading opt-in before assuming this is a code problem.")


if __name__ == "__main__":
    main()
