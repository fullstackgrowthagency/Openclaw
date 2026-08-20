#!/usr/bin/env python3
"""
Read-only, stage-by-stage health check for the full discovery pipeline
(broker connectivity -> universe sources -> free-float providers ->
BroadScanner funnel -> live market data), built for a real incident
(2026-08-20): the dashboard's Candidates list was observed empty, and
diagnosing why turned up a real observability gap -- there is NO log line
anywhere in this codebase for "universe returned N symbols" or "scan
found M candidates". Today the only way to investigate is grepping for a
handful of FAILURE log lines ("Universe source %s failed", "Primary
float provider failed for", "Webull rate limit hit") -- there was no way
to positively confirm "the pipeline is healthy and genuinely found
nothing right now" versus "something is silently broken". This session
has no direct shell access to the VPS either, so every diagnosis
round-trips through the user pasting output -- this script exists so
that round-trip produces a single, complete report instead of another
round of "can you also grep for X".

Entirely read-only: no orders, no DB writes, no state mutation -- safe to
run at any time, including mid-session. ONE exception, noted where it
happens below: step 5's BroadScanner funnel check goes through the same
on-disk float-data cache (data/float_providers/cache.py) production
uses, so it may write/refresh cache files under settings.float_cache_dir
-- same cache format/TTL production already relies on, not a new side-
effect class, and deliberate: querying float data any other way there
would test something other than what BroadScanner.scan() actually
experiences.

Deliberately does NOT gate any check on market hours -- RiskEngine.evaluate
gates ENTRIES by market hours (risk/risk_engine.py), not discovery, and the
whole point of this script is confirming the pipeline works even when
nothing interesting exists to find right now (e.g. the middle of the
night).

Multi-tenant (2026-08-15 conversion, see auth/broker_credential_routes.py):
steps 1 (settings sanity) and 4 (float-provider connectivity) run ONCE,
since FMP/Massive/yfinance config is shared process-wide
(build_settings_for_user never overrides them per user). Steps 2/3/5/6
run once per stored BrokerCredential row (scripts/find_account_id.py's
iterate-every-user pattern), since each user's Webull credentials and
rate-limit budget are independent.

Usage: python scripts/system_check.py
Needs the same env vars/DB the bot itself runs with (DATABASE_URL,
CREDENTIAL_ENCRYPTION_KEY, FMP_API_KEY, ...) -- run this on the VPS, in
the same environment the systemd service uses.
"""
import time

from webull_bot.auth.broker_credential_routes import build_settings_for_user
from webull_bot.brokers import get_broker_client
from webull_bot.brokers.webull.retry import is_rate_limited
from webull_bot.config import Settings, get_settings
from webull_bot.data.float_providers import get_float_provider
from webull_bot.data.universe import (
    WebullGainersLosersConfig,
    WebullGainersLosersUniverseProvider,
    WebullUniverseConfig,
    WebullUniverseProvider,
)
from webull_bot.db.models import BrokerCredential, User
from webull_bot.db.session import session_scope
from webull_bot.scanner.broad_scanner import BroadScanner

# Fixed, known-liquid sample for the standalone float-provider connectivity
# check (step 4) -- deliberately NOT drawn from today's actual momentum
# universe (step 3), since real candidates are often obscure micro-caps
# that may legitimately have no FMP/Yahoo coverage; using them here would
# conflate "the provider/credentials are broken" with "this one ticker
# isn't in FMP's database", muddying exactly the signal this check exists
# to produce.
_FLOAT_CHECK_SYMBOLS = ["AAPL", "MSFT", "TSLA", "AMD", "F"]

# Caps to keep a full run bounded -- BroadScanner.scan()'s own thread pool
# is fast (batched snapshot fetch), but check_symbol_verbose (used for the
# per-filter-stage breakdown below) is sequential, uncached, real network
# round-trips paced by Webull's rate limiter -- so it gets a much smaller
# sample.
_SCAN_UNIVERSE_CAP = 300
_REASON_BREAKDOWN_SAMPLE_CAP = 25
_SNAPSHOT_CHECK_FALLBACK_SYMBOL = "AAPL"


def _mask(value: str) -> str:
    """Same convention as auth/broker_credential_routes.py's _mask -- kept
    local rather than imported since that helper is module-private."""
    return f"...{value[-4:]}" if len(value) >= 4 else "****"


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def check_settings_sanity(settings: Settings) -> None:
    _section("1. Settings sanity (global)")
    print(f"  FMP configured:      {'PASS' if settings.fmp.is_configured() else 'FAIL -- FMP_API_KEY not set'}")
    print(f"  Massive configured:  {'PASS' if settings.massive.is_configured() else 'FAIL -- MASSIVE_API_KEY not set'}")
    print(f"  yfinance fallback:   {'enabled' if settings.enable_yfinance_fallback else 'disabled'}")
    print(f"  session_secret_key:  {'set' if settings.session_secret_key else 'FAIL -- not set'}")
    print(f"  credential_encryption_key: {'set' if settings.credential_encryption_key else 'FAIL -- not set'}")
    if not settings.fmp.is_configured() and not settings.massive.is_configured():
        print("  *** No free-float provider configured at all -- get_float_provider() will raise. ***")


def connect_broker_for_user(user: User, cred: BrokerCredential, process_settings: Settings):
    """Mirrors scripts/find_account_id.py's per-user loop: decrypt this
    user's real stored credentials, connect, and call the cheapest real
    auth-proving call (account_v2.get_account_list()). Returns
    (user_settings, broker) on success, (None, None) on any failure --
    printed as a FAIL, never propagated, so one broken user doesn't stop
    the rest of this script's checks for everyone else."""
    print(f"\n--- user_id={user.id} email={user.email} trading_mode={cred.trading_mode} ---")
    try:
        user_settings = build_settings_for_user(cred, process_settings)
    except Exception as exc:
        print(f"  2. Broker connectivity: FAIL -- could not decrypt/build settings: {exc!r}")
        return None, None

    print(f"  app_key={_mask(user_settings.webull.app_key)} account_id={_mask(user_settings.webull.account_id)}")
    try:
        broker = get_broker_client(user_settings)
        broker.connect()
        trade_client = broker._require_trade_client()
        accounts = trade_client.account_v2.get_account_list()
        accounts.raise_for_status()
        print(f"  2. Broker connectivity: PASS -- {len(accounts.json())} account(s) visible")
        return user_settings, broker
    except Exception as exc:
        print(f"  2. Broker connectivity: FAIL -- {exc!r}")
        return None, None


def check_universe_sources(broker) -> list[str]:
    """Reconstructs the same 7 discovery sources main.py's build_trading_loop
    wires up (exact rank_value_field/min_rank_value thresholds copied
    verbatim from there), calling .get_symbols() on each individually
    rather than through MultiSourceUniverseProvider -- which would only
    give one opaque combined total. This is the literal "N symbols per
    source" signal that doesn't exist anywhere else in this codebase."""
    _section("3. Universe sources (per user)")
    sources = [
        ("RELATIVE_VOLUME_10D", WebullUniverseProvider.from_broker(broker, WebullUniverseConfig(
            rank_type="RELATIVE_VOLUME_10D", rank_value_field="relative_volume_10d", min_rank_value=2.0,
        ))),
        ("TURNOVER_RATE", WebullUniverseProvider.from_broker(broker, WebullUniverseConfig(
            rank_type="TURNOVER_RATE", rank_value_field="turnover_rate", min_rank_value=0.10,
        ))),
        ("DAY_1", WebullGainersLosersUniverseProvider.from_broker(broker, WebullGainersLosersConfig(
            rank_type="DAY_1", sort_by="CHANGE_RATIO", direction="DESC",
            rank_value_field="change_ratio", min_rank_value=0.10,
        ))),
        ("MIN_5", WebullGainersLosersUniverseProvider.from_broker(broker, WebullGainersLosersConfig(
            rank_type="MIN_5", sort_by="CHANGE_RATIO", direction="DESC",
            rank_value_field="change_ratio", min_rank_value=0.05,
        ))),
        ("PRE_MARKET", WebullGainersLosersUniverseProvider.from_broker(broker, WebullGainersLosersConfig(
            rank_type="PRE_MARKET", sort_by="CHANGE_RATIO", direction="DESC",
            rank_value_field="change_ratio", min_rank_value=0.15,
        ))),
        ("AFTER_MARKET", WebullGainersLosersUniverseProvider.from_broker(broker, WebullGainersLosersConfig(
            rank_type="AFTER_MARKET", sort_by="CHANGE_RATIO", direction="DESC",
            rank_value_field="change_ratio", min_rank_value=0.15,
        ))),
        ("AMPLITUDE", WebullUniverseProvider.from_broker(broker, WebullUniverseConfig(
            rank_type="AMPLITUDE", rank_value_field="amplitude", min_rank_value=10.0,
        ))),
    ]

    combined: list[str] = []
    seen: set[str] = set()
    for name, provider in sources:
        try:
            symbols = provider.get_symbols()
            print(f"  {name:20s} PASS -- {len(symbols)} symbol(s)")
        except Exception as exc:
            print(f"  {name:20s} FAIL -- {exc!r}")
            continue
        for symbol in symbols:
            if symbol not in seen:
                seen.add(symbol)
                combined.append(symbol)

    print(f"  Combined, deduped: {len(combined)} unique symbol(s) across all sources")
    return combined


def check_float_providers(settings: Settings) -> None:
    """Constructs FMPFloatProvider/YFinanceFloatProvider DIRECTLY, bypassing
    get_float_provider()'s cache/fallback wrapping, so this check stays
    side-effect-free and surfaces a yfinance failure on its own merits --
    yfinance failures are currently not logged anywhere else in this
    codebase."""
    _section("4. Free-float provider connectivity (global)")

    if settings.fmp.is_configured():
        from webull_bot.data.float_providers.fmp import FMPFloatProvider

        try:
            fmp_provider = FMPFloatProvider(settings.fmp)
            for symbol in _FLOAT_CHECK_SYMBOLS:
                try:
                    data = fmp_provider.get_float_data(symbol)
                    print(f"  FMP {symbol:6s} PASS -- free_float_shares={data.free_float_shares:,.0f}")
                except Exception as exc:
                    print(f"  FMP {symbol:6s} FAIL -- {exc!r}")
        except Exception as exc:
            print(f"  FMP: FAIL to construct provider -- {exc!r}")
    else:
        print("  FMP: skipped -- not configured")

    if settings.enable_yfinance_fallback:
        from webull_bot.data.float_providers.yfinance_provider import YFinanceFloatProvider

        yf_provider = YFinanceFloatProvider()
        for symbol in _FLOAT_CHECK_SYMBOLS:
            try:
                data = yf_provider.get_float_data(symbol)
                print(f"  yfinance {symbol:6s} PASS -- free_float_shares={data.free_float_shares:,.0f}")
            except Exception as exc:
                print(f"  yfinance {symbol:6s} FAIL -- {exc!r}")
    else:
        print("  yfinance fallback: skipped -- disabled")


def check_broad_scanner_funnel(user_settings: Settings, broker, universe: list[str]) -> None:
    """Part A runs the real production entrypoint (BroadScanner.scan())
    against the universe from step 3. Part B runs check_symbol_verbose --
    already used by the dashboard's single-symbol scan endpoint -- against
    a smaller sample and buckets the returned rejection-reason strings.
    This bucketing is a substring match against broad_scanner.py's CURRENT
    wording, not a structurally exposed reason code -- an unrecognized
    string lands in "other", it does not raise, but it also means this
    will silently stop categorizing correctly if that wording changes."""
    _section("5. BroadScanner funnel (per user)")
    if not universe:
        print("  Skipped -- no universe symbols to scan (see step 3).")
        return

    try:
        float_provider = get_float_provider(user_settings)
    except Exception as exc:
        print(f"  FAIL -- could not build float provider: {exc!r}")
        return

    scanner = BroadScanner(broker, float_provider)
    scan_universe = universe[:_SCAN_UNIVERSE_CAP]
    try:
        candidates = scanner.scan(scan_universe)
        print(f"  scan(): {len(candidates)} candidate(s) found out of {len(scan_universe)} scanned "
              f"(universe had {len(universe)} total, capped at {_SCAN_UNIVERSE_CAP})")
    except Exception as exc:
        print(f"  scan(): FAIL -- {exc!r}")

    sample = universe[:_REASON_BREAKDOWN_SAMPLE_CAP]
    print(f"  Per-symbol rejection reasons for a {len(sample)}-symbol sample "
          f"(sequential, uncached -- not the full universe):")
    buckets: dict[str, int] = {}
    for symbol in sample:
        try:
            _candidate, reason = scanner.check_symbol_verbose(symbol)
        except Exception as exc:
            reason = f"unexpected exception: {exc!r}"
        if reason is None:
            bucket = "passed"
        elif "Failed to fetch a market snapshot" in reason:
            bucket = "snapshot fetch failed"
        elif "is outside the allowed range" in reason:
            bucket = "price out of range"
        elif "Failed to fetch free-float data" in reason:
            bucket = "float fetch failed"
        elif "exceeds the" in reason and "share ceiling" in reason:
            bucket = "float ceiling exceeded"
        elif "below their floors" in reason:
            bucket = "volume floor"
        else:
            bucket = "other/unrecognized reason"
        buckets[bucket] = buckets.get(bucket, 0) + 1
    for bucket, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
        print(f"    {bucket:28s} {count}")


def check_snapshot_and_rate_limit(broker, symbol: str) -> None:
    _section("6. Live market data / rate limit (per user)")
    start = time.perf_counter()
    try:
        snapshot = broker.get_snapshot(symbol)
        elapsed = time.perf_counter() - start
        print(f"  get_snapshot({symbol}): PASS in {elapsed:.2f}s -- "
              f"last_price={snapshot.last_price} timestamp={snapshot.timestamp}")
    except Exception as exc:
        elapsed = time.perf_counter() - start
        if is_rate_limited(exc):
            print(f"  get_snapshot({symbol}): FAIL (RATE LIMITED, 429) after {elapsed:.2f}s -- {exc!r}")
        else:
            print(f"  get_snapshot({symbol}): FAIL after {elapsed:.2f}s -- {exc!r}")


def main() -> None:
    process_settings = get_settings()

    check_settings_sanity(process_settings)
    check_float_providers(process_settings)

    with session_scope(process_settings) as session:
        rows = session.query(BrokerCredential, User).join(User, BrokerCredential.user_id == User.id).all()

    if not rows:
        print("\nNo broker_credentials rows found in the DB -- nothing more to check.")
        return

    print(f"\nFound {len(rows)} stored broker credential(s) across all users.")
    for cred, user in rows:
        user_settings, broker = connect_broker_for_user(user, cred, process_settings)
        if broker is None:
            continue  # step 2 already printed FAIL -- nothing else to check for this user

        universe = check_universe_sources(broker)
        check_broad_scanner_funnel(user_settings, broker, universe)
        sample_symbol = universe[0] if universe else _SNAPSHOT_CHECK_FALLBACK_SYMBOL
        check_snapshot_and_rate_limit(broker, sample_symbol)

    print("\nDone.")


if __name__ == "__main__":
    main()
