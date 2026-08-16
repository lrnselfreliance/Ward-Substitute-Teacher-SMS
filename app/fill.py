"""The fill loop: batches, offers, expiry, and the periodic tick."""

from __future__ import annotations

import logging
from datetime import date, time, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import messages as M
from .clock import Clock, to_local, to_utc
from .config import Config, tier_for
from .models import (
    EXPIRED,
    FILLED,
    OPEN,
    PENDING,
    SUPERSEDED,
    UNFILLED,
    Notice,
    Offer,
    Person,
    Request,
)
from .quiet import QuietHours
from .ranking import eligible

log = logging.getLogger(__name__)


class Filler:
    def __init__(
        self,
        session_factory,
        gateway,
        clock: Clock,
        config: Config,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.clock = clock
        self.config = config
        self.quiet = QuietHours(config.quiet_start, config.quiet_end, config.timezone)

    # -- helpers ------------------------------------------------------------

    def today(self) -> date:
        return to_local(self.clock.now(), self.config.timezone).date()

    def admins(self, session: Session) -> list[Person]:
        return list(
            session.scalars(
                select(Person).where(
                    Person.is_admin.is_(True), Person.active.is_(True)
                )
            ).all()
        )

    def notify_admins(self, session: Session, body: str) -> None:
        for admin in self.admins(session):
            self.gateway.send(admin.phone, body)

    def _once(self, session: Session, kind: str, key: str) -> bool:
        """True the first time this (kind, key) is seen; False afterwards."""
        existing = session.scalar(
            select(Notice).where(Notice.kind == kind, Notice.key == key)
        )
        if existing:
            return False
        session.add(
            Notice(kind=kind, key=key, sent_at=self.clock.now())
        )
        return True

    # -- sending offers -----------------------------------------------------

    def send_next_batch(self, session: Session, request: Request) -> int:
        """Ask the next tier-sized group. Returns how many were asked.

        Refuses to send during quiet hours -- the tick will call again at 8am.
        """
        now = self.clock.now()
        if self.quiet.is_quiet(now):
            return 0
        if request.status != OPEN:
            return 0

        days_until = (request.service_date - self.today()).days
        tier = tier_for(days_until)

        candidates = eligible(session, request)[: tier.batch_size]
        if not candidates:
            self._mark_unfilled(session, request)
            return 0

        expires_at = self.quiet.add_waking(now, tier.ttl)
        for person in candidates:
            session.add(
                Offer(
                    request_id=request.id,
                    person_id=person.id,
                    sent_at=now,
                    expires_at=expires_at,
                    status=PENDING,
                )
            )
            self.gateway.send(
                person.phone,
                M.offer(
                    request.teacher.name,
                    request.class_name,
                    request.service_date,
                    request.note,
                ),
            )
        session.flush()
        return len(candidates)

    def _mark_unfilled(self, session: Session, request: Request) -> None:
        request.status = UNFILLED
        session.flush()
        self.gateway.send(
            request.teacher.phone,
            M.unfilled_teacher(request.class_name, request.service_date),
        )
        self.notify_admins(
            session,
            M.admin_unfilled(
                request.class_name, request.service_date, request.teacher.name
            ),
        )

    # -- responses ----------------------------------------------------------

    def accept(self, session: Session, offer: Offer, raw: str) -> str:
        """Try to claim the request. Returns the message to send back.

        The conditional UPDATE is the entire concurrency story: whoever's
        statement changes a row wins, everyone else is told it's filled.
        """
        now = self.clock.now()
        offer.raw_reply = raw
        offer.responded_at = now

        result = session.execute(
            update(Request)
            .where(Request.id == offer.request_id, Request.status == OPEN)
            .values(status=FILLED, filled_by_id=offer.person_id, filled_at=now)
        )

        if result.rowcount != 1:
            offer.status = SUPERSEDED
            session.flush()
            return M.TOO_LATE

        offer.status = "accepted"
        session.flush()
        session.expire_all()

        request = session.get(Request, offer.request_id)
        teacher = request.teacher
        sub = offer.person

        # Everyone else asked about this date stands down.
        others = session.scalars(
            select(Offer).where(
                Offer.request_id == request.id,
                Offer.status == PENDING,
                Offer.id != offer.id,
            )
        ).all()
        for other in others:
            other.status = SUPERSEDED
            self.gateway.send(other.person.phone, M.SUPERSEDED)

        self.gateway.send(
            teacher.phone,
            M.accepted_teacher(sub.name, sub.phone, request.service_date),
        )
        session.flush()
        return M.accepted_sub(
            teacher.name, teacher.phone, request.class_name, request.service_date
        )

    def decline(self, session: Session, offer: Offer, raw: str) -> None:
        offer.status = "declined"
        offer.raw_reply = raw
        offer.responded_at = self.clock.now()
        session.flush()

    # -- the periodic tick --------------------------------------------------

    def tick(self) -> None:
        with self.session_factory() as session:
            try:
                self._expire_offers(session)
                self._advance_open_requests(session)
                self._saturday_pushes(session)
                session.commit()
            except Exception:
                session.rollback()
                log.exception("tick failed")

    def _expire_offers(self, session: Session) -> None:
        now = self.clock.now()
        stale = session.scalars(
            select(Offer).where(Offer.status == PENDING, Offer.expires_at <= now)
        ).all()
        for offer in stale:
            # No penalty, no flag. Silence is a non-event; ranking handles it
            # via last_asked_at so non-responders keep cycling through fairly.
            offer.status = EXPIRED
        if stale:
            session.flush()

    def _advance_open_requests(self, session: Session) -> None:
        if self.quiet.is_quiet(self.clock.now()):
            return

        open_requests = session.scalars(
            select(Request).where(Request.status == OPEN).order_by(Request.service_date)
        ).all()

        for request in open_requests:
            if request.service_date < self.today():
                request.status = UNFILLED
                continue
            outstanding = session.scalar(
                select(Offer).where(
                    Offer.request_id == request.id, Offer.status == PENDING
                )
            )
            if outstanding:
                continue
            self.send_next_batch(session, request)

    def _saturday_pushes(self, session: Session) -> None:
        now_local = to_local(self.clock.now(), self.config.timezone)
        if now_local.weekday() != 5:  # Saturday
            return

        tomorrow = now_local.date() + timedelta(days=1)
        open_tomorrow = session.scalars(
            select(Request).where(
                Request.status == OPEN, Request.service_date == tomorrow
            )
        ).all()
        if not open_tomorrow:
            return

        key = tomorrow.isoformat()

        def existing_at(trigger: time) -> list[Request]:
            """Requests that already existed when this push was due.

            A request filed at 9:30pm tonight did not exist at either trigger,
            so it appears in neither push -- and that is deliberate. The
            teacher was already told at request time that it probably would
            not fill, so there is nothing new to report and nobody to page.
            """
            moment = to_utc(
                now_local.replace(
                    hour=trigger.hour, minute=trigger.minute, second=0, microsecond=0
                )
            )
            return [r for r in open_tomorrow if r.created_at <= moment]

        def label(rows: list[Request]) -> list[str]:
            return [f"{r.class_name or 'class'} ({r.teacher.name})" for r in rows]

        if now_local.time() >= self.config.digest_at:
            rows = existing_at(self.config.digest_at)
            if rows and self._once(session, "sat_digest", key):
                self.notify_admins(session, M.admin_digest(label(rows)))

        if now_local.time() >= self.config.last_call_at:
            rows = existing_at(self.config.last_call_at)
            if rows and self._once(session, "sat_last_call", key):
                self.notify_admins(session, M.admin_last_call(label(rows)))
