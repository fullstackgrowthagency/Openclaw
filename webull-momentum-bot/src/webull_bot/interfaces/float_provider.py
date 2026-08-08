from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import FloatData


class FloatDataProvider(ABC):
    """External free-float data source (e.g. Massive). Results should be cached
    by the caller (see data/float_providers/cache.py) since float data does not
    need sub-daily freshness."""

    @abstractmethod
    def get_float_data(self, symbol: str) -> FloatData:
        ...

    @abstractmethod
    def get_float_data_bulk(self, symbols: list[str]) -> dict[str, FloatData]:
        ...
