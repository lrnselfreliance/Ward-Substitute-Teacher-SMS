"""The only place the current time is allowed to come from.

Every module takes a Clock so tests can freeze and advance time deterministically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, always timezone-aware UTC."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """Test clock. Accepts aware datetimes, or naive ones treated as UTC."""

    def __init__(self, at: datetime) -> None:
        self.set(at)

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        self._now = at.astimezone(timezone.utc)

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


def to_local(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.astimezone(tz)


def to_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)
