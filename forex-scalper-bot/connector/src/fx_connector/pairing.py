"""
HTTP pairing client -- exchanges a user-entered pairing code for a
long-lived bearer token via the cloud's POST /connector/pair route (see
the main forex-scalper-bot project's
src/fx_bot/brokers/local_connector/pairing/routes.py for the server side
this calls). The connector never calls POST /connector/pairing-codes
itself -- that's issued by the cloud dashboard for a human to read and
type in here.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx


class PairingError(Exception):
    """Raised for any non-200 response from /connector/pair -- each
    message tells the user to retry with a fresh code, matching the
    server's own 404-unknown/400-expired/409-already-used convention."""


@dataclass(frozen=True)
class PairingCredentials:
    token: str
    account_id: str


def pair(base_url: str, code: str, *, client: Optional[httpx.Client] = None) -> PairingCredentials:
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        response = client.post(f"{base_url}/connector/pair", json={"code": code})
    finally:
        if owns_client:
            client.close()

    if response.status_code == 200:
        body = response.json()
        return PairingCredentials(token=body["token"], account_id=body["account_id"])
    if response.status_code == 404:
        raise PairingError("Unknown pairing code -- double-check it and try again, or request a new one.")
    if response.status_code == 400:
        raise PairingError("That pairing code has expired -- request a new one from the dashboard.")
    if response.status_code == 409:
        raise PairingError("That pairing code has already been used -- request a new one from the dashboard.")
    raise PairingError(f"Pairing failed with unexpected status {response.status_code}: {response.text}")


def save_credentials(path: Path, creds: PairingCredentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": creds.token, "account_id": creds.account_id}))
    if os.name == "posix":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600 -- owner read/write only
    # Windows ACL-based protection is a separate installer-level concern,
    # deliberately not attempted here -- see docs/ARCHITECTURE.md's Phase
    # 5d section in the main forex-scalper-bot project.


def load_credentials(path: Path) -> Optional[PairingCredentials]:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return PairingCredentials(token=data["token"], account_id=data["account_id"])


def prompt_and_pair(base_url: str, path: Path, *, code: Optional[str] = None) -> PairingCredentials:
    """`code=None` interactively prompts the user; a real value exists
    purely for testability."""
    if code is None:
        code = input("Enter the pairing code shown in your dashboard: ").strip()
    creds = pair(base_url, code)
    save_credentials(path, creds)
    return creds
