"""Onboarding, commands, and role-aware help."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import DONE, Person
from tests.conftest import SUBS, TEACHER

NEW = "+15559998888"


def test_onboarding_a_substitute(app):
    app.sms(NEW, "hi")
    assert "consent" in app.last(NEW).lower()

    app.sms(NEW, "yes")
    assert "What's your name" in app.last(NEW)

    app.sms(NEW, "Jane Doe")
    assert "TEACHER or a SUBSTITUTE" in app.last(NEW)

    app.sms(NEW, "substitute")
    assert "MALE or FEMALE" in app.last(NEW)

    app.sms(NEW, "female")
    assert "Which Sundays" in app.last(NEW)

    app.sms(NEW, "1 3")
    assert "Jane Doe" in app.last(NEW)
    assert "1st/3rd Sundays" in app.last(NEW)

    app.sms(NEW, "yes")
    assert "all set" in app.last(NEW)

    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.enroll_state == DONE
        assert person.is_substitute and not person.is_teacher
        assert person.gender == "F"
        assert person.sundays == 0b00101


def test_onboarding_asks_teachers_for_their_class(app):
    app.sms(NEW, "hi")
    app.sms(NEW, "yes")
    app.sms(NEW, "Tom")
    app.sms(NEW, "teacher")
    app.sms(NEW, "male")
    assert "What class" in app.last(NEW)

    app.sms(NEW, "3rd grade")
    assert "3rd grade" in app.last(NEW)  # straight to confirm, no Sundays asked

    app.sms(NEW, "yes")
    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.class_name == "3rd grade"
        assert person.enroll_state == DONE


def test_onboarding_both_roles_asks_everything(app):
    for reply in ("hi", "yes", "Pat", "both", "female", "Nursery", "all"):
        app.sms(NEW, reply)
    assert "Correct?" in app.last(NEW)
    app.sms(NEW, "yes")
    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.is_teacher and person.is_substitute
        assert person.class_name == "Nursery"


def test_confirm_no_restarts(app):
    for reply in ("hi", "yes", "Jane", "substitute", "female", "all"):
        app.sms(NEW, reply)
    app.sms(NEW, "no")
    assert "start over" in app.last(NEW)
    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.name is None


def test_bad_answers_reprompt_without_advancing(app):
    app.sms(NEW, "hi")
    app.sms(NEW, "yes")
    app.sms(NEW, "Jane")
    app.sms(NEW, "banana")
    assert "TEACHER, SUBSTITUTE, or BOTH" in app.last(NEW)
    app.sms(NEW, "substitute")
    assert "MALE or FEMALE" in app.last(NEW)


def test_help_is_role_aware(app):
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll(SUBS[0], "Sam")
    app.enroll(SUBS[1], "Ada", admin=True)

    app.sms(TEACHER, "HELP")
    teacher_help = app.last(TEACHER)
    assert "SUB <date>" in teacher_help
    assert "SUNDAYS" not in teacher_help
    assert "ADMIN" not in teacher_help

    app.sms(SUBS[0], "HELP")
    sub_help = app.last(SUBS[0])
    assert "SUNDAYS" in sub_help
    assert "SUB <date>" not in sub_help

    app.sms(SUBS[1], "help")
    assert "ADMIN" in app.last(SUBS[1])


def test_commands_beat_yes_no(staffed):
    """A teacher holding an offer can still file a new request."""
    app = staffed
    app.enroll("+15550000009", "Tina", gender="M", teacher=True, substitute=True,
               class_name="5th grade")
    app.sms(TEACHER, "SUB 3/15")
    app.clear()

    # Tina may be holding a pending offer; SUB must not read as an answer.
    app.sms("+15550000009", "SUB 3/22")
    assert "sub needed" in app.last("+15550000009").lower()


def test_class_command_changes_future_requests_only(app):
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll(SUBS[0], "Sam")

    app.sms(TEACHER, "SUB 3/15")
    app.sms(TEACHER, "CLASS 5th grade")
    assert "now 5th grade" in app.last(TEACHER)

    app.sms(TEACHER, "SUB 3/22")

    from app.models import Request

    with app.session_factory() as session:
        rows = session.scalars(select(Request).order_by(Request.service_date)).all()
        assert rows[0].class_name == "3rd grade"  # snapshot preserved
        assert rows[1].class_name == "5th grade"


def test_non_sunday_is_questioned_not_guessed(app):
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.sms(TEACHER, "SUB 3/16")
    reply = app.last(TEACHER)
    assert "Monday" in reply
    assert "Mar 15" in reply


def test_teacher_without_a_class_is_asked_for_one(app):
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name=None)
    app.sms(TEACHER, "SUB 3/15")
    assert "What class" in app.last(TEACHER)


def test_stop_deactivates(app):
    app.enroll(SUBS[0], "Sam")
    app.sms(SUBS[0], "STOP")
    with app.session_factory() as session:
        assert session.scalar(
            select(Person).where(Person.phone == SUBS[0])
        ).active is False


def test_duplicate_message_sid_is_ignored(app):
    """Twilio retries on any non-2xx; a retry must not re-run the command."""
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll(SUBS[0], "Sam")

    app.sms(TEACHER, "SUB 3/15", sid="SM123")
    app.sms(TEACHER, "SUB 3/15", sid="SM123")

    assert app.count(TEACHER) == 1
    assert app.count(SUBS[0]) == 1  # asked once, not twice


def test_unparseable_offer_reply_reprompts_once(staffed):
    app = staffed
    app.enroll(SUBS[9], "Ada", admin=True)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:6]))[0]
    app.clear()

    app.sms(asked, "what is this about")
    assert "reply YES or NO" in app.last(asked)

    app.sms(asked, "still confused")
    assert "Couldn't parse" in app.last(SUBS[9])


def test_consent_is_required_before_anything_is_collected(app):
    """A campaign reviewer texting the number sees the disclosure first."""
    app.sms(NEW, "hi")
    first = app.last(NEW)
    assert "Clover Leaf Ward" in first
    assert "Msg frequency varies" in first
    assert "STOP to opt out" in first

    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.enroll_state == "ask_consent"
        assert person.consent_at is None        # not consented merely by texting
        assert person.consent_text == "hi"      # but their first words are kept


def test_declining_consent_stops_everything(app):
    app.sms(NEW, "hi")
    app.sms(NEW, "no")

    assert "won't be texted again" in app.last(NEW)
    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.active is False
        assert person.consent_at is None
        assert person.name is None


def test_consent_can_be_given_later(app):
    app.sms(NEW, "hi")
    app.sms(NEW, "no")
    app.sms(NEW, "yes")

    assert "What's your name" in app.last(NEW)
    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.active is True
        assert person.consent_at is not None


def test_unclear_consent_reply_repeats_the_disclosure(app):
    app.sms(NEW, "hi")
    app.sms(NEW, "who is this")
    assert "Msg frequency varies" in app.last(NEW)
    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.enroll_state == "ask_consent"


def test_consent_timestamp_marks_the_yes_not_first_contact(app):
    app.sms(NEW, "hi")
    app.advance(hours=2)
    app.sms(NEW, "yes")

    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.consent_at == app.clock.now()
        assert person.created_at < person.consent_at


# -- registered A2P opt-in keywords ----------------------------------------


def test_start_keyword_opts_a_stranger_in(app):
    app.sms(NEW, "START")
    reply = app.last(NEW)
    assert reply.startswith("Clover Leaf Ward:")
    assert "opted in" in reply and "HELP" in reply and "STOP" in reply
    with app.session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == NEW))
        assert person.active is True
        assert person.consent_at is not None
        assert person.enroll_state == "ask_name"


def test_yes_cold_is_an_opt_in(app):
    """YES is a registered opt-in keyword, so it works with no context."""
    app.sms(NEW, "yes")
    assert "opted in" in app.last(NEW)


def test_unstop_reactivates_after_stop(app):
    app.enroll(SUBS[0], "Sam")
    app.sms(SUBS[0], "STOP")
    app.sms(SUBS[0], "UNSTOP")
    assert "opted in" in app.last(SUBS[0])
    with app.session_factory() as session:
        assert session.scalar(
            select(Person).where(Person.phone == SUBS[0])
        ).active is True


def test_opt_in_keyword_never_hijacks_an_active_members_yes(staffed):
    """The one way this could break: swallowing an answer to an offer."""
    app = staffed
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:6]))[0]
    app.clear()

    app.sms(asked, "yes")

    assert "You're subbing" in app.last(asked)
    assert "opted in" not in app.last(asked)


def test_every_opt_in_path_gives_the_same_confirmation(app):
    """Reviewer may arrive via START, cold YES, or the consent prompt."""
    a, b, c = "+15551110001", "+15551110002", "+15551110003"
    app.sms(a, "START")
    app.sms(b, "yes")
    app.sms(c, "hi")
    app.sms(c, "yes")

    for phone in (a, b, c):
        reply = app.last(phone)
        assert "opted in" in reply
        assert "What's your name?" in reply


def test_join_keyword_works(app):
    """The public opt-in page tells people to text JOIN, so JOIN must work."""
    app.sms(NEW, "JOIN")
    assert "opted in" in app.last(NEW)


# -- registered A2P opt-out and help keywords ------------------------------
# These must match exactly what the campaign registration declares; carriers
# test them, and a keyword that does something else is a compliance failure.


@pytest.mark.parametrize(
    "keyword",
    ["STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "QUIT", "END", "OPTOUT", "REVOKE"],
)
def test_every_registered_opt_out_keyword_opts_out(app, keyword):
    app.enroll(SUBS[0], "Sam")
    app.sms(SUBS[0], keyword)
    assert "unsubscribed" in app.last(SUBS[0])
    with app.session_factory() as session:
        assert session.scalar(
            select(Person).where(Person.phone == SUBS[0])
        ).active is False


def test_cancel_with_a_date_still_cancels_the_request(app):
    """Bare CANCEL opts out, but the teacher command keeps working."""
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll(SUBS[0], "Sam")
    app.sms(TEACHER, "SUB 3/15")
    app.sms(TEACHER, "CANCEL 3/15")

    assert "Cancelled" in app.last(TEACHER)
    with app.session_factory() as session:
        assert session.scalar(
            select(Person).where(Person.phone == TEACHER)
        ).active is True


@pytest.mark.parametrize("keyword", ["HELP", "INFO"])
def test_registered_help_keywords_work(app, keyword):
    app.enroll(SUBS[0], "Sam")
    app.sms(SUBS[0], keyword)
    assert "Substitute Finder" in app.last(SUBS[0])


def test_help_reply_carries_brand_and_disclosures(app):
    """What a carrier checks a HELP response for."""
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll(SUBS[0], "Sam", admin=True)
    for phone in (TEACHER, SUBS[0]):
        app.sms(phone, "HELP")
        reply = app.last(phone)
        assert reply.startswith("Clover Leaf Ward Substitute Finder:")
        assert "rates may apply" in reply
        assert "STOP" in reply


def test_stop_confirmation_is_branded(app):
    app.enroll(SUBS[0], "Sam")
    app.sms(SUBS[0], "STOP")
    reply = app.last(SUBS[0])
    assert reply.startswith("Clover Leaf Ward:")
    assert "START to rejoin" in reply
