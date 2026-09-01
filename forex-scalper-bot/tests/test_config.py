from fx_bot.config import Environment, Settings, TradingMode


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
