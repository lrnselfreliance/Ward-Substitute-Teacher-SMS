"""Polling mode: running with no inbound network access at all."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.models import ProcessedMessage
from app.poller import FakeInbox, Poller
from tests.conftest import SUBS, TEACHER, TZ


@pytest.fixture
def polled(staffed):
    """A fully staffed app driven by polling instead of webhooks."""
    app = staffed
    app.inbox = FakeInbox()
    app.poller = Poller(
        app.inbox,
        app.router,
        app.session_factory,
        app.clock,
        lookback=timedelta(minutes=10),
    )
    # First poll absorbs history; start from a clean slate.
    app.poller.poll()
    return app


def test_polled_message_is_handled(polled):
    polled.inbox.deliver(TEACHER, "SUB 3/15", polled.clock.now())
    assert polled.poller.poll() == 1
    assert "sub needed" in polled.last(TEACHER)
    assert len(polled.texted(*SUBS[:6])) == 3


def test_overlapping_windows_do_not_double_handle(polled):
    """The lookback deliberately re-fetches; MessageSid dedupe absorbs it."""
    polled.inbox.deliver(TEACHER, "SUB 3/15", polled.clock.now())
    assert polled.poller.poll() == 1

    polled.clear()
    polled.advance(seconds=20)
    assert polled.poller.poll() == 0  # same message re-fetched, ignored
    assert polled.gateway.sent == []


def test_a_full_conversation_over_polling(polled):
    now = polled.clock.now()
    polled.inbox.deliver(TEACHER, "SUB 3/15", now)
    polled.poller.poll()
    asked = sorted(polled.texted(*SUBS[:6]))
    polled.clear()

    polled.advance(minutes=1)
    polled.inbox.deliver(asked[0], "yes", polled.clock.now())
    polled.poller.poll()

    assert "You're subbing" in polled.last(asked[0])
    assert asked[0] in polled.last(TEACHER)


def test_messages_are_handled_in_order_sent(polled):
    base = polled.clock.now()
    polled.inbox.deliver("+15557770000", "Jane", base + timedelta(seconds=3))
    polled.inbox.deliver("+15557770000", "hi", base + timedelta(seconds=1))
    polled.inbox.deliver("+15557770000", "yes", base + timedelta(seconds=2))

    polled.advance(seconds=5)
    polled.poller.poll()

    replies = polled.outbox("+15557770000")
    assert "consent" in replies[0].lower()
    assert "What's your name" in replies[1]
    assert "TEACHER or a SUBSTITUTE" in replies[2]


def test_bootstrap_ignores_history(staffed):
    """Switching a live number to polling must not replay old messages."""
    app = staffed
    inbox = FakeInbox()
    for body in ("hi", "SUB 3/15", "yes"):
        inbox.deliver(TEACHER, body, app.clock.now())

    poller = Poller(inbox, app.router, app.session_factory, app.clock)
    assert poller.poll() == 0
    assert app.gateway.sent == []

    with app.session_factory() as session:
        assert session.query(ProcessedMessage).count() == 3

    # Anything arriving afterwards is handled normally.
    app.advance(minutes=1)
    inbox.deliver(TEACHER, "SUB 3/15", app.clock.now())
    assert poller.poll() == 1


def test_fetch_failure_does_not_crash_the_loop(polled):
    class Broken:
        def fetch(self, since):
            raise ConnectionError("network down")

    polled.poller.inbox = Broken()
    assert polled.poller.poll() == 0  # logged, not raised


def test_poll_mode_exposes_no_webhook():
    """Nothing to secure, because there is no endpoint."""
    api = create_app(
        Config(db_path=":memory:", timezone=TZ, inbound_mode="poll"),
        run_scheduler=False,
    )
    with TestClient(api) as client:
        assert client.get("/health").json()["mode"] == "poll"
        assert client.post("/sms", data={"From": "+1555", "Body": "hi"}).status_code == 404


def test_webhook_mode_still_serves_the_endpoint():
    api = create_app(
        Config(db_path=":memory:", timezone=TZ, validate_signature=False),
        run_scheduler=False,
    )
    with TestClient(api) as client:
        assert client.post(
            "/sms", data={"From": "+15551110001", "Body": "hi"}
        ).status_code == 204
