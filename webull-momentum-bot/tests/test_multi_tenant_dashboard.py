"""Tests for create_app's loop_registry wiring (2026-08-15 multi-tenant
conversion): once a LoopRegistry is passed in, every /api/* endpoint
reads from the REQUESTING user's own TradingLoop (and DB rows scoped to
their own user_id), never another user's -- see dashboard/app.py's
_resolve_loop/_current_user_id."""
from __future__ import annotations

from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from webull_bot.brokers.paper.client import PaperBrokerClient
from webull_bot.config import Settings
from webull_bot.dashboard.app import create_app
from webull_bot.data.universe import StaticUniverseProvider
from webull_bot.db.models import Base
from webull_bot.db.repository import record_trade
from webull_bot.enums import ExitReason, OrderSide
from webull_bot.execution.order_manager import OrderManager
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import FloatData, Trade
from webull_bot.position.position_manager import PositionManager
from webull_bot.risk.risk_engine import RiskEngine
from webull_bot.runtime.loop_registry import LoopRegistry
from webull_bot.runtime.trading_loop import TradingLoop
from webull_bot.scanner.broad_scanner import BroadScanner
from webull_bot.scanner.candidate_watcher import CandidateWatcher
from webull_bot.scanner.trigger_engine import TriggerEngine
from webull_bot.state_machine import new_candidate, transition
from webull_bot.enums import CandidateState
from webull_bot.strategy.momentum_breakout import MomentumBreakoutStrategy


class _DummyFloatProvider(FloatDataProvider):
    def get_float_data(self, symbol):
        return FloatData(
            symbol=symbol, free_float_shares=3_000_000, shares_outstanding=4_000_000,
            market_cap=None, float_percent=None, effective_date=None, fetched_at=datetime.utcnow(),
        )

    def get_float_data_bulk(self, symbols):
        return {s: self.get_float_data(s) for s in symbols}


def _make_loop() -> TradingLoop:
    broker = PaperBrokerClient()
    broker.connect()
    risk_engine = RiskEngine()
    return TradingLoop(
        broker, StaticUniverseProvider([]), BroadScanner(broker, _DummyFloatProvider()),
        CandidateWatcher(), TriggerEngine([MomentumBreakoutStrategy()]),
        OrderManager(broker, risk_engine, Settings()), PositionManager(), risk_engine,
    )


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def settings():
    return Settings(session_secret_key="test-secret", credential_encryption_key=Fernet.generate_key().decode())


def _signed_up_client(app, email: str) -> TestClient:
    client = TestClient(app)
    resp = client.post("/api/auth/signup", json={"email": email, "password": "password123"})
    assert resp.status_code == 200
    return client


def test_loop_registry_with_no_session_secret_key_is_rejected(session_factory):
    registry = LoopRegistry(lambda user_id, s: _make_loop())
    with pytest.raises(RuntimeError):
        create_app(None, session_factory, "paper", settings=Settings(session_secret_key=""), loop_registry=registry)


def test_user_with_no_running_loop_gets_a_clear_404(session_factory, settings):
    registry = LoopRegistry(lambda user_id, s: _make_loop())
    app = create_app(None, session_factory, "paper", settings=settings, loop_registry=registry)
    client = _signed_up_client(app, "nobroker@example.com")

    resp = client.get("/api/status")
    assert resp.status_code == 404
    assert "broker" in resp.json()["detail"].lower()


def test_two_users_see_only_their_own_live_candidates(session_factory, settings):
    loops = {}

    def factory(user_id, s):
        loop = _make_loop()
        loops[user_id] = loop
        return loop

    registry = LoopRegistry(factory)
    app = create_app(None, session_factory, "paper", settings=settings, loop_registry=registry)

    alice = _signed_up_client(app, "alice@example.com")
    alice_id = alice.get("/api/auth/me").json()["id"]
    registry.start_for_user(alice_id, settings)
    alice_candidate = new_candidate("AAAA")
    transition(alice_candidate, CandidateState.WATCHING)
    loops[alice_id].candidates["AAAA"] = alice_candidate

    bob = _signed_up_client(app, "bob@example.com")
    bob_id = bob.get("/api/auth/me").json()["id"]
    registry.start_for_user(bob_id, settings)
    bob_candidate = new_candidate("BBBB")
    transition(bob_candidate, CandidateState.WATCHING)
    loops[bob_id].candidates["BBBB"] = bob_candidate

    alice_rows = alice.get("/api/candidates").json()
    bob_rows = bob.get("/api/candidates").json()

    assert [r["symbol"] for r in alice_rows] == ["AAAA"]
    assert [r["symbol"] for r in bob_rows] == ["BBBB"]

    registry.stop_all()


def test_kill_switch_on_one_users_loop_does_not_affect_the_others(session_factory, settings):
    loops = {}

    def factory(user_id, s):
        loop = _make_loop()
        loops[user_id] = loop
        return loop

    registry = LoopRegistry(factory)
    app = create_app(None, session_factory, "paper", settings=settings, loop_registry=registry)

    alice = _signed_up_client(app, "alice2@example.com")
    alice_id = alice.get("/api/auth/me").json()["id"]
    registry.start_for_user(alice_id, settings)

    bob = _signed_up_client(app, "bob2@example.com")
    bob_id = bob.get("/api/auth/me").json()["id"]
    registry.start_for_user(bob_id, settings)

    resp = alice.post("/api/kill-switch", json={"active": True})
    assert resp.status_code == 200
    assert resp.json()["kill_switch_active"] is True

    assert loops[alice_id].risk_engine.kill_switch_active is True
    assert loops[bob_id].risk_engine.kill_switch_active is False
    assert bob.get("/api/status").json()["kill_switch_active"] is False

    registry.stop_all()


def test_trades_are_scoped_per_user(session_factory, settings):
    registry = LoopRegistry(lambda user_id, s: _make_loop())
    app = create_app(None, session_factory, "paper", settings=settings, loop_registry=registry)

    alice = _signed_up_client(app, "alicetrades@example.com")
    alice_id = alice.get("/api/auth/me").json()["id"]
    registry.start_for_user(alice_id, settings)

    bob = _signed_up_client(app, "bobtrades@example.com")
    bob_id = bob.get("/api/auth/me").json()["id"]
    registry.start_for_user(bob_id, settings)

    trade = Trade(
        symbol="ALICE", strategy_name="test", side=OrderSide.BUY,
        entry_price=1.0, exit_price=1.1, quantity=100,
        opened_at=datetime.utcnow(), closed_at=datetime.utcnow(),
        exit_reason=ExitReason.PROFIT_TARGET, pnl=10.0, pnl_pct=10.0,
        max_favorable_excursion=0.15, max_adverse_excursion=0.0,
    )
    with session_factory() as session:
        record_trade(session, trade, trading_mode="paper", user_id=alice_id)
        session.commit()

    alice_trades = alice.get("/api/trades").json()
    bob_trades = bob.get("/api/trades").json()

    assert [t["symbol"] for t in alice_trades] == ["ALICE"]
    assert bob_trades == []

    registry.stop_all()
