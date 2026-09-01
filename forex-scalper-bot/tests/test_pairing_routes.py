import pytest
from fastapi.testclient import TestClient

from fx_bot.brokers.local_connector.pairing.app import create_pairing_app
from fx_bot.brokers.local_connector.pairing.store import PairingStore
from fx_bot.brokers.local_connector.pairing.tokens import hash_token
from fx_bot.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(pairing_db_path=str(tmp_path / "pairing.db"), pairing_code_ttl_seconds=600)
    app = create_pairing_app(settings=settings)
    return TestClient(app)


def test_issue_pairing_code_returns_201_with_code_and_expiry(client):
    response = client.post("/connector/pairing-codes")

    assert response.status_code == 201
    body = response.json()
    assert "-" in body["code"]
    assert "expires_at" in body


def test_pair_with_valid_code_returns_200_with_token_and_account_id(client):
    code = client.post("/connector/pairing-codes").json()["code"]

    response = client.post("/connector/pair", json={"code": code})

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "default-account"
    assert len(body["token"]) >= 32


def test_pair_with_unknown_code_returns_404(client):
    response = client.post("/connector/pair", json={"code": "BOGUS-CODE"})

    assert response.status_code == 404


def test_pair_with_expired_code_returns_400(tmp_path):
    settings = Settings(pairing_db_path=str(tmp_path / "pairing.db"), pairing_code_ttl_seconds=0.0)
    app = create_pairing_app(settings=settings)
    client = TestClient(app)
    code = client.post("/connector/pairing-codes").json()["code"]

    import time
    time.sleep(0.05)
    response = client.post("/connector/pair", json={"code": code})

    assert response.status_code == 400


def test_pair_with_already_used_code_returns_409(client):
    code = client.post("/connector/pairing-codes").json()["code"]
    client.post("/connector/pair", json={"code": code})

    response = client.post("/connector/pair", json={"code": code})

    assert response.status_code == 409


def test_repairing_invalidates_previous_token(tmp_path, client):
    settings = Settings(pairing_db_path=str(tmp_path / "pairing.db"), pairing_code_ttl_seconds=600)
    store = PairingStore(settings.pairing_db_path)

    first_code = client.post("/connector/pairing-codes").json()["code"]
    first_token = client.post("/connector/pair", json={"code": first_code}).json()["token"]

    second_code = client.post("/connector/pairing-codes").json()["code"]
    client.post("/connector/pair", json={"code": second_code})

    assert store.lookup_account_id_by_token_hash(hash_token(first_token)) is None
