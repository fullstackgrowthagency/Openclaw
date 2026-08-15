"""Tests for auth/security.py, auth/crypto.py, auth/dependencies.py, and
the /api/auth/* routes wired into dashboard/app.py's create_app."""
from __future__ import annotations

from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from webull_bot.auth.crypto import CredentialEncryptionError, decrypt_secret, encrypt_secret
from webull_bot.auth.dependencies import build_get_current_user
from webull_bot.auth.security import hash_password, verify_password
from webull_bot.brokers.paper.client import PaperBrokerClient
from webull_bot.config import Settings
from webull_bot.dashboard.app import create_app
from webull_bot.data.universe import StaticUniverseProvider
from webull_bot.db.models import Base, User
from webull_bot.execution.order_manager import OrderManager
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import FloatData
from webull_bot.position.position_manager import PositionManager
from webull_bot.risk.risk_engine import RiskEngine
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


# --- security.py -------------------------------------------------------

def test_hash_password_round_trips():
    hashed = hash_password("correct-horse-battery-staple")
    assert hashed != "correct-horse-battery-staple"
    assert verify_password("correct-horse-battery-staple", hashed)


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert not verify_password("wrong-password", hashed)


def test_verify_password_rejects_malformed_hash_instead_of_raising():
    assert not verify_password("anything", "not-a-real-bcrypt-hash")


def test_hash_password_handles_passwords_over_bcrypts_72_byte_cap():
    long_password = "x" * 200
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed)


# --- crypto.py -----------------------------------------------------------

def _settings_with_key() -> Settings:
    return Settings(credential_encryption_key=Fernet.generate_key().decode())


def test_encrypt_decrypt_round_trips():
    settings = _settings_with_key()
    ciphertext = encrypt_secret("WEBULL_APP_SECRET_VALUE", settings)
    assert ciphertext != "WEBULL_APP_SECRET_VALUE"
    assert decrypt_secret(ciphertext, settings) == "WEBULL_APP_SECRET_VALUE"


def test_encrypt_without_key_raises_clear_error():
    settings = Settings(credential_encryption_key="")
    with pytest.raises(CredentialEncryptionError):
        encrypt_secret("secret", settings)


def test_decrypt_with_wrong_key_raises_clear_error():
    ciphertext = encrypt_secret("secret", _settings_with_key())
    with pytest.raises(CredentialEncryptionError):
        decrypt_secret(ciphertext, _settings_with_key())  # different key


# --- dashboard app wiring / /api/auth/* ----------------------------------

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


def test_auth_routes_are_not_mounted_without_a_session_secret(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key=""))
    client = TestClient(app)
    resp = client.post("/api/auth/signup", json={"email": "a@example.com", "password": "password123"})
    # Not a registered FastAPI route -- falls through to the "/" StaticFiles
    # mount (see create_app's tail), which 404s if there's truly no such
    # file or 405s if it exists but doesn't support POST; either response
    # confirms the auth router itself was never mounted.
    assert resp.status_code in (404, 405)


def test_signup_then_me_reflects_the_logged_in_user(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)

    signup = client.post("/api/auth/signup", json={"email": "New@Example.com", "password": "password123"})
    assert signup.status_code == 200
    assert signup.json()["email"] == "new@example.com"  # normalized lowercase

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "new@example.com"


def test_signup_rejects_duplicate_email(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    client.post("/api/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    resp = client.post("/api/auth/signup", json={"email": "dup@example.com", "password": "password123"})
    assert resp.status_code == 409


def test_signup_rejects_short_password(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    resp = client.post("/api/auth/signup", json={"email": "short@example.com", "password": "abc"})
    assert resp.status_code == 422


def test_signup_rejects_invalid_email(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    resp = client.post("/api/auth/signup", json={"email": "not-an-email", "password": "password123"})
    assert resp.status_code == 422


def test_login_with_correct_credentials_succeeds(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    client.post("/api/auth/signup", json={"email": "login@example.com", "password": "password123"})
    client.post("/api/auth/logout")

    resp = client.post("/api/auth/login", json={"email": "login@example.com", "password": "password123"})
    assert resp.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_login_with_wrong_password_fails(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    client.post("/api/auth/signup", json={"email": "login2@example.com", "password": "password123"})
    client.post("/api/auth/logout")

    resp = client.post("/api/auth/login", json={"email": "login2@example.com", "password": "wrong-password"})
    assert resp.status_code == 401


def test_login_with_unknown_email_fails(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "password123"})
    assert resp.status_code == 401


def test_logout_clears_the_session(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    client.post("/api/auth/signup", json={"email": "logout@example.com", "password": "password123"})
    assert client.get("/api/auth/me").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_me_without_a_session_is_401(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    assert client.get("/api/auth/me").status_code == 401


# --- dependencies.py -------------------------------------------------

def test_get_current_user_dependency_resolves_the_session_user(session_factory):
    with session_factory() as session:
        user = User(email="dep@example.com", password_hash=hash_password("password123"))
        session.add(user)
        session.commit()
        user_id = user.id

    get_current_user = build_get_current_user(session_factory)

    class _FakeRequest:
        session = {"user_id": user_id}

    resolved = get_current_user(_FakeRequest())
    assert resolved.email == "dep@example.com"


def test_get_current_user_dependency_rejects_missing_session(session_factory):
    from fastapi import HTTPException, Request

    get_current_user = build_get_current_user(session_factory)

    class _FakeRequest:
        session = {}

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(_FakeRequest())
    assert exc_info.value.status_code == 401


def test_get_current_user_dependency_rejects_inactive_user(session_factory):
    with session_factory() as session:
        user = User(email="inactive@example.com", password_hash=hash_password("password123"), is_active=False)
        session.add(user)
        session.commit()
        user_id = user.id

    from fastapi import HTTPException

    get_current_user = build_get_current_user(session_factory)

    class _FakeRequest:
        session = {"user_id": user_id}

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(_FakeRequest())
    assert exc_info.value.status_code == 401


# --- landing/signup/login/app pages -----------------------------------

def test_landing_page_is_served_at_root(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Sign up" in resp.text


def test_signup_and_login_pages_are_served(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    assert client.get("/signup").status_code == 200
    assert client.get("/login").status_code == 200


def test_app_page_redirects_to_login_when_not_authenticated(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/app")
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/login"


def test_app_page_is_served_once_logged_in(loop, session_factory):
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key="test-secret"))
    client = TestClient(app)
    client.post("/api/auth/signup", json={"email": "dash@example.com", "password": "password123"})
    resp = client.get("/app")
    assert resp.status_code == 200


def test_app_page_is_served_unauthenticated_when_auth_is_not_configured(loop, session_factory):
    # No SESSION_SECRET_KEY -- matches today's single-tenant deployment,
    # which has no login step at all (see create_app's session_secret_key
    # guard).
    app = create_app(loop, session_factory, "paper", settings=Settings(session_secret_key=""))
    client = TestClient(app)
    resp = client.get("/app")
    assert resp.status_code == 200
