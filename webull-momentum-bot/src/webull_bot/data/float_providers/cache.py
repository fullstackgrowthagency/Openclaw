"""
Local disk cache for free-float data. Float data doesn't need sub-daily
freshness, so we avoid hammering the external provider on every scan pass.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ...interfaces.float_provider import FloatDataProvider
from ...models import FloatData


class CachedFloatProvider(FloatDataProvider):
    def __init__(self, delegate: FloatDataProvider, cache_dir: Path, ttl_hours: int = 24):
        self.delegate = delegate
        self.cache_dir = cache_dir
        self.ttl = timedelta(hours=ttl_hours)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol.upper()}.json"

    def _read_cache(self, symbol: str) -> Optional[FloatData]:
        path = self._cache_path(symbol)
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(raw["fetched_at"])
        if datetime.utcnow() - fetched_at > self.ttl:
            return None
        effective_date = datetime.fromisoformat(raw["effective_date"]) if raw.get("effective_date") else None
        return FloatData(
            symbol=raw["symbol"],
            free_float_shares=raw["free_float_shares"],
            shares_outstanding=raw["shares_outstanding"],
            market_cap=raw.get("market_cap"),
            float_percent=raw.get("float_percent"),
            effective_date=effective_date,
            fetched_at=fetched_at,
            source=raw.get("source", "cache"),
        )

    def _write_cache(self, data: FloatData) -> None:
        payload = asdict(data)
        payload["fetched_at"] = data.fetched_at.isoformat()
        payload["effective_date"] = data.effective_date.isoformat() if data.effective_date else None
        self._cache_path(data.symbol).write_text(json.dumps(payload))

    def get_float_data(self, symbol: str) -> FloatData:
        cached = self._read_cache(symbol)
        if cached is not None:
            return cached
        fresh = self.delegate.get_float_data(symbol)
        self._write_cache(fresh)
        return fresh

    def get_float_data_bulk(self, symbols: list[str]) -> dict[str, FloatData]:
        result: dict[str, FloatData] = {}
        to_fetch: list[str] = []
        for symbol in symbols:
            cached = self._read_cache(symbol)
            if cached is not None:
                result[symbol] = cached
            else:
                to_fetch.append(symbol)
        if to_fetch:
            fresh = self.delegate.get_float_data_bulk(to_fetch)
            for symbol, data in fresh.items():
                self._write_cache(data)
                result[symbol] = data
        return result
