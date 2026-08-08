"""
Financial Modeling Prep (FMP) free-float data provider -- the currently
active implementation of FloatDataProvider (see interfaces/float_provider.py).

Verified live against FMP's `stable` API namespace on 2026-08-08:
  GET {base_url}/shares-float?symbol=SYM&apikey=KEY
      -> [{"symbol", "date", "freeFloat" (a PERCENT, e.g. 91.18), "floatShares",
           "outstandingShares", "source"}]
  GET {base_url}/profile?symbol=SYM&apikey=KEY
      -> [{"symbol", "marketCap", "companyName", "exchange", ...}]

FMP's older /api/v3 and /api/v4 endpoints (including the legacy
/api/v4/shares_float) return a hard "Legacy Endpoint" 403 as of this
integration and must not be used -- this was confirmed against the live
API, not just documentation. If FMP changes their schema again, re-verify
against the live endpoint (their docs site blocks scripted fetches) before
changing the field mapping below.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

import requests

from ...config import FMPCredentials
from ...interfaces.float_provider import FloatDataProvider
from ...models import FloatData

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


class FMPFloatProvider(FloatDataProvider):
    def __init__(
        self,
        credentials: FMPCredentials,
        *,
        http_get: Optional[Callable[..., "requests.Response"]] = None,
        timeout: float = 10.0,
    ):
        if not credentials.is_configured():
            raise RuntimeError("FMP_API_KEY is not configured (see .env.example)")
        self.credentials = credentials
        # Injectable for tests -- defaults to a real network call.
        self._http_get = http_get or requests.get
        self.timeout = timeout

    def _get(self, path: str, symbol: str) -> list[dict]:
        response = self._http_get(
            f"{self.credentials.base_url}/{path}",
            params={"symbol": symbol, "apikey": self.credentials.api_key},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and "Error Message" in payload:
            raise RuntimeError(f"FMP API error for {path}/{symbol}: {payload['Error Message']}")
        return payload or []

    def _market_cap(self, symbol: str) -> Optional[float]:
        try:
            rows = self._get("profile", symbol)
        except (requests.RequestException, RuntimeError):
            return None
        return rows[0].get("marketCap") if rows else None

    def get_float_data(self, symbol: str) -> FloatData:
        rows = self._get("shares-float", symbol)
        if not rows:
            raise ValueError(f"FMP returned no shares-float data for {symbol}")
        row = rows[0]

        free_float_shares = float(row["floatShares"])
        shares_outstanding = float(row["outstandingShares"])
        free_float_pct_field = row.get("freeFloat")
        float_percent = (
            free_float_pct_field / 100.0
            if free_float_pct_field is not None
            else (free_float_shares / shares_outstanding if shares_outstanding else None)
        )

        return FloatData(
            symbol=row.get("symbol", symbol),
            free_float_shares=free_float_shares,
            shares_outstanding=shares_outstanding,
            market_cap=self._market_cap(symbol),
            float_percent=float_percent,
            effective_date=_parse_date(row.get("date")),
            fetched_at=datetime.utcnow(),
            source="fmp",
        )

    def get_float_data_bulk(self, symbols: list[str]) -> dict[str, FloatData]:
        # FMP's free-float endpoint takes one symbol per call on the plans
        # we've verified against; no bulk endpoint confirmed yet.
        result: dict[str, FloatData] = {}
        for symbol in symbols:
            try:
                result[symbol] = self.get_float_data(symbol)
            except (requests.RequestException, RuntimeError, ValueError, KeyError):
                continue
        return result
