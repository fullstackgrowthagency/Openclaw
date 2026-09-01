"""
Central configuration. Mirrors webull-momentum-bot/src/webull_bot/config.py's
shape (env-driven, frozen dataclasses, .env auto-loaded from project root) --
the live-trading safety gate (TradingMode.LIVE authorization) is added in
Phase 1, not here; Phase 0 only needs enough Settings for the domain model
and interfaces to exist.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

# parents[2] from src/fx_bot/config.py is the forex-scalper-bot project root
# itself (fx_bot -> src -> project root) -- NOT the outer Openclaw monorepo.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Loads forex-scalper-bot/.env if present; never overrides a variable
# already set in the real environment, and is a no-op with no .env file.
load_dotenv(PROJECT_ROOT / ".env")


class TradingMode(str, Enum):
    DEMO = "demo"  # a demo/practice account, fake money
    LIVE = "live"  # a real account, real money


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


@dataclass(frozen=True)
class Settings:
    trading_mode: TradingMode = field(
        default_factory=lambda: TradingMode(os.environ.get("TRADING_MODE", "demo").lower())
    )
    environment: Environment = field(
        default_factory=lambda: Environment(os.environ.get("APP_ENV", "dev").lower())
    )


_settings: Settings | None = None


def get_settings(force_reload: bool = False) -> Settings:
    global _settings
    if _settings is None or force_reload:
        _settings = Settings()
    return _settings
