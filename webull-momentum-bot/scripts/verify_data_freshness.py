#!/usr/bin/env python3
"""
One-off live check for whether this account's sandbox market data is
REAL-TIME or Webull's documented 15-minute-delayed default.

Real motivation (2026-08-14/17): the user pasted Webull's own Market Data
API docs, which state that a sandbox account without a real-time OpenAPI
market-data subscription (Nasdaq Basic Level 1 or Nasdaq TotalView Level
2, purchased specifically for "Non-Display OpenAPI usage" -- NOT the
same thing as a subscription bought through the regular Webull mobile
app/desktop platform, which is documented as independent of OpenAPI) gets
15-minute-delayed data instead. This codebase has never verified which of
those two states this account is actually in, and nothing in it would
notice a systematic 15-minute content delay on its own -- a delayed feed
still delivers messages continuously and looks "healthy" by every check
this project already has (see runtime/trading_loop.py's
streaming_staleness_seconds, which only detects the CONNECTION going
quiet, not stale CONTENT arriving on schedule). The user has since
subscribed to Nasdaq Basic; this script is how to actually confirm the
upgrade took effect, rather than assume it did.

The check is simple and unambiguous: every Webull snapshot/quote/tick
message this codebase already parses carries the BROKER'S OWN reported
timestamp for that data point (REST get_snapshot's `quote_time`; the
streaming SNAPSHOT/QUOTE/TICK sub-types' `basic.timestamp` -- see
brokers/webull/client.py's _snapshot_from_dict/_snapshot_from_streamed_result/
_tick_from_streamed_result). Comparing that timestamp against this
script's own wall-clock time the instant it receives the message tells
you the real delay directly: a gap of a few seconds means real-time; a
gap sitting consistently around 15 minutes means still delayed; anything
in between or wildly inconsistent is worth a closer look before trusting
either conclusion.

Usage: python scripts/verify_data_freshness.py SYMBOL [--seconds N]
  Fetches a REST get_snapshot for SYMBOL once, then subscribes to
  streaming SNAPSHOT+QUOTE for it and reports the delay for every message
  received over `--seconds` (default 30). Prints a running per-message
  delay and a final min/max/avg summary with a verdict. Read-only --
  places no orders, costs nothing to run. Run during real core trading
  hours on an actively-trading symbol -- a quiet/halted symbol's REST
  snapshot can look "delayed" simply because nothing has traded recently,
  which isn't the same thing as the FEED itself being delayed (compare
  against a highly liquid, currently-moving name like SPY/QQQ/AAPL if a
  low-float candidate's own result looks ambiguous).
"""
import sys
import time
import uuid
from datetime import datetime, timezone

from webull_bot.brokers.webull.client import WebullBrokerClient
from webull_bot.config import TradingMode, get_settings

_DEFAULT_WAIT_SECONDS = 30.0
# A gap under this is called "real-time" -- generous enough to absorb
# normal network/processing latency, far short of anything close to 15
# minutes, so there's no ambiguous middle ground with the delayed case.
_REALTIME_THRESHOLD_SECONDS = 60.0
# Webull's documented delayed-data window. A measured gap within this
# tolerance of 15 minutes is called "delayed"; anything else is reported
# as unclear rather than forced into either bucket.
_DELAYED_TARGET_SECONDS = 15 * 60
_DELAYED_TOLERANCE_SECONDS = 120.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _classify(delay_seconds: float) -> str:
    if delay_seconds <= _REALTIME_THRESHOLD_SECONDS:
        return "REAL-TIME"
    if abs(delay_seconds - _DELAYED_TARGET_SECONDS) <= _DELAYED_TOLERANCE_SECONDS:
        return "DELAYED (~15 min)"
    return "UNCLEAR"


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

    # -- REST get_snapshot -----------------------------------------------
    print("REST get_snapshot:")
    try:
        received_at = _now_utc()
        snapshot = client.get_snapshot(symbol)
        delay = (received_at - snapshot.timestamp).total_seconds()
        print(
            f"  reported quote_time={snapshot.timestamp.isoformat()}  received_at={received_at.isoformat()}  "
            f"delay={delay:.1f}s  last_price={snapshot.last_price}  -> {_classify(delay)}"
        )
    except Exception as exc:
        print(f"  get_snapshot raised: {exc!r}")

    # -- Streaming SNAPSHOT+QUOTE ------------------------------------------
    print(f"\nStreaming (SNAPSHOT+QUOTE) for {wait_seconds:.0f}s:")
    try:
        from webull.data.common.category import Category
        from webull.data.data_streaming_client import DataStreamingClient
    except ImportError as exc:
        print(f"  Could not import the streaming client from the installed SDK: {exc!r}")
        client.disconnect()
        sys.exit(1)

    mqtt_host = "data-api.sandbox.webull.com" if settings.trading_mode == TradingMode.SANDBOX else None
    session_id = str(uuid.uuid4())
    delays: list[float] = []

    streaming_client = DataStreamingClient(
        settings.webull.app_key, settings.webull.app_secret, "us", session_id,
        http_host=settings.webull.base_url,
        mqtt_host=mqtt_host,
    )

    def _on_connect_success(client_, api_client_, session_id_):
        print(f"  [event] MQTT connected (session_id={session_id_}); subscribing...")
        try:
            client_.subscribe([symbol], Category.US_STOCK.name, ["QUOTE", "SNAPSHOT"])
        except Exception as exc:
            print(f"  [event] subscribe raised: {exc!r}")

    def _on_quotes_message(client_, topic, payload):
        received_at = _now_utc()
        if topic not in ("snapshot", "quote"):
            return
        reported_ms = payload.basic.timestamp
        reported_at = datetime.fromtimestamp(reported_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
        delay = (received_at - reported_at).total_seconds()
        delays.append(delay)
        print(f"  [{topic}] reported={reported_at.isoformat()}  received_at={received_at.isoformat()}  delay={delay:.1f}s")

    streaming_client.on_connect_success = _on_connect_success
    streaming_client.on_quotes_message = _on_quotes_message

    try:
        streaming_client.connect_and_loop_start()
    except Exception as exc:
        print(f"  connect_and_loop_start() raised immediately: {exc!r}")
        client.disconnect()
        sys.exit(1)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(1.0)

    try:
        streaming_client.loop_stop()
        streaming_client.disconnect()
    except Exception:
        pass
    client.disconnect()

    print(f"\nReceived {len(delays)} streaming message(s) over {wait_seconds:.0f}s.")
    if not delays:
        print(
            "No streaming messages arrived -- either the symbol didn't trade during this window (try a "
            "more liquid one, or run during core hours) or the connection/subscribe itself failed (see "
            "events above). Not evidence either way about data freshness."
        )
        return

    avg_delay = sum(delays) / len(delays)
    print(f"  min delay: {min(delays):.1f}s")
    print(f"  max delay: {max(delays):.1f}s")
    print(f"  avg delay: {avg_delay:.1f}s")
    print(f"\nVerdict: {_classify(avg_delay)}")
    if _classify(avg_delay) == "DELAYED (~15 min)":
        print(
            "This account's sandbox data is still ~15 minutes delayed despite the Nasdaq Basic "
            "subscription. Possible causes: the subscription hasn't finished provisioning yet (may take "
            "some time after purchase), it was purchased on the wrong account/environment, or it needs to "
            "be explicitly the 'Non-Display OpenAPI' variant rather than a regular app/QT subscription -- "
            "see this script's module docstring. Re-run this script again later before concluding it's "
            "still not working."
        )
    elif _classify(avg_delay) == "REAL-TIME":
        print("This account's sandbox data is real-time -- the Nasdaq Basic subscription is working.")
    else:
        print(
            "Delay doesn't cleanly match either real-time or the documented 15-minute delay -- re-run "
            "during a period of active trading on a more liquid symbol before drawing a conclusion."
        )


if __name__ == "__main__":
    main()
