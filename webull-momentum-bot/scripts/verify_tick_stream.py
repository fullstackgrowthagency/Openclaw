#!/usr/bin/env python3
"""
One-off live check for the TICK streaming sub-type's real `side` field
encoding -- the single unconfirmed piece blocking
brokers/webull/client.py's _TICK_SIDE_MAP (and therefore
metrics.order_flow_imbalance / MomentumScoreComponents.order_flow_score /
TradingLoopConfig.order_flow_sell_pressure_threshold) from being trusted
in production. See docs/ARCHITECTURE.md's "TICK-derived order flow"
section for the full design this unblocks.

CONFIRMED BY READING THE INSTALLED SDK (2026-08-14, not yet live-tested):
  - TICK is a real, already-registered streaming sub-type
    (webull.data.quotes.subscribe.tick_decoder.TickDecoder, pre-registered
    by DataStreamingClient.__init__ for payload type 'tick') -- the same
    mechanism SNAPSHOT/QUOTE already use, confirmed live 2026-08-11 (see
    verify_streaming.py). No new connection/auth mechanism to verify here,
    only the payload's own field values.
  - TickResult exposes basic/time/price/volume/side. `price` and `volume`
    are plain numeric strings on the wire (Decimal/no-cast respectively in
    the SDK's own wrapper -- see tick_result.py), same convention as every
    other field in this schema.
  - `side` is ALSO a plain string on the wire (protobuf field type STRING,
    confirmed by inspecting Tick's own descriptor -- not an enum, despite
    looking like one) with NO cast applied by the SDK wrapper at all. Its
    real values have never been observed against a live message.

NOT CONFIRMED -- exactly what this script exists to find out:
  - The real string value(s) `side` takes for a buyer-initiated print vs.
    a seller-initiated one. brokers/webull/client.py's _TICK_SIDE_MAP
    currently guesses "1"/"B"/"BUY" -> BUY and "2"/"S"/"SELL" -> SELL,
    with everything else falling through to UNKNOWN (a deliberately safe
    default -- see that map's own docstring). This script's summary
    compares each tick's price against the QUOTE feed's simultaneous
    bid/ask to independently INFER the likely real side (a print at/above
    the ask is conventionally buyer-initiated, at/below the bid seller-
    initiated), then cross-tabulates that inference against the raw
    `side` string actually reported -- if they consistently agree, that
    confirms (or corrects) the map; if they don't, the map needs rework
    beyond a simple string swap.
  - Whether `side` is ever the empty string (Tick.side falsy -> `None` in
    the wrapper, per tick_result.py's `if pb_tick.side else None`) for a
    meaningful fraction of prints, which would mean a real class of trades
    is structurally unclassifiable rather than just mismapped.

Usage: python scripts/verify_tick_stream.py SYMBOL [--seconds N]
  Subscribes to TICK and QUOTE (not SNAPSHOT -- irrelevant here) for
  SYMBOL, prints every raw tick as it arrives alongside the current
  best bid/ask, waits up to `--seconds` (default 60 -- longer than
  verify_streaming.py's default since a real trade print, unlike a quote
  update, may take a while on a quiet symbol; use an actively-moving
  low-float name during core hours for a fast, meaningful sample) for
  ticks to arrive, then prints a summary table: raw `side` value ->
  count, and how often each raw value's prints landed at/above the ask
  vs. at/below the bid vs. in between. Read-only -- places no orders,
  costs nothing to run.

Run this during real core trading hours against a symbol that's actually
trading (a quiet/halted symbol will simply produce zero ticks, which is
NOT evidence the sub-type itself doesn't work -- see verify_streaming.py's
own note about distinguishing "no data" from "not supported").
"""
import sys
import time
import uuid
from collections import Counter, defaultdict

from webull_bot.config import get_settings

_DEFAULT_WAIT_SECONDS = 60.0


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

    try:
        from webull.data.common.category import Category
        from webull.data.data_streaming_client import DataStreamingClient
    except ImportError as exc:
        print(f"Could not import the streaming client from the installed SDK: {exc!r}")
        sys.exit(1)

    # mqtt_host: sandbox needs the confirmed data-api.sandbox.webull.com
    # host explicitly (see verify_streaming.py/brokers/webull/client.py's
    # subscribe_quotes docstring) -- the SDK's own auto-resolution only
    # knows the production host.
    from webull_bot.config import TradingMode
    mqtt_host = "data-api.sandbox.webull.com" if settings.trading_mode == TradingMode.SANDBOX else None

    best_bid = best_ask = None
    ticks: list[dict] = []
    session_id = str(uuid.uuid4())

    client = DataStreamingClient(
        settings.webull.app_key, settings.webull.app_secret, "us", session_id,
        http_host=settings.webull.base_url,
        mqtt_host=mqtt_host,
    )

    def _on_connect_success(client_, api_client_, session_id_):
        print(f"  [event] MQTT connected (session_id={session_id_}); subscribing to TICK+QUOTE for {symbol}...")
        try:
            client_.subscribe([symbol], Category.US_STOCK.name, ["QUOTE", "TICK"])
        except Exception as exc:
            print(f"  [event] subscribe raised: {exc!r}")

    def _on_quotes_message(client_, topic, payload):
        nonlocal best_bid, best_ask
        if topic == "quote":
            asks, bids = payload.asks, payload.bids
            if asks and asks[0].price is not None:
                best_ask = float(asks[0].price)
            if bids and bids[0].price is not None:
                best_bid = float(bids[0].price)
        elif topic == "tick":
            price = float(payload.price) if payload.price is not None else None
            volume = payload.volume
            raw_side = payload.side
            inferred = "?"
            if price is not None and best_bid is not None and best_ask is not None:
                if price >= best_ask:
                    inferred = "BUY (at/above ask)"
                elif price <= best_bid:
                    inferred = "SELL (at/below bid)"
                else:
                    inferred = "mid (between bid/ask)"
            ticks.append({"price": price, "volume": volume, "raw_side": raw_side, "inferred": inferred})
            print(
                f"  [tick] price={price} volume={volume} raw_side={raw_side!r} "
                f"bid={best_bid} ask={best_ask} -> inferred={inferred}"
            )

    client.on_connect_success = _on_connect_success
    client.on_quotes_message = _on_quotes_message

    print("Connecting (non-blocking, subscribe fires once connected)...")
    try:
        client.connect_and_loop_start()
    except Exception as exc:
        print(f"connect_and_loop_start() raised immediately: {exc!r}")
        sys.exit(1)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(1.0)

    try:
        client.loop_stop()
        client.disconnect()
    except Exception:
        pass

    print(f"\nWaited {wait_seconds:.0f}s. Received {len(ticks)} tick(s).")
    if not ticks:
        print(
            "No ticks arrived -- either the symbol simply didn't trade during this window (try a more "
            "active one, or run during core hours), or TICK isn't actually delivering data for this "
            "account. Not distinguishable from this run alone; re-run with a known-liquid, actively "
            "moving symbol before concluding anything about TICK support itself."
        )
        return

    print("\nRaw `side` value -> count:")
    for value, count in Counter(t["raw_side"] for t in ticks).most_common():
        print(f"  {value!r}: {count}")

    print("\nRaw `side` value -> inferred direction breakdown (use this to build/correct _TICK_SIDE_MAP):")
    by_value: dict = defaultdict(Counter)
    for t in ticks:
        by_value[t["raw_side"]][t["inferred"]] += 1
    for value, breakdown in by_value.items():
        print(f"  side={value!r}:")
        for inferred, count in breakdown.most_common():
            print(f"      {inferred}: {count}")

    print(
        "\nIf a given raw `side` value overwhelmingly correlates with one inferred direction, that's your "
        "real mapping -- update brokers/webull/client.py's _TICK_SIDE_MAP accordingly (and remove any "
        "guessed entries that turned out wrong or unused). If the correlation is weak/mixed, the "
        "at/above-ask vs. at/below-bid heuristic itself may not hold cleanly for this symbol's spread -- "
        "try a more liquid one, or widen the sample."
    )


if __name__ == "__main__":
    main()
