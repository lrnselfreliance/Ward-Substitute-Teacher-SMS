"""Who gets asked, and in what order.

Done in Python rather than SQL. At a few dozen people the performance
difference is nil, and the fairness rule is the thing most likely to need
explaining to a human, so it should be readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import FILLED, PENDING, Offer, Person, Request
from .parsers import nth_sunday

EPOCH_DATE = date.min
EPOCH_TIME = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class Candidate:
    person: Person
    last_served: date | None
    last_asked: datetime | None
    times_served: int

    def sort_key(self, request_id: int) -> tuple:
        # Never-served sorts first (date.min), then least-recently-asked within
        # that tier, then fewest total serves, then a stable scramble so the
        # same two people aren't perpetually in the same order.
        return (
            self.last_served or EPOCH_DATE,
            self.last_asked or EPOCH_TIME,
            self.times_served,
            (self.person.id * 2654435761 + request_id) % 1000,
        )


def _served_stats(session: Session) -> dict[int, tuple[date, int]]:
    rows = session.execute(
        select(
            Request.filled_by_id,
            func.max(Request.service_date),
            func.count(Request.id),
        )
        .where(Request.status == FILLED, Request.filled_by_id.is_not(None))
        .group_by(Request.filled_by_id)
    ).all()
    return {pid: (last, count) for pid, last, count in rows}


def _asked_stats(session: Session) -> dict[int, datetime]:
    rows = session.execute(
        select(Offer.person_id, func.max(Offer.sent_at)).group_by(Offer.person_id)
    ).all()
    return {pid: sent for pid, sent in rows}


def eligible(session: Session, request: Request) -> list[Person]:
    """Ordered list of everyone who may be asked about this request."""
    teacher = request.teacher

    people = session.scalars(
        select(Person).where(
            Person.active.is_(True),
            Person.is_substitute.is_(True),
            Person.enroll_state == "done",
            Person.gender == teacher.gender,
            Person.id != teacher.id,
        )
    ).all()

    already_offered = set(
        session.scalars(
            select(Offer.person_id).where(Offer.request_id == request.id)
        ).all()
    )

    # Anyone holding a pending offer anywhere, so a bare YES stays unambiguous.
    holding_pending = set(
        session.scalars(
            select(Offer.person_id).where(Offer.status == PENDING)
        ).all()
    )

    # Anyone already committed to another class on the same Sunday.
    committed = set(
        session.scalars(
            select(Request.filled_by_id).where(
                Request.service_date == request.service_date,
                Request.status == FILLED,
            )
        ).all()
    )

    nth = nth_sunday(request.service_date)
    served = _served_stats(session)
    asked = _asked_stats(session)

    candidates: list[Candidate] = []
    for person in people:
        if person.id in already_offered:
            continue
        if person.id in holding_pending:
            continue
        if person.id in committed:
            continue
        if not person.available_on(nth):
            continue
        last_served, times = served.get(person.id, (None, 0))
        candidates.append(
            Candidate(
                person=person,
                last_served=last_served,
                last_asked=asked.get(person.id),
                times_served=times,
            )
        )

    candidates.sort(key=lambda c: c.sort_key(request.id))
    return [c.person for c in candidates]


def exhausted(session: Session, request: Request) -> bool:
    """No one left to ask and nothing outstanding."""
    if eligible(session, request):
        return False
    pending = session.scalar(
        select(func.count(Offer.id)).where(
            Offer.request_id == request.id, Offer.status == PENDING
        )
    )
    return not pending
