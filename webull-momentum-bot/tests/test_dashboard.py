"""
Tests for the dashboard's FastAPI backend. Uses a real PaperBrokerClient +
TradingLoop (so live-state endpoints reflect real objects) and an in-memory
SQLite database (StaticPool, so all connections share the same DB -- plain
sqlite:///:memory: gives each connection its own empty database) for the
historical endpoints.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from webull_bot.brokers.paper.client import PaperBrokerClient
from webull_bot.config import get_settings
from webull_bot.dashboard.app import create_app
from webull_bot.data.universe import StaticUniverseProvider
from webull_bot.db.models import Base
from webull_bot.db.repository import record_trade
from webull_bot.enums import CandidateState, ExitReason, OrderSide
from webull_bot.execution.order_manager import OrderManager
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import FloatData, MarketSnapshot, Position, Trade
from webull_bot.position.position_manager import PositionManager
from webull_bot.risk.risk_engine import RiskEngine
from webull_bot.runtime.trading_loop import TradingLoop
from webull_bot.scanner.broad_scanner import BroadScanner
from webull_bot.scanner.candidate_watcher import CandidateWatcher
from webull_bot.scanner.trigger_engine import TriggerEngine
from webull_bot.state_machine import new_candidate, transition
from webull_bot.strategy.momentum_breakout import MomentumBreakoutStrategy


class _DummyFloatProvider(FloatDataProvider):
    def get_float_data(self, symbol):
        return FloatData(
            symbol=symbol, free_float_shares=3_000_000, shares_outstanding=4_000_000,
            market_cap=None, float_percent=None, effective_date=None, fetched_at=datetime.utcnow(),
        )

    def get_float_data_bulk(self, symbols):
        return {s: self.get_float_data(s) for s in symbols}


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def loop():
    broker = PaperBrokerClient()
    broker.connect()
    risk_engine = RiskEngine()
    return TradingLoop(
        broker, StaticUniverseProvider([]), BroadScanner(broker, _DummyFloatProvider()),
        CandidateWatcher(), TriggerEngine([MomentumBreakoutStrategy()]),
        OrderManager(broker, risk_engine, get_settings()), PositionManager(), risk_engine,
    )


@pytest.fixture
def client(loop, session_factory):
    app = create_app(loop, session_factory, "paper")
    return TestClient(app)


def test_status_reports_paper_mode_and_equity(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trading_mode"] == "paper"
    assert body["equity"] == 25_000.0
    assert body["candidate_count"] == 0
    assert body["kill_switch_active"] is False


def test_status_reflects_kill_switch(loop, client):
    loop.risk_engine.engage_kill_switch("test halt")
    resp = client.get("/api/status")
    assert resp.json()["kill_switch_active"] is True


def test_candidates_reflects_live_loop_state(loop, client):
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    loop.candidates["TEST"] = candidate

    resp = client.get("/api/candidates")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "TEST"
    assert rows[0]["state"] == "watching"
    assert rows[0]["reason"] is None  # no transition reason was given above


def test_candidates_exposes_the_reason_for_the_current_state(loop, client):
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING, reason="passed broad scanner filters")
    transition(candidate, CandidateState.REJECTED, reason="failed liquidity/spread check")
    loop.candidates["TEST"] = candidate

    rows = client.get("/api/candidates").json()
    assert rows[0]["state"] == "rejected"
    # Only the reason for the *current* (most recent) transition should show,
    # not the full multi-line history, and without the redundant
    # "[timestamp] -> rejected:" prefix already covered by other columns.
    assert rows[0]["reason"] == "failed liquidity/spread check"


def test_positions_includes_unrealized_pnl_from_live_snapshot(loop, client):
    loop.broker.feed_snapshot(
        MarketSnapshot(
            symbol="TEST", timestamp=datetime.utcnow(), last_price=12.0, bid=11.9, ask=12.1,
            bid_size=100, ask_size=100, cumulative_volume=100_000, vwap=11.5, high_of_day=12.5,
            low_of_day=10.0, open_price=10.5,
        )
    )
    loop._positions["TEST"] = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=10.0, stop_price=9.0,
        target_price=13.0, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )
    resp = client.get("/api/positions")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["current_price"] == 12.0
    assert rows[0]["unrealized_pnl"] == pytest.approx((12.0 - 10.0) * 100)


def test_risk_events_reflects_live_risk_engine(loop, client):
    loop.risk_engine.engage_kill_switch("manual test")
    resp = client.get("/api/risk-events")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "kill_switch_engaged"


def test_trades_and_performance_read_from_database(session_factory, client):
    with session_factory() as session:
        record_trade(
            session,
            Trade(
                symbol="GME", strategy_name="momentum_breakout", side=OrderSide.BUY,
                entry_price=5.0, exit_price=5.5, quantity=100,
                opened_at=datetime.utcnow() - timedelta(minutes=10), closed_at=datetime.utcnow(),
                exit_reason=ExitReason.PROFIT_TARGET, pnl=50.0, pnl_pct=10.0,
                max_favorable_excursion=12.0, max_adverse_excursion=1.0,
            ),
            trading_mode="paper",
        )
        session.commit()

    trades_resp = client.get("/api/trades")
    assert trades_resp.status_code == 200
    trades = trades_resp.json()
    assert len(trades) == 1
    assert trades[0]["symbol"] == "GME"

    perf_resp = client.get("/api/performance")
    perf = perf_resp.json()
    assert perf["total_trades"] == 1
    assert perf["win_rate"] == 1.0
    assert perf["total_pnl"] == 50.0


def test_index_and_static_assets_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/app.js").status_code == 200
