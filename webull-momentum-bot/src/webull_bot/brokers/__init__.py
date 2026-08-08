from __future__ import annotations

from ..config import Settings, TradingMode
from ..interfaces.broker import BrokerClient


def get_broker_client(settings: Settings) -> BrokerClient:
    """Factory that picks the broker backend from Settings.trading_mode.

    This is the one place a broker instance gets constructed -- callers
    should never `import` a concrete broker class directly.
    """
    if settings.trading_mode == TradingMode.PAPER:
        from .paper.client import PaperBrokerClient

        return PaperBrokerClient()

    if settings.trading_mode in (TradingMode.SANDBOX, TradingMode.LIVE):
        settings.require_non_live_or_authorized()
        from .webull.client import WebullBrokerClient

        return WebullBrokerClient(settings)

    raise ValueError(f"Unknown trading mode: {settings.trading_mode}")
