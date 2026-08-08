"""
Symbol universe providers for the BroadScanner's first pass.

This is deliberately separate from BrokerClient: PaperBrokerClient/backtests
get their universe handed to them directly by the caller, so "what feeds the
scanner a list of symbols" is a data-source concern, not something every
broker backend needs to implement.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class SymbolUniverseProvider(ABC):
    @abstractmethod
    def get_symbols(self) -> list[str]:
        ...


@dataclass
class WebullUniverseConfig:
    # RELATIVE_VOLUME_10D directly matches the project's "high relative
    # volume" criterion -- confirmed live against the sandbox screener
    # endpoint (/openapi/market-data/screener/top-active) on 2026-08-08.
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

        symbols = []
        for row in rows:
            try:
                price = float(row["price"])
            except (KeyError, ValueError, TypeError):
                continue
            if not (self.config.min_price <= price <= self.config.max_price):
                continue
            if self.config.max_market_value is not None:
                market_value = row.get("market_value")
                try:
                    if market_value is not None and float(market_value) > self.config.max_market_value:
                        continue
                except ValueError:
                    pass
            symbols.append(row["symbol"])
        return symbols


class StaticUniverseProvider(SymbolUniverseProvider):
    """Fixed symbol list -- useful for tests, manual watchlists, or backtests."""

    def __init__(self, symbols: list[str]):
        self._symbols = list(symbols)

    def get_symbols(self) -> list[str]:
        return list(self._symbols)
