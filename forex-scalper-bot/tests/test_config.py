import pytest

from fx_bot.config import Environment, Settings, TradingMode


def _settings(**overrides) -> Settings:
    base = dict(trading_mode=TradingMode.DEMO, live_trading_enabled=False, live_trading_confirmation="")
    base.update(overrides)
    return Settings(**base)


def test_demo_mode_is_not_live_authorized():
    s = _settings(trading_mode=TradingMode.DEMO)
    assert not s.is_live_trading_authorized()
    s.require_non_live_or_authorized()  # should not raise


def test_live_mode_requires_all_three_conditions():
    assert not _settings(trading_mode=TradingMode.LIVE).is_live_trading_authorized()
    assert not _settings(trading_mode=TradingMode.LIVE, live_trading_enabled=True).is_live_trading_authorized()
    assert not _settings(
        trading_mode=TradingMode.LIVE, live_trading_enabled=True, live_trading_confirmation="wrong phrase"
    ).is_live_trading_authorized()
    assert _settings(
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
        live_trading_confirmation="I_UNDERSTAND_LIVE_TRADING_RISK",
    ).is_live_trading_authorized()


def test_require_non_live_or_authorized_raises_when_unauthorized():
    s = _settings(trading_mode=TradingMode.LIVE)
    with pytest.raises(RuntimeError):
        s.require_non_live_or_authorized()


def test_settings_defaults_to_demo_and_dev(monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = Settings()
    assert settings.trading_mode == TradingMode.DEMO
    assert settings.environment == Environment.DEV


def test_settings_reads_trading_mode_from_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    assert Settings().trading_mode == TradingMode.LIVE


def test_settings_reads_app_env_from_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    assert Settings().environment == Environment.PROD
