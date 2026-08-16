"""Quiet-hours arithmetic.

Two rules from the plan live here:

1. No proactive message is ever sent between quiet_start and quiet_end.
2. Offer TTLs count only waking time. An offer sent at 8:50pm with a 6h window
   must not expire at 2:50am while its recipient is asleep -- that would burn
   through the whole substitute roster overnight and mark everyone
   non-responsive by breakfast.

This is its own module (rather than living in fill.py as the plan sketched)
because it carries the densest edge cases in the codebase and deserves to be
tested in isolation.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .clock import to_local, to_utc


class QuietHours:
    def __init__(self, start: time, end: time, tz: ZoneInfo) -> None:
        if start == end:
            raise ValueError("quiet_start and quiet_end must differ")
        self.start = start
        self.end = end
        self.tz = tz

    # -- predicates ---------------------------------------------------------

    def is_quiet(self, moment: datetime) -> bool:
        """True if the given instant falls inside the quiet window."""
        local = to_local(moment, self.tz).time()
        if self.start > self.end:  # window spans midnight, e.g. 21:00 -> 08:00
            return local >= self.start or local < self.end
        return self.start <= local < self.end

    # -- boundary helpers ---------------------------------------------------

    def _next_boundary(self, local: datetime, boundary: time) -> datetime:
        """First occurrence of `boundary` strictly after `local`."""
        candidate = local.replace(
            hour=boundary.hour,
            minute=boundary.minute,
            second=0,
            microsecond=0,
        )
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate

    def next_wake(self, moment: datetime) -> datetime:
        """The instant sending may resume. Returns `moment` if already awake."""
        if not self.is_quiet(moment):
            return moment
        local = to_local(moment, self.tz)
        return to_utc(self._next_boundary(local, self.end))

    def next_sleep(self, moment: datetime) -> datetime:
        """The next instant sending must stop, strictly after `moment`."""
        local = to_local(moment, self.tz)
        return to_utc(self._next_boundary(local, self.start))

    # -- the TTL calculation ------------------------------------------------

    def add_waking(self, start: datetime, duration: timedelta) -> datetime:
        """Advance `start` by `duration` of *waking* time.

        Time spent inside quiet hours does not count against the total, so a
        6h window opened at 8:50pm expires at 2pm the next day rather than at
        2:50am.
        """
        if duration <= timedelta(0):
            return start

        cursor = self.next_wake(start)
        remaining = duration

        # Each pass consumes one contiguous waking stretch.
        while True:
            sleep_at = self.next_sleep(cursor)
            available = sleep_at - cursor
            if remaining <= available:
                return cursor + remaining
            remaining -= available
            cursor = self.next_wake(sleep_at)
