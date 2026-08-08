"""
Massive (or similar) free-float data provider.

STATUS: skeleton. `_fetch_raw` is NotImplemented -- wire it up against
Massive's current official API docs at integration time (confirm auth
scheme, endpoint paths, and response schema rather than guessing). Every
other provider (float API alternatives) should implement the same
FloatDataProvider interface so swapping providers doesn't touch scanner or
scoring code.
"""
from __future__ import annotations

from datetime import datetime

from ...config import MassiveCredentials
from ...interfaces.float_provider import FloatDataProvider
from ...models import FloatData


class MassiveFloatProvider(FloatDataProvider):
    def __init__(self, credentials: MassiveCredentials):
        if not credentials.is_configured():
            raise RuntimeError("MASSIVE_API_KEY is not configured (see .env.example)")
        self.credentials = credentials

    def _fetch_raw(self, symbol: str) -> dict:
        raise NotImplementedError(
            "Wire up the real Massive API call here per their current docs: "
            "auth header, endpoint path, and response fields for free float, "
            "shares outstanding, market cap, and float effective date."
        )

    def get_float_data(self, symbol: str) -> FloatData:
        raw = self._fetch_raw(symbol)
        free_float = float(raw["free_float_shares"])
        shares_out = float(raw["shares_outstanding"])
        return FloatData(
            symbol=symbol,
            free_float_shares=free_float,
            shares_outstanding=shares_out,
            market_cap=raw.get("market_cap"),
            float_percent=(free_float / shares_out) if shares_out else None,
            effective_date=raw.get("effective_date"),
            fetched_at=datetime.utcnow(),
            source="massive",
        )

    def get_float_data_bulk(self, symbols: list[str]) -> dict[str, FloatData]:
        return {s: self.get_float_data(s) for s in symbols}
