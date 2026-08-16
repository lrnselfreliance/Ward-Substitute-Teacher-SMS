"""Outbound SMS.

FakeGateway is the default during development so the whole app is exercisable
without a phone number -- which is what makes the 10-15 day A2P registration
wait free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

log = logging.getLogger(__name__)


class Gateway(Protocol):
    def send(self, to: str, body: str) -> None: ...


@dataclass
class Sent:
    to: str
    body: str
    at: datetime | None = None


@dataclass
class FakeGateway:
    sent: list[Sent] = field(default_factory=list)
    clock: object | None = None

    def send(self, to: str, body: str) -> None:
        at = self.clock.now() if self.clock else None
        self.sent.append(Sent(to=to, body=body, at=at))
        log.info("SMS -> %s: %s", to, body)

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


class TwilioGateway:
    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        from twilio.rest import Client

        self._client = Client(account_sid, auth_token)
        self._from = from_number

    def send(self, to: str, body: str) -> None:
        try:
            self._client.messages.create(to=to, from_=self._from, body=body)
        except Exception:
            # A single failed send must never take down the tick loop or leave
            # a webhook unanswered; Twilio would then retry the inbound message.
            log.exception("failed sending SMS to %s", to)
