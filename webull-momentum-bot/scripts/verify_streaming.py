#!/usr/bin/env python3
"""
One-off live check for whether Webull's MQTT Data Streaming API is usable
against THIS project's sandbox account -- the question this project's own
docstrings (brokers/webull/client.py, this module's module docstring
before today) have flagged as unresolved since the WebullBrokerClient was
first built: "Its constructor needs an http_host/mqtt_host; the production
values are documented (data-api.webull.com) but no sandbox equivalent was
found or confirmed live."

Static SDK inspection (2026-08-11, this session) found real, usable
progress on that question, but NOT a full answer -- this script exists to
get the rest of it live, the same way every other "is X really supported"
question in this project has been settled (see scripts/verify_bracket_orders.py,
scripts/cancel_order.py):

CONFIRMED BY READING THE INSTALLED SDK (not yet live-tested):
  - webull.data.data_streaming_client.DataStreamingClient is real and
    implemented (MQTT via paho-mqtt, matching the docs the user pasted).
  - Its `mqtt_host` constructor arg, if left None, is auto-resolved via
    DefaultEndpointResolver -> LocalConfigRegionalEndpointResolver, which
    reads a config file bundled INSIDE the SDK package itself
    (webull/core/data/endpoints.json). For region "us" that file resolves
    quotes-api to exactly one host: data-api.webull.com -- the same
    PRODUCTION host this project's docs already knew about. There is no
    separate sandbox entry for quotes-api anywhere in that bundled
    config -- only one "us" entry total, used for both api/quotes-api/
    events-api types alike.
  - `session_id` is NOT obtained via any special handshake -- it's a
    plain caller-generated ID (e.g. a UUID) passed to both the MQTT
    client constructor and the subscribe REST call, used purely to
    correlate a specific MQTT connection with its REST-side subscription.
  - Connecting requires `on_connect_success` to be set BEFORE connecting
    (the underlying on-MQTT-connect callback raises
    SDK_INVALID_PARAMETER otherwise) -- and per the class's own design,
    the actual `client.subscribe(...)` call belongs INSIDE that callback,
    not immediately after connect_and_loop_start() returns (the MQTT
    connect itself happens on a background thread; subscribe must wait
    until that connection is actually established).
  - connect_and_loop_start() is non-blocking (runs the MQTT loop on a
    daemon thread) -- unlike connect_and_loop_forever(), which is why
    this script uses it: nothing here should be able to hang the whole
    process the way TradeClient/DataClient's construction can (documented
    2FA/token-check wait elsewhere in this project).

NOT CONFIRMED -- exactly what this script exists to find out:
  - Whether data-api.webull.com (the only host the SDK knows about at
    all) actually accepts and correctly streams data for THIS SANDBOX
    account's credentials, or whether it's production-only and a sandbox
    connection attempt fails auth / silently receives nothing.
  - Whether a separate "OpenAPI data-streaming entitlement" (mentioned in
    third-party research the user pasted, NOT Webull's own official docs
    directly cited in this codebase) is required and whether this account
    has it -- if it doesn't, expect an explicit rejection somewhere in
    this script's output (an HTTP error on the subscribe call, or an MQTT
    CONNACK failure) rather than silence.
  - The exact accepted `sub_types` string values for the subscribe REST
    call -- inferred from the payload-type constants used for DECODING
    incoming messages (webull/data/quotes/subscribe/payload_type.py:
    'quote'/'snapshot'/'tick', lowercase) and this project's established
    UPPERCASE convention for every other Webull enum-like string field
    (Category.US_STOCK.name, order sides/types, etc.) -- tries "QUOTE"
    first; if that's rejected, the raw error is what confirms the real
    value, not a second guess baked into this script.

Do NOT wire this into TradingLoop/WebullBrokerClient until this script has
actually run clean against the sandbox and printed real ticks -- exactly
the same rule this project applied to support_trading_session and the
OCO bracket feature before either was trusted.

FIRST LIVE RUN (2026-08-11, during real core trading hours -- 14:38 ET):
MQTT connect succeeded (on_connect_success fired), but the immediately-
following client.subscribe() call -- fired ~0.4s later, inside that same
callback, per the SDK's own intended design -- was rejected outright:

    417 INVALID_SESSION: "Mqtt connection not exist for session:<id>"

SECOND LIVE RUN (same day, ~9 minutes later): added a 2.0s delay between
MQTT connect and calling subscribe(), to test whether it was a timing
race (REST-side session registry not yet caught up to the MQTT handshake).
Identical error, verbatim, even with the delay -- **that theory is now
ruled out.**

Current leading theory instead: mqtt_host=None resolves to
data-api.webull.com -- the SDK's ONE bundled quotes-api host, with no
sandbox equivalent anywhere in its config (see above) -- meaning this
MQTT session is very likely being registered against Webull's PRODUCTION
quotes system, while the subscribe REST call correctly targets
api.sandbox.webull.com. Those would be two independent backend systems
with no shared session state -- "Mqtt connection not exist for session"
is exactly what you'd expect if a session created on one system is looked
up on the other, and it requires no missing entitlement to explain (this
project already separately confirmed sandbox/production are fully
separate systems for the REST API specifically -- see
brokers/webull/client.py's module docstring's "App key/secret are
environment-locked" note). This script now accepts --mqtt-host to test
the natural next hypothesis: a sandbox-specific quotes host following the
same naming pattern as the REST API's own sandbox/production split
(api.webull.com -> api.sandbox.webull.com), i.e.
data-api.sandbox.webull.com -- try that explicitly; if it doesn't even
resolve/connect, that's informative too (rules out that specific guess
rather than leaving it untested).

A separate, likely-unrelated artifact appeared ~10s after the failed
subscribe on both runs, during the SDK's own automatic reconnect attempt:
an MQTT CONNACK return code 1 ("Protocol not supported") on that specific
retry -- not yet understood, may be a session_id reuse issue in the retry
path; watch for whether it recurs regardless of which mqtt_host is used.

Usage: python scripts/verify_streaming.py SYMBOL [--seconds N] [--subscribe-delay N] [--mqtt-host HOST] [--sub-type TYPE]
  Connects (to HOST if given, else the SDK's own auto-resolved default --
  see above), waits `--subscribe-delay` seconds (default 2.0) after MQTT
  connect succeeds before subscribing to SYMBOL's TYPE data (default
  "QUOTE", confirmed live -- see below; the richer "SNAPSHOT" type, which
  carries price/open/high/low/volume instead of just bid/ask, is
  implemented by the same SDK/decoder machinery per static inspection but
  NOT yet live-tested), then waits up to `--seconds` (default 20) for a
  message to arrive, reports exactly what happened at each stage, and
  disconnects. Read-only -- places no orders, costs nothing to run.
"""
import sys
import time
import uuid

from webull_bot.config import get_settings

_DEFAULT_WAIT_SECONDS = 20.0
_DEFAULT_SUBSCRIBE_DELAY_SECONDS = 2.0


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    symbol = sys.argv[1].upper()
    wait_seconds = _DEFAULT_WAIT_SECONDS
    if "--seconds" in sys.argv:
        wait_seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    subscribe_delay_seconds = _DEFAULT_SUBSCRIBE_DELAY_SECONDS
    if "--subscribe-delay" in sys.argv:
        subscribe_delay_seconds = float(sys.argv[sys.argv.index("--subscribe-delay") + 1])
    mqtt_host = None
    if "--mqtt-host" in sys.argv:
        mqtt_host = sys.argv[sys.argv.index("--mqtt-host") + 1]
    sub_type = "QUOTE"
    if "--sub-type" in sys.argv:
        sub_type = sys.argv[sys.argv.index("--sub-type") + 1]

    settings = get_settings()
    print(f"mqtt_host={mqtt_host or '(SDK auto-resolved default)'}  sub_type={sub_type}")
    print(
        f"trading_mode={settings.trading_mode.value}  base_url={settings.webull.base_url}  symbol={symbol}  "
        f"subscribe_delay={subscribe_delay_seconds:.1f}s\n"
    )

    try:
        from webull.data.common.category import Category
        from webull.data.data_streaming_client import DataStreamingClient
    except ImportError as exc:
        print(f"Could not import the streaming client from the installed SDK: {exc!r}")
        print("(webull-openapi-python-sdk may be an older version without streaming support -- check `pip show webull`.)")
        sys.exit(1)

    events: list[str] = []
    messages: list[tuple[str, object]] = []
    session_id = str(uuid.uuid4())

    client = DataStreamingClient(
        settings.webull.app_key, settings.webull.app_secret, "us", session_id,
        http_host=settings.webull.base_url,  # sandbox REST host for the subscribe call
        mqtt_host=mqtt_host,  # None lets the SDK auto-resolve -- see this module's docstring for what that resolves to
    )

    def _on_connect_success(client_, api_client_, session_id_):
        events.append("connect_success")
        print(f"  [event] MQTT connected (session_id={session_id_})")
        if subscribe_delay_seconds > 0:
            # Testing the timing-race theory from this script's own
            # "FIRST LIVE RUN" note: sleeping here blocks the MQTT client's
            # own network-loop thread (this callback runs on it) for
            # `subscribe_delay_seconds` -- acceptable for a short,
            # diagnostic-only delay like this, not something a real
            # production integration should do.
            print(f"  [event] waiting {subscribe_delay_seconds:.1f}s before subscribing...")
            time.sleep(subscribe_delay_seconds)
        try:
            client_.subscribe([symbol], Category.US_STOCK.name, [sub_type])
        except Exception as exc:
            events.append(f"subscribe_raised:{exc!r}")
            print(f"  [event] client.subscribe() raised: {exc!r}")

    def _on_subscribe_success(client_, api_client_, session_id_):
        events.append("subscribe_success")
        print("  [event] subscribe request accepted (200) by the REST endpoint")

    def _on_quotes_message(client_, topic, payload):
        events.append(f"message:{topic}")
        messages.append((topic, payload))
        print(f"  [event] message received -- topic={topic!r} payload={payload!r}")

    client.on_connect_success = _on_connect_success
    client.on_subscribe_success = _on_subscribe_success
    client.on_quotes_message = _on_quotes_message

    print("Connecting (non-blocking, on_connect_success will fire the subscribe call once connected)...")
    try:
        client.connect_and_loop_start()
    except Exception as exc:
        print(f"connect_and_loop_start() raised immediately: {exc!r}")
        sys.exit(1)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(1.0)
        if messages:
            break  # got at least one real message -- no need to wait out the full window

    print(f"\nWaited up to {wait_seconds:.0f}s. Results:")
    print(f"  MQTT connect succeeded: {'connect_success' in events}")
    print(f"  Subscribe accepted:     {'subscribe_success' in events}")
    print(f"  Messages received:      {len(messages)}")
    if messages:
        print(f"  First message: topic={messages[0][0]!r} payload={messages[0][1]!r}")

    try:
        client.loop_stop()
        client.disconnect()
    except Exception:
        pass

    if not events:
        print(
            "\nNo events fired at all -- the MQTT connect itself likely never completed within the "
            "wait window (network/firewall issue, wrong resolved host, or the connection is silently "
            "hanging). Not evidence either way about sandbox support specifically."
        )
    elif "connect_success" not in events:
        print("\nConnect callback never fired -- see above for whether connect_and_loop_start() itself raised.")
    elif "subscribe_success" not in events and not any(e.startswith("subscribe_raised") for e in events):
        print(
            "\nConnected, but no subscribe_success and no raised exception either -- the REST subscribe "
            "call may have been rejected with a non-2xx status that didn't raise (check raise_for_status "
            "behavior) or is still pending. Re-run with a longer --seconds window."
        )
    elif not messages:
        print(
            "\nConnected and subscribed, but zero quote messages arrived. During real core trading hours "
            "for a liquid symbol (e.g. AAPL) this would be a strong signal that this sandbox account "
            "cannot actually receive streamed data -- possibly the missing 'standalone data-streaming "
            "entitlement' the user-provided research mentioned. Re-run during market hours if this ran "
            "outside them before concluding that."
        )
    else:
        print(
            "\nReal quote data arrived -- streaming IS usable against this sandbox account. Safe to build "
            "the actual TradingLoop/WebullBrokerClient integration now that this is live-confirmed, not "
            "just SDK-inspected."
        )


if __name__ == "__main__":
    main()
