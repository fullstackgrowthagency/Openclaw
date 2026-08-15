"""Tests for /api/broker-credential* -- letting a logged-in user connect,
test, and disconnect their own Webull API key (see
auth/broker_credential_routes.py), and the self-serve live-trading
toggle. `_verify` (the real Webull-hitting call) is monkeypatched at the
module level throughout -- these tests exercise the routing/storage/
auth logic, not WebullBrokerClient itself (already covered by
tests/test_webull_broker_client.py)."""
from __future__ import annotations

from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from webull_bot.auth import broker_credential_routes
from webull_bot.brokers.paper.client import PaperBrokerClient
from webull_bot.config import Settings
from webull_bot.dashboard.app import create_app
from webull_bot.data.universe import StaticUniverseProvider
from webull_bot.db.models import Base
from webull_bot.execution.order_manager import OrderManager
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import FloatData
from webull_bot.position.position_manager import PositionManager
from webull_bot.risk.risk_engine import RiskEngine
from webull_bot.runtime.loop_registry import LoopRegistry
from webull_bot.runtime.trading_loop import TradingLoop
from webull_bot.scanner.broad_scanner import BroadScanner
from webull_bot.scanner.candidate_watcher import CandidateWatcher
from webull_bot.scanner.trigger_engine import TriggerEngine
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
        OrderManager(broker, risk_engine, Settings()), PositionManager(), risk_engine,
    )


@pytest.fixture
def settings():
    return Settings(session_secret_key="test-secret", credential_encryption_key=Fernet.generate_key().decode())


@pytest.fixture
def client(loop, session_factory, settings):
    app = create_app(loop, session_factory, "paper", settings=settings)
    c = TestClient(app)
    c.post("/api/auth/signup", json={"email": "trader@example.com", "password": "password123"})
    return c


_VALID_CREDENTIAL_BODY = {
    "app_key": "key123",
    "app_secret": "secret456",
    "account_id": "ACCT789",
    "base_url": "api.sandbox.webull.com",
    "trading_mode": "sandbox",
}


def test_get_credential_before_connecting_reports_not_connected(client):
    resp = client.get("/api/broker-credential")
    assert resp.status_code == 200
    assert resp.json() == {"connected": False}


def test_broker_credential_routes_require_login(loop, session_factory, settings):
    app = create_app(loop, session_factory, "paper", settings=settings)
    anon_client = TestClient(app)
    assert anon_client.get("/api/broker-credential").status_code == 401


def test_saving_verified_credentials_succeeds_and_masks_the_key(client, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))

    resp = client.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["masked_app_key"] == "...y123"
    assert body["last_verified_at"] is not None
    assert body["last_verify_error"] is None
    assert "key123" not in resp.text  # raw secret is never echoed back


def test_saving_unverifiable_credentials_returns_400_but_still_stores_the_row(client, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (False, "invalid credentials"))

    resp = client.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)
    assert resp.status_code == 400
    assert "invalid credentials" in resp.json()["detail"]

    status = client.get("/api/broker-credential").json()
    assert status["connected"] is True
    assert status["last_verified_at"] is None
    assert status["last_verify_error"] == "invalid credentials"


def test_saving_credentials_rejects_missing_fields(client):
    resp = client.post("/api/broker-credential", json={**_VALID_CREDENTIAL_BODY, "app_key": ""})
    assert resp.status_code == 422


def test_saving_credentials_rejects_unknown_trading_mode(client):
    resp = client.post("/api/broker-credential", json={**_VALID_CREDENTIAL_BODY, "trading_mode": "yolo"})
    assert resp.status_code == 422


def test_test_endpoint_reverifies_stored_credentials_without_resubmitting_secrets(client, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (False, "down"))
    client.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)

    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))
    resp = client.post("/api/broker-credential/test")
    assert resp.status_code == 200
    assert resp.json()["last_verified_at"] is not None


def test_test_endpoint_without_a_connection_is_404(client):
    resp = client.post("/api/broker-credential/test")
    assert resp.status_code == 404


def test_delete_disconnects(client, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))
    client.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)

    resp = client.delete("/api/broker-credential")
    assert resp.status_code == 200
    assert resp.json() == {"connected": False}
    assert client.get("/api/broker-credential").json() == {"connected": False}


def test_two_users_credentials_are_isolated(loop, session_factory, settings, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))
    app = create_app(loop, session_factory, "paper", settings=settings)

    alice = TestClient(app)
    alice.post("/api/auth/signup", json={"email": "alice@example.com", "password": "password123"})
    alice.post("/api/broker-credential", json={**_VALID_CREDENTIAL_BODY, "app_key": "alice-key"})

    bob = TestClient(app)
    bob.post("/api/auth/signup", json={"email": "bob@example.com", "password": "password123"})
    assert bob.get("/api/broker-credential").json() == {"connected": False}

    assert alice.get("/api/broker-credential").json()["masked_app_key"] == "...-key"


# --- live trading toggle ------------------------------------------------

def test_live_trading_cannot_be_enabled_without_a_verified_connection(client):
    resp = client.post("/api/broker-credential/live-trading", json={"enabled": True, "confirm": True})
    assert resp.status_code == 404  # no credential connected at all yet


def test_live_trading_requires_verification_first(client, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (False, "bad key"))
    client.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)

    resp = client.post("/api/broker-credential/live-trading", json={"enabled": True, "confirm": True})
    assert resp.status_code == 422


def test_live_trading_requires_explicit_confirm(client, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))
    client.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)

    resp = client.post("/api/broker-credential/live-trading", json={"enabled": True, "confirm": False})
    assert resp.status_code == 422


def test_live_trading_can_be_enabled_once_verified_and_confirmed(client, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))
    client.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)

    resp = client.post("/api/broker-credential/live-trading", json={"enabled": True, "confirm": True})
    assert resp.status_code == 200
    assert resp.json()["live_trading_enabled"] is True


def test_live_trading_can_always_be_disabled_without_confirm(client, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))
    client.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)
    client.post("/api/broker-credential/live-trading", json={"enabled": True, "confirm": True})

    resp = client.post("/api/broker-credential/live-trading", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["live_trading_enabled"] is False


# --- loop_registry restart wiring (2026-08-15 Phase D) -------------------

class _FakeLoop:
    def run_forever(self, stop_flag=None):
        while stop_flag is not None and not stop_flag():
            import time
            time.sleep(0.01)


@pytest.fixture
def client_with_registry(loop, session_factory, settings):
    registry = LoopRegistry(lambda user_id, s: _FakeLoop())
    app = create_app(loop, session_factory, "paper", settings=settings, loop_registry=registry)
    c = TestClient(app)
    c.post("/api/auth/signup", json={"email": "loopwiring@example.com", "password": "password123"})
    c.registry = registry  # stash for assertions
    return c


def test_verifying_credentials_starts_a_loop_for_that_user(client_with_registry, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))
    user_id = client_with_registry.get("/api/auth/me").json()["id"]

    assert client_with_registry.registry.get(user_id) is None
    resp = client_with_registry.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)
    assert resp.status_code == 200
    assert client_with_registry.registry.get(user_id) is not None
    client_with_registry.registry.stop_all()


def test_unverified_credentials_do_not_start_a_loop(client_with_registry, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (False, "bad key"))
    user_id = client_with_registry.get("/api/auth/me").json()["id"]

    client_with_registry.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)
    assert client_with_registry.registry.get(user_id) is None


def test_disconnecting_stops_the_running_loop(client_with_registry, monkeypatch):
    monkeypatch.setattr(broker_credential_routes, "_verify", lambda *a, **k: (True, None))
    user_id = client_with_registry.get("/api/auth/me").json()["id"]
    client_with_registry.post("/api/broker-credential", json=_VALID_CREDENTIAL_BODY)
    assert client_with_registry.registry.get(user_id) is not None

    client_with_registry.delete("/api/broker-credential")
    assert client_with_registry.registry.get(user_id) is None


# --- app/settings page ---------------------------------------------------

def test_app_settings_page_redirects_when_not_authenticated(loop, session_factory, settings):
    app = create_app(loop, session_factory, "paper", settings=settings)
    anon_client = TestClient(app, follow_redirects=False)
    resp = anon_client.get("/app/settings")
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/login"


def test_app_settings_page_is_served_once_logged_in(client):
    resp = client.get("/app/settings")
    assert resp.status_code == 200
    assert "Connect Your Webull Account" in resp.text
