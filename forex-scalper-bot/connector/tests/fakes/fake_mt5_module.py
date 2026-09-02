"""
FakeMT5Module -- a plain object exposing exactly the `mt5.*` surface
MT5Client calls, plus mirrored constants as plain int attributes, so
MT5Client's tests never need the real (Windows-only) MetaTrader5 package
installed. Scripted per test via set_account_info/set_tick/set_positions/
set_rates/script_order_send_result -- there is no IPC/socket boundary to
simulate here (unlike tests/fakes/fake_cloud_peer.py's real websockets
double), just plain Python attribute/return-value scripting.

Values chosen for the mirrored constants are internally consistent
(what matters is that MT5Client's own code, which always reads these off
the injected module rather than hardcoding literals, agrees with
whatever this fake defines) -- NOT asserted to match the real package's
actual integer values, which are unverified without a real terminal (see
docs/ARCHITECTURE.md's Phase 5d section).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional


class FakeMT5Module:
    # --- mirrored constants -----------------------------------------------
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TYPE_BUY_STOP_LIMIT = 6
    ORDER_TYPE_SELL_STOP_LIMIT = 7

    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_MODIFY = 7
    TRADE_ACTION_REMOVE = 8

    ORDER_TIME_DAY = 0
    ORDER_TIME_GTC = 1

    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2

    TRADE_RETCODE_DONE = 10009

    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408
    TIMEFRAME_W1 = 32769

    def __init__(self) -> None:
        self._initialize_ok = True
        self._last_error = (0, "no error")
        self._account_info = SimpleNamespace(
            login=12345, trade_mode=self.ACCOUNT_TRADE_MODE_DEMO, balance=10_000.0,
            equity=10_000.0, margin=0.0, margin_free=10_000.0, margin_level=0.0,
            currency="USD", name="Test Account", company="Test Broker", server="Test-Server",
        )
        self._ticks: dict[str, SimpleNamespace] = {}
        self._positions: list[SimpleNamespace] = []
        self._rates: dict[str, list[dict]] = {}
        self._next_order_send_result: Optional[SimpleNamespace] = None
        self.selected_symbols: list[str] = []
        self.sent_requests: list[dict] = []

    # --- scripting API -------------------------------------------------------

    def set_initialize_result(self, ok: bool, *, error: tuple = (1, "initialize failed")) -> None:
        self._initialize_ok = ok
        if not ok:
            self._last_error = error

    def set_account_info(self, **fields) -> None:
        self._account_info = SimpleNamespace(**{**self._account_info.__dict__, **fields})

    def set_tick(self, symbol: str, *, bid: float, ask: float, time: Optional[datetime] = None) -> None:
        time = time or datetime.now(timezone.utc)
        self._ticks[symbol] = SimpleNamespace(
            time=int(time.timestamp()), bid=bid, ask=ask, last=bid, volume=0,
            time_msc=int(time.timestamp() * 1000), flags=0, volume_real=0.0,
        )

    def set_positions(self, positions: list[dict]) -> None:
        self._positions = [SimpleNamespace(**p) for p in positions]

    def set_rates(self, symbol: str, rates: list[dict]) -> None:
        self._rates[symbol] = rates

    def script_order_send_result(
        self, *, retcode: int, order: int = 0, deal: int = 0,
        price: float = 0.0, volume: float = 0.0, comment: str = "",
    ) -> None:
        self._next_order_send_result = SimpleNamespace(
            retcode=retcode, order=order, deal=deal, volume=volume, price=price,
            bid=price, ask=price, comment=comment, request_id=0, retcode_external=0, request=None,
        )

    # --- mt5.* surface ---------------------------------------------------------

    def initialize(self, **kwargs) -> bool:
        return self._initialize_ok

    def login(self, login, password=None, server=None, timeout=None) -> bool:
        return self._initialize_ok

    def shutdown(self) -> None:
        pass

    def last_error(self) -> tuple:
        return self._last_error

    def account_info(self):
        return self._account_info

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        self.selected_symbols.append(symbol)
        return True

    def symbol_info_tick(self, symbol: str):
        return self._ticks.get(symbol)

    def copy_rates_from(self, symbol, timeframe, date_from, count):
        return self._rates.get(symbol)

    def positions_get(self, symbol=None, group=None, ticket=None):
        rows = self._positions
        if ticket is not None:
            rows = [p for p in rows if p.ticket == ticket]
        elif symbol is not None:
            rows = [p for p in rows if p.symbol == symbol]
        return tuple(rows)

    def order_send(self, request: dict):
        self.sent_requests.append(request)
        if self._next_order_send_result is not None:
            return self._next_order_send_result
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE, order=1, deal=1,
            volume=request.get("volume", 0.0), price=request.get("price", 0.0),
            bid=request.get("price", 0.0), ask=request.get("price", 0.0),
            comment="", request_id=0, retcode_external=0, request=request,
        )

    def order_check(self, request: dict):
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE, balance=self._account_info.balance,
            equity=self._account_info.equity, profit=0.0, margin=0.0,
            margin_free=self._account_info.margin_free, margin_level=0.0, comment="", request=request,
        )
