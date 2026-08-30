"""Tests for scripts/run_dashboard.py's _build_loop_for_user -- specifically
that it loads a user's saved risk/position settings (db/repository.py's
BotSettings) into the TradingLoop it builds, instead of always constructing
RiskConfig/PositionManagementConfig at their hardcoded defaults. This is the
regression test for the reported bug: settings saved via the dashboard's
Settings modal were reverting to defaults on the next loop rebuild (a
process restart, or a mid-session broker-credential save triggering
auth/broker_credential_routes.py's _restart_loop_for).

get_broker_client (imported into, and called from, webull_bot.main's
build_trading_loop) is monkeypatched there to return a PaperBrokerClient
-- since brokers/__init__.py's factory only knows how to build a real,
network-hitting WebullBrokerClient for SANDBOX/LIVE mode, and this suite
has no live broker credentials or network access. PaperBrokerClient.
connect() is a local no-op, so this lets _build_loop_for_user's real code
path -- bot_id resolution, BotSettings loading, build_trading_loop's
config wiring -- run genuinely in-process."""
from __future__ import annotations

import webull_bot.main as main_module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from webull_bot.brokers.paper.client import PaperBrokerClient
from webull_bot.config import Settings
from webull_bot.db.models import Base
from webull_bot.db.repository import update_bot_settings
from webull_bot.position.position_manager import PositionManagementConfig
from webull_bot.risk.risk_engine import RiskConfig
from webull_bot.runtime.trading_loop import TradingLoop
from scripts.run_dashboard import _build_loop_for_user


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _build_loop(session_factory, user_id=1) -> TradingLoop:
    return _build_loop_for_user(user_id, Settings(), session_factory)


def test_build_loop_for_user_applies_saved_risk_settings(monkeypatch):
    monkeypatch.setattr(main_module, "get_broker_client", lambda settings: PaperBrokerClient())
    session_factory = _session_factory()
    with session_factory() as session:
        update_bot_settings(session, user_id=1, bot_id=1, stop_loss_pct=2.5, max_daily_loss_pct=1.0)
        session.commit()

    loop = _build_loop(session_factory)

    assert loop.risk_engine.config.stop_loss_pct == 2.5
    assert loop.risk_engine.config.max_daily_loss_pct == 1.0
    # Every other field keeps RiskConfig's own hardcoded default.
    assert loop.risk_engine.config.min_risk_reward_ratio == RiskConfig().min_risk_reward_ratio


def test_build_loop_for_user_applies_saved_position_settings(monkeypatch):
    monkeypatch.setattr(main_module, "get_broker_client", lambda settings: PaperBrokerClient())
    session_factory = _session_factory()
    with session_factory() as session:
        update_bot_settings(session, user_id=1, bot_id=1, trailing_stop_pct=4.0, breakeven_trigger_pct=8.0)
        session.commit()

    loop = _build_loop(session_factory)

    assert loop.position_manager.config.trailing_stop_pct == 4.0
    assert loop.position_manager.config.breakeven_trigger_pct == 8.0


def test_build_loop_for_user_with_no_saved_settings_uses_defaults(monkeypatch):
    monkeypatch.setattr(main_module, "get_broker_client", lambda settings: PaperBrokerClient())
    session_factory = _session_factory()

    loop = _build_loop(session_factory)

    assert loop.risk_engine.config == RiskConfig()
    assert loop.position_manager.config == PositionManagementConfig()


def test_build_loop_for_user_rebuild_picks_up_settings_saved_after_first_build(monkeypatch):
    """The concrete regression test: a loop built once with defaults, then a
    dashboard save happens (writing BotSettings while that loop is still
    running), then the loop gets rebuilt (simulating
    auth/broker_credential_routes.py's _restart_loop_for stop-then-start
    after a broker-credential change) -- the rebuilt loop must reflect the
    saved value, not silently revert to RiskConfig's hardcoded default."""
    monkeypatch.setattr(main_module, "get_broker_client", lambda settings: PaperBrokerClient())
    session_factory = _session_factory()

    first_loop = _build_loop(session_factory)
    assert first_loop.risk_engine.config.stop_loss_pct == RiskConfig().stop_loss_pct

    with session_factory() as session:
        update_bot_settings(session, user_id=1, bot_id=1, stop_loss_pct=1.5)
        session.commit()

    second_loop = _build_loop(session_factory)
    assert second_loop.risk_engine.config.stop_loss_pct == 1.5


def test_build_loop_for_user_applies_saved_bot_enabled_false(monkeypatch):
    # The dashboard's bot ON/OFF toggle must survive a restart/rebuild --
    # a bot left off should come back up off, not silently re-enabled.
    monkeypatch.setattr(main_module, "get_broker_client", lambda settings: PaperBrokerClient())
    session_factory = _session_factory()
    with session_factory() as session:
        update_bot_settings(session, user_id=1, bot_id=1, bot_enabled=False)
        session.commit()

    loop = _build_loop(session_factory)

    assert loop.risk_engine.bot_enabled is False


def test_build_loop_for_user_with_no_saved_bot_enabled_defaults_to_on(monkeypatch):
    monkeypatch.setattr(main_module, "get_broker_client", lambda settings: PaperBrokerClient())
    session_factory = _session_factory()

    loop = _build_loop(session_factory)

    assert loop.risk_engine.bot_enabled is True


def test_build_loop_for_user_applies_saved_bot_enabled_true(monkeypatch):
    # An explicit True (re-enabled after a prior off) must also survive a
    # rebuild, same as False -- not just left at the loop's own default.
    monkeypatch.setattr(main_module, "get_broker_client", lambda settings: PaperBrokerClient())
    session_factory = _session_factory()
    with session_factory() as session:
        update_bot_settings(session, user_id=1, bot_id=1, bot_enabled=True)
        session.commit()

    loop = _build_loop(session_factory)

    assert loop.risk_engine.bot_enabled is True
