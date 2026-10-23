"""ADMIN BAN and ADMIN HALT: the two switches for when something is wrong."""

from __future__ import annotations

from sqlalchemy import select

from app.conversation import Router
from app.halt import halted
from app.models import CANCELLED, EXPIRED, OPEN, DroppedMessage, Person, Request
from tests.conftest import SUBS, TEACHER

ADMIN = "+15551110000"
ADMIN2 = "+15551110002"
STRANGER = "+15559990000"


def _setup(app):
    app.enroll(ADMIN, "Ada", admin=True, substitute=False)
    app.enroll(ADMIN2, "Bob", admin=True, substitute=False)
    app.enroll(TEACHER, "Tom Teacher", teacher=True, substitute=False,
               class_name="3rd grade")
    for i, phone in enumerate(SUBS[:4]):
        app.enroll(phone, f"Sub{i}")
    return app


# -- BAN -------------------------------------------------------------------


def test_banned_person_gets_silence_except_stop(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN BAN sub0 rude texts")
    assert "Banned Sub0" in app.last(ADMIN)
    assert "Ada banned Sub0: rude texts" in app.last(ADMIN2)
    app.clear()

    for body in ("hi", "HELP", "YES", "SUB 3/15", "garbage"):
        app.sms(SUBS[0], body)
    assert app.count(SUBS[0]) == 0

    app.sms(SUBS[0], "STOP")
    assert "unsubscribed" in app.last(SUBS[0])
    assert app.count(SUBS[0]) == 1


def test_ban_unwinds_offers_and_excludes_from_ranking(app):
    _setup(app)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:4]))
    victim = asked[0]
    app.clear()

    app.sms(ADMIN, f"ADMIN BAN {victim[-4:]}")
    with app.session_factory() as session:
        p = session.scalar(select(Person).where(Person.phone == victim))
        assert p.banned and p.ban_reason is None
        assert all(o.status == EXPIRED for o in session.query(
            __import__("app.models", fromlist=["Offer"]).Offer).filter_by(person_id=p.id))

    # Their late YES is ignored; the next request never asks them.
    app.sms(victim, "yes")
    assert app.count(victim) == 0
    app.sms(TEACHER, "SUB 3/22")
    assert victim not in app.texted(*SUBS[:4])


def test_ban_withdraws_a_banned_teachers_open_request(app):
    _setup(app)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:4]))
    app.clear()
    app.sms(ADMIN, "ADMIN BAN tom")
    with app.session_factory() as session:
        assert session.scalar(select(Request)).status == CANCELLED
    for phone in asked:
        assert "covered now" in app.last(phone)
    assert app.count(TEACHER) == 0


def test_ban_by_full_number_blocks_someone_who_never_enrolled(app):
    _setup(app)
    app.sms(ADMIN, f"ADMIN BAN {STRANGER} spam")
    assert "Banned" in app.last(ADMIN)
    app.clear()
    app.sms(STRANGER, "hi")  # would normally start onboarding
    assert app.count(STRANGER) == 0
    with app.session_factory() as session:
        assert session.scalar(select(Person).where(Person.phone == STRANGER)).banned


def test_admins_cannot_be_banned_by_text(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN BAN bob")
    assert "can't be banned by text" in app.last(ADMIN)
    with app.session_factory() as session:
        assert not session.scalar(select(Person).where(Person.name == "Bob")).banned
    # And a banned-admin edge: Bob still receives admin notices.
    app.sms(ADMIN, "ADMIN BAN sub1")
    assert "banned Sub1" in app.last(ADMIN2)


def test_unban_restores_them(app):
    _setup(app)
    app.sms(SUBS[0], "PAUSE")
    app.sms(ADMIN, "ADMIN BAN sub0")
    app.sms(ADMIN, "ADMIN UNBAN sub0")
    assert "Unbanned Sub0" in app.last(ADMIN)
    assert "unbanned Sub0" in app.last(ADMIN2)
    with app.session_factory() as session:
        p = session.scalar(select(Person).where(Person.phone == SUBS[0]))
        assert not p.banned and not p.active  # still paused, as they were
    app.clear()
    app.sms(SUBS[0], "RESUME")
    assert "Welcome back" in app.last(SUBS[0])


def test_ban_rejects_ambiguous_and_unknown(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN BAN sub")
    assert "More than one match" in app.last(ADMIN)
    app.sms(ADMIN, "ADMIN BAN zelda")
    assert "full phone number" in app.last(ADMIN)
    app.sms(ADMIN, "ADMIN BAN")
    assert "ADMIN BAN <" in app.last(ADMIN)


# -- HALT ------------------------------------------------------------------


def test_halt_stops_everything_at_the_gateway(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN HALT offers going to the wrong people")
    assert app.last(ADMIN).startswith("HALTED")
    assert "Ada HALTED the service: offers going" in app.last(ADMIN2)
    app.clear()

    # A member text is dropped: no reply, no onboarding, nothing.
    app.sms(TEACHER, "SUB 3/15")
    app.sms(STRANGER, "hi")
    assert app.gateway.sent == []
    with app.session_factory() as session:
        assert session.scalar(select(Request)) is None  # never even created
        assert session.query(DroppedMessage).count() == 2

    # Even a direct attempt to send from inside the app is swallowed.
    app.gateway.send(SUBS[0], "should not go")
    assert app.gateway.sent == []


def test_halt_pauses_the_tick_and_resumes_after_unhalt(app):
    _setup(app)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:4]))
    app.sms(ADMIN, "ADMIN HALT")
    app.clear()

    # Next afternoon: the 12h waking-time TTL has lapsed, so without the halt
    # the tick would expire the offers and send the next batch.
    app.at("2026-03-03 14:30")
    app.tick()
    assert app.gateway.sent == []

    app.sms(ADMIN, "ADMIN UNHALT")
    assert "Service resumed" in app.last(ADMIN)
    assert "Bob" not in app.last(ADMIN)
    assert "resumed the service" in app.last(ADMIN2)
    app.clear()
    app.tick()
    later = app.texted(*SUBS[:4])
    assert later and not (later & set(asked))  # next batch, nobody re-asked


def test_admins_can_still_inspect_and_operate_while_halted(app):
    _setup(app)
    app.sms(TEACHER, "SUB 3/15")
    app.sms(ADMIN, "ADMIN HALT")
    app.clear()

    app.sms(ADMIN, "ADMIN OPEN")
    assert "3rd grade" in app.last(ADMIN)
    app.sms(ADMIN, "STATUS")
    assert "SERVICE HALTED by Ada" in app.last(ADMIN)
    app.sms(ADMIN, "ADMIN")
    assert "SERVICE HALTED" in app.last(ADMIN)
    app.sms(ADMIN, "ADMIN SUB 3/22 3rd")
    assert "Blocked while halted" in app.last(ADMIN)
    app.sms(ADMIN, "ADMIN BROADCAST hello")
    assert "Blocked while halted" in app.last(ADMIN)
    app.sms(ADMIN, "SUB 3/22")  # admins' member commands are refused too
    assert "Service is halted" in app.last(ADMIN)
    with app.session_factory() as session:
        assert session.query(Request).count() == 1


def test_stop_is_still_confirmed_while_halted(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN HALT")
    app.clear()
    app.sms(SUBS[0], "STOP")
    assert "unsubscribed" in app.last(SUBS[0])
    with app.session_factory() as session:
        assert not session.scalar(select(Person).where(Person.phone == SUBS[0])).active


def test_unhalt_reports_who_went_unanswered(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN HALT")
    app.sms(TEACHER, "SUB 3/15")
    app.sms(TEACHER, "hello?")
    app.sms(STRANGER, "hi")
    app.sms(ADMIN, "ADMIN UNHALT")
    reply = app.last(ADMIN)
    assert "3 text(s)" in reply
    assert "Tom Teacher x2" in reply
    assert STRANGER in reply
    with app.session_factory() as session:
        assert session.query(DroppedMessage).count() == 0
    app.sms(ADMIN, "ADMIN UNHALT")
    assert "isn't halted" in app.last(ADMIN)


def test_halt_survives_a_restart(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN HALT")
    # A fresh router and guard over the same database, as after a reboot.
    from app.gateway import FakeGateway, GuardedGateway
    gateway = GuardedGateway(FakeGateway(clock=app.clock), app.session_factory)
    assert gateway.halted
    router = Router(app.session_factory, gateway, app.clock, app.config, app.filler)
    router.handle(TEACHER, "SUB 3/15")
    assert gateway.sent == []
    with app.session_factory() as session:
        assert halted(session).by == "Ada"


def test_halt_twice_is_harmless(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN HALT")
    app.sms(ADMIN2, "ADMIN HALT")
    assert "Already halted" in app.last(ADMIN2)


# -- ADMIN CANCEL ----------------------------------------------------------


def test_admin_cancel_tells_teacher_and_offerees(app):
    _setup(app)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:4]))
    app.clear()
    app.sms(ADMIN, "ADMIN CANCEL 3/15 tom")
    assert "Withdrew Tom Teacher's request" in app.last(ADMIN)
    assert "Ada cancelled your sub request" in app.last(TEACHER)
    for phone in asked:
        assert "covered now" in app.last(phone)
    with app.session_factory() as session:
        assert session.scalar(select(Request)).status == CANCELLED
    app.sms(ADMIN, "ADMIN CANCEL 3/15 tom")
    assert "No open request" in app.last(ADMIN)


def test_admin_cancel_while_halted_says_who_was_not_told(app):
    _setup(app)
    app.sms(TEACHER, "SUB 3/15")
    app.sms(ADMIN, "ADMIN HALT")
    app.clear()
    app.sms(ADMIN, "ADMIN CANCEL 3/15 3rd")
    assert "NOT notified (halted)" in app.last(ADMIN)
    assert app.texted(*SUBS[:4]) == set()
    assert app.count(TEACHER) == 0
    with app.session_factory() as session:
        assert session.scalar(select(Request)).status == CANCELLED
        assert session.scalar(select(Request)).status != OPEN


def test_dropped_texts_keep_the_sender_but_not_the_words(app):
    _setup(app)
    app.sms(ADMIN, "ADMIN HALT")
    app.sms(TEACHER, "something private")
    with app.session_factory() as session:
        row = session.query(DroppedMessage).one()
        assert row.phone == TEACHER
        assert row.body is None
