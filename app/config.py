"""Environment-driven settings.

Everything tunable lives here so behaviour can change without code edits.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time, timedelta
from zoneinfo import ZoneInfo


E164 = re.compile(r"\+[1-9]\d{7,14}")


def _parse_time(value: str) -> time:
    hh, _, mm = value.partition(":")
    return time(int(hh), int(mm or 0))


@dataclass(frozen=True)
class Tier:
    """How aggressively to ask, based on days remaining until the service."""

    min_days: int
    batch_size: int
    ttl: timedelta


# Ordered most-distant first; the first tier whose min_days <= days_until wins.
# "All remaining" is expressed as a batch size larger than any real roster.
TIERS: tuple[Tier, ...] = (
    Tier(min_days=8, batch_size=3, ttl=timedelta(hours=12)),
    Tier(min_days=3, batch_size=3, ttl=timedelta(hours=6)),
    Tier(min_days=2, batch_size=4, ttl=timedelta(hours=3)),
    Tier(min_days=1, batch_size=9999, ttl=timedelta(minutes=90)),
    Tier(min_days=-36500, batch_size=9999, ttl=timedelta(minutes=45)),
)


def tier_for(days_until: int) -> Tier:
    for tier in TIERS:
        if days_until >= tier.min_days:
            return tier
    return TIERS[-1]


@dataclass(frozen=True)
class Config:
    org_name: str = "Clover Leaf Ward"
    db_path: str = "/data/substitute.db"
    backup_dir: str = "/data/backups"
    timezone: ZoneInfo = ZoneInfo("America/Denver")
    quiet_start: time = time(21, 0)
    quiet_end: time = time(8, 0)

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from: str | None = None
    public_url: str | None = None
    validate_signature: bool = True

    # Optional Uptime Kuma "push" monitor URL. Pinged after every successful
    # Twilio fetch, so silence means the poll loop is not actually working --
    # a stronger signal than "the container is up".
    heartbeat_url: str | None = None

    # "webhook" -> Twilio POSTs to us, needs a public HTTPS URL.
    # "poll"    -> we ask Twilio for new messages, needs no inbound access.
    inbound_mode: str = "webhook"
    poll_seconds: int = 20

    # Minimum age of a request before the tick may declare it unfilled. Keeps
    # "I couldn't find a sub" from being sent in the same instant as "Got it";
    # Twilio does not preserve order between messages sent in the same second.
    ack_grace: timedelta = timedelta(seconds=30)

    # Saturday admin nudges (local time).
    digest_at: time = time(17, 0)
    last_call_at: time = time(20, 0)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            org_name=os.environ.get("ORG_NAME", "Clover Leaf Ward"),
            db_path=os.environ.get("DB_PATH", "/data/substitute.db"),
            backup_dir=os.environ.get("BACKUP_DIR", "/data/backups"),
            timezone=ZoneInfo(os.environ.get("TZ", "America/Denver")),
            quiet_start=_parse_time(os.environ.get("QUIET_START", "21:00")),
            quiet_end=_parse_time(os.environ.get("QUIET_END", "08:00")),
            twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN"),
            twilio_from=os.environ.get("TWILIO_FROM"),
            public_url=os.environ.get("PUBLIC_URL"),
            validate_signature=os.environ.get("VALIDATE_SIGNATURE", "1") != "0",
            heartbeat_url=os.environ.get("HEARTBEAT_URL") or None,
            inbound_mode=os.environ.get("INBOUND_MODE", "webhook").lower(),
            poll_seconds=int(os.environ.get("POLL_SECONDS", "20")),
        )

    @property
    def polling(self) -> bool:
        return self.inbound_mode == "poll"

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from)

    def validate(self) -> None:
        """Refuse to start on settings that would fail silently.

        A malformed TWILIO_FROM is the nasty one: in poll mode it becomes the
        `To` filter on every inbox query, which then simply returns nothing
        while every other sign of life stays green.
        """
        if self.twilio_from and not E164.fullmatch(self.twilio_from):
            raise ValueError(
                f"TWILIO_FROM must be E.164 like +18015551234, got {self.twilio_from!r}"
            )
        if self.poll_seconds < 5:
            raise ValueError("POLL_SECONDS must be at least 5")
