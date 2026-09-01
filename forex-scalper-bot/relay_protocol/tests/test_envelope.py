from datetime import datetime, timezone

from relay_protocol.envelope import Envelope, EnvelopeKind, WIRE_PROTOCOL_VERSION
from relay_protocol.methods import EventMethod, RequestMethod


def _round_trip(envelope: Envelope) -> Envelope:
    return Envelope.from_wire(envelope.to_wire())


def test_request_round_trips_with_a_generated_id():
    envelope = Envelope.make_request(RequestMethod.GET_SNAPSHOT, {"symbol": "EUR/USD"})

    restored = _round_trip(envelope)

    assert restored.kind == EnvelopeKind.REQUEST
    assert restored.id == envelope.id
    assert restored.id is not None
    assert restored.method == RequestMethod.GET_SNAPSHOT
    assert restored.payload == {"symbol": "EUR/USD"}
    assert restored.v == WIRE_PROTOCOL_VERSION


def test_two_requests_get_different_ids():
    first = Envelope.make_request(RequestMethod.GET_ACCOUNT_EQUITY)
    second = Envelope.make_request(RequestMethod.GET_ACCOUNT_EQUITY)

    assert first.id != second.id


def test_response_echoes_the_request_id():
    request = Envelope.make_request(RequestMethod.GET_ACCOUNT_EQUITY)

    response = Envelope.make_response(request.id, RequestMethod.GET_ACCOUNT_EQUITY, {"equity": 10_000.0})
    restored = _round_trip(response)

    assert restored.kind == EnvelopeKind.RESPONSE
    assert restored.id == request.id
    assert restored.payload == {"equity": 10_000.0}


def test_error_carries_the_error_type_and_message():
    request = Envelope.make_request(RequestMethod.PLACE_ORDER)

    error = Envelope.make_error(
        request.id, RequestMethod.PLACE_ORDER,
        error_type="BrokerRejectedError", message="Insufficient margin.",
    )
    restored = _round_trip(error)

    assert restored.kind == EnvelopeKind.ERROR
    assert restored.id == request.id
    assert restored.payload == {"error_type": "BrokerRejectedError", "message": "Insufficient margin."}


def test_event_has_no_id_and_round_trips_that_absence():
    event = Envelope.make_event(EventMethod.QUOTE, {"symbol": "EUR/USD", "bid": 1.1000, "ask": 1.1002})

    restored = _round_trip(event)

    assert restored.kind == EnvelopeKind.EVENT
    assert restored.id is None
    assert restored.method == EventMethod.QUOTE


def test_auth_frame_carries_token_and_account_id():
    auth = Envelope.make_auth(token="secret-token", account_id="acct-1")

    restored = _round_trip(auth)

    assert restored.kind == EnvelopeKind.AUTH
    assert restored.id is not None
    assert restored.payload == {"token": "secret-token", "account_id": "acct-1"}


def test_sent_at_survives_the_round_trip_as_utc():
    event = Envelope.make_event(EventMethod.HEARTBEAT, {"mt5_connected": True})

    restored = _round_trip(event)

    assert restored.sent_at.tzinfo is not None
    # Pydantic parses ISO8601 back to a real datetime -- prove it's the
    # same instant, not just the same string, by comparing as UTC.
    assert restored.sent_at.astimezone(timezone.utc) == event.sent_at.astimezone(timezone.utc)


def test_default_sent_at_is_recent_when_not_explicitly_set():
    before = datetime.now(timezone.utc)
    envelope = Envelope.make_event(EventMethod.HEARTBEAT)
    after = datetime.now(timezone.utc)

    assert before <= envelope.sent_at <= after


def test_wire_bytes_are_plain_json_a_non_python_client_can_parse():
    import json

    envelope = Envelope.make_request(RequestMethod.GET_BARS, {"symbol": "EUR/USD", "interval": "1m", "lookback": 500})

    parsed = json.loads(envelope.to_wire())

    assert parsed["kind"] == "request"
    assert parsed["method"] == "get_bars"
    assert parsed["payload"]["lookback"] == 500
    assert isinstance(parsed["sent_at"], str)
