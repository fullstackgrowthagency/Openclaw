import pytest

from webull_bot.config import Settings, TradingMode, WebullCredentials


def _settings(**overrides) -> Settings:
    base = dict(trading_mode=TradingMode.SANDBOX, live_trading_enabled=False, live_trading_confirmation="")
    base.update(overrides)
    return Settings(**base)


def test_sandbox_mode_is_not_live_authorized():
    s = _settings(trading_mode=TradingMode.SANDBOX)
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


def test_webull_credentials_not_configured_by_default():
    assert not WebullCredentials(app_key="", app_secret="", account_id="").is_configured()
    assert WebullCredentials(app_key="k", app_secret="s", account_id="1").is_configured()
