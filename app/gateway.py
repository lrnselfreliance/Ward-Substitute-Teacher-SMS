"""Outbound SMS.

FakeGateway is the default during development so the whole app is exercisable
without a phone number -- which is what makes the 10-15 day A2P registration
wait free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

log = logging.getLogger(__name__)


class Gateway(Protocol):
    def send(self, to: str, body: str, *, force: bool = False) -> bool:
        """True if the carrier accepted the message. Never raises."""


@dataclass
class Sent:
    to: str
    body: str
    at: datetime | None = None


@dataclass
class FakeGateway:
    sent: list[Sent] = field(default_factory=list)
    clock: object | None = None
    # Phones whose sends should fail, to exercise the failure paths.
    failing: set[str] = field(default_factory=set)

    def send(self, to: str, body: str, *, force: bool = False) -> bool:
        if to in self.failing:
            log.warning("SMS -> %s FAILED (fake)", to)
            return False
        at = self.clock.now() if self.clock else None
        self.sent.append(Sent(to=to, body=body, at=at))
        log.debug("SMS -> %s: %s", to, body)
        return True

    # -- test helpers -------------------------------------------------------

    def to(self, phone: str) -> list[Sent]:
        return [m for m in self.sent if m.to == phone]

    def last_to(self, phone: str) -> str | None:
        msgs = self.to(phone)
        return msgs[-1].body if msgs else None

    def count_to(self, phone: str) -> int:
        return len(self.to(phone))

    def clear(self) -> None:
        self.sent.clear()


# Seconds to wait on any single Twilio API call. Without this a dead TCP
# connection hangs until the OS gives up, stalling the poll loop for minutes.
HTTP_TIMEOUT = 15


def twilio_client(account_sid: str, auth_token: str):
    from twilio.http.http_client import TwilioHttpClient
    from twilio.rest import Client

    return Client(
        account_sid, auth_token, http_client=TwilioHttpClient(timeout=HTTP_TIMEOUT)
    )


class TwilioGateway:
    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self._client = twilio_client(account_sid, auth_token)
        self._from = from_number

    def send(self, to: str, body: str, *, force: bool = False) -> bool:
        try:
            self._client.messages.create(to=to, from_=self._from, body=body)
            return True
        except Exception:
            # A single failed send must never take down the tick loop or leave
            # a webhook unanswered. Callers see False and decide what that
            # means for them; an offer, for instance, is not recorded as asked.
            log.exception("failed sending SMS to %s", to)
            return False


class GuardedGateway:
    """Enforces HALT at the lowest layer, so it holds even if the bug being
    stopped is in the logic that decides what to send.

    While halted, only two things pass: anything addressed to an active admin
    (so they can still operate the switch) and sends marked force=True, which
    the router uses for STOP confirmations only.

    The state is a cache refreshed from the database by whoever already holds
    a session (each inbound message, each tick, and the HALT/UNHALT commands
    themselves), rather than a query per send. Opening a second session from
    inside a send would be wasteful in production and, on a shared in-memory
    connection, would roll back the caller's work.
    """

    def __init__(self, inner, session_factory, db_path: str | None = None) -> None:
        self._inner = inner
        self._session_factory = session_factory
        # A real file lets send() re-check the switch cheaply with its own
        # sqlite connection, closing the gap where `subctl halt` from another
        # process lands while a tick is mid-batch. In-memory databases
        # (tests) rely on refresh() alone.
        self._db_path = db_path if db_path and db_path != ":memory:" else None
        self._halted = False
        self._admin_phones: set[str] = set()
        # Outbound sends not yet written to the database. The tick drains
        # this with its own session; opening one per send would be wasteful.
        self._unrecorded: list[tuple[str, datetime]] = []
        with session_factory() as session:
            self.refresh(session)

    def refresh(self, session) -> None:
        from sqlalchemy import select

        from .halt import halted
        from .models import Person

        self._halted = halted(session) is not None
        if self._halted:
            self._admin_phones = set(
                session.scalars(
                    select(Person.phone).where(
                        Person.is_admin.is_(True),
                        Person.active.is_(True),
                        Person.banned.is_(False),
                    )
                ).all()
            )

    @property
    def halted(self) -> bool:
        return self._halted

    def _recheck(self) -> None:
        """Pick up a halt set by another process since the last refresh."""
        if self._halted or not self._db_path:
            return
        import sqlite3

        try:
            with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=2) as conn:
                row = conn.execute("SELECT value FROM setting WHERE key='halt'").fetchone()
                if row and row[0]:
                    self._halted = True
                    self._admin_phones = {
                        r[0] for r in conn.execute(
                            "SELECT phone FROM person WHERE is_admin=1 AND active=1 AND banned=0"
                        )
                    }
                    log.warning("HALT detected from another process")
        except sqlite3.Error as exc:
            log.warning("halt re-check failed: %s", exc)

    def send(self, to: str, body: str, *, force: bool = False) -> bool:
        self._recheck()
        if self._halted and not force and to not in self._admin_phones:
            log.warning("HALTED: dropped message to %s", to)
            return False
        ok = self._inner.send(to, body)
        if ok:
            self._unrecorded.append((to, datetime.now(timezone.utc)))
        return ok

    def drain(self, session, now: datetime) -> int:
        """Write buffered sends to SentMessage. Returns how many."""
        from .models import SentMessage

        pending, self._unrecorded = self._unrecorded, []
        for to, _at in pending:
            session.add(SentMessage(to=to, sent_at=now))
        return len(pending)

    def __getattr__(self, name):
        # Test helpers (sent, to, last_to, clear, ...) live on the inner fake.
        return getattr(self._inner, name)
