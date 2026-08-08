from __future__ import annotations

from ...config import Settings
from ...interfaces.float_provider import FloatDataProvider
from .cache import CachedFloatProvider


def get_float_provider(settings: Settings) -> FloatDataProvider:
    """Factory that picks the free-float data backend from Settings.

    FMP is the currently active/verified implementation. Massive remains as
    an alternate skeleton (see massive.py) in case the project switches
    providers later -- callers should go through this factory rather than
    importing a concrete provider class directly.
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

    return CachedFloatProvider(delegate, settings.float_cache_dir, settings.float_cache_ttl_hours)
