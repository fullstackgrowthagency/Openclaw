#!/usr/bin/env python3
"""
One-off live check for whether this account's sandbox market data is
REAL-TIME or Webull's documented 15-minute-delayed default.

Real motivation (2026-08-14/17): the user pasted Webull's own Market Data
API docs, which state that a sandbox account without a real-time OpenAPI
market-data subscription (Nasdaq Basic Level 1 or Nasdaq TotalView Level
2, purchased specifically for "Non-Display OpenAPI usage" -- NOT the same
thing as a subscription bought through the regular Webull mobile
app/desktop platform, which is documented as independent of OpenAPI) gets
15-minute-delayed data instead. This codebase has never verified which of
those two states this account is actually in, and nothing in it would
notice a systematic 15-minute content delay on its own -- a delayed feed
still delivers messages continuously and looks "healthy" by every check
this project already has.

IMPORTANT (2026-08-17 correction): an earlier version of this script only
compared each message's OWN reported timestamp (REST get_snapshot's
`quote_time`; streaming's `basic.timestamp`) against wall-clock arrival
time, on the theory that a delayed feed would report a timestamp ~15
minutes behind. The user correctly pointed out the hole in that: a vendor
serving delayed data could just as easily stream continuously (a message
every second, so arrival CADENCE looks live) while stamping each message
with the CURRENT send time even though the PRICE VALUE inside it was
computed from data that's actually 15 minutes stale. Timestamp comparison
alone cannot distinguish "real-time" from "continuously-streamed-but-
stale-content" -- it only proves messages are ARRIVING on schedule, not
that their CONTENT is current.

The only test that actually settles this is comparing the PRICE VALUE
itself against a real-time reference you trust (your Webull app/desktop,
a live TradingView chart, etc. -- whatever you already know is real-time),
at the same wall-clock moment. This script's `--watch` mode (the default,
primary mode) exists to make that comparison easy: it prints a
continuous, timestamped log of exactly what price this account's sandbox
feed reports, second by second, for you to hold up against your live
reference side by side. If the printed prices track your live reference
in real time, it's real-time. If the printed prices are stuck wherever
the stock was trading ~15 minutes before you started the script (most
obvious on a symbol that's actively moving right now -- a flat/quiet
symbol proves nothing either way), it's still delayed. The old
timestamp-delay check is still run first as a cheap first signal, but is
now explicitly labeled as inconclusive on its own -- see its own output.

Usage: python scripts/verify_data_freshness.py SYMBOL [--seconds N]
  Runs the timestamp-delay check once (fast, informational only -- see
  the caveat above), then watches and prints SYMBOL's live price for
  `--seconds` (default 120 -- price-watching needs longer than a pure
  cadence check to be meaningful) from BOTH REST (polled every 5s) and
  streaming (printed on every update), interleaved in one chronological
  log. Pick a symbol that's ACTIVELY MOVING right now (check your live
  reference first) -- a quiet/flat symbol's price won't visibly change
  whether the feed is real-time or 15 minutes stale, which would make the
  comparison meaningless either way. Read-only -- places no orders, costs
  nothing to run.
"""
import sys
import time
import uuid
from datetime import datetime, timezone

from webull_bot.brokers.webull.client import WebullBrokerClient
from webull_bot.config import TradingMode, get_settings

_DEFAULT_WAIT_SECONDS = 120.0
_REST_POLL_INTERVAL_SECONDS = 5.0
# Timestamp-delay check thresholds -- see module docstring's "IMPORTANT"
# section for why this check alone is NOT definitive.
_REALTIME_THRESHOLD_SECONDS = 60.0
_DELAYED_TARGET_SECONDS = 15 * 60
_DELAYED_TOLERANCE_SECONDS = 120.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _classify(delay_seconds: float) -> str:
    if delay_seconds <= _REALTIME_THRESHOLD_SECONDS:
        return "cadence looks real-time"
    if abs(delay_seconds - _DELAYED_TARGET_SECONDS) <= _DELAYED_TOLERANCE_SECONDS:
        return "cadence matches the ~15min delayed pattern"
    return "cadence unclear"


def _run_timestamp_delay_check(client: WebullBrokerClient, symbol: str) -> None:
    print("Step 1/2 -- timestamp-delay check (fast, but NOT definitive -- see this")
    print("script's module docstring for why a stale-content feed could still pass this):")
    try:
        received_at = _now_utc()
        snapshot = client.get_snapshot(symbol)
        delay = (received_at - snapshot.timestamp).total_seconds()
        print(
            f"  REST: reported quote_time={snapshot.timestamp.isoformat()}  received_at={received_at.isoformat()}  "
            f"gap={delay:.1f}s  last_price={snapshot.last_price}  -> {_classify(delay)}"
        )
    except Exception as exc:
        print(f"  REST get_snapshot raised: {exc!r}")
    print()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    symbol = sys.argv[1].upper()
    wait_seconds = _DEFAULT_WAIT_SECONDS
    if "--seconds" in sys.argv:
        wait_seconds = float(sys.argv[sys.argv.index("--seconds") + 1])

    settings = get_settings()
    print(
        f"trading_mode={settings.trading_mode.value}  base_url={settings.webull.base_url}  "
        f"symbol={symbol}  wait={wait_seconds:.0f}s\n"
    )

    client = WebullBrokerClient(settings)
    client.connect()

    _run_timestamp_delay_check(client, symbol)

    print("Step 2/2 -- live price log (THE definitive test). Open your real-time")
    print(f"reference for {symbol} right now, side by side, and watch both for {wait_seconds:.0f}s.")
    print("If the prices below track your reference in real time, this feed is real-time.")
    print("If they're stuck near where the stock was trading ~15 minutes ago, it's still delayed.\n")

    try:
        from webull.data.common.category import Category
        from webull.data.data_streaming_client import DataStreamingClient
    except ImportError as exc:
        print(f"Could not import the streaming client from the installed SDK: {exc!r}")
        client.disconnect()
        sys.exit(1)

    mqtt_host = "data-api.sandbox.webull.com" if settings.trading_mode == TradingMode.SANDBOX else None
    session_id = str(uuid.uuid4())

    streaming_client = DataStreamingClient(
        settings.webull.app_key, settings.webull.app_secret, "us", session_id,
        http_host=settings.webull.base_url,
        mqtt_host=mqtt_host,
    )

    def _on_connect_success(client_, api_client_, session_id_):
        print(f"  [event] MQTT connected (session_id={session_id_}); subscribing to {symbol}...")
        try:
            client_.subscribe([symbol], Category.US_STOCK.name, ["QUOTE", "SNAPSHOT"])
        except Exception as exc:
            print(f"  [event] subscribe raised: {exc!r}")

    def _on_quotes_message(client_, topic, payload):
        if topic != "snapshot" or payload.price is None:
            return
        now = _now_utc()
        print(f"  {now.strftime('%H:%M:%S.%f')[:-3]}  [stream]  price={float(payload.price)}")

    streaming_client.on_connect_success = _on_connect_success
    streaming_client.on_quotes_message = _on_quotes_message

    try:
        streaming_client.connect_and_loop_start()
    except Exception as exc:
        print(f"  connect_and_loop_start() raised immediately: {exc!r}")
        client.disconnect()
        sys.exit(1)

    deadline = time.time() + wait_seconds
    next_rest_poll = time.time()
    while time.time() < deadline:
        if time.time() >= next_rest_poll:
            try:
                snapshot = client.get_snapshot(symbol)
                now = _now_utc()
                print(f"  {now.strftime('%H:%M:%S.%f')[:-3]}  [rest]    price={snapshot.last_price}")
            except Exception as exc:
                print(f"  [rest] get_snapshot raised: {exc!r}")
            next_rest_poll = time.time() + _REST_POLL_INTERVAL_SECONDS
        time.sleep(0.5)

    try:
        streaming_client.loop_stop()
        streaming_client.disconnect()
    except Exception:
        pass
    client.disconnect()

    print(
        f"\nDone. Compare the [rest]/[stream] prices/timestamps above against what your real-time "
        f"reference showed for {symbol} at the SAME wall-clock moments -- that comparison, not this "
        f"script alone, is what actually answers the question. A brief MQTT disconnect/retry message "
        f"right at the very end of the window is a known, harmless SDK quirk on shutdown; one appearing "
        f"well before the window ended would be worth a second run."
    )


if __name__ == "__main__":
    main()
