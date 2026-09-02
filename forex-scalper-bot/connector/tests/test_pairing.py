import os
import stat

import httpx
import pytest

from fx_connector.pairing import PairingCredentials, PairingError, load_credentials, pair, save_credentials


def _client_with_response(status_code: int, json_body: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pair_success_returns_credentials():
    client = _client_with_response(200, {"token": "abc123", "account_id": "default-account"})

    creds = pair("http://cloud", "CODE-1234", client=client)

    assert creds == PairingCredentials(token="abc123", account_id="default-account")


def test_pair_404_raises_pairing_error():
    client = _client_with_response(404, {"detail": "Unknown pairing code."})
    with pytest.raises(PairingError):
        pair("http://cloud", "BOGUS-CODE", client=client)


def test_pair_400_expired_raises_pairing_error():
    client = _client_with_response(400, {"detail": "Pairing code has expired."})
    with pytest.raises(PairingError):
        pair("http://cloud", "CODE-1234", client=client)


def test_pair_409_already_used_raises_pairing_error():
    client = _client_with_response(409, {"detail": "Pairing code has already been used."})
    with pytest.raises(PairingError):
        pair("http://cloud", "CODE-1234", client=client)


def test_save_and_load_credentials_round_trip(tmp_path):
    path = tmp_path / "creds.json"
    creds = PairingCredentials(token="tok", account_id="acct")

    save_credentials(path, creds)

    assert load_credentials(path) == creds


def test_load_credentials_returns_none_when_missing(tmp_path):
    assert load_credentials(tmp_path / "nope.json") is None


@pytest.mark.skipif(os.name != "posix", reason="chmod 0600 is a POSIX-only behavior")
def test_save_credentials_chmods_0600_on_posix(tmp_path):
    path = tmp_path / "creds.json"

    save_credentials(path, PairingCredentials(token="t", account_id="a"))

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
