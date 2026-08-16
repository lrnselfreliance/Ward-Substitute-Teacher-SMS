"""HTTP layer.

These exist because a missing python-multipart only showed up when the
container started -- the unit tests all drove the Router directly and never
touched FastAPI. Anything that can break at import or request-parsing time
needs a test that actually issues a request.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app

TZ = ZoneInfo("America/Denver")


def _config(**overrides) -> Config:
    base = dict(db_path=":memory:", timezone=TZ, validate_signature=False)
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def client():
    api = create_app(_config(), run_scheduler=False)
    with TestClient(api) as c:
        c.api = api
        yield c


def test_health(client):
    assert client.get("/health").json() == {"ok": True, "mode": "webhook"}


def test_inbound_form_post_is_accepted(client):
    response = client.post(
        "/sms",
        data={"From": "+15551110001", "Body": "hi", "MessageSid": "SM1"},
    )
    assert response.status_code == 204
    sent = client.api.state.gateway.sent
    assert len(sent) == 1
    assert "consent" in sent[0].body.lower()


def test_missing_from_is_a_422_not_a_500(client):
    assert client.post("/sms", data={"Body": "hi"}).status_code == 422


def test_body_may_be_empty(client):
    assert client.post("/sms", data={"From": "+15551110002"}).status_code == 204


def test_unsigned_request_is_rejected_when_validation_is_on():
    """The single most important security check in the project."""
    api = create_app(
        _config(validate_signature=True, twilio_auth_token="fake-token"),
        run_scheduler=False,
    )
    with TestClient(api) as client:
        response = client.post(
            "/sms", data={"From": "+15551110001", "Body": "hi"}
        )
        assert response.status_code == 403
        assert api.state.gateway.sent == []


def test_validation_fails_closed_without_a_token():
    """No token configured must mean reject, never allow."""
    api = create_app(_config(validate_signature=True), run_scheduler=False)
    with TestClient(api) as client:
        response = client.post("/sms", data={"From": "+1555", "Body": "hi"})
        assert response.status_code == 403


def test_signed_request_is_accepted():
    from twilio.request_validator import RequestValidator

    token = "fake-token"
    url = "https://sub.example.org/sms"
    api = create_app(
        _config(validate_signature=True, twilio_auth_token=token, public_url=url),
        run_scheduler=False,
    )
    payload = {"From": "+15551110001", "Body": "hi", "MessageSid": "SM9"}
    signature = RequestValidator(token).compute_signature(url, payload)

    with TestClient(api) as client:
        response = client.post(
            "/sms", data=payload, headers={"X-Twilio-Signature": signature}
        )
        assert response.status_code == 204
        assert len(api.state.gateway.sent) == 1


def test_retry_of_the_same_sid_is_ignored(client):
    payload = {"From": "+15551110001", "Body": "hi", "MessageSid": "SM-retry"}
    client.post("/sms", data=payload)
    client.post("/sms", data=payload)
    assert len(client.api.state.gateway.sent) == 1
