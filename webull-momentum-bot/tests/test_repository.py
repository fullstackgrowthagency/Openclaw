from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from webull_bot.db.models import Base, MomentumEventRecord, MomentumScoreRecord, OrderRecord, ScannerEvent, TradeRecord
from webull_bot.db.repository import (
    DBBackedEventRecorder,
    get_momentum_score_component_summary,
    get_momentum_scores,
    get_performance_summary,
    get_recent_trades,
    record_momentum_event,
    record_momentum_score,
    record_order,
    record_scanner_event,
    record_trade,
)
from webull_bot.enums import CandidateState, ExitReason, MomentumOutcome, OrderSide, OrderStatus, OrderType
from webull_bot.models import MomentumEvent, MomentumMetrics, MomentumScore, MomentumScoreComponents, Order, Trade


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def session_factory():
    """A real, callable session factory (unlike the `session` fixture above)
    for tests that need multiple independent sessions against the same
    in-memory DB -- plain sqlite:///:memory: gives each new connection its
    own empty database, so StaticPool is required to share one."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _trade(**overrides) -> Trade:
    base = dict(
        symbol="AAPL", strategy_name="momentum_breakout", side=OrderSide.BUY,
        entry_price=10.0, exit_price=11.0, quantity=100,
        opened_at=datetime(2026, 1, 1, 9, 31), closed_at=datetime(2026, 1, 1, 9, 45),
        exit_reason=ExitReason.PROFIT_TARGET, pnl=100.0, pnl_pct=10.0,
        max_favorable_excursion=12.0, max_adverse_excursion=1.0,
    )
    base.update(overrides)
    return Trade(**base)


def _order(**overrides) -> Order:
    base = dict(
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100,
        status=OrderStatus.SUBMITTED, client_order_id="abc-123", broker_order_id="abc-123",
        strategy_name="momentum_breakout",
    )
    base.update(overrides)
    return Order(**base)


def test_record_trade_persists(session):
    record_trade(session, _trade(), trading_mode="paper")
    session.commit()
    rows = session.query(TradeRecord).all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].pnl == 100.0
    assert rows[0].exit_reason == "profit_target"


def test_record_order_inserts_then_updates_same_row(session):
    record_order(session, _order(status=OrderStatus.SUBMITTED), trading_mode="paper")
    session.commit()
    assert session.query(OrderRecord).count() == 1

    record_order(session, _order(status=OrderStatus.FILLED), trading_mode="paper")
    session.commit()

    rows = session.query(OrderRecord).all()
    assert len(rows) == 1  # updated in place, not a second row
    assert rows[0].status == "filled"


def test_record_order_requires_client_order_id(session):
    with pytest.raises(ValueError):
        record_order(session, _order(client_order_id=None), trading_mode="paper")


def test_get_recent_trades_orders_by_closed_at_desc(session):
    record_trade(session, _trade(closed_at=datetime(2026, 1, 1, 9, 45)), trading_mode="paper")
    record_trade(session, _trade(closed_at=datetime(2026, 1, 2, 9, 45)), trading_mode="paper")
    session.commit()
    rows = get_recent_trades(session, limit=10)
    assert rows[0].closed_at > rows[1].closed_at


def test_get_performance_summary_empty(session):
    summary = get_performance_summary(session)
    assert summary["total_trades"] == 0
    assert summary["win_rate"] == 0.0


def test_get_performance_summary_computes_win_rate_and_pnl(session):
    record_trade(session, _trade(pnl=100.0, pnl_pct=10.0), trading_mode="paper")
    record_trade(session, _trade(pnl=-50.0, pnl_pct=-5.0, exit_reason=ExitReason.STOP_LOSS), trading_mode="paper")
    session.commit()
    summary = get_performance_summary(session)
    assert summary["total_trades"] == 2
    assert summary["win_rate"] == 0.5
    assert summary["total_pnl"] == 50.0


# -- scanner events, momentum scores, momentum events -----------------------

def test_record_scanner_event_persists(session):
    record_scanner_event(
        session, symbol="TEST", from_state=CandidateState.WATCHING, to_state=CandidateState.HEATING_UP,
        timestamp=datetime(2026, 1, 1, 9, 31), reason="MIS crossed threshold",
    )
    session.commit()
    rows = session.query(ScannerEvent).all()
    assert len(rows) == 1
    assert rows[0].from_state == "watching"
    assert rows[0].to_state == "heating_up"
    assert rows[0].event_type == "state_transition"


def _score(**overrides) -> MomentumScore:
    base = dict(
        symbol="TEST", timestamp=datetime(2026, 1, 1, 9, 31), score=72.5, weights_version="v1-test",
        components=MomentumScoreComponents(
            float_score=80, float_velocity_score=70, relative_volume_score=60, volume_acceleration_score=50,
            price_acceleration_score=40, breakout_proximity_score=30, trend_quality_score=20, liquidity_score=10,
            float_turnover_score=90, short_term_relative_volume_score=85, dollar_volume_acceleration_score=75,
        ),
    )
    base.update(overrides)
    return MomentumScore(**base)


def test_record_momentum_score_persists_components_as_json(session):
    record_momentum_score(session, _score())
    session.commit()
    rows = session.query(MomentumScoreRecord).all()
    assert len(rows) == 1
    assert rows[0].score == 72.5
    assert rows[0].components["float_score"] == 80


# -- historical score reads (sanity-checking weights.yaml) -------------------

def test_get_momentum_scores_filters_by_symbol_and_orders_newest_first(session):
    record_momentum_score(session, _score(symbol="AAA", timestamp=datetime(2026, 1, 1, 9, 30)))
    record_momentum_score(session, _score(symbol="AAA", timestamp=datetime(2026, 1, 1, 9, 31)))
    record_momentum_score(session, _score(symbol="BBB", timestamp=datetime(2026, 1, 1, 9, 32)))
    session.commit()

    rows = get_momentum_scores(session, symbol="AAA")
    assert [r.timestamp for r in rows] == [datetime(2026, 1, 1, 9, 31), datetime(2026, 1, 1, 9, 30)]


def test_get_momentum_scores_without_symbol_returns_everything(session):
    record_momentum_score(session, _score(symbol="AAA"))
    record_momentum_score(session, _score(symbol="BBB"))
    session.commit()

    assert len(get_momentum_scores(session)) == 2


def test_component_summary_averages_and_weights_only_the_latest_version(session):
    from webull_bot.scoring.momentum_ignition_score import MISConfig

    # An older-version row (fewer components, different formula) must not
    # get mixed into the average -- see get_momentum_score_component_summary's
    # docstring for why that would be meaningless.
    record_momentum_score(session, _score(
        weights_version="v1-old", timestamp=datetime(2026, 1, 1, 9, 30), components=MomentumScoreComponents(
            float_score=10, float_velocity_score=10, relative_volume_score=10, volume_acceleration_score=10,
            price_acceleration_score=10, breakout_proximity_score=10, trend_quality_score=10, liquidity_score=10,
            float_turnover_score=10, short_term_relative_volume_score=10, dollar_volume_acceleration_score=10,
        )))
    record_momentum_score(session, _score(
        weights_version="v2-current", timestamp=datetime(2026, 1, 1, 9, 31), components=MomentumScoreComponents(
            float_score=20, float_velocity_score=20, relative_volume_score=80, volume_acceleration_score=20,
            price_acceleration_score=20, breakout_proximity_score=20, trend_quality_score=20, liquidity_score=20,
            float_turnover_score=60, short_term_relative_volume_score=90, dollar_volume_acceleration_score=20,
        )))
    record_momentum_score(session, _score(
        weights_version="v2-current", timestamp=datetime(2026, 1, 1, 9, 32), components=MomentumScoreComponents(
            float_score=20, float_velocity_score=20, relative_volume_score=80, volume_acceleration_score=20,
            price_acceleration_score=20, breakout_proximity_score=20, trend_quality_score=20, liquidity_score=20,
            float_turnover_score=60, short_term_relative_volume_score=90, dollar_volume_acceleration_score=20,
        )))
    session.commit()

    config = MISConfig.load()
    summary = get_momentum_score_component_summary(session, mis_config=config)

    assert summary["weights_version"] == "v2-current"
    assert summary["sample_size"] == 2  # the v1-old row is excluded

    by_name = {c["name"]: c for c in summary["components"]}
    assert by_name["relative_volume_score"]["avg_raw_score"] == pytest.approx(80.0)
    assert by_name["relative_volume_score"]["sample_size"] == 2
    # short_term_relative_volume_score has both a high raw score AND a high
    # configured weight (see weights.yaml's v2 reweight) so it should come
    # out on top of the sorted list -- the whole point of this endpoint.
    assert summary["components"][0]["name"] == "short_term_relative_volume_score"


def test_component_summary_with_no_data_returns_empty():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as empty_session:
        summary = get_momentum_score_component_summary(empty_session)
    assert summary["weights_version"] is None
    assert summary["sample_size"] == 0
    assert summary["components"] == []


def _metrics() -> MomentumMetrics:
    return MomentumMetrics(
        symbol="TEST", timestamp=datetime(2026, 1, 1, 9, 31), float_turnover=0.1,
        float_velocity_1m=0.01, float_velocity_3m=0.02, float_velocity_5m=0.03,
        relative_volume=3.0, relative_volume_1m=1.0, relative_volume_5m=1.0, volume_accel_1m_3m=1.5,
        volume_1m=0.0, volume_5m=0.0, volume_15m=0.0,
        dollar_volume_1m=0.0, dollar_volume_5m=0.0, dollar_volume_15m=0.0, dollar_volume_accel_1m_3m=1.0,
        price_velocity_1m=1.0, price_velocity_3m=2.0,
        price_velocity_5m=3.0, price_velocity_15m=4.0, price_acceleration=1.0, vwap=10.0,
        distance_from_vwap_pct=1.0, distance_from_hod_pct=1.0, distance_from_premarket_high_pct=None,
        distance_from_resistance_pct=None, spread_abs=0.01, spread_pct=0.1, dollar_volume=1_000_000,
    )


def _momentum_event(**overrides) -> MomentumEvent:
    base = dict(
        symbol="TEST", detected_at=datetime(2026, 1, 1, 9, 31), trigger_reason="momentum_breakout:enter_long",
        was_traded=True, score_at_event=72.5, metrics_at_event=_metrics(), price_at_event=5.20,
    )
    base.update(overrides)
    return MomentumEvent(**base)


def test_record_momentum_event_serializes_metrics_with_isoformat_timestamp(session):
    record_momentum_event(session, _momentum_event())
    session.commit()
    row = session.query(MomentumEventRecord).one()
    assert row.symbol == "TEST"
    assert row.was_traded is True
    assert row.metrics_at_event["symbol"] == "TEST"
    assert row.metrics_at_event["timestamp"] == "2026-01-01T09:31:00"  # datetime -> isoformat string
    assert row.outcome_label == "unknown"


def test_record_momentum_event_upserts_by_existing_id_not_duplicating_rows(session):
    row1 = record_momentum_event(session, _momentum_event())
    session.commit()
    assert session.query(MomentumEventRecord).count() == 1

    event = _momentum_event(outcome_label=MomentumOutcome.CONTINUED, outcome_15m={"pct_change": 5.0})
    record_momentum_event(session, event, existing_id=row1.id)
    session.commit()

    assert session.query(MomentumEventRecord).count() == 1
    row = session.query(MomentumEventRecord).one()
    assert row.outcome_label == "continued"
    assert row.outcome_15m == {"pct_change": 5.0}


def test_db_backed_event_recorder_writes_through_and_updates_same_row(session_factory):
    recorder = DBBackedEventRecorder(session_factory)
    event = _momentum_event(was_traded=False)

    event_id = recorder.save(event)
    with session_factory() as s:
        assert s.query(MomentumEventRecord).count() == 1
        assert s.query(MomentumEventRecord).one().was_traded is False

    # Mutate in place (mirrors how MomentumEventTracker updates a tracked event) and flush again.
    event.was_traded = True
    event.outcome_label = MomentumOutcome.CONTINUED
    recorder.update(event_id)

    with session_factory() as s:
        rows = s.query(MomentumEventRecord).all()
        assert len(rows) == 1  # still one row, not a duplicate
        assert rows[0].was_traded is True
        assert rows[0].outcome_label == "continued"

    # The in-memory base-class behavior (used by MomentumEventTracker.get()) must still work.
    assert recorder.get(event_id) is event
