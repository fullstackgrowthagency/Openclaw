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
from webull_bot.db.repository import record_momentum_score, record_trade
from webull_bot.enums import CandidateState, ExitReason, OrderSide
from webull_bot.execution.order_manager import OrderManager
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import FloatData, MarketSnapshot, MomentumScore, MomentumScoreComponents, Position, Trade
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


def test_responses_disable_caching(client):
    # Regression for a real bug: a browser kept a stale cached app.js after
    # a deploy added a table column, silently shifting every value after it
    # under the wrong header with no visible error (see _NoCacheMiddleware).
    resp = client.get("/api/status")
    assert resp.headers["cache-control"] == "no-store"


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
    assert rows[0]["price"] is None  # CandidateWatcher.update() hasn't ticked yet


def test_candidates_exposes_the_latest_price(loop, client):
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    candidate.last_price = 7.42
    loop.candidates["TEST"] = candidate

    rows = client.get("/api/candidates").json()
    assert rows[0]["price"] == 7.42


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


def test_candidates_exposes_component_breakdown_when_scored(loop, client):
    # Feeds dashboard/static/app.js's click-a-candidate-row -> show its live
    # score breakdown feature -- see /api/mis-weights for the weights this
    # pairs with.
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    candidate.latest_score = MomentumScore(
        symbol="TEST", timestamp=datetime.utcnow(), score=61.5, weights_version="v2-test",
        components=MomentumScoreComponents(
            float_score=10, float_velocity_score=20, relative_volume_score=90, volume_acceleration_score=10,
            price_acceleration_score=10, breakout_proximity_score=10, trend_quality_score=10, liquidity_score=10,
            float_turnover_score=10, short_term_relative_volume_score=10, dollar_volume_acceleration_score=10,
        ),
    )
    loop.candidates["TEST"] = candidate

    rows = client.get("/api/candidates").json()
    assert rows[0]["components"]["relative_volume_score"] == 90
    assert rows[0]["score_weights_version"] == "v2-test"


def test_candidates_components_is_none_before_first_score(loop, client):
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    loop.candidates["TEST"] = candidate

    rows = client.get("/api/candidates").json()
    assert rows[0]["components"] is None
    assert rows[0]["score_weights_version"] is None


def test_mis_weights_endpoint_returns_current_config(client):
    from webull_bot.scoring.momentum_ignition_score import MISConfig

    resp = client.get("/api/mis-weights")
    assert resp.status_code == 200
    body = resp.json()
    config = MISConfig.load()
    assert body["weights_version"] == config.version
    assert body["weights"]["relative_volume_score"] == pytest.approx(config.weights["relative_volume_score"])
    assert abs(sum(body["weights"].values()) - 1.0) < 1e-9


def test_scan_symbol_adds_a_passing_ticker(loop, client):
    loop.broker.feed_snapshot(
        MarketSnapshot(
            symbol="NEWSYM", timestamp=datetime.utcnow(), last_price=5.0, bid=4.99, ask=5.01,
            bid_size=100, ask_size=100, cumulative_volume=200_000, vwap=5.0, high_of_day=5.0,
            low_of_day=5.0, open_price=5.0,
        )
    )
    resp = client.post("/api/scan-symbol", params={"symbol": "newsym"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "NEWSYM"
    assert body["added"] is True
    assert body["already_tracked"] is False
    assert body["state"] == "watching"
    assert body["reason"] == "passed broad scanner filters"
    assert "NEWSYM" in loop.candidates


def test_scan_symbol_reports_rejected_state_with_reason(loop, client):
    loop.broker.feed_snapshot(
        MarketSnapshot(
            symbol="TOOEXPENSIVE", timestamp=datetime.utcnow(), last_price=30.0, bid=29.99, ask=30.01,
            bid_size=100, ask_size=100, cumulative_volume=200_000, vwap=30.0, high_of_day=30.0,
            low_of_day=30.0, open_price=30.0,
        )
    )
    resp = client.post("/api/scan-symbol", params={"symbol": "TOOEXPENSIVE"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] is False
    assert body["state"] == "rejected"
    assert "range" in body["reason"].lower()
    assert "TOOEXPENSIVE" not in loop.candidates


def test_scan_symbol_reports_already_tracked_state_without_rescanning(loop, client):
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    transition(candidate, CandidateState.HEATING_UP)
    loop.candidates["TEST"] = candidate
    # No snapshot fed for TEST -- if this re-scanned instead of returning
    # the existing candidate, the response would come back rejected.

    resp = client.post("/api/scan-symbol", params={"symbol": "TEST"})
    body = resp.json()
    assert body["added"] is False
    assert body["already_tracked"] is True
    assert body["state"] == "heating_up"


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
    # PaperBrokerClient (this fixture's broker) has no place_oco_bracket, so
    # this position was never (and never could be) broker-bracketed -- see
    # TradingLoop._attach_broker_bracket.
    assert rows[0]["broker_managed"] is False


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


def _momentum_score(**overrides) -> MomentumScore:
    base = dict(
        symbol="TEST", timestamp=datetime.utcnow(), score=72.5, weights_version="v2-test",
        components=MomentumScoreComponents(
            float_score=50, float_velocity_score=50, relative_volume_score=90, volume_acceleration_score=50,
            price_acceleration_score=50, breakout_proximity_score=50, trend_quality_score=50, liquidity_score=50,
            float_turnover_score=50, short_term_relative_volume_score=50, dollar_volume_acceleration_score=50,
        ),
    )
    base.update(overrides)
    return MomentumScore(**base)


def test_score_breakdown_reads_from_database(session_factory, client):
    with session_factory() as session:
        record_momentum_score(session, _momentum_score())
        session.commit()

    resp = client.get("/api/score-breakdown")
    assert resp.status_code == 200
    body = resp.json()
    assert body["weights_version"] == "v2-test"
    assert body["sample_size"] == 1
    names = [c["name"] for c in body["components"]]
    assert "relative_volume_score" in names
    # relative_volume_score had the highest raw sub-score in the fixture
    # above and a non-trivial configured weight, so it should be at/near
    # the top of the sorted-by-contribution list.
    assert body["components"][0]["name"] == "relative_volume_score"


def test_score_breakdown_with_no_data_is_empty(client):
    resp = client.get("/api/score-breakdown")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sample_size"] == 0
    assert body["components"] == []


def test_score_history_filters_by_symbol_and_returns_components(session_factory, client):
    with session_factory() as session:
        record_momentum_score(session, _momentum_score(symbol="AAA"))
        record_momentum_score(session, _momentum_score(symbol="BBB"))
        session.commit()

    resp = client.get("/api/score-history?symbol=aaa")  # lowercase -- endpoint should uppercase it
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["score"] == 72.5
    assert rows[0]["components"]["relative_volume_score"] == 90


def test_risk_settings_returns_current_config(loop, client):
    resp = client.get("/api/risk-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stop_loss_pct"] == loop.risk_engine.config.stop_loss_pct
    assert body["min_risk_reward_ratio"] == loop.risk_engine.config.min_risk_reward_ratio
    assert body["max_position_size_pct"] == loop.risk_engine.config.max_position_size_pct
    assert body["max_total_risk_pct"] == loop.risk_engine.config.max_total_risk_pct
    assert body["max_daily_loss_pct"] == loop.risk_engine.config.max_daily_loss_pct
    assert body["max_simultaneous_positions"] == loop.risk_engine.config.max_simultaneous_positions
    assert body["allow_extended_hours_trading"] == loop.risk_engine.config.allow_extended_hours_trading


def test_risk_settings_update_toggles_allow_extended_hours_trading(loop, client):
    assert loop.risk_engine.config.allow_extended_hours_trading is False
    resp = client.post("/api/risk-settings", json={"allow_extended_hours_trading": True})
    assert resp.status_code == 200
    assert resp.json()["allow_extended_hours_trading"] is True
    assert loop.risk_engine.config.allow_extended_hours_trading is True
    # And back off -- False is a legitimate, meaningful value here too, not
    # something the exclude_none=True update logic should ever treat as
    # "omitted."
    resp = client.post("/api/risk-settings", json={"allow_extended_hours_trading": False})
    assert resp.status_code == 200
    assert resp.json()["allow_extended_hours_trading"] is False
    assert loop.risk_engine.config.allow_extended_hours_trading is False


def test_risk_settings_update_mutates_live_engine(loop, client):
    resp = client.post("/api/risk-settings", json={"stop_loss_pct": 2.5})
    assert resp.status_code == 200
    assert resp.json()["stop_loss_pct"] == 2.5
    assert loop.risk_engine.config.stop_loss_pct == 2.5
    # Omitted fields are left untouched.
    assert loop.risk_engine.config.max_total_risk_pct == 50.0


def test_risk_settings_update_mutates_daily_loss_limit(loop, client):
    resp = client.post("/api/risk-settings", json={"max_daily_loss_pct": 1.5})
    assert resp.status_code == 200
    assert resp.json()["max_daily_loss_pct"] == 1.5
    assert loop.risk_engine.config.max_daily_loss_pct == 1.5


def test_risk_settings_update_rejects_non_positive_values(loop, client):
    resp = client.post("/api/risk-settings", json={"stop_loss_pct": 0})
    assert resp.status_code == 422
    assert loop.risk_engine.config.stop_loss_pct != 0


def test_risk_settings_update_rejects_pct_field_over_100(loop, client):
    resp = client.post("/api/risk-settings", json={"max_position_size_pct": 150})
    assert resp.status_code == 422
    assert loop.risk_engine.config.max_position_size_pct != 150


def test_risk_settings_update_allows_ratio_field_over_100(loop, client):
    # min_risk_reward_ratio isn't a percentage -- e.g. 5.0 ("5x reward for 1x
    # risk") is a legitimate, if conservative, setting.
    resp = client.post("/api/risk-settings", json={"min_risk_reward_ratio": 150})
    assert resp.status_code == 200
    assert loop.risk_engine.config.min_risk_reward_ratio == 150


def test_risk_settings_update_allows_zero_max_simultaneous_positions(loop, client):
    # Unlike every other adjustable field, 0 is a valid, meaningful value
    # here (unlimited) rather than a rejected "must be greater than 0."
    resp = client.post("/api/risk-settings", json={"max_simultaneous_positions": 0})
    assert resp.status_code == 200
    assert resp.json()["max_simultaneous_positions"] == 0
    assert loop.risk_engine.config.max_simultaneous_positions == 0


def test_risk_settings_update_rejects_negative_max_simultaneous_positions(loop, client):
    resp = client.post("/api/risk-settings", json={"max_simultaneous_positions": -1})
    assert resp.status_code == 422
    assert loop.risk_engine.config.max_simultaneous_positions != -1


def test_risk_settings_update_allows_max_simultaneous_positions_over_100(loop, client):
    # Also not a percentage -- no 100 ceiling applies.
    resp = client.post("/api/risk-settings", json={"max_simultaneous_positions": 250})
    assert resp.status_code == 200
    assert loop.risk_engine.config.max_simultaneous_positions == 250


def test_position_settings_returns_current_config(loop, client):
    resp = client.get("/api/position-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trailing_stop_pct"] == loop.position_manager.config.trailing_stop_pct
    assert body["breakeven_trigger_pct"] == loop.position_manager.config.breakeven_trigger_pct


def test_position_settings_update_mutates_live_manager(loop, client):
    resp = client.post("/api/position-settings", json={"breakeven_trigger_pct": 7.5})
    assert resp.status_code == 200
    assert resp.json()["breakeven_trigger_pct"] == 7.5
    assert loop.position_manager.config.breakeven_trigger_pct == 7.5
    # Omitted fields are left untouched.
    assert loop.position_manager.config.trailing_stop_pct == 3.0


def test_position_settings_update_rejects_non_positive_values(loop, client):
    resp = client.post("/api/position-settings", json={"trailing_stop_pct": 0})
    assert resp.status_code == 422
    assert loop.position_manager.config.trailing_stop_pct != 0


def test_position_settings_update_rejects_value_over_100(loop, client):
    resp = client.post("/api/position-settings", json={"breakeven_trigger_pct": 150})
    assert resp.status_code == 422
    assert loop.position_manager.config.breakeven_trigger_pct != 150


def test_kill_switch_engage_via_dashboard(loop, client):
    resp = client.post("/api/kill-switch", json={"active": True})
    assert resp.status_code == 200
    assert resp.json()["kill_switch_active"] is True
    # Takes effect immediately -- no need to wait for a trading-loop tick.
    assert loop.risk_engine.kill_switch_active is True
    # The flatten-all-positions action the trading loop's own next tick
    # (and every tick after, until it succeeds or the switch is
    # disengaged -- see TradingLoop.engage_kill_switch_and_flatten's
    # docstring) will carry out is driven directly off
    # risk_engine.kill_switch_active, not a separate one-shot flag.
    assert loop._close_all_positions_reason == "Kill switch engaged from dashboard"


def test_kill_switch_disengage_via_dashboard(loop, client):
    loop.risk_engine.engage_kill_switch("manual test")

    resp = client.post("/api/kill-switch", json={"active": False})

    assert resp.status_code == 200
    assert resp.json()["kill_switch_active"] is False
    assert loop.risk_engine.kill_switch_active is False


def test_kill_switch_disengage_stops_the_flatten_retry(loop, client):
    # Once disengaged, _process_all_candidates' kill-switch check
    # (gated on risk_engine.kill_switch_active) must not keep trying to
    # force-close anything -- an open position at this point is left
    # exactly as-is, per the dashboard's own disengage confirmation text.
    loop.risk_engine.engage_kill_switch("manual test")

    client.post("/api/kill-switch", json={"active": False})

    assert loop.risk_engine.kill_switch_active is False


def test_index_and_static_assets_are_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/app.js").status_code == 200
