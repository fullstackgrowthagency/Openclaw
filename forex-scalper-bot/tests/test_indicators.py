import pytest

from fx_bot.indicators.moving_average import ema, sma
from fx_bot.indicators.registry import INDICATOR_REGISTRY
from fx_bot.indicators.rsi import rsi


def test_sma_has_leading_nones_before_enough_data():
    prices = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = sma(prices, period=3)
    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)   # (1+2+3)/3
    assert result[3] == pytest.approx(3.0)   # (2+3+4)/3
    assert result[4] == pytest.approx(4.0)   # (3+4+5)/3


def test_sma_series_is_same_length_as_input():
    prices = [1.0] * 10
    assert len(sma(prices, period=3)) == len(prices)


def test_ema_seeds_with_the_sma_of_the_first_window():
    prices = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ema(prices, period=3)
    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)  # seeded with SMA(1,2,3)
    # multiplier = 2/(3+1) = 0.5 -> ema[3] = (4-2)*0.5 + 2 = 3.0
    assert result[3] == pytest.approx(3.0)


def test_ema_all_none_when_shorter_than_period():
    assert ema([1.0, 2.0], period=5) == [None, None]


def test_rsi_all_none_until_period_deltas_exist():
    prices = [1.0, 2.0, 3.0]
    assert rsi(prices, period=5) == [None, None, None]


def test_rsi_is_100_when_every_move_is_a_gain():
    prices = [float(i) for i in range(1, 20)]  # strictly rising
    result = rsi(prices, period=14)
    assert result[14] == pytest.approx(100.0)


def test_rsi_is_0_when_every_move_is_a_loss():
    prices = [float(i) for i in range(20, 1, -1)]  # strictly falling
    result = rsi(prices, period=14)
    assert result[14] == pytest.approx(0.0)


def test_rsi_is_50_when_gains_and_losses_are_balanced():
    # Alternating +1/-1 moves -- equal average gain and average loss.
    prices = [10.0]
    for i in range(20):
        prices.append(prices[-1] + (1.0 if i % 2 == 0 else -1.0))
    result = rsi(prices, period=14)
    assert result[14] == pytest.approx(50.0)


def test_registry_contains_exactly_the_implemented_indicators():
    assert set(INDICATOR_REGISTRY.keys()) == {"sma", "ema", "rsi"}
    for spec in INDICATOR_REGISTRY.values():
        assert callable(spec["fn"])
        assert spec["params"] == {"period": int}
