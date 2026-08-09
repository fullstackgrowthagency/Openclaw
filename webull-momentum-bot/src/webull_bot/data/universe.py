"""
Symbol universe providers for the BroadScanner's first pass.

This is deliberately separate from BrokerClient: PaperBrokerClient/backtests
get their universe handed to them directly by the caller, so "what feeds the
scanner a list of symbols" is a data-source concern, not something every
broker backend needs to implement.

Multiple independent sources are combined via MultiSourceUniverseProvider
(a plain union, not a priority-ordered fallback chain) so a ticker only
needs to show up on ONE list to reach BroadScanner -- which is what
actually vets it (price/free-float structurally; dollar volume/average
volume are informational only, see scanner/broad_scanner.py), independent
of which list(s) surfaced it. TradingLoop scans every symbol this returns
-- there is no cap that could cause a ticker to be silently dropped before
BroadScanner ever sees it.

Pagination (confirmed live 2026-08-09): both get_most_active and
get_gainers_losers accept page_index (1-based)/page_size and return a
`has_more` flag -- pages were confirmed non-overlapping (no duplicate
symbols across 10 consecutive pages of 50). This module paginates instead
of taking a single fixed-size page, since the earlier flat page_size=100
was an arbitrary internal cap, not a real API limit -- see
_paginate_screener below for the actual stopping rule, since blindly
paginating until has_more=False can go very deep (confirmed live: still
True at 1000+ rows for RELATIVE_VOLUME_10D) into territory that's no
longer a meaningful momentum signal.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class SymbolUniverseProvider(ABC):
    @abstractmethod
    def get_symbols(self) -> list[str]:
        ...


def _filter_screener_rows(
    rows: list[dict], *, min_price: float, max_price: float, max_market_value: Optional[float]
) -> list[str]:
    """Shared cheap prefilter applied to raw rows from any Webull screener
    endpoint -- all of them return the same symbol/price/market_value shape
    (confirmed live for both get_most_active and get_gainers_losers)."""
    symbols = []
    for row in rows:
        try:
            price = float(row["price"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (min_price <= price <= max_price):
            continue
        if max_market_value is not None:
            market_value = row.get("market_value")
            try:
                if market_value is not None and float(market_value) > max_market_value:
                    continue
            except ValueError:
                pass
        symbols.append(row["symbol"])
    return symbols


def _page_below_rank_threshold(rows: list[dict], *, rank_value_field: Optional[str], min_rank_value: Optional[float]) -> bool:
    """True once a page's *minimum* value for the ranking field drops below
    min_rank_value. Results are sorted descending by that field, so once
    this trips, every subsequent page is guaranteed even less relevant --
    this is what lets pagination stop at a real, data-driven "no longer
    interesting" boundary instead of an arbitrary page/result count."""
    if rank_value_field is None or min_rank_value is None:
        return False
    values = []
    for row in rows:
        raw = row.get(rank_value_field)
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return bool(values) and min(values) < min_rank_value


def _paginate_screener(
    fetch_page: Callable[[int], "object"],
    *,
    min_price: float,
    max_price: float,
    max_market_value: Optional[float],
    rank_value_field: Optional[str],
    min_rank_value: Optional[float],
    max_pages: int,
) -> list[str]:
    """Shared pagination loop for both screener endpoints below. Stops on
    whichever comes first: an empty page, the value-based rank threshold
    (see _page_below_rank_threshold), has_more=False, or max_pages -- the
    last one is a safety valve against pathological pagination (e.g. a
    has_more flag that never turns False), not a deliberate content cap;
    see WebullUniverseConfig.max_pages for why it's set generously."""
    symbols: list[str] = []
    for page in range(1, max_pages + 1):
        response = fetch_page(page)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data", [])
        if not rows:
            break
        symbols.extend(_filter_screener_rows(
            rows, min_price=min_price, max_price=max_price, max_market_value=max_market_value,
        ))
        if _page_below_rank_threshold(rows, rank_value_field=rank_value_field, min_rank_value=min_rank_value):
            break
        if not payload.get("has_more", False):
            break
    return symbols


@dataclass
class WebullUniverseConfig:
    # RELATIVE_VOLUME_10D directly matches the project's "high relative
    # volume" criterion -- confirmed live against the sandbox screener
    # endpoint (/openapi/market-data/screener/top-active) on 2026-08-08.
    # TURNOVER_RATE (% of float traded today) is an equally-valid rank_type
    # on this same endpoint -- confirmed live on 2026-08-08 to surface a
    # meaningfully different (generally more extreme, lower-priced) set of
    # names than RELATIVE_VOLUME_10D, since it's directly analogous to the
    # float_turnover metric the Momentum Ignition Score already computes.
    rank_type: str = "RELATIVE_VOLUME_10D"
    page_size: int = 100  # confirmed live 2026-08-08 that this endpoint accepts page_size=100
    # Trading price range this bot operates in -- momentum/scalping names,
    # not deep penny stocks or high-priced large caps.
    min_price: float = 0.40
    max_price: float = 25.00
    max_market_value: Optional[float] = 2_000_000_000  # cheap prefilter; free float is checked properly later via FMP
    # See _page_below_rank_threshold: only set these for a rank_type this
    # config has a live-calibrated stopping value for. RELATIVE_VOLUME_10D
    # is the one case with an existing calibrated number to reuse
    # (scoring/weights.yaml's min_relative_volume_for_watch) -- see the
    # from_broker call site in main.py that sets these for that instance.
    # TURNOVER_RATE has no such calibration yet, so its instance leaves
    # these None and relies on max_pages below instead of a fabricated
    # threshold.
    rank_value_field: Optional[str] = None
    min_rank_value: Optional[float] = None
    # Safety valve against pathological pagination (e.g. a has_more flag
    # that never turns False) -- confirmed live 2026-08-09 that
    # RELATIVE_VOLUME_10D's has_more was still True 1000+ rows deep, so
    # this is not meant to be hit under normal operation when
    # min_rank_value is set; it's a last-resort circuit breaker, not a
    # deliberate content cap. At page_size=100 this is up to 2000 raw
    # results (before price/market-cap filtering) per source per cycle.
    max_pages: int = 20


class WebullUniverseProvider(SymbolUniverseProvider):
    """Wraps DataClient.screener.get_most_active(). Requires an already-connected
    WebullBrokerClient's underlying DataClient -- constructed via
    `from_broker()` rather than taking raw credentials, since the SDK's
    DataClient does its own auth handshake on construction and we don't want
    two independent sessions for one process."""

    def __init__(self, data_client, config: Optional[WebullUniverseConfig] = None):
        self._data_client = data_client
        self.config = config or WebullUniverseConfig()

    @classmethod
    def from_broker(cls, webull_broker, config: Optional[WebullUniverseConfig] = None) -> "WebullUniverseProvider":
        return cls(webull_broker._require_data_client(), config)  # noqa: SLF001 -- intentional, same package

    def get_symbols(self) -> list[str]:
        from webull.data.common.category import Category

        from ..brokers.webull.retry import call_with_retry

        def fetch_page(page_index: int):
            return call_with_retry(lambda: self._data_client.screener.get_most_active(
                Category.US_STOCK.name,
                rank_type=self.config.rank_type,
                sort_by="DESC",
                page_index=str(page_index),
                page_size=str(self.config.page_size),
            ))

        return _paginate_screener(
            fetch_page,
            min_price=self.config.min_price, max_price=self.config.max_price,
            max_market_value=self.config.max_market_value,
            rank_value_field=self.config.rank_value_field, min_rank_value=self.config.min_rank_value,
            max_pages=self.config.max_pages,
        )


@dataclass
class WebullGainersLosersConfig:
    # rank_type here is a TIME PERIOD (confirmed live 2026-08-08 -- this
    # endpoint's rank_type is unrelated to get_most_active's rank_type,
    # despite the shared parameter name): PRE_MARKET, AFTER_MARKET, MIN_3,
    # MIN_5, DAY_1, DAY_5, MONTH_1, MONTH_3, WEEK_52. DAY_1 = today's move,
    # MIN_5 = the last 5 minutes' move -- see WebullGainersLosersUniverseProvider's
    # docstring for how MIN_5 is used as a 4th discovery source.
    rank_type: str = "DAY_1"
    sort_by: str = "CHANGE_RATIO"   # rank by % price change, not volume/turnover/etc.
    direction: str = "DESC"         # DESC = gainers, ASC = losers
    page_size: int = 100
    min_price: float = 0.40
    max_price: float = 25.00
    max_market_value: Optional[float] = 2_000_000_000
    # No live-calibrated stopping value exists for change_ratio decay (unlike
    # RELATIVE_VOLUME_10D -- see WebullUniverseConfig), so these default to
    # None and pagination relies on max_pages/has_more instead of a
    # fabricated threshold. Can still be set explicitly if one is calibrated
    # later.
    rank_value_field: Optional[str] = None
    min_rank_value: Optional[float] = None
    max_pages: int = 20  # same safety-valve rationale as WebullUniverseConfig.max_pages


class WebullGainersLosersUniverseProvider(SymbolUniverseProvider):
    """Wraps DataClient.screener.get_gainers_losers() -- today's top %
    price movers, independent of the get_most_active-based providers above.
    A stock can show up here well before it shows up on a volume/turnover
    ranking, especially early in a move.

    Also doubles as the 4th discovery source when configured with
    rank_type="MIN_5": confirmed live (2026-08-09) this endpoint supports a
    genuine 5-minute price-change ranking distinct from DAY_1 (e.g. a real
    pull surfaced stocks up double-digit % in just the last 5 minutes that
    weren't necessarily among today's overall leaders yet) -- this is
    Webull's real, verified equivalent of "most active last 5 minutes" for
    *price*. Also confirmed: there is no equivalent 5-minute *volume*
    ranking -- get_gainers_losers' `volume` field reflects the whole day's
    cumulative volume regardless of rank_type, and get_most_active has no
    time-windowed rank_type at all. A synthetic volume-based 5-minute
    signal is tracked per-candidate instead, once a symbol is already being
    watched -- see MomentumMetrics.volume_5m/float_velocity_5m in
    metrics/rolling.py, computed from real per-candidate history rather
    than invented from a screener endpoint that doesn't exist."""

    def __init__(self, data_client, config: Optional[WebullGainersLosersConfig] = None):
        self._data_client = data_client
        self.config = config or WebullGainersLosersConfig()

    @classmethod
    def from_broker(
        cls, webull_broker, config: Optional[WebullGainersLosersConfig] = None
    ) -> "WebullGainersLosersUniverseProvider":
        return cls(webull_broker._require_data_client(), config)  # noqa: SLF001 -- intentional, same package

    def get_symbols(self) -> list[str]:
        from webull.data.common.category import Category

        from ..brokers.webull.retry import call_with_retry

        def fetch_page(page_index: int):
            return call_with_retry(lambda: self._data_client.screener.get_gainers_losers(
                rank_type=self.config.rank_type,
                category=Category.US_STOCK.name,
                sort_by=self.config.sort_by,
                direction=self.config.direction,
                page_index=str(page_index),
                page_size=str(self.config.page_size),
            ))

        return _paginate_screener(
            fetch_page,
            min_price=self.config.min_price, max_price=self.config.max_price,
            max_market_value=self.config.max_market_value,
            rank_value_field=self.config.rank_value_field, min_rank_value=self.config.min_rank_value,
            max_pages=self.config.max_pages,
        )


class MultiSourceUniverseProvider(SymbolUniverseProvider):
    """Combines several independent SymbolUniverseProviders into one
    deduplicated list. This is a plain union, not a priority-ordered
    fallback chain -- every source is queried on every call, and a symbol
    only needs to appear on ONE list to be included. Each source is
    independent: one raising an exception is logged and skipped rather than
    aborting the whole scan, so a single broken/rate-limited source doesn't
    starve the others.

    Symbols are deduplicated but otherwise left in source order (source 1's
    symbols, then source 2's new symbols, etc.) -- TradingLoop._rescan_universe
    no longer truncates the combined list, so every symbol from every
    source gets scanned regardless of order. An earlier version of this
    class interleaved results round-robin across sources specifically to
    stop truncation from favoring whichever source came first; now that
    nothing truncates this list, that interleaving had nothing left to
    protect against, so it was simplified away."""

    def __init__(self, providers: list[SymbolUniverseProvider]):
        self.providers = providers

    def get_symbols(self) -> list[str]:
        seen: set[str] = set()
        combined: list[str] = []
        for provider in self.providers:
            try:
                symbols = provider.get_symbols()
            except Exception:
                logger.exception("Universe source %s failed; skipping it this cycle.", type(provider).__name__)
                continue
            for symbol in symbols:
                if symbol not in seen:
                    seen.add(symbol)
                    combined.append(symbol)
        return combined


class StaticUniverseProvider(SymbolUniverseProvider):
    """Fixed symbol list -- useful for tests, manual watchlists, or backtests."""

    def __init__(self, symbols: list[str]):
        self._symbols = list(symbols)

    def get_symbols(self) -> list[str]:
        return list(self._symbols)
