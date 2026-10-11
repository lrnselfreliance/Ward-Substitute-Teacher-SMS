"""Every send to a third party honours banned and opted-out, and PAUSE does
not leave a request stuck behind the paused person's offer."""

from __future__ import annotations

from sqlalchemy import select

from app.models import ACCEPTED, EXPIRED, FILLED, SUPERSEDED, Offer, Person, Request
from tests.conftest import SUBS, TEACHER

ADMIN = "+15551110000"


def _admin(app):
    app.enroll(ADMIN, "Ada", admin=True, substitute=False)
    return app


def test_broadcast_skips_banned_and_paused(staffed):
    app = _admin(staffed)
    app.sms(ADMIN, "ADMIN BAN sub0")
    app.sms(SUBS[1], "PAUSE")
    app.clear()
    app.sms(ADMIN, "ADMIN BROADCAST Potluck Sunday!")
    assert "Sent to 5 people" in app.last(ADMIN)  # teacher + 4 subs
    assert app.count(SUBS[0]) == 0
    assert app.count(SUBS[1]) == 0
    assert "Potluck" in app.last(SUBS[2])


def test_cancel_does_not_text_a_sub_who_opted_out_after_accepting(staffed):
    app = staffed
    app.sms(TEACHER, "SUB 3/15")
    sub = sorted(app.texted(*SUBS[:6]))[0]
    app.sms(sub, "yes")
    app.sms(sub, "STOP")
    app.clear()
    app.sms(TEACHER, "CANCEL 3/15")
    assert app.count(sub) == 0
    assert "Cancelled" in app.last(TEACHER)


def test_pause_releases_the_offer_and_the_batch_moves_on(staffed):
    app = staffed
    app.sms(TEACHER, "SUB 3/15")
    first = sorted(app.texted(*SUBS[:6]))
    app.clear()

    # Two decline; the third pauses instead of answering.
    app.sms(first[0], "no")
    app.sms(first[1], "no")
    app.sms(first[2], "PAUSE")
    assert "Paused" in app.last(first[2])

    with app.session_factory() as session:
        p = session.scalar(select(Person).where(Person.phone == first[2]))
        mine = session.scalars(select(Offer).where(Offer.person_id == p.id)).all()
        assert [o.status for o in mine] == [EXPIRED]

    # The next batch went out immediately rather than after the 12h TTL.
    nxt = app.texted(*SUBS[:6]) - set(first)
    assert len(nxt) == 3


def test_paused_persons_yes_is_not_a_resubscribe(staffed):
    app = staffed
    app.sms(TEACHER, "SUB 3/15")
    sub = sorted(app.texted(*SUBS[:6]))[0]
    app.sms(sub, "PAUSE")
    app.clear()
    app.sms(sub, "YES")
    assert "moved on" in app.last(sub)
    with app.session_factory() as session:
        p = session.scalar(select(Person).where(Person.phone == sub))
        assert not p.active  # still paused, not silently opted back in
        assert session.scalar(select(Request)).status != FILLED


def test_stopped_person_yes_still_resubscribes_when_no_offer_lapsed(staffed):
    app = staffed
    app.sms(SUBS[5], "STOP")
    app.sms(SUBS[5], "YES")
    assert "opted in" in app.last(SUBS[5])


def test_admin_fill_texts_both_parties_and_accepts_the_batched_sub(staffed):
    app = _admin(staffed)
    app.sms(TEACHER, "SUB 3/15")
    asked = sorted(app.texted(*SUBS[:6]))
    app.clear()

    with app.session_factory() as session:
        name = session.scalar(select(Person).where(Person.phone == asked[0])).name
    app.sms(ADMIN, f"ADMIN FILL 3/15 {name}")
    assert "Both have been texted" in app.last(ADMIN)
    assert "You're subbing" in app.last(asked[0])
    assert "can sub for you" in app.last(TEACHER)
    for other in asked[1:]:
        assert "covered now" in app.last(other)
    with app.session_factory() as session:
        statuses = {o.person.phone: o.status for o in session.scalars(select(Offer)).all()}
    assert statuses[asked[0]] == ACCEPTED
    assert all(statuses[o] == SUPERSEDED for o in asked[1:])


def test_guard_picks_up_a_halt_written_by_another_process(tmp_path):
    from datetime import datetime, timezone

    from app.db import make_engine, make_session_factory
    from app.gateway import FakeGateway, GuardedGateway
    from app.halt import set_halt

    path = str(tmp_path / "live.db")
    sf = make_session_factory(make_engine(path))
    fake = FakeGateway()
    guard = GuardedGateway(fake, sf, path)
    assert guard.send("+15550000001", "before") is True

    # "subctl halt" in another process: a second engine over the same file.
    other = make_session_factory(make_engine(path))
    with other() as session:
        set_halt(session, "server", None, datetime.now(timezone.utc))
        session.commit()

    assert guard.send("+15550000001", "after") is False
    assert [m.body for m in fake.sent] == ["before"]
