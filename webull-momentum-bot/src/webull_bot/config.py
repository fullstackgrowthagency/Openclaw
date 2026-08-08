"""
Central configuration and safety gate.

Production (live) trading is disabled by default and stays disabled unless
every one of the conditions in `Settings.is_live_trading_authorized` is met.
No other module should re-implement this check -- `OrderManager` and the
broker factory both call it as the single source of truth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

# parents[2] from src/webull_bot/config.py is the webull-momentum-bot project
# root itself (webull_bot -> src -> project root) -- NOT the outer Openclaw
# repo, despite the name of this constant's more common usage elsewhere.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Loads webull-momentum-bot/.env if present. Does not override variables
# already set in the real environment (e.g. by a process manager), and is a
# no-op if no .env file exists -- safe to call unconditionally at import time.
load_dotenv(PROJECT_ROOT / ".env")

# The exact phrase an operator must set in LIVE_TRADING_CONFIRMATION to ever
# route an order to a live account. Changing this string is itself a
# deliberate, auditable code change -- it must never be read from a
# user-editable config file.
_LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_LIVE_TRADING_RISK"


class TradingMode(str, Enum):
    SANDBOX = "sandbox"  # Webull sandbox/dev environment, fake money
    PAPER = "paper"       # Local simulated broker, no external orders at all
    LIVE = "live"         # Real Webull account, real money


class Environment(str, Enum):
    DEV = "dev"
    BACKTEST = "backtest"
    STAGING = "staging"
    PROD = "prod"


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val not in (None, "") else default


@dataclass(frozen=True)
class WebullCredentials:
    app_key: str = field(default_factory=lambda: os.environ.get("WEBULL_APP_KEY", ""))
    app_secret: str = field(default_factory=lambda: os.environ.get("WEBULL_APP_SECRET", ""))
    account_id: str = field(default_factory=lambda: os.environ.get("WEBULL_ACCOUNT_ID", ""))
    # Base URL is intentionally not hard-coded here. Webull's OpenAPI hosts
    # differ between sandbox and production and change over time -- pull the
    # current values from the official docs at integration time rather than
    # guessing, and set them via env vars.
    base_url: str = field(default_factory=lambda: os.environ.get("WEBULL_BASE_URL", ""))

    def is_configured(self) -> bool:
        return bool(self.app_key and self.app_secret and self.account_id)


@dataclass(frozen=True)
class MassiveCredentials:
    api_key: str = field(default_factory=lambda: os.environ.get("MASSIVE_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("MASSIVE_BASE_URL", ""))

    def is_configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class FMPCredentials:
    """Financial Modeling Prep -- the currently active free-float data source
    (see data/float_providers/fmp.py). Verified live against FMP's `stable`
    API namespace; their older /api/v3 and /api/v4 endpoints are being
    retired, so base_url defaults to /stable rather than the legacy paths."""

    api_key: str = field(default_factory=lambda: os.environ.get("FMP_API_KEY", ""))
    base_url: str = field(
        default_factory=lambda: os.environ.get("FMP_BASE_URL", "https://financialmodelingprep.com/stable")
    )

    def is_configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://webull_bot:webull_bot@localhost:5432/webull_bot"
        )
    )


@dataclass(frozen=True)
class Settings:
    trading_mode: TradingMode = field(
        default_factory=lambda: TradingMode(os.environ.get("TRADING_MODE", "sandbox").lower())
    )
    environment: Environment = field(
        default_factory=lambda: Environment(os.environ.get("APP_ENV", "dev").lower())
    )

    # --- Live trading kill-switch. All three must be true/matching. ---
    live_trading_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_TRADING_ENABLED", False)
    )
    live_trading_confirmation: str = field(
        default_factory=lambda: os.environ.get("LIVE_TRADING_CONFIRMATION", "")
    )

    webull: WebullCredentials = field(default_factory=WebullCredentials)
    massive: MassiveCredentials = field(default_factory=MassiveCredentials)
    fmp: FMPCredentials = field(default_factory=FMPCredentials)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)

    float_cache_dir: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "float_cache"
    )
    float_cache_ttl_hours: int = field(default_factory=lambda: _env_int("FLOAT_CACHE_TTL_HOURS", 24))

    def is_live_trading_authorized(self) -> bool:
        """
        The single gate every code path must consult before an order can
        reach a real brokerage account. All three conditions are required:
        explicit mode selection, explicit enable flag, and a typed
        confirmation phrase (so a stray `LIVE_TRADING_ENABLED=true` in an
        env file alone can never flip this on).
        """
        return (
            self.trading_mode == TradingMode.LIVE
            and self.live_trading_enabled
            and self.live_trading_confirmation == _LIVE_CONFIRMATION_PHRASE
        )

    def require_non_live_or_authorized(self) -> None:
        if self.trading_mode == TradingMode.LIVE and not self.is_live_trading_authorized():
            raise RuntimeError(
                "Refusing to start in LIVE mode: live trading is not authorized. "
                "Set TRADING_MODE, LIVE_TRADING_ENABLED=true, and "
                "LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING_RISK explicitly, "
                "and only after the system has been validated in sandbox/paper mode."
            )


_settings: Settings | None = None


def get_settings(force_reload: bool = False) -> Settings:
    global _settings
    if _settings is None or force_reload:
        _settings = Settings()
    return _settings
