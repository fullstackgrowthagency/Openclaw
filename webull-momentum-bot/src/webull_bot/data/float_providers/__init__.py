from __future__ import annotations

from ...config import Settings
from ...interfaces.float_provider import FloatDataProvider
from .cache import CachedFloatProvider


def get_float_provider(settings: Settings) -> FloatDataProvider:
    """Factory that picks the free-float data backend from Settings.

    FMP is the currently active/verified primary implementation. Massive
    remains as an alternate skeleton (see massive.py) in case the project
    switches providers later -- callers should go through this factory
    rather than importing a concrete provider class directly.

    When settings.enable_yfinance_fallback is set (default True), whichever
    primary is chosen above gets wrapped in FallbackFloatProvider with
    YFinanceFloatProvider as a free, best-effort secondary -- see
    fallback.py/yfinance_provider.py's module docstrings for why this is
    additive-only (never a standalone/primary source) and safe to leave on.
    """
    if settings.fmp.is_configured():
        from .fmp import FMPFloatProvider

        delegate = FMPFloatProvider(settings.fmp)
    elif settings.massive.is_configured():
        from .massive import MassiveFloatProvider

        delegate = MassiveFloatProvider(settings.massive)
    else:
        raise RuntimeError(
            "No free-float data provider is configured. Set FMP_API_KEY "
            "(or MASSIVE_API_KEY) in .env."
        )

    if settings.enable_yfinance_fallback:
        from .fallback import FallbackFloatProvider
        from .yfinance_provider import YFinanceFloatProvider

        delegate = FallbackFloatProvider(delegate, [YFinanceFloatProvider()])

    return CachedFloatProvider(delegate, settings.float_cache_dir, settings.float_cache_ttl_hours)
