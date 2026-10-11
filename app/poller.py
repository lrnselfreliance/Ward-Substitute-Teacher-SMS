"""Pull inbound SMS from Twilio instead of receiving webhooks.

Why this exists: a webhook needs Twilio to reach *you*, which means a public
HTTPS URL, which means a tunnel or port forwarding and a certificate. Polling
inverts that -- the app makes only outbound HTTPS calls, exactly like sending
does. Nothing about the home network has to change, and there is no public
endpoint to secure because there is no endpoint at all.

The cost is latency: a reply arrives within one poll interval rather than
instantly. For a scheduler whose tightest deadline is measured in 45-minute
batches, that is not a real cost.

Correctness rests on ProcessedMessage: the same MessageSid dedupe that guards
webhook retries makes overlapping poll windows harmless.
"""

from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import select

from .models import Notice, ProcessedMessage

log = logging.getLogger(__name__)

BOOTSTRAP = ("poll", "bootstrapped")
# When the last fetch succeeded. Persisted so an outage that spans a restart
# is still covered.
LAST_OK = ("poll", "last_ok")


@dataclass
class Inbound:
    sid: str
    from_: str
    body: str
    sent_at: datetime


class Inbox(Protocol):
    def fetch(self, since: datetime) -> list[Inbound]: ...


class TwilioInbox:
    def __init__(self, account_sid: str, auth_token: str, our_number: str) -> None:
        from .gateway import twilio_client

        self._client = twilio_client(account_sid, auth_token)
        self._number = our_number

    def fetch(self, since: datetime) -> list[Inbound]:
        messages = self._client.messages.list(to=self._number, date_sent_after=since)
        found = []
        for m in messages:
            if m.direction != "inbound":
                continue
            sent = m.date_sent or m.date_created
            if sent is not None and sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            found.append(
                Inbound(sid=m.sid, from_=m.from_, body=m.body or "", sent_at=sent)
            )
        return found


@dataclass
class FakeInbox:
    """Test double. `deliver` simulates someone texting the service."""

    messages: list[Inbound] = None

    def __post_init__(self) -> None:
        self.messages = self.messages or []
        self._counter = 0

    def deliver(self, from_: str, body: str, at: datetime) -> Inbound:
        self._counter += 1
        msg = Inbound(sid=f"SM{self._counter:08d}", from_=from_, body=body, sent_at=at)
        self.messages.append(msg)
        return msg

    def fetch(self, since: datetime) -> list[Inbound]:
        return [m for m in self.messages if m.sent_at >= since]


class Poller:
    def __init__(
        self,
        inbox: Inbox,
        router,
        session_factory,
        clock,
        lookback: timedelta = timedelta(minutes=10),
        heartbeat_url: str | None = None,
        halted=None,
    ) -> None:
        self.inbox = inbox
        self.router = router
        self.session_factory = session_factory
        self.clock = clock
        self.lookback = lookback
        self.heartbeat_url = heartbeat_url
        self.halted = halted  # callable -> bool; shows HALTED on the monitor

    def poll(self) -> int:
        """Fetch and dispatch new inbound messages. Returns how many ran."""
        now = self.clock.now()
        since = now - self.lookback
        # After an outage longer than the lookback, a fixed window would skip
        # everything sent in its early minutes. Reach back to the last fetch
        # that worked instead; the MessageSid dedupe absorbs the overlap.
        last_ok = self._last_ok()
        if last_ok is not None and last_ok < since:
            since = last_ok
        try:
            messages = self.inbox.fetch(since)
        except Exception:
            log.exception("inbox fetch failed")
            return 0
        self._mark_ok(now)
        self._heartbeat()

        if self._bootstrap(messages):
            return 0

        handled = 0
        for message in sorted(messages, key=lambda m: m.sent_at):
            if self._seen(message.sid):
                continue
            self.router.handle(message.from_, message.body, message.sid)
            handled += 1
        return handled

    def _heartbeat(self) -> None:
        """Tell the uptime monitor the poll loop is alive. Never raises."""
        if not self.heartbeat_url:
            return
        url = self.heartbeat_url
        if self.halted and self.halted():
            url = url.replace("msg=OK", "msg=HALTED")
        try:
            urllib.request.urlopen(url, timeout=5).close()
        except Exception as exc:  # monitoring must never break the thing monitored
            log.warning("heartbeat failed: %s", exc)

    def _last_ok(self) -> datetime | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(Notice).where(Notice.kind == LAST_OK[0], Notice.key == LAST_OK[1])
            )
            return row.sent_at if row else None

    def _mark_ok(self, at: datetime) -> None:
        with self.session_factory() as session:
            row = session.scalar(
                select(Notice).where(Notice.kind == LAST_OK[0], Notice.key == LAST_OK[1])
            )
            if row:
                row.sent_at = at
            else:
                session.add(Notice(kind=LAST_OK[0], key=LAST_OK[1], sent_at=at))
            session.commit()

    def _seen(self, sid: str) -> bool:
        with self.session_factory() as session:
            return session.get(ProcessedMessage, sid) is not None

    def _bootstrap(self, messages: list[Inbound]) -> bool:
        """On the very first poll, absorb history without acting on it.

        Otherwise switching a live number over to polling would replay every
        message the account has ever received -- re-running onboarding and
        re-answering old offers for everyone at once.
        """
        with self.session_factory() as session:
            kind, key = BOOTSTRAP
            done = session.scalar(
                select(Notice).where(Notice.kind == kind, Notice.key == key)
            )
            if done:
                return False

            now = self.clock.now()
            for message in messages:
                if not session.get(ProcessedMessage, message.sid):
                    session.add(
                        ProcessedMessage(sid=message.sid, received_at=now)
                    )
            session.add(Notice(kind=kind, key=key, sent_at=now))
            session.commit()
            log.info("poller bootstrapped, ignoring %d pre-existing", len(messages))
            return True
