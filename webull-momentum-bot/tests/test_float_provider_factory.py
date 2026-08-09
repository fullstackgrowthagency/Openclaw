"""
Tests for get_float_provider()'s wiring -- specifically that the
yfinance fallback wraps whichever primary is configured, and can be
switched off (data/float_providers/__init__.py).
"""
import tempfile
from pathlib import Path

from webull_bot.config import FMPCredentials, MassiveCredentials, Settings
from webull_bot.data.float_providers import get_float_provider
from webull_bot.data.float_providers.cache import CachedFloatProvider
from webull_bot.data.float_providers.fallback import FallbackFloatProvider
from webull_bot.data.float_providers.fmp import FMPFloatProvider


def _settings(**overrides) -> Settings:
    base = dict(
        fmp=FMPCredentials(api_key="test-key", base_url="https://x"),
        massive=MassiveCredentials(),
        float_cache_dir=Path(tempfile.mkdtemp()),
        enable_yfinance_fallback=True,
    )
    base.update(overrides)
    return Settings(**base)


def test_yfinance_fallback_wraps_the_primary_by_default():
    provider = get_float_provider(_settings())
    assert isinstance(provider, CachedFloatProvider)
    assert isinstance(provider.delegate, FallbackFloatProvider)
    assert isinstance(provider.delegate.primary, FMPFloatProvider)
    assert len(provider.delegate.fallbacks) == 1


def test_yfinance_fallback_can_be_disabled():
    provider = get_float_provider(_settings(enable_yfinance_fallback=False))
    assert isinstance(provider, CachedFloatProvider)
    assert isinstance(provider.delegate, FMPFloatProvider)
