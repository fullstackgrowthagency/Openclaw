"""
Connector settings. Mirrors fx_bot.config.Settings's shape (frozen
dataclass, env-driven, .env auto-loaded) for consistency across the
monorepo -- deliberately a developer/CI-friendly stopgap, NOT the
end-user experience. A real Windows distribution will need a GUI
settings screen or installer-written config: a non-technical trader
won't hand-edit a .env file, and MT5 credentials sitting in plaintext
env vars is worse than the JSON file pairing.py already isolates for the
bearer token specifically. Designing that real config UX is out of scope
for Phase 5d.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _env_int(name: str) -> Optional[int]:
    val = os.environ.get(name)
    return int(val) if val else None


def _default_token_file_path() -> Path:
    configured = os.environ.get("TOKEN_FILE_PATH", "")
    if configured:
        return Path(configured)
    return Path.home() / ".fx_connector" / "credentials.json"


@dataclass(frozen=True)
class ConnectorSettings:
    relay_ws_url: str = field(
        default_factory=lambda: os.environ.get("RELAY_WS_URL", "ws://localhost:8765/connector")
    )
    pairing_base_url: str = field(
        default_factory=lambda: os.environ.get("PAIRING_BASE_URL", "http://localhost:8000")
    )
    token_file_path: Path = field(default_factory=_default_token_file_path)

    mt5_login: Optional[int] = field(default_factory=lambda: _env_int("MT5_LOGIN"))
    mt5_password: Optional[str] = field(default_factory=lambda: os.environ.get("MT5_PASSWORD") or None)
    mt5_server: Optional[str] = field(default_factory=lambda: os.environ.get("MT5_SERVER") or None)
    mt5_path: Optional[str] = field(default_factory=lambda: os.environ.get("MT5_TERMINAL_PATH") or None)
    symbol_suffix: str = field(default_factory=lambda: os.environ.get("MT5_SYMBOL_SUFFIX", ""))

    reconnect_base_seconds: float = 1.0
    reconnect_cap_seconds: float = 60.0
    request_timeout_seconds: float = 5.0
    quote_poll_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("QUOTE_POLL_INTERVAL_SECONDS", "1.0"))
    )
    heartbeat_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "15.0"))
    )


_settings: Optional[ConnectorSettings] = None


def get_settings(force_reload: bool = False) -> ConnectorSettings:
    global _settings
    if _settings is None or force_reload:
        _settings = ConnectorSettings()
    return _settings
