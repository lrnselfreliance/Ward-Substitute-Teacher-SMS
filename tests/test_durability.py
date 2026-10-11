"""Sends and inbound handling must be durable: a rejected text is not an
ask, and a crashing message is dropped once rather than replayed."""

from __future__ import annotations

from sqlalchemy import select

from app.models import FAILED, PENDING, Offer, ProcessedMessage
from tests.conftest import SUBS, TEACHER


def test_rejected_send_is_not_counted_as_asked(staffed):
    from app.models import UNFILLED, Request

    app = staffed
    app.gateway.failing.update(SUBS[:6])  # every carrier send bounces
    app.sms(TEACHER, "SUB 3/15")

    def statuses():
        with app.session_factory() as session:
            return [o.status for o in session.scalars(select(Offer)).all()]

    assert statuses() == [FAILED] * 3
    assert app.texted(*SUBS[:6]) == set()

    # Nothing is pending, so the batch is not stuck behind the bounces: the
    # next ticks move on to the remaining three, then declare it unfilled.
    app.tick()
    assert statuses() == [FAILED] * 6
    app.advance(seconds=31)
    app.tick()
    with app.session_factory() as session:
        assert session.scalar(select(Request)).status == UNFILLED
    assert "couldn't find a sub" in app.last(TEACHER)

    # Bounced people are eligible again for a different Sunday.
    app.gateway.failing.clear()
    app.clear()
    app.sms(TEACHER, "SUB 3/22")
    assert len(app.texted(*SUBS[:6])) == 3
    assert statuses().count(PENDING) == 3


def test_crashing_message_is_claimed_once_and_not_replayed(staffed, monkeypatch):
    app = staffed
    calls = []

    def boom(session, phone, body):
        calls.append(body)
        raise RuntimeError("bug")

    monkeypatch.setattr(app.router, "_route", boom)
    app.sms(TEACHER, "SUB 3/15", sid="SM-crash")
    assert "didn't understand" in app.last(TEACHER)
    with app.session_factory() as session:
        assert session.get(ProcessedMessage, "SM-crash") is not None

    # A retry (webhook) or re-fetch (poll) of the same sid does nothing.
    app.sms(TEACHER, "SUB 3/15", sid="SM-crash")
    assert calls == ["SUB 3/15"]


def test_crash_path_stays_silent_for_banned_senders(staffed, monkeypatch):
    app = staffed
    app.enroll("+15551110000", "Ada", admin=True, substitute=False)
    app.sms("+15551110000", "ADMIN BAN sub0")
    app.clear()

    def boom(session, phone, body):
        raise RuntimeError("bug")

    monkeypatch.setattr(app.router, "_route", boom)
    app.sms(SUBS[0], "hi", sid="SM-x")
    assert app.count(SUBS[0]) == 0


def test_poll_cursor_only_advances_after_dispatch(staffed, monkeypatch):
    from datetime import timedelta

    from app.poller import FakeInbox, Poller

    app = staffed
    inbox = FakeInbox()
    poller = Poller(inbox, app.router, app.session_factory, app.clock,
                    lookback=timedelta(minutes=10))
    poller.poll()  # bootstrap
    t0 = app.clock.now()

    inbox.deliver(TEACHER, "SUB 3/15", t0 + timedelta(seconds=5))

    # Simulate the process dying mid-dispatch: handle raises out of poll.
    def die(phone, body, sid):
        raise SystemExit("power cut")

    monkeypatch.setattr(app.router, "handle", die)
    app.advance(seconds=10)
    import pytest
    with pytest.raises(SystemExit):
        poller.poll()
    assert poller._last_ok() == t0  # cursor did not move past the lost batch

    monkeypatch.undo()
    app.advance(minutes=30)  # longer than the lookback
    assert poller.poll() == 1  # still found via the old cursor
