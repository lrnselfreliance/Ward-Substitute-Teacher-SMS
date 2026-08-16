"""Environment-driven settings.

Everything tunable lives here so behaviour can change without code edits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time, timedelta
from zoneinfo import ZoneInfo


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

    # "webhook" -> Twilio POSTs to us, needs a public HTTPS URL.
    # "poll"    -> we ask Twilio for new messages, needs no inbound access.
    inbound_mode: str = "webhook"
    poll_seconds: int = 20

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
            inbound_mode=os.environ.get("INBOUND_MODE", "webhook").lower(),
            poll_seconds=int(os.environ.get("POLL_SECONDS", "20")),
        )

    @property
    def polling(self) -> bool:
        return self.inbound_mode == "poll"
