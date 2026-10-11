"""The HALT switch: an admin-operated emergency stop.

While halted, nothing is sent to anyone except replies to admins and STOP
confirmations, the fill tick does not run, and member texts are dropped
(and counted, so the admin who unhalts knows who to follow up with).

The switch is a database row, not process memory: a restart must not
silently resume a service someone stopped on purpose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from .models import Setting

KEY = "halt"


@dataclass(frozen=True)
class HaltState:
    by: str
    reason: str | None
    at: datetime


def halted(session: Session) -> HaltState | None:
    row = session.get(Setting, KEY)
    if not row or not row.value:
        return None
    data = json.loads(row.value)
    return HaltState(by=data["by"], reason=data.get("reason"), at=row.updated_at)


def set_halt(session: Session, by: str, reason: str | None, now: datetime) -> None:
    row = session.get(Setting, KEY)
    value = json.dumps({"by": by, "reason": reason})
    if row:
        row.value, row.updated_at = value, now
    else:
        session.add(Setting(key=KEY, value=value, updated_at=now))
    session.flush()


def clear_halt(session: Session, now: datetime) -> None:
    row = session.get(Setting, KEY)
    if row:
        row.value, row.updated_at = None, now
        session.flush()


def is_halted(session_factory) -> bool:
    """Convenience for callers that do not already hold a session."""
    with session_factory() as session:
        return halted(session) is not None
