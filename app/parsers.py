"""Text parsing for inbound SMS.

Deliberately explicit rather than using a general date library: guessing wrong
about what a member meant is worse than asking them again.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .models import ALL_SUNDAYS, FEMALE, MALE

YES_WORDS = {
    "y", "yes", "yeah", "yep", "yup", "yea", "sure", "ok", "okay", "k",
    "i can", "ican", "can do", "absolutely", "affirmative", "1", "👍", "yes!",
}

NO_WORDS = {
    "n", "no", "nope", "nah", "cant", "can't", "cannot", "busy", "sorry",
    "unable", "negative", "2", "👎", "no sorry", "sorry no", "not this time",
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def normalize(body: str) -> str:
    return re.sub(r"\s+", " ", (body or "").strip()).casefold()


def parse_yes_no(text: str) -> bool | None:
    cleaned = normalize(text).rstrip(".!")
    if cleaned in YES_WORDS:
        return True
    if cleaned in NO_WORDS:
        return False
    return None


def parse_gender(text: str) -> str | None:
    cleaned = normalize(text)
    if cleaned in {"m", "male", "man", "boy"}:
        return MALE
    if cleaned in {"f", "female", "woman", "girl"}:
        return FEMALE
    return None


def parse_role(text: str) -> tuple[bool, bool] | None:
    """Returns (is_teacher, is_substitute)."""
    cleaned = normalize(text)
    if cleaned in {"both", "b", "teacher and substitute", "either"}:
        return True, True
    if "teacher" in cleaned or cleaned in {"t", "teach"}:
        return True, False
    if "sub" in cleaned or cleaned in {"s"}:
        return False, True
    return None


def parse_sundays(text: str) -> int | None:
    cleaned = normalize(text)
    if cleaned in {"all", "any", "every", "all of them", "everyone"}:
        return ALL_SUNDAYS
    digits = re.findall(r"[1-5]", cleaned)
    if not digits:
        return None
    # Reject stray digits mixed into prose, e.g. "I have 2 kids".
    if re.search(r"[a-z]{4,}", cleaned):
        return None
    mask = 0
    for d in digits:
        mask |= 1 << (int(d) - 1)
    return mask or None


def nth_sunday(d: date) -> int:
    """Which Sunday of the month this date is. 1-5."""
    return ((d.day - 1) // 7) + 1


def next_weekday(from_date: date, weekday: int, *, allow_today: bool = False) -> date:
    """weekday: Monday=0 ... Sunday=6."""
    delta = (weekday - from_date.weekday()) % 7
    if delta == 0 and not allow_today:
        delta = 7
    return from_date + timedelta(days=delta)


def parse_date(text: str, today: date) -> date | None:
    """Parse a date from member text. Returns None if nothing recognisable.

    The caller is responsible for checking the result is a Sunday -- this
    function reports what was written, not what would be convenient.
    """
    cleaned = normalize(text)

    if cleaned in {"today", "tonight"}:
        return today
    if cleaned == "tomorrow":
        return today + timedelta(days=1)
    if cleaned in {"sunday", "this sunday"}:
        return next_weekday(today, 6, allow_today=True)
    if cleaned == "next sunday":
        base = next_weekday(today, 6, allow_today=True)
        return base if base != today else base + timedelta(days=7)

    # 3/15 or 3/15/26 or 3-15-2026
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", cleaned)
    if m:
        month, day, year = int(m[1]), int(m[2]), m[3]
        if year is None:
            resolved = _with_year_rollover(today, month, day)
        else:
            y = int(year)
            resolved = _safe_date(y + 2000 if y < 100 else y, month, day)
        return resolved

    # "march 15" / "mar 15th"
    m = re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)?", cleaned)
    if m and m[1] in MONTHS:
        return _with_year_rollover(today, MONTHS[m[1]], int(m[2]))

    # "15 march"
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)\.?", cleaned)
    if m and m[2] in MONTHS:
        return _with_year_rollover(today, MONTHS[m[2]], int(m[1]))

    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _with_year_rollover(today: date, month: int, day: int) -> date | None:
    """A bare month/day means the next such date, not one in the past."""
    candidate = _safe_date(today.year, month, day)
    if candidate is None:
        return None
    if candidate < today:
        return _safe_date(today.year + 1, month, day)
    return candidate


def split_command(text: str) -> tuple[str, str]:
    """First word uppercased, plus the rest verbatim (case preserved)."""
    stripped = (text or "").strip()
    head, _, tail = stripped.partition(" ")
    return head.upper(), tail.strip()


def clean_name(text: str) -> str | None:
    name = re.sub(r"\s+", " ", (text or "").strip())
    name = re.sub(r"^(my name is|i'm|im|this is|it's|its)\s+", "", name, flags=re.I)
    name = name.strip(" .!,")
    if not name or len(name) > 80:
        return None
    # Tidy up "jane doe" and "JANE DOE", but leave anything containing digits
    # alone -- .title() would turn "3rd grade" into "3Rd Grade".
    if not any(ch.isdigit() for ch in name) and (name.islower() or name.isupper()):
        return name.title()
    return name
