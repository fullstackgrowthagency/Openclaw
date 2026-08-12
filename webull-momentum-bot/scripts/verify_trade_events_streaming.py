#!/usr/bin/env python3
"""
One-off live check for whether Webull's gRPC Trade Events streaming API
(order/position status change push, as opposed to REST polling via
get_order_status/poll_fills/get_positions) is usable against THIS
project's sandbox account -- surfaced 2026-08-12 when the user pasted a
usage snippet for webull.trade.trade_events_client.TradeEventsClient.

CONFIRMED BY READING THE INSTALLED SDK (not yet live-tested):
  - webull.trade.trade_events_client.TradeEventsClient is real and
    implemented (grpc, secure channel by default, HMAC-signed metadata
    per request via webull.trade.events.signature_composer).
  - webull.trade.events.types defines the subscribe/event type
    constants the user's snippet imported:
    EVENT_TYPE_ORDER=1024, EVENT_TYPE_POSITION=1028, EVENT_TYPE_OPTION=1032
    (event types on incoming messages) and
    ORDER_STATUS_CHANGED=1, POSITION_STATUS_CHANGED=2, OPTION_STATUS_CHANGED=4
    (subscribeType bits) -- but TradeEventsClient._build_request
    hardcodes subscribeType=7 (1|2|4, "all three") regardless of what's
    requested; there's no public parameter to subscribe to order events
    only. Not a blocker (position events are arguably also useful here),
    just means this script can't test ORDER-only in isolation.
  - do_subscribe(accounts) BLOCKS FOREVER (an infinite retry loop with
    its own internal grpc-status-aware retry policy, see
    webull/trade/events/default_retry_policy.py) -- must run on a
    background (daemon) thread, same pattern this project already uses
    for DataStreamingClient's MQTT loop (see WebullBrokerClient.
    subscribe_quotes's docstring). Unlike DataStreamingClient, there's no
    exposed stop()/disconnect() method -- the loop only exits on a
    non-retryable grpc error or an unhandled exception, so this script
    just lets the daemon thread die with the process rather than trying
    to stop it cleanly.
  - Payload delivery: on_events_message(event_type, subscribe_type,
    payload, raw_message) -- payload is JSON-decoded into a dict when
    the message's contentType is "application/json" (see
    TradeEventsClient._handle_message), else left as the raw string.
  - Host resolution: same DefaultEndpointResolver machinery as
    DataStreamingClient, reading the SDK's own bundled
    webull/core/data/endpoints.json. For region "us" that file has
    EXACTLY ONE events-api entry: events-api.webull.com -- no separate
    sandbox host anywhere in it, structurally identical to quotes-api's
    single data-api.webull.com entry that caused two failed live runs
    for the MQTT quotes streaming (see verify_streaming.py's docstring)
    before "events-api.sandbox.webull.com" turned out to be worth trying
    as a next-hypothesis guess for that one. Same risk applies here --
    this script accepts --host for exactly that reason, tried second if
    the default host fails outright.

NOT CONFIRMED -- exactly what this script exists to find out:
  - Whether events-api.webull.com actually accepts and correctly streams
    events for THIS SANDBOX account's credentials at all, or is
    production-only (see the host-resolution risk above).
  - The exact JSON payload shape for an ORDER_STATUS_CHANGED event --
    field names for symbol/order_id/client_order_id/status/filled_price/
    filled_quantity are completely unconfirmed; nothing in the SDK
    docstrings or samples shows a real example. This script only dumps
    whatever arrives raw -- do NOT write parsing code against a guessed
    shape until this has actually printed a real message.
  - Whether a resting order placed live (e.g. via
    scripts/verify_bracket_orders.py, or just this bot's own normal
    trading) reliably triggers a push here at all, and how quickly.

Do NOT wire this into TradingLoop/WebullBrokerClient until this script
has actually run clean against the sandbox and printed a real event --
same rule this project already applied to support_trading_session, the
OCO bracket feature, and the MQTT quotes streaming before any of them
were trusted.

Usage: python scripts/verify_trade_events_streaming.py [--seconds N] [--host HOST]
  Connects, subscribes this account's own account_id (from Settings,
  same account already in use everywhere else in this project) to all
  event types, and waits up to `--seconds` (default 60 -- deliberately
  longer than the quotes-streaming script's default, since an order/
  position event only fires when something actually happens, unlike a
  quote tick for a liquid symbol) for anything to arrive, printing every
  raw event exactly as received. Read-only by itself -- places no
  orders. To actually see an ORDER_STATUS_CHANGED event, run this in one
  terminal and let the live bot (or a manual test order) submit/fill/
  cancel something in another while this is listening.
"""
import json
import sys
import threading
import time

from webull_bot.config import get_settings

_DEFAULT_WAIT_SECONDS = 60.0


def main() -> None:
    wait_seconds = _DEFAULT_WAIT_SECONDS
    if "--seconds" in sys.argv:
        wait_seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    host = None
    if "--host" in sys.argv:
        host = sys.argv[sys.argv.index("--host") + 1]

    settings = get_settings()
    print(f"host={host or '(SDK auto-resolved default -- events-api.webull.com for region us)'}")
    print(
        f"trading_mode={settings.trading_mode.value}  base_url={settings.webull.base_url}  "
        f"account_id={settings.webull.account_id}  wait={wait_seconds:.0f}s\n"
    )

    try:
        from webull.trade.trade_events_client import TradeEventsClient
    except ImportError as exc:
        print(f"Could not import TradeEventsClient from the installed SDK: {exc!r}")
        print("(webull-openapi-python-sdk may be an older version without trade-events support.)")
        sys.exit(1)

    events: list[str] = []
    messages: list[tuple[int, int, object]] = []

    client = TradeEventsClient(
        settings.webull.app_key, settings.webull.app_secret, "us",
        host=host,  # None lets the SDK auto-resolve -- see this module's docstring for what that resolves to
    )
    client.enable_logger()

    def _on_connect(client_, payload, raw_message):
        events.append("connect_success")
        print(f"  [event] subscribe succeeded -- payload={payload!r}")

    def _on_events_message(event_type, subscribe_type, payload, raw_message):
        events.append(f"message:event_type={event_type}:subscribe_type={subscribe_type}")
        messages.append((event_type, subscribe_type, payload))
        print(f"  [event] event_type={event_type} subscribe_type={subscribe_type} payload={payload!r}")
        if isinstance(payload, dict):
            print(f"           (as JSON) {json.dumps(payload, indent=2, default=str)}")

    client.on_connect = _on_connect
    client.on_events_message = _on_events_message

    print("Connecting + subscribing (blocking do_subscribe() call, run on a daemon thread)...")
    thread = threading.Thread(
        target=client.do_subscribe, args=([settings.webull.account_id],), daemon=True,
    )
    thread.start()

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(1.0)
        if messages:
            break  # got at least one real message -- no need to wait out the full window

    print(f"\nWaited up to {wait_seconds:.0f}s. Results:")
    print(f"  Subscribe succeeded:    {'connect_success' in events}")
    print(f"  Messages received:      {len(messages)}")
    if messages:
        event_type, subscribe_type, payload = messages[0]
        print(f"  First message: event_type={event_type} subscribe_type={subscribe_type} payload={payload!r}")

    if not events:
        print(
            "\nNo events fired at all within the wait window -- either the gRPC connection itself "
            "never completed (network/firewall/host issue), or it connected but the subscribe was "
            "silently rejected. Check above for any 'grpc error'/'grpc exception' log lines this "
            "script's enable_logger() call should have surfaced."
        )
    elif "connect_success" not in events:
        print(
            "\nConnected/attempted but no 'connect_success' (SubscribeSuccess) event -- check the "
            "logged grpc error/exception lines above for the real rejection reason."
        )
    elif not messages:
        print(
            "\nSubscribe succeeded, but zero order/position events arrived. This just means nothing "
            "happened at the broker during the wait window -- re-run this while a real order is being "
            "placed/filled/cancelled (the live bot's own trading, or a manual test order) rather than "
            "concluding streaming doesn't work from silence alone."
        )
    else:
        print(
            "\nReal event(s) arrived -- trade-events streaming IS usable against this sandbox account. "
            "Copy the exact payload shape printed above before writing any parsing code against it."
        )


if __name__ == "__main__":
    main()
