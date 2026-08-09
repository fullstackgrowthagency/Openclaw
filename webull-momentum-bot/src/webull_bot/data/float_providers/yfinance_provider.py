"""
Yahoo Finance (via the unofficial `yfinance` library) free-float data
provider -- a SECONDARY/fallback source only, wired in behind FMP (or
Massive) by fallback.py's FallbackFloatProvider. Never used standalone: see
get_float_provider() in __init__.py, which only wraps a real primary
provider with this one, never substitutes it.

Yahoo does not publish an official, supported API for this -- yfinance
scrapes/reverse-engineers Yahoo's own web endpoints, so field availability
or the whole thing can break without notice, and Yahoo is known to
aggressively throttle traffic from datacenter/cloud IP ranges, which is
exactly what a VPS deployment looks like to them. That's an acceptable
risk here specifically because this only ever runs after the primary
provider has already failed for a symbol -- a Yahoo block just means "no
fallback available today," not a new failure mode on top of a working one.

The reason to bother with this over other free tiers (Finnhub, Polygon,
Alpha Vantage) is that `Ticker.get_info()`'s `floatShares` field is real
free float, not an approximation built from shares-outstanding-only data.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from ...interfaces.float_provider import FloatDataProvider
from ...models import FloatData


def _fetch_info(symbol: str) -> dict:
    # Imported lazily so the dependency is only touched when this fallback
    # is actually exercised, not on every import of this package.
    import yfinance

    return yfinance.Ticker(symbol).get_info()


class YFinanceFloatProvider(FloatDataProvider):
    def __init__(self, *, info_fetcher: Optional[Callable[[str], dict]] = None):
        # Injectable for tests -- defaults to a real (unofficial, scraped) Yahoo Finance call.
        self._info_fetcher = info_fetcher or _fetch_info

    def get_float_data(self, symbol: str) -> FloatData:
        info = self._info_fetcher(symbol)
        float_shares = info.get("floatShares")
        if float_shares is None:
            raise ValueError(f"Yahoo Finance returned no floatShares for {symbol}")
        free_float_shares = float(float_shares)
        shares_outstanding_raw = info.get("sharesOutstanding")
        # Falls back to free_float_shares itself (float_percent then comes
        # out as 1.0) rather than None -- sharesOutstanding is occasionally
        # absent from Yahoo's payload even when floatShares is present, and
        # this is meant to be a best-effort fallback, not a second gate
        # that can itself fail on a partially-populated response.
        shares_outstanding = float(shares_outstanding_raw) if shares_outstanding_raw is not None else free_float_shares

        return FloatData(
            symbol=symbol,
            free_float_shares=free_float_shares,
            shares_outstanding=shares_outstanding,
            market_cap=info.get("marketCap"),
            float_percent=(free_float_shares / shares_outstanding) if shares_outstanding else None,
            effective_date=None,  # Yahoo doesn't report an as-of date for this field
            fetched_at=datetime.utcnow(),
            source="yfinance",
        )

    def get_float_data_bulk(self, symbols: list[str]) -> dict[str, FloatData]:
        result: dict[str, FloatData] = {}
        for symbol in symbols:
            try:
                result[symbol] = self.get_float_data(symbol)
            except Exception:
                # Deliberately broad: yfinance's exception surface isn't a
                # documented, stable contract the way requests.RequestException
                # is for FMP -- it can raise its own errors, network errors,
                # or JSON-parsing errors depending on what Yahoo's scraped
                # endpoint returns that day. One bad symbol must not abort
                # the rest of a bulk fallback pass.
                continue
        return result
