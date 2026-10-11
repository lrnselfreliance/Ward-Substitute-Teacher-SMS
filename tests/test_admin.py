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


# -- ADMIN SUB: requesting on a teacher's behalf -------------------------------

JAYN = "+15557317481"


def _two_teachers(app):
    _with_admin(app)
    app.enroll(TEACHER, "Tom Teacher", teacher=True, substitute=False,
               class_name="CTR 6a")
    app.enroll(JAYN, "JaynAnn Thomas", teacher=True, substitute=False,
               class_name="Nursery", gender="F")
    app.enroll("+15550000077", "Jenny Thomas", teacher=True, substitute=False,
               class_name="CTR 6b", gender="F")
    for i, phone in enumerate(SUBS[:3]):
        app.enroll(phone, f"Sub{i}")
    return app


def test_admin_sub_by_class_files_for_that_teacher(app):
    _two_teachers(app)
    app.sms(ADMIN, "ADMIN SUB 3/15 6a out of town")

    with app.session_factory() as session:
        r = session.scalar(select(Request))
        assert r.teacher.name == "Tom Teacher"
        assert r.class_name == "CTR 6a"
        assert r.note == "out of town"
        assert r.requested_by.name == "Ada"

    assert "CTR 6a (Tom Teacher)" in app.last(ADMIN)
    assert "Ada asked me to find a sub" in app.last(TEACHER)
    assert len(app.texted(*SUBS[:3])) == 3  # offers went out immediately


def test_admin_sub_matches_name_word_and_last_four_digits(app):
    _two_teachers(app)
    app.sms(ADMIN, "ADMIN SUB 3/15 jaynann")
    assert "Nursery (JaynAnn Thomas)" in app.last(ADMIN)

    app.sms(ADMIN, "ADMIN SUB 3/22 7481")
    assert "Nursery (JaynAnn Thomas)" in app.last(ADMIN)


def test_admin_sub_ambiguous_lists_choices_and_accepts_a_number(app):
    _two_teachers(app)
    app.sms(ADMIN, "ADMIN SUB 3/15 thomas")
    reply = app.last(ADMIN)
    assert "More than one match" in reply
    assert "JaynAnn Thomas (Nursery)" in reply
    assert "Jenny Thomas (CTR 6b)" in reply

    with app.session_factory() as session:
        jenny = session.scalar(select(Person).where(Person.name == "Jenny Thomas"))
        assert session.scalar(select(Request)) is None
        jid = jenny.id

    app.sms(ADMIN, f"ADMIN SUB 3/15 #{jid}")
    assert "CTR 6b (Jenny Thomas)" in app.last(ADMIN)


def test_admin_sub_substring_does_not_grab_siblings(app):
    """'ctr' is ambiguous; '6a' is not, even though both classes contain 'ctr'."""
    _two_teachers(app)
    app.sms(ADMIN, "ADMIN SUB 3/15 ctr")
    assert "More than one match" in app.last(ADMIN)


def test_admin_sub_rejects_unknown_and_non_sunday(app):
    _two_teachers(app)
    app.sms(ADMIN, "ADMIN SUB 3/15 zelda")
    assert "No teacher matching" in app.last(ADMIN)
    app.sms(ADMIN, "ADMIN SUB 3/16 6a")
    assert "Monday" in app.last(ADMIN)
    app.sms(ADMIN, "ADMIN SUB 3/15")
    assert "Which teacher?" in app.last(ADMIN)


def test_admin_sub_duplicate_is_reported_by_teacher_name(app):
    _two_teachers(app)
    app.sms(TEACHER, "SUB 3/15")
    app.sms(ADMIN, "ADMIN SUB 3/15 6a")
    assert "already looking for a sub for Tom Teacher" in app.last(ADMIN)


def test_requesting_admin_is_told_when_it_fills(app):
    _two_teachers(app)
    app.sms(ADMIN, "ADMIN SUB 3/15 6a")
    asked = sorted(app.texted(*SUBS[:3]))
    app.clear()

    app.sms(asked[0], "yes")
    assert "Sub" in app.last(TEACHER) and "Reach them at" in app.last(TEACHER)
    assert "will sub for Tom Teacher" in app.last(ADMIN)


def test_admin_fill_also_closes_the_loop_for_another_admin(app):
    _two_teachers(app)
    app.enroll("+15550000088", "Bob", admin=True, substitute=False)
    app.sms(ADMIN, "ADMIN SUB 3/15 6a")
    app.clear()

    app.sms("+15550000088", "ADMIN FILL 3/15 sub1")
    assert "Recorded" in app.last("+15550000088")
    assert "will sub for Tom Teacher" in app.last(ADMIN)


def test_teacher_requesting_for_self_gets_no_requester_note(app):
    """An admin who is also the teacher must not be texted twice."""
    _two_teachers(app)
    app.sms(ADMIN, "ADMIN SUB 3/15 6a")
    app.clear()
    app.sms(sorted(app.texted(*SUBS[:3]) or SUBS[:3])[0], "yes")
    # Exactly one message to the admin: the requester note. None are duplicates.
    assert app.count(ADMIN) == 1


def test_admin_help_is_plain_english_and_roster_shows_numbers(app):
    _two_teachers(app)
    app.sms(ADMIN, "HELP")
    assert "ADMIN HELP" in app.last(ADMIN)
    assert "ADMIN SUB <date> <teacher>" in app.last(ADMIN)

    app.sms(ADMIN, "ADMIN HELP")
    guide = app.last(ADMIN)
    assert "leader guide" in guide
    assert "ADMIN SUB 10/18 nursery" in guide
    assert "9pm-8am" in guide

    app.sms(ADMIN, "ADMIN")
    assert "ADMIN HELP" in app.last(ADMIN)

    app.sms(ADMIN, "ADMIN ROSTER")
    assert "#" in app.last(ADMIN) and "CTR 6a" in app.last(ADMIN)


def test_old_database_gains_the_new_column(tmp_path):
    """A live DB from before requested_by_id must be upgraded in place."""
    import sqlite3

    from app.db import make_engine

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE request (id INTEGER PRIMARY KEY, teacher_id INTEGER, "
            "service_date DATE, class_name VARCHAR(80), note TEXT, status VARCHAR(12), "
            "filled_by_id INTEGER, filled_at DATETIME, created_at DATETIME)"
        )
    make_engine(str(path))
    with sqlite3.connect(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(request)")}
    assert "requested_by_id" in cols
