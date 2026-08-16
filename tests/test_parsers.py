from datetime import date

import pytest

from app.parsers import (
    clean_name,
    nth_sunday,
    parse_date,
    parse_gender,
    parse_role,
    parse_sundays,
    parse_yes_no,
)

TODAY = date(2026, 3, 2)  # a Monday


@pytest.mark.parametrize(
    "text", ["y", "Yes", "YEP", "sure", "ok", "Okay!", "1", "yeah", "I can", "👍"]
)
def test_yes(text):
    assert parse_yes_no(text) is True


@pytest.mark.parametrize(
    "text", ["n", "No", "NOPE", "can't", "cant", "busy", "sorry", "2", "👎"]
)
def test_no(text):
    assert parse_yes_no(text) is False


@pytest.mark.parametrize("text", ["maybe", "what?", "who is this", "", "yes but"])
def test_neither(text):
    assert parse_yes_no(text) is None


def test_gender():
    assert parse_gender("male") == "M"
    assert parse_gender("F") == "F"
    assert parse_gender("banana") is None


def test_role():
    assert parse_role("teacher") == (True, False)
    assert parse_role("substitute") == (False, True)
    assert parse_role("BOTH") == (True, True)
    assert parse_role("hello") is None


def test_sundays():
    assert parse_sundays("1 3 5") == 0b10101
    assert parse_sundays("ALL") == 0b11111
    assert parse_sundays("2") == 0b00010
    assert parse_sundays("1,3") == 0b00101
    assert parse_sundays("nope") is None


def test_sundays_ignores_prose_digits():
    assert parse_sundays("I have 2 kids so probably not") is None


def test_nth_sunday():
    assert nth_sunday(date(2026, 3, 1)) == 1
    assert nth_sunday(date(2026, 3, 8)) == 2
    assert nth_sunday(date(2026, 3, 29)) == 5


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3/15", date(2026, 3, 15)),
        ("3-15", date(2026, 3, 15)),
        ("3/15/26", date(2026, 3, 15)),
        ("march 15", date(2026, 3, 15)),
        ("Mar 15th", date(2026, 3, 15)),
        ("15 March", date(2026, 3, 15)),
        ("this sunday", date(2026, 3, 8)),
        ("tomorrow", date(2026, 3, 3)),
    ],
)
def test_dates(text, expected):
    assert parse_date(text, TODAY) == expected


def test_bare_date_rolls_forward_a_year():
    """February when it's already March means next February."""
    assert parse_date("2/1", TODAY) == date(2027, 2, 1)


def test_nonsense_date():
    assert parse_date("sometime soon", TODAY) is None
    assert parse_date("13/45", TODAY) is None


def test_clean_name():
    assert clean_name("  jane doe ") == "Jane Doe"
    assert clean_name("My name is Bob") == "Bob"
    assert clean_name("") is None
