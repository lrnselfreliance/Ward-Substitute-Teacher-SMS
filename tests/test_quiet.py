from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.quiet import QuietHours

TZ = ZoneInfo("America/Denver")
QH = QuietHours(time(21, 0), time(8, 0), TZ)


def at(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


@pytest.mark.parametrize(
    "stamp,quiet",
    [
        ("2026-03-09 20:59", False),
        ("2026-03-09 21:00", True),
        ("2026-03-09 23:30", True),
        ("2026-03-10 03:00", True),
        ("2026-03-10 07:59", True),
        ("2026-03-10 08:00", False),
        ("2026-03-10 12:00", False),
    ],
)
def test_is_quiet(stamp, quiet):
    assert QH.is_quiet(at(stamp)) is quiet


def test_next_wake_jumps_to_morning():
    assert QH.next_wake(at("2026-03-09 22:00")) == at("2026-03-10 08:00")
    assert QH.next_wake(at("2026-03-10 03:00")) == at("2026-03-10 08:00")


def test_next_wake_is_a_noop_while_awake():
    moment = at("2026-03-10 12:00")
    assert QH.next_wake(moment) == moment


def test_ttl_does_not_burn_overnight():
    """The bug this module exists to prevent.

    A 6h window opened at 8:50pm must not expire at 2:50am -- that would
    march through the whole roster while everyone slept.
    """
    expires = QH.add_waking(at("2026-03-09 20:50"), timedelta(hours=6))
    # 10 waking minutes tonight, then 5h50m from 8am.
    assert expires == at("2026-03-10 13:50")


def test_ttl_within_one_day_is_plain_addition():
    expires = QH.add_waking(at("2026-03-10 09:00"), timedelta(hours=3))
    assert expires == at("2026-03-10 12:00")


def test_ttl_started_during_quiet_hours_begins_at_wake():
    expires = QH.add_waking(at("2026-03-10 02:00"), timedelta(hours=2))
    assert expires == at("2026-03-10 10:00")


def test_ttl_spanning_multiple_nights():
    # 13 waking hours per day; 20h of waking time = 13h today + 7h tomorrow.
    expires = QH.add_waking(at("2026-03-10 08:00"), timedelta(hours=20))
    assert expires == at("2026-03-11 15:00")
