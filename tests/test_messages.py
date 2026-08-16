"""Copy hygiene.

Every member-facing message should fit one 160-character SMS segment. Going
over is not an error -- it just costs another segment and can arrive split --
so anything longer must be a deliberate, listed exception.
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

from app import messages as M

SUNDAY = date(2026, 3, 15)


class _Person:
    """Worst case for confirm(): every field populated."""

    name = "Katherine Wetherington"
    gender = "F"
    is_teacher = True
    is_substitute = True
    class_name = "Kindergarten"
    sundays = 0b11111

# Deliberately longer than one segment: the honesty is worth the extra cost.
# consent_prompt carries the frequency/rate/HELP/STOP disclosure carriers
# expect at the opt-in moment, and is sent once per person.
MULTI_SEGMENT_BY_DESIGN = {"request_ack_late", "consent_prompt"}

RENDERED = {
    "consent_prompt": M.consent_prompt("Clover Leaf Ward"),
    "CONSENT_DECLINED": M.CONSENT_DECLINED,
    "ASK_NAME_PROMPT": M.ASK_NAME_PROMPT,
    "ASK_ROLE": M.ASK_ROLE,
    "ASK_GENDER": M.ASK_GENDER,
    "ASK_CLASS": M.ASK_CLASS,
    "ASK_SUNDAYS": M.ASK_SUNDAYS,
    "ENROLLED_TEACHER": M.ENROLLED_TEACHER,
    "ENROLLED_SUB": M.ENROLLED_SUB,
    "RESTART": M.RESTART,
    "BAD_ROLE": M.BAD_ROLE,
    "BAD_GENDER": M.BAD_GENDER,
    "BAD_SUNDAYS": M.BAD_SUNDAYS,
    "BAD_YESNO": M.BAD_YESNO,
    "TOO_LATE": M.TOO_LATE,
    "SUPERSEDED": M.SUPERSEDED,
    "PAUSED": M.PAUSED,
    "RESUMED": M.RESUMED,
    "STOPPED": M.STOPPED,
    "UNKNOWN": M.UNKNOWN,
    "QUIET_BROADCAST": M.QUIET_BROADCAST,
    "NEED_CLASS": M.NEED_CLASS,
    "NO_SUCH_REQUEST": M.NO_SUCH_REQUEST,
    "confirm": M.confirm(_Person()),
    "request_ack": M.request_ack("3rd grade", SUNDAY),
    "request_ack_after_hours": M.request_ack_after_hours("3rd grade", SUNDAY),
    "offer": M.offer("Tom Teacher", "3rd grade", SUNDAY, None),
    "accepted_sub": M.accepted_sub("Tom Teacher", "+15551234567", "3rd grade", SUNDAY),
    "accepted_teacher": M.accepted_teacher("Sam Sub", "+15551234567", SUNDAY),
    "unfilled_teacher": M.unfilled_teacher("3rd grade", SUNDAY),
    "cancelled": M.cancelled(SUNDAY),
    "class_set": M.class_set("5th grade"),
    "sundays_set": M.sundays_set(0b10101),
    "admin_unfilled": M.admin_unfilled("3rd grade", SUNDAY, "Tom Teacher"),
    "admin_short_notice": M.admin_short_notice("3rd grade", SUNDAY, "Tom Teacher"),
}


@pytest.mark.parametrize(
    "name,text",
    sorted((k, v) for k, v in RENDERED.items() if k not in MULTI_SEGMENT_BY_DESIGN),
)
def test_fits_one_segment(name, text):
    assert len(text) <= M.SEGMENT, f"{name} is {len(text)} chars: {text!r}"


def test_long_message_is_a_declared_exception():
    text = M.request_ack_late("3rd grade", SUNDAY)
    assert len(text) > M.SEGMENT
    assert "request_ack_late" in MULTI_SEGMENT_BY_DESIGN


def test_consent_prompt_carries_the_required_disclosures():
    """A campaign reviewer texting the number sees this first."""
    text = M.consent_prompt("Clover Leaf Ward")
    assert "Clover Leaf Ward" in text
    assert "consent" in text.lower()
    assert "msg frequency varies" in text.lower()
    assert "rates may apply" in text.lower()
    assert "STOP" in text


def test_every_public_message_is_covered():
    """A new message must be added to RENDERED, not silently unchecked."""
    public = {
        name
        for name, value in vars(M).items()
        if not name.startswith("_")
        and name.isupper()
        and isinstance(value, str)
        and name != "SEGMENT"
    }
    functions = {
        name
        for name, value in vars(M).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and name not in {"pretty_date", "pretty_sundays", "describe", "help_for"}
    }
    missing = (public | functions) - set(RENDERED) - MULTI_SEGMENT_BY_DESIGN
    # NOT_SUNDAY is a template filled in at call time.
    missing -= {"NOT_SUNDAY", "HELP_STRANGER", "admin_last_call", "admin_digest"}
    assert not missing, f"add these to RENDERED: {sorted(missing)}"


def test_sunday_formatting():
    assert M.pretty_sundays(0b11111) == "all Sundays"
    assert M.pretty_sundays(0b00101) == "1st/3rd Sundays"
    assert M.pretty_date(SUNDAY) == "Sun Mar 15"
