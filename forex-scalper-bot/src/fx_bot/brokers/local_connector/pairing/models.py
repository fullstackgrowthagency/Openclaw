"""
Pydantic request/response models for the pairing HTTP routes.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: datetime


class PairRequest(BaseModel):
    code: str


class PairResponse(BaseModel):
    token: str
    account_id: str
