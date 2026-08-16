"""Quiet hours as experienced through the app, not just the arithmetic."""

from __future__ import annotations

from sqlalchemy import select

from app.models import OPEN, Offer, Request
from tests.conftest import SUBS, TEACHER


def test_night_request_holds_until_morning(staffed):
    app = staffed
    app.at("2026-03-09 22:00")  # Monday, 10pm
    app.sms(TEACHER, "SUB 3/15")

    assert "8am" in app.last(TEACHER)
    assert app.texted(*SUBS[:6]) == set()

    app.at("2026-03-10 07:59")
    app.tick()
    assert app.texted(*SUBS[:6]) == set()

    app.at("2026-03-10 08:00")
    app.tick()
    assert len(app.texted(*SUBS[:6])) == 3


def test_the_request_still_exists_overnight(staffed):
    app = staffed
    app.at("2026-03-09 22:00")
    app.sms(TEACHER, "SUB 3/15")
    with app.session_factory() as session:
        request = session.scalar(select(Request))
        assert request is not None and request.status == OPEN


def test_replies_are_never_delayed(app):
    """Direct answers go out at once; only proactive messages are gated."""
    app.at("2026-03-09 23:30")
    app.sms("+15557770000", "hello")
    assert app.count("+15557770000") == 1
    assert "consent" in app.last("+15557770000").lower()


def test_saturday_night_request_is_told_it_may_fail(staffed):
    app = staffed
    app.at("2026-03-14 22:00")  # Saturday night, service is tomorrow
    app.sms(TEACHER, "SUB 3/15")

    reply = app.last(TEACHER)
    assert "may not be enough time" in reply
    assert "calling someone directly" in reply
    assert app.texted(*SUBS[:6]) == set()


def test_late_request_still_tries_in_the_morning(staffed):
    app = staffed
    app.at("2026-03-14 22:00")
    app.sms(TEACHER, "SUB 3/15")
    app.at("2026-03-15 08:00")
    app.tick()
    # Day-of tier asks everyone at once rather than in threes.
    assert len(app.texted(*SUBS[:6])) == 6


def test_offer_ttl_does_not_expire_overnight(staffed):
    app = staffed
    app.at("2026-03-10 20:50")
    app.sms(TEACHER, "SUB 3/22")
    assert len(app.texted(*SUBS[:6])) == 3

    # 6h of wall-clock later it is 2:50am -- the offers must still stand.
    app.at("2026-03-11 02:50")
    app.tick()
    with app.session_factory() as session:
        assert all(o.status == "pending" for o in session.scalars(select(Offer)).all())


def test_no_offer_is_ever_sent_during_quiet_hours(staffed):
    """Swept across a fortnight of ticks rather than a single case."""
    app = staffed
    app.at("2026-03-09 22:00")
    app.sms(TEACHER, "SUB 3/29")

    for day in range(9, 23):
        for hour in range(0, 24):
            app.at(f"2026-03-{day:02d} {hour:02d}:00")
            app.tick()

    for message in app.gateway.sent:
        if message.to == TEACHER:
            continue
        assert not app.filler.quiet.is_quiet(message.at), (
            f"sent during quiet hours: {message}"
        )


def test_saturday_last_call_alerts_admins_for_stale_requests(staffed):
    app = staffed
    admin = SUBS[9]
    app.enroll(admin, "Ada", admin=True)

    app.at("2026-03-10 09:00")  # Tuesday
    app.sms(TEACHER, "SUB 3/15")
    for phone in list(app.texted(*SUBS[:6])):
        app.sms(phone, "no")

    app.clear()
    app.at("2026-03-14 20:00")  # Saturday 8pm
    app.tick()

    assert any("make some calls tonight" in m for m in app.outbox(admin))


def test_saturday_night_request_does_not_page_admins(staffed):
    """It was already disclosed at request time; nothing new to report."""
    app = staffed
    admin = SUBS[9]
    app.enroll(admin, "Ada", admin=True)

    app.at("2026-03-14 21:30")
    app.sms(TEACHER, "SUB 3/15")
    app.clear()

    app.at("2026-03-14 22:00")
    app.tick()

    assert app.outbox(admin) == []
