"""
Symbol universe providers for the BroadScanner's first pass.

This is deliberately separate from BrokerClient: PaperBrokerClient/backtests
get their universe handed to them directly by the caller, so "what feeds the
scanner a list of symbols" is a data-source concern, not something every
broker backend needs to implement.

Multiple independent sources are combined via MultiSourceUniverseProvider
(a plain union, not a priority-ordered fallback chain) so a ticker only
needs to show up on ONE list to reach BroadScanner -- which is what
actually vets it (price/dollar-volume/free-float), independent of which
list(s) surfaced it.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

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
    page_size: int = 50
    min_price: float = 1.0
    max_price: float = 20.0
    max_market_value: Optional[float] = 2_000_000_000  # cheap prefilter; free float is checked properly later via FMP


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

        response = self._data_client.screener.get_most_active(
            Category.US_STOCK.name,
            rank_type=self.config.rank_type,
            sort_by="DESC",
            page_size=str(self.config.page_size),
        )
        response.raise_for_status()
        rows = response.json().get("data", [])
        return _filter_screener_rows(
            rows, min_price=self.config.min_price, max_price=self.config.max_price,
            max_market_value=self.config.max_market_value,
        )


@dataclass
class WebullGainersLosersConfig:
    # rank_type here is a TIME PERIOD (confirmed live 2026-08-08 -- this
    # endpoint's rank_type is unrelated to get_most_active's rank_type,
    # despite the shared parameter name): PRE_MARKET, AFTER_MARKET, MIN_3,
    # MIN_5, DAY_1, DAY_5, MONTH_1, MONTH_3, WEEK_52. DAY_1 = today's move.
    rank_type: str = "DAY_1"
    sort_by: str = "CHANGE_RATIO"   # rank by % price change, not volume/turnover/etc.
    direction: str = "DESC"         # DESC = gainers, ASC = losers
    page_size: int = 50
    min_price: float = 1.0
    max_price: float = 20.0
    max_market_value: Optional[float] = 2_000_000_000


class WebullGainersLosersUniverseProvider(SymbolUniverseProvider):
    """Wraps DataClient.screener.get_gainers_losers() -- today's top %
    price movers, independent of the get_most_active-based providers above.
    A stock can show up here well before it shows up on a volume/turnover
    ranking, especially early in a move."""

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

        response = self._data_client.screener.get_gainers_losers(
            rank_type=self.config.rank_type,
            category=Category.US_STOCK.name,
            sort_by=self.config.sort_by,
            direction=self.config.direction,
            page_size=str(self.config.page_size),
        )
        response.raise_for_status()
        rows = response.json().get("data", [])
        return _filter_screener_rows(
            rows, min_price=self.config.min_price, max_price=self.config.max_price,
            max_market_value=self.config.max_market_value,
        )


class MultiSourceUniverseProvider(SymbolUniverseProvider):
    """Combines several independent SymbolUniverseProviders into one
    deduplicated list. This is a plain union, not a priority-ordered
    fallback chain -- every source is queried on every call, and a symbol
    only needs to appear on ONE list to be included. Each source is
    independent: one raising an exception is logged and skipped rather than
    aborting the whole scan, so a single broken/rate-limited source doesn't
    starve the others.

    Results are interleaved round-robin across sources (one symbol from
    each list in turn) rather than concatenated source-by-source. This
    matters because the caller (TradingLoop._rescan_universe) truncates the
    combined list to a fixed size -- concatenating would let the first
    source in `providers` fill the entire cap before the others ever
    contributed a single symbol, which defeats the point of having
    independent sources at all."""

    def __init__(self, providers: list[SymbolUniverseProvider]):
        self.providers = providers

    def get_symbols(self) -> list[str]:
        per_source: list[list[str]] = []
        for provider in self.providers:
            try:
                per_source.append(provider.get_symbols())
            except Exception:
                logger.exception("Universe source %s failed; skipping it this cycle.", type(provider).__name__)
                per_source.append([])

        seen: set[str] = set()
        combined: list[str] = []
        max_len = max((len(s) for s in per_source), default=0)
        for i in range(max_len):
            for symbols in per_source:
                if i >= len(symbols):
                    continue
                symbol = symbols[i]
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
