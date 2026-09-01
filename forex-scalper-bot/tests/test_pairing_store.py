import time
from datetime import datetime

import pytest

from fx_bot.brokers.local_connector.pairing.exceptions import (
    PairingCodeAlreadyUsed,
    PairingCodeExpired,
    PairingCodeNotFound,
)
from fx_bot.brokers.local_connector.pairing.store import PairingStore
from fx_bot.brokers.local_connector.pairing.tokens import generate_token, hash_token


@pytest.fixture
def store(tmp_path):
    return PairingStore(tmp_path / "pairing.db")


def test_create_pairing_code_returns_unused_code_with_future_expiry(store):
    issued = store.create_pairing_code("acct-1", ttl_seconds=600)

    assert "-" in issued.code
    assert issued.account_id == "acct-1"
    assert isinstance(issued.expires_at, datetime)
    assert issued.expires_at > datetime.utcnow()


def test_consume_pairing_code_returns_account_id_and_marks_used(store):
    issued = store.create_pairing_code("acct-1", ttl_seconds=600)

    account_id = store.consume_pairing_code(issued.code)

    assert account_id == "acct-1"


def test_consume_unknown_code_raises_not_found(store):
    with pytest.raises(PairingCodeNotFound):
        store.consume_pairing_code("BOGUS-CODE")


def test_consume_expired_code_raises_expired(store):
    issued = store.create_pairing_code("acct-1", ttl_seconds=0.0)
    time.sleep(0.05)

    with pytest.raises(PairingCodeExpired):
        store.consume_pairing_code(issued.code)


def test_consume_already_used_code_raises_already_used(store):
    issued = store.create_pairing_code("acct-1", ttl_seconds=600)
    store.consume_pairing_code(issued.code)

    with pytest.raises(PairingCodeAlreadyUsed):
        store.consume_pairing_code(issued.code)


def test_store_token_then_lookup_by_hash_returns_account_id(store):
    token = generate_token()
    store.store_token("acct-1", hash_token(token))

    assert store.lookup_account_id_by_token_hash(hash_token(token)) == "acct-1"


def test_lookup_unknown_token_hash_returns_none(store):
    assert store.lookup_account_id_by_token_hash(hash_token("never-issued")) is None


def test_storing_new_token_revokes_previous_token_for_same_account(store):
    old_token = generate_token()
    store.store_token("acct-1", hash_token(old_token))

    new_token = generate_token()
    store.store_token("acct-1", hash_token(new_token))

    assert store.lookup_account_id_by_token_hash(hash_token(old_token)) is None
    assert store.lookup_account_id_by_token_hash(hash_token(new_token)) == "acct-1"


def test_revoking_one_accounts_token_does_not_affect_another_accounts(store):
    token_a = generate_token()
    store.store_token("acct-a", hash_token(token_a))
    token_b = generate_token()
    store.store_token("acct-b", hash_token(token_b))

    store.store_token("acct-a", hash_token(generate_token()))  # rotate acct-a again

    assert store.lookup_account_id_by_token_hash(hash_token(token_b)) == "acct-b"


def test_generate_token_has_high_entropy_and_hash_is_deterministic():
    token_a, token_b = generate_token(), generate_token()

    assert token_a != token_b
    assert len(token_a) >= 32  # base64url of 32 raw bytes -- comfortably high-entropy
    assert hash_token(token_a) == hash_token(token_a)
    assert hash_token(token_a) != hash_token(token_b)
