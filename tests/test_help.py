"""Per-command help: <COMMAND> HELP, HELP <COMMAND>, and the admin forms."""

from __future__ import annotations

from datetime import time

import pytest

from app import messages as M
from app.commands import ADMIN_ALIASES, ADMIN_COMMANDS, ALIASES, COMMANDS
from tests.conftest import SUBS, TEACHER

ADMIN = "+15551110000"


@pytest.fixture
def people(staffed):
    staffed.enroll(ADMIN, "Ada", admin=True, substitute=False)
    return staffed


def test_every_command_has_help():
    canonical = {c for c in COMMANDS if c not in ALIASES and c != "ADMIN"}
    assert canonical <= set(M.COMMAND_HELP), canonical - set(M.COMMAND_HELP)
    for sub in ADMIN_COMMANDS:
        assert M.admin_command_help(sub, time(21, 0), time(8, 0)), sub
    assert set(ADMIN_ALIASES.values()) <= set(ADMIN_COMMANDS)


def test_help_texts_fit_a_text_message():
    for name, text in M.COMMAND_HELP.items():
        assert len(text) <= 160, f"{name}: {len(text)} chars"
    for sub in ADMIN_COMMANDS:
        text = M.admin_command_help(sub, time(21, 0), time(8, 0))
        assert len(text) <= 200, f"ADMIN {sub}: {len(text)} chars"


@pytest.mark.parametrize("body", ["SUB HELP", "HELP SUB", "sub help", "help sub"])
def test_command_help_both_orders(people, body):
    people.sms(TEACHER, body)
    reply = people.last(TEACHER)
    assert reply.startswith("SUB asks for a substitute")
    assert "e.g. SUB 10/18" in reply


def test_help_is_role_aware(people):
    people.sms(SUBS[0], "SUB HELP")
    assert people.last(SUBS[0]).startswith("SUB is a teacher command.")
    people.sms(TEACHER, "MINE HELP")
    assert people.last(TEACHER).startswith("MINE is a substitute command.")
    people.sms(SUBS[0], "PAUSE HELP")
    assert people.last(SUBS[0]).startswith("PAUSE stops")


def test_yes_no_and_stop_have_help_without_acting(people):
    people.sms(TEACHER, "SUB 3/15")
    asked = sorted(people.texted(*SUBS[:6]))
    people.clear()
    people.sms(asked[0], "YES HELP")
    assert "first YES wins" in people.last(asked[0])
    assert "subbing" not in people.last(asked[0])  # it explained, it did not accept
    people.sms(asked[0], "STOP HELP")
    assert "START to come back" in people.last(asked[0])
    people.sms(asked[0], "STATUS")
    assert "Waiting on your YES/NO" in people.last(asked[0])  # still active, still pending


def test_admin_help_in_every_form(people):
    for body in ["ADMIN OPEN HELP", "HELP ADMIN OPEN", "ADMIN HELP OPEN", "admin open help"]:
        people.sms(ADMIN, body)
        assert people.last(ADMIN).startswith("ADMIN OPEN lists"), body
    people.sms(ADMIN, "ADMIN HELP")
    assert "leader guide" in people.last(ADMIN)
    assert "ADMIN OPEN HELP" in people.last(ADMIN)
    people.sms(ADMIN, "ADMIN CANCEL HELP")
    assert people.last(ADMIN).startswith("ADMIN WITHDRAW")
    people.sms(ADMIN, "ADMIN BROADCAST HELP")
    assert "8am-9pm" in people.last(ADMIN)


def test_non_admin_asking_admin_help_learns_nothing(people):
    people.sms(TEACHER, "HELP ADMIN OPEN")
    assert "didn't understand" in people.last(TEACHER)


def test_missing_argument_gets_the_help_text(people):
    people.sms(TEACHER, "SUB")
    assert people.last(TEACHER).startswith("SUB asks for")
    people.sms(TEACHER, "WITHDRAW")
    assert "Always include the date" in people.last(TEACHER)
    people.sms(ADMIN, "ADMIN BAN")
    assert people.last(ADMIN).startswith("ADMIN BAN <who>")


def test_main_help_mentions_withdraw_and_the_hint(people):
    people.sms(TEACHER, "HELP")
    reply = people.last(TEACHER)
    assert "WITHDRAW <date>" in reply
    assert "CANCEL" not in reply
    assert "SUB HELP" in reply


def test_withdraw_and_its_aliases(people):
    from sqlalchemy import select

    from app.models import CANCELLED, Request

    for word in ["WITHDRAW", "NEVERMIND", "nvm", "CANCEL"]:
        people.sms(TEACHER, "SUB 3/15")
        people.clear()
        people.sms(TEACHER, f"{word} 3/15")
        assert "Withdrawn: your sub request" in people.last(TEACHER), word
        with people.session_factory() as session:
            assert session.scalars(select(Request)).all()[-1].status == CANCELLED
    people.sms(ADMIN, "ADMIN SUB 3/22 3rd")
    people.sms(ADMIN, "ADMIN CANCEL 3/22 3rd")
    assert "Withdrew" in people.last(ADMIN)
