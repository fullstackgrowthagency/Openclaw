"""
Webull OpenAPI broker client.

STATUS: skeleton only. The method bodies below are NOT wired to the real
Webull OpenAPI/SDK yet -- they raise NotImplementedError. Filling them in is
Phase 2 of the project and must be done against Webull's current official
OpenAPI documentation and official Python SDK, not guessed endpoint names.
Do not hand-write REST paths or SDK method names from memory; confirm them
against the live docs at integration time, since brokerage APIs change.

Safety:
  - `WebullBrokerClient.is_live` reflects the *configured* trading mode.
  - The constructor calls `settings.require_non_live_or_authorized()`, so a
    live-mode instance cannot even be constructed unless
    `Settings.is_live_trading_authorized()` returns True.
  - `OrderManager` (execution/order_manager.py) additionally re-checks
    authorization before every single order, so a mode flip mid-process
    can't slip an order through.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from ...config import Settings, TradingMode
from ...interfaces.broker import BrokerClient
from ...models import Fill, MarketSnapshot, Order, Position


class WebullBrokerClient(BrokerClient):
    def __init__(self, settings: Settings):
        settings.require_non_live_or_authorized()
        if not settings.webull.is_configured():
            raise RuntimeError(
                "Webull credentials are not configured. Set WEBULL_APP_KEY, "
                "WEBULL_APP_SECRET, WEBULL_ACCOUNT_ID and WEBULL_BASE_URL "
                "(see .env.example)."
            )
        self.settings = settings
        self._connected = False
        # TODO(Phase 2): construct the official Webull OpenAPI SDK client here,
        # pointed at the sandbox host unless settings.is_live_trading_authorized().

    def connect(self) -> None:
        raise NotImplementedError(
            "Wire up the official Webull OpenAPI SDK auth/session handshake here. "
            "Consult current Webull OpenAPI docs for the sandbox vs. production hosts."
        )

    def disconnect(self) -> None:
        raise NotImplementedError

    def get_account_equity(self) -> float:
        raise NotImplementedError

    def get_buying_power(self) -> float:
        raise NotImplementedError

    def get_positions(self) -> list[Position]:
        raise NotImplementedError

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        raise NotImplementedError

    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[MarketSnapshot]:
        raise NotImplementedError

    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None:
        raise NotImplementedError(
            "Wire up Webull's streaming quote/bar subscription here. Prefer this over "
            "polling REST endpoints per the project's architecture requirements."
        )

    def place_order(self, order: Order) -> Order:
        if self.is_live and not self.settings.is_live_trading_authorized():
            # Defense in depth: even though __init__ already checked this, a
            # long-lived process could have its settings reloaded/mutated.
            raise RuntimeError("Live trading authorization lost; refusing to place order.")
        raise NotImplementedError("Wire up Webull order placement per current OpenAPI docs.")

    def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError

    def modify_order(self, broker_order_id: str, **changes) -> Order:
        raise NotImplementedError

    def get_order_status(self, broker_order_id: str) -> Order:
        raise NotImplementedError

    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]:
        raise NotImplementedError

    @property
    def is_live(self) -> bool:
        return self.settings.trading_mode == TradingMode.LIVE
