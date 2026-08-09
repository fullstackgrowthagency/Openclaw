"""
Chains a primary FloatDataProvider with one or more fallback providers so a
symbol still gets float data -- and therefore doesn't get structurally
rejected by BroadScanner, which treats any get_float_data exception as
disqualifying (see scanner/broad_scanner.py's `_check_symbol`) -- when the
primary source fails or rate-limits for that symbol. This itself just
implements FloatDataProvider, so it slots into get_float_provider()'s
existing CachedFloatProvider wrapping unchanged; callers never know a
fallback happened except via FloatData.source on the result.

Added 2026-08-09 specifically to pair FMP (which has hit real 429s in this
project's own live testing -- see fmp.py/ARCHITECTURE.md) with
YFinanceFloatProvider as a free, best-effort secondary (see
get_float_provider() in __init__.py). Order matters: `primary` is always
tried first for every symbol; `fallbacks` are tried in order only once the
primary has already failed for that specific symbol.
"""
from __future__ import annotations

import logging
from typing import Optional

from ...interfaces.float_provider import FloatDataProvider
from ...models import FloatData

logger = logging.getLogger(__name__)


class FallbackFloatProvider(FloatDataProvider):
    def __init__(self, primary: FloatDataProvider, fallbacks: list[FloatDataProvider]):
        self.primary = primary
        self.fallbacks = fallbacks

    def get_float_data(self, symbol: str) -> FloatData:
        try:
            return self.primary.get_float_data(symbol)
        except Exception:
            logger.warning(
                "Primary float provider failed for %s; trying %d fallback(s).",
                symbol, len(self.fallbacks), exc_info=True,
            )

        last_exc: Optional[Exception] = None
        for fallback in self.fallbacks:
            try:
                return fallback.get_float_data(symbol)
            except Exception as exc:
                last_exc = exc
                continue
        # All providers failed -- re-raise the last error so the caller
        # (BroadScanner) sees the same "lookup failed" contract it always
        # has, rather than a bespoke exception type.
        raise last_exc or RuntimeError(f"No float data provider returned data for {symbol}")

    def get_float_data_bulk(self, symbols: list[str]) -> dict[str, FloatData]:
        result = self.primary.get_float_data_bulk(symbols)
        missing = [s for s in symbols if s not in result]
        for fallback in self.fallbacks:
            if not missing:
                break
            recovered = fallback.get_float_data_bulk(missing)
            result.update(recovered)
            missing = [s for s in missing if s not in recovered]
        return result
