"""
Central configuration and safety gate. Mirrors webull-momentum-bot/src/
webull_bot/config.py's shape (env-driven, frozen dataclasses, .env
auto-loaded from project root) -- including its live-trading kill switch,
built here in Phase 1 (deliberately early: the user chose to support both
demo and live accounts from early on, not gate live behind a late phase --
see the approved plan), mirroring how the equities bot itself treated this
as literally its first-ever task.

This is only the DEPLOYMENT-WIDE half of the gate. A per-user opt-in toggle
(mirroring webull_bot's BrokerCredential.live_trading_enabled) is added
later, once there's a user/auth system for it to belong to (multi-tenant
hardening phase) -- exactly the same two-phase order the equities bot
built these in.
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

# The exact phrase an operator must set in LIVE_TRADING_CONFIRMATION to
# ever route an order to a live account. Changing this string is itself a
# deliberate, auditable code change -- it must never be read from a
# user-editable config file. Same phrase/reasoning as webull_bot's own
# gate, kept identical rather than inventing a different one for no reason.
_LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_LIVE_TRADING_RISK"


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

    # --- Live trading kill-switch. All three must be true/matching. ---
    live_trading_enabled: bool = field(
        default_factory=lambda: _env_bool("LIVE_TRADING_ENABLED", False)
    )
    live_trading_confirmation: str = field(
        default_factory=lambda: os.environ.get("LIVE_TRADING_CONFIRMATION", "")
    )

    def is_live_trading_authorized(self) -> bool:
        """The single gate every code path must consult before an order
        can reach a real account. All three conditions are required:
        explicit mode selection, explicit enable flag, and a typed
        confirmation phrase -- so a stray LIVE_TRADING_ENABLED=true in an
        env file alone can never flip this on."""
        return (
            self.trading_mode == TradingMode.LIVE
            and self.live_trading_enabled
            and self.live_trading_confirmation == _LIVE_CONFIRMATION_PHRASE
        )

    def require_non_live_or_authorized(self) -> None:
        if self.trading_mode == TradingMode.LIVE and not self.is_live_trading_authorized():
            raise RuntimeError(
                "Refusing to start in LIVE mode: live trading is not authorized. "
                "Set TRADING_MODE=live, LIVE_TRADING_ENABLED=true, and "
                "LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING_RISK explicitly, "
                "and only after the system has been validated extensively in demo mode."
            )


_settings: Settings | None = None


def get_settings(force_reload: bool = False) -> Settings:
    global _settings
    if _settings is None or force_reload:
        _settings = Settings()
    return _settings
