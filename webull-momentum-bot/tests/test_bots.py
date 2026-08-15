"""Tests for the multi-bot framework (2026-08-15): every user gets a
default "Day Trading Quant" bot at signup, GET /api/bots exposes it for
the dashboard's hamburger menu, and trade/performance history is
scoped by bot_id the same way it's already scoped by user_id."""
from __future__ import annotations

from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from webull_bot.config import Settings
from webull_bot.dashboard.app import create_app
from webull_bot.db.models import Base, Bot
from webull_bot.db.repository import get_or_create_default_bot, get_recent_trades, record_trade
from webull_bot.enums import ExitReason, OrderSide
from webull_bot.models import Trade


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def settings():
    return Settings(session_secret_key="test-secret", credential_encryption_key=Fernet.generate_key().decode())


@pytest.fixture
def client(session_factory, settings):
    app = create_app(None, session_factory, "sandbox", settings=settings)
    c = TestClient(app)
    c.post("/api/auth/signup", json={"email": "trader@example.com", "password": "password123"})
    return c


def _trade(symbol="AAPL", pnl=10.0, pnl_pct=1.0):
    return Trade(
        symbol=symbol, strategy_name="momentum_breakout", side=OrderSide.BUY,
        entry_price=100.0, exit_price=101.0, quantity=10,
        opened_at=datetime(2026, 1, 1, 9, 30), closed_at=datetime(2026, 1, 1, 9, 45),
        exit_reason=ExitReason.PROFIT_TARGET, pnl=pnl, pnl_pct=pnl_pct,
        max_favorable_excursion=1.0, max_adverse_excursion=0.0,
    )


def test_signup_creates_default_bot(session_factory, client):
    with session_factory() as session:
        bots = session.query(Bot).all()
    assert len(bots) == 1
    assert bots[0].slug == "day-trading-quant"
    assert bots[0].name == "Day Trading Quant"


def test_get_bots_returns_default_bot(client):
    resp = client.get("/api/bots")
    assert resp.status_code == 200
    bots = resp.json()
    assert len(bots) == 1
    assert bots[0]["slug"] == "day-trading-quant"
    assert bots[0]["name"] == "Day Trading Quant"
    assert isinstance(bots[0]["id"], int)


def test_get_bots_requires_login(session_factory, settings):
    app = create_app(None, session_factory, "sandbox", settings=settings)
    anon_client = TestClient(app)
    assert anon_client.get("/api/bots").status_code == 401


def test_get_or_create_default_bot_is_idempotent(session_factory):
    with session_factory() as session:
        from webull_bot.db.models import User
        user = User(email="idempotent@example.com", password_hash="x")
        session.add(user)
        session.flush()
        first = get_or_create_default_bot(session, user.id)
        second = get_or_create_default_bot(session, user.id)
        session.commit()
        assert first.id == second.id
        assert session.query(Bot).filter(Bot.user_id == user.id).count() == 1


def test_trades_are_scoped_by_bot_id(session_factory):
    with session_factory() as session:
        record_trade(session, _trade(), trading_mode="sandbox", user_id=1, bot_id=10)
        record_trade(session, _trade(symbol="MSFT"), trading_mode="sandbox", user_id=1, bot_id=20)
        session.commit()

        bot_10_trades = get_recent_trades(session, user_id=1, bot_id=10)
        bot_20_trades = get_recent_trades(session, user_id=1, bot_id=20)
        bot_30_trades = get_recent_trades(session, user_id=1, bot_id=30)

        assert [t.symbol for t in bot_10_trades] == ["AAPL"]
        assert [t.symbol for t in bot_20_trades] == ["MSFT"]
        assert bot_30_trades == []
