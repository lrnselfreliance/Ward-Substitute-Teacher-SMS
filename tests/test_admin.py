"""Admin visibility and the two commands that keep the data honest."""

from __future__ import annotations

from sqlalchemy import select

from app.models import FILLED, Person, Request
from tests.conftest import SUBS, TEACHER

ADMIN = "+15551110000"


def _with_admin(app):
    app.enroll(ADMIN, "Ada", admin=True, substitute=False)
    return app


def test_non_admins_cannot_use_admin_commands(staffed):
    app = staffed
    app.sms(SUBS[0], "ADMIN ROSTER")
    assert "didn't understand" in app.last(SUBS[0])


def test_admin_open_lists_unfilled_work(staffed):
    app = _with_admin(staffed)
    app.sms(TEACHER, "SUB 3/15")
    app.clear()

    app.sms(ADMIN, "ADMIN OPEN")
    reply = app.last(ADMIN)
    assert "3rd grade" in reply
    assert "Tom Teacher" in reply
    assert "open" in reply


def test_admin_request_shows_who_was_asked(staffed):
    app = _with_admin(staffed)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:6]))
    app.sms(asked[0], "no")
    app.clear()

    app.sms(ADMIN, "ADMIN REQUEST 1")
    reply = app.last(ADMIN)
    assert "declined" in reply
    assert "pending" in reply


def test_admin_sunday_reports_fill_status(staffed):
    app = _with_admin(staffed)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:6]))
    app.sms(asked[0], "yes")
    app.clear()

    app.sms(ADMIN, "ADMIN SUNDAY 3/15")
    assert "filled" in app.last(ADMIN)


def test_admin_fill_records_a_hallway_arrangement(staffed):
    """Swaps arranged in person must reach the DB or fairness drifts."""
    app = _with_admin(staffed)
    app.sms(TEACHER, "SUB 3/15")
    app.clear()

    app.sms(ADMIN, "ADMIN FILL 3/15 Sub4")
    assert "Recorded" in app.last(ADMIN)

    with app.session_factory() as session:
        request = session.scalar(select(Request))
        assert request.status == FILLED
        assert request.filled_by.name == "Sub4"

    # And it counts against them in the ranking from now on.
    app.clear()
    app.sms(TEACHER, "SUB 3/22")
    order = [m.to for m in app.gateway.sent if m.to in SUBS[:6]]
    assert SUBS[4] not in order[:3]


def test_admin_move_preserves_history(staffed):
    app = _with_admin(staffed)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:6]))[0]
    app.sms(asked, "yes")
    app.clear()

    new_number = "+15558887777"
    app.sms(ADMIN, f"ADMIN MOVE {asked} {new_number}")
    assert "History kept" in app.last(ADMIN)

    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == new_number))
        assert person is not None
        served = session.scalars(
            select(Request).where(Request.filled_by_id == person.id)
        ).all()
        assert len(served) == 1  # the fill followed them to the new number


def test_admin_broadcast_refused_during_quiet_hours(staffed):
    app = _with_admin(staffed)
    app.at("2026-03-09 22:00")
    app.sms(ADMIN, "ADMIN BROADCAST Potluck Sunday!")
    assert "8am-9pm" in app.last(ADMIN)
    assert app.count(SUBS[0]) == 0


def test_admin_told_when_a_request_cannot_be_filled(app):
    _with_admin(app)
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll(SUBS[0], "Sam")

    app.sms(TEACHER, "SUB 3/15")
    app.sms(SUBS[0], "no")

    assert any("UNFILLED" in m for m in app.outbox(ADMIN))
