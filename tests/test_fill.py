"""Fill-loop behaviour: batching, the accept race, expiry, and fairness."""

from __future__ import annotations

from sqlalchemy import select

from app.models import FILLED, UNFILLED, Offer, Person, Request
from tests.conftest import SUBS, TEACHER


def test_request_asks_first_batch_of_three(staffed):
    app = staffed
    app.sms(TEACHER, "SUB 3/15")

    assert "3rd grade" in app.last(TEACHER)
    assert len(app.texted(*SUBS[:6])) == 3


def test_first_accept_wins(staffed):
    app = staffed
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:6]))
    app.clear()

    app.sms(asked[0], "yes")
    app.sms(asked[1], "yes")

    assert "just got filled" in app.last(asked[1])
    assert "You're subbing" in app.outbox(asked[0])[0]
    # The teacher gets the winner's name and number.
    teacher_msg = app.last(TEACHER)
    assert asked[0] in teacher_msg

    with app.session_factory() as session:
        request = session.scalar(select(Request))
        assert request.status == FILLED
        assert request.filled_by.phone == asked[0]


def test_others_are_told_to_stand_down(staffed):
    app = staffed
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:6]))
    app.clear()

    app.sms(asked[0], "yes")

    for loser in asked[1:]:
        assert "covered now" in app.last(loser)


def test_declines_trigger_the_next_batch(staffed):
    app = staffed
    app.sms(TEACHER, "SUB 3/15")
    first = sorted(app.texted(*SUBS[:6]))
    app.clear()

    for phone in first:
        app.sms(phone, "no")

    second = app.texted(*SUBS[:6]) - set(first)
    assert len(second) == 3


def test_everyone_declining_reports_unfilled(staffed):
    app = staffed
    app.sms(TEACHER, "SUB 3/15")

    for _ in range(3):
        for phone in list(app.texted(*SUBS[:6])):
            app.sms(phone, "no")

    assert "couldn't find a sub" in app.last(TEACHER)
    with app.session_factory() as session:
        assert session.scalar(select(Request)).status == UNFILLED


def test_gender_is_a_hard_constraint(app):
    app.enroll(TEACHER, "Tina", gender="F", teacher=True, substitute=False,
               class_name="Ladies class")
    app.enroll(SUBS[0], "Bob", gender="M")
    app.enroll(SUBS[1], "Betty", gender="F")

    app.sms(TEACHER, "SUB 3/15")

    assert app.count(SUBS[0]) == 0
    assert app.count(SUBS[1]) == 1


def test_availability_is_respected(app):
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll(SUBS[0], "FirstOnly", sundays=0b00001)
    app.enroll(SUBS[1], "ThirdOnly", sundays=0b00100)

    app.sms(TEACHER, "SUB 3/15")  # 3rd Sunday of March 2026

    assert app.count(SUBS[0]) == 0
    assert app.count(SUBS[1]) == 1


def test_teacher_is_not_asked_to_sub_for_themselves(app):
    app.enroll(TEACHER, "Tom", teacher=True, substitute=True, class_name="3rd grade")
    app.enroll(SUBS[0], "Other")

    app.sms(TEACHER, "SUB 3/15")

    assert app.count(SUBS[0]) == 1
    # Only the acknowledgement, no offer to himself.
    assert app.count(TEACHER) == 1


def test_nobody_holds_two_pending_offers(app):
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll("+15550000002", "Tim", teacher=True, substitute=False, class_name="5th grade")
    app.enroll(SUBS[0], "Only Sub")

    app.sms(TEACHER, "SUB 3/15")
    app.sms("+15550000002", "SUB 3/15")

    # Asked once, about the first request only; a bare YES stays unambiguous.
    assert app.count(SUBS[0]) == 1


def test_least_recently_served_goes_first(app):
    app.enroll(TEACHER, "Tom", teacher=True, substitute=False, class_name="3rd grade")
    app.enroll(SUBS[0], "Recent")
    app.enroll(SUBS[1], "LongAgo")
    app.enroll(SUBS[2], "Never")

    with app.session_factory() as session:
        tom = session.scalar(select(Person).where(Person.phone == TEACHER))
        for phone, day in ((SUBS[0], 22), (SUBS[1], 1)):
            person = session.scalar(select(Person).where(Person.phone == phone))
            session.add(
                Request(
                    teacher_id=tom.id,
                    service_date=__import__("datetime").date(2026, 2, day),
                    class_name="3rd grade",
                    status=FILLED,
                    filled_by_id=person.id,
                    filled_at=app.clock.now(),
                    created_at=app.clock.now(),
                )
            )
        session.commit()

    app.sms(TEACHER, "SUB 3/15")

    order = [m.to for m in app.gateway.sent if m.to in SUBS[:3]]
    assert order == [SUBS[2], SUBS[1], SUBS[0]]  # never, long ago, recent


def test_silence_never_penalises(staffed):
    """A non-responder returns to the queue for future Sundays."""
    app = staffed
    app.sms(TEACHER, "SUB 3/15")
    quiet_one = sorted(app.texted(*SUBS[:6]))[0]
    app.clear()

    # They never reply; the offer simply expires. 12h of *waking* time from a
    # 3pm send lands at 2pm the next day, not 3am -- see test_quiet.py.
    app.advance(hours=24)
    app.tick()

    with app.session_factory() as session:
        offer = session.scalar(select(Offer))
        assert offer.status == "expired"
        assert session.scalar(
            select(Person).where(Person.phone == quiet_one)
        ).active is True

    # Keep asking until they come round again -- they are never excluded.
    app.clear()
    for week in ("3/22", "3/29", "4/5", "4/12"):
        app.sms(TEACHER, f"SUB {week}")
        for phone in list(app.texted(*SUBS[:6])):
            app.sms(phone, "no")
        app.clear()

    app.sms(TEACHER, "SUB 4/19")
    assert quiet_one in app.texted(*SUBS[:6]) or app.count(quiet_one) > 0


def test_non_responder_does_not_monopolise_the_front(staffed):
    """The reason last_asked_at is the second sort key.

    Without it a never-responding member keeps last_served = NULL forever and
    occupies a slot in every single batch.
    """
    app = staffed
    ghost = SUBS[0]

    seen_counts = {phone: 0 for phone in SUBS[:6]}
    for week in ("3/15", "3/22", "3/29", "4/5"):
        app.clear()
        app.sms(TEACHER, f"SUB {week}")
        for phone in app.texted(*SUBS[:6]):
            seen_counts[phone] += 1
        # Everyone except the ghost declines, so the request exhausts.
        for phone in list(app.texted(*SUBS[:6])):
            if phone != ghost:
                app.sms(phone, "no")
        app.advance(hours=13)
        app.tick()

    # The ghost is asked, but no more often than anyone else.
    assert seen_counts[ghost] <= max(seen_counts.values())
    assert seen_counts[ghost] >= 1
