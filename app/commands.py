"""Member and admin command handlers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import messages as M
from .clock import to_local
from .models import (
    CANCELLED,
    DONE,
    FILLED,
    OPEN,
    PENDING,
    SUPERSEDED,
    UNFILLED,
    Offer,
    Person,
    Request,
)
from .parsers import clean_name, parse_date, parse_sundays, split_command


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+"):
        return digits if len(digits) >= 11 else None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def nearest_sunday(d: date) -> date:
    """Closest Sunday to a non-Sunday date, preferring the earlier one on ties."""
    back = (d.weekday() + 1) % 7
    previous = d - timedelta(days=back or 7)
    following = d + timedelta(days=(6 - d.weekday()) % 7)
    return previous if (d - previous) <= (following - d) else following


@dataclass
class Ctx:
    session: Session
    person: Person
    arg: str
    filler: object
    gateway: object
    clock: object
    config: object

    @property
    def today(self) -> date:
        return to_local(self.clock.now(), self.config.timezone).date()


# -- teacher ---------------------------------------------------------------


def _resolve_service_date(ctx: Ctx) -> tuple[date | None, str | None, str]:
    """Pull a date (and trailing note) off the argument.

    Returns (date, note, error_message). Tries the longest sensible prefix
    first so "march 15 3rd grade" doesn't lose its month name.
    """
    words = ctx.arg.split()
    if not words:
        return None, None, "Which Sunday? e.g. SUB 3/15"

    for take in (3, 2, 1):
        if take > len(words):
            continue
        parsed = parse_date(" ".join(words[:take]), ctx.today)
        if parsed:
            note = " ".join(words[take:]).strip() or None
            return parsed, note, ""

    return None, None, "I couldn't read that date. Try SUB 3/15"


def cmd_sub(ctx: Ctx) -> str:
    if not ctx.person.is_teacher:
        return "Only teachers can request a sub. Text HELP for what you can do."
    if not ctx.person.class_name:
        return M.NEED_CLASS

    service_date, note, error = _resolve_service_date(ctx)
    if error:
        return error

    if service_date.weekday() != 6:
        return M.NOT_SUNDAY.format(
            given=service_date.strftime("%b %-d"),
            weekday=service_date.strftime("%A"),
            suggestion=M.pretty_date(nearest_sunday(service_date)),
        )
    if service_date < ctx.today:
        return f"{M.pretty_date(service_date)} is in the past."

    existing = ctx.session.scalar(
        select(Request).where(
            Request.teacher_id == ctx.person.id,
            Request.service_date == service_date,
            Request.status.in_([OPEN, FILLED]),
        )
    )
    if existing:
        if existing.status == FILLED:
            return (
                f"{existing.filled_by.name} is already subbing for you on "
                f"{M.pretty_date(service_date)}."
            )
        return f"I'm already looking for a sub for you on {M.pretty_date(service_date)}."

    request = Request(
        teacher_id=ctx.person.id,
        service_date=service_date,
        class_name=ctx.person.class_name,
        note=note,
        status=OPEN,
        created_at=ctx.clock.now(),
    )
    ctx.session.add(request)
    ctx.session.flush()

    if ctx.filler.quiet.is_quiet(ctx.clock.now()):
        days = (service_date - ctx.today).days
        if days <= 1:
            return M.request_ack_late(request.class_name, service_date)
        return M.request_ack_after_hours(request.class_name, service_date)

    # Acknowledge before asking anyone. send_next_batch may itself text the
    # teacher (when nobody is eligible at all), and "I couldn't find a sub"
    # arriving ahead of "Got it" reads as a malfunction.
    ctx.gateway.send(ctx.person.phone, M.request_ack(request.class_name, service_date))
    ctx.filler.send_next_batch(ctx.session, request)
    return None


def cmd_cancel(ctx: Ctx) -> str:
    service_date, _, error = _resolve_service_date(ctx)
    if error:
        return "Which Sunday? e.g. CANCEL 3/15"

    request = ctx.session.scalar(
        select(Request).where(
            Request.teacher_id == ctx.person.id,
            Request.service_date == service_date,
            Request.status.in_([OPEN, FILLED]),
        )
    )
    if not request:
        return M.NO_SUCH_REQUEST

    request.status = CANCELLED
    for offer in request.offers:
        if offer.status == PENDING:
            offer.status = SUPERSEDED
            ctx.gateway.send(offer.person.phone, M.SUPERSEDED)
    if request.filled_by:
        ctx.gateway.send(request.filled_by.phone, M.SUPERSEDED)
    ctx.session.flush()
    return M.cancelled(service_date)


def cmd_who(ctx: Ctx) -> str:
    rows = ctx.session.scalars(
        select(Request)
        .where(
            Request.teacher_id == ctx.person.id,
            Request.service_date >= ctx.today,
            Request.status.in_([OPEN, FILLED]),
        )
        .order_by(Request.service_date)
    ).all()
    if not rows:
        return "You have no sub requests coming up."
    lines = []
    for r in rows:
        if r.status == FILLED:
            lines.append(
                f"{M.pretty_date(r.service_date)}: {r.filled_by.name} {r.filled_by.phone}"
            )
        else:
            lines.append(f"{M.pretty_date(r.service_date)}: still looking")
    return "\n".join(lines)


def cmd_class(ctx: Ctx) -> str:
    name = clean_name(ctx.arg)
    if not name:
        return "What class? e.g. CLASS 3rd grade"
    ctx.person.class_name = name
    ctx.person.is_teacher = True
    ctx.session.flush()
    return M.class_set(name)


# -- substitute ------------------------------------------------------------


def cmd_sundays(ctx: Ctx) -> str:
    mask = parse_sundays(ctx.arg)
    if mask is None:
        return M.BAD_SUNDAYS
    ctx.person.sundays = mask
    ctx.person.is_substitute = True
    ctx.session.flush()
    return M.sundays_set(mask)


def cmd_mine(ctx: Ctx) -> str:
    rows = ctx.session.scalars(
        select(Request)
        .where(
            Request.filled_by_id == ctx.person.id,
            Request.service_date >= ctx.today,
            Request.status == FILLED,
        )
        .order_by(Request.service_date)
    ).all()
    if not rows:
        return "You're not signed up to sub for anything right now."
    return "\n".join(
        f"{M.pretty_date(r.service_date)}: {r.class_name} for {r.teacher.name}"
        for r in rows
    )


# -- everyone --------------------------------------------------------------


def cmd_status(ctx: Ctx) -> str:
    parts: list[str] = []
    if ctx.person.is_teacher:
        parts.append(cmd_who(ctx))
    if ctx.person.is_substitute:
        pending = ctx.session.scalar(
            select(Offer).where(
                Offer.person_id == ctx.person.id, Offer.status == PENDING
            )
        )
        if pending:
            parts.append(
                f"Waiting on your YES/NO for {M.pretty_date(pending.request.service_date)}."
            )
        parts.append(cmd_mine(ctx))
    if not ctx.person.active:
        parts.append("You're paused. Text RESUME to start being asked again.")
    return "\n".join(parts) or "Nothing going on right now."


def cmd_pause(ctx: Ctx) -> str:
    ctx.person.active = False
    ctx.session.flush()
    return M.PAUSED


def cmd_resume(ctx: Ctx) -> str:
    ctx.person.active = True
    ctx.session.flush()
    return M.RESUMED


def cmd_help(ctx: Ctx) -> str:
    return M.help_for(ctx.person)


# -- admin -----------------------------------------------------------------


def _require_admin(ctx: Ctx) -> str | None:
    if not ctx.person.is_admin:
        return M.UNKNOWN
    return None


def cmd_admin(ctx: Ctx) -> str:
    denied = _require_admin(ctx)
    if denied:
        return denied

    sub, rest = split_command(ctx.arg)
    inner = Ctx(
        session=ctx.session,
        person=ctx.person,
        arg=rest,
        filler=ctx.filler,
        gateway=ctx.gateway,
        clock=ctx.clock,
        config=ctx.config,
    )
    handler = ADMIN_COMMANDS.get(sub)
    if not handler:
        return "ADMIN OPEN | SUNDAY <date> | REQUEST <id> | ROSTER | FILL | MOVE | BROADCAST"
    return handler(inner)


def _describe_request(r: Request) -> str:
    who = f" -> {r.filled_by.name}" if r.filled_by else ""
    return f"#{r.id} {M.pretty_date(r.service_date)} {r.class_name or '?'} ({r.teacher.name}) {r.status}{who}"


def admin_open(ctx: Ctx) -> str:
    rows = ctx.session.scalars(
        select(Request)
        .where(
            Request.status.in_([OPEN, UNFILLED]),
            Request.service_date >= ctx.today,
        )
        .order_by(Request.service_date)
    ).all()
    if not rows:
        return "Nothing open."
    return "\n".join(_describe_request(r) for r in rows)


def admin_sunday(ctx: Ctx) -> str:
    when = parse_date(ctx.arg, ctx.today)
    if not when:
        return "Which Sunday? e.g. ADMIN SUNDAY 3/15"
    rows = ctx.session.scalars(
        select(Request).where(Request.service_date == when).order_by(Request.id)
    ).all()
    if not rows:
        return f"No requests for {M.pretty_date(when)}."
    return "\n".join(_describe_request(r) for r in rows)


def admin_request(ctx: Ctx) -> str:
    if not ctx.arg.strip().lstrip("#").isdigit():
        return "Which request? e.g. ADMIN REQUEST 12"
    request = ctx.session.get(Request, int(ctx.arg.strip().lstrip("#")))
    if not request:
        return "No such request."
    lines = [_describe_request(request)]
    for offer in sorted(request.offers, key=lambda o: o.sent_at):
        stamp = to_local(offer.sent_at, ctx.config.timezone).strftime("%a %-I:%M%p")
        lines.append(f"  {offer.person.name}: {offer.status} (asked {stamp})")
    if len(lines) == 1:
        lines.append("  nobody asked yet")
    return "\n".join(lines)


def admin_roster(ctx: Ctx) -> str:
    people = ctx.session.scalars(
        select(Person).where(Person.enroll_state == DONE).order_by(Person.name)
    ).all()
    if not people:
        return "Nobody enrolled yet."
    lines = []
    for p in people:
        flags = "".join(
            [
                "T" if p.is_teacher else "",
                "S" if p.is_substitute else "",
                "A" if p.is_admin else "",
                "" if p.active else " (paused)",
            ]
        )
        lines.append(f"{p.name} [{flags}] {p.gender} {p.phone}")
    return "\n".join(lines)


def admin_fill(ctx: Ctx) -> str:
    words = ctx.arg.split()
    if len(words) < 2:
        return "ADMIN FILL <date> <name>"
    when = parse_date(words[0], ctx.today)
    if not when:
        return "I couldn't read that date."
    name = " ".join(words[1:]).strip()

    person = ctx.session.scalar(
        select(Person).where(Person.name.ilike(f"%{name}%"), Person.enroll_state == DONE)
    )
    if not person:
        return f"No enrolled person matching '{name}'."

    candidates = ctx.session.scalars(
        select(Request).where(
            Request.service_date == when, Request.status.in_([OPEN, UNFILLED])
        )
    ).all()
    if not candidates:
        return f"No open request for {M.pretty_date(when)}."
    if len(candidates) > 1:
        return "More than one open that day:\n" + "\n".join(
            _describe_request(r) for r in candidates
        )

    request = candidates[0]
    request.status = FILLED
    request.filled_by_id = person.id
    request.filled_at = ctx.clock.now()
    for offer in request.offers:
        if offer.status == PENDING:
            offer.status = SUPERSEDED
            ctx.gateway.send(offer.person.phone, M.SUPERSEDED)
    ctx.session.flush()
    return f"Recorded: {person.name} subbing {M.pretty_date(when)}."


def admin_move(ctx: Ctx) -> str:
    words = ctx.arg.split()
    if len(words) != 2:
        return "ADMIN MOVE <old number> <new number>"
    old, new = normalize_phone(words[0]), normalize_phone(words[1])
    if not old or not new:
        return "I couldn't read those numbers."
    person = ctx.session.scalar(select(Person).where(Person.phone == old))
    if not person:
        return f"Nobody at {old}."
    if ctx.session.scalar(select(Person).where(Person.phone == new)):
        return f"{new} already belongs to someone."
    person.phone = new
    ctx.session.flush()
    return f"Moved {person.name} to {new}. History kept."


def admin_broadcast(ctx: Ctx) -> str:
    body = ctx.arg.strip()
    if not body:
        return "ADMIN BROADCAST <message>"
    if ctx.filler.quiet.is_quiet(ctx.clock.now()):
        return M.QUIET_BROADCAST
    people = ctx.session.scalars(
        select(Person).where(Person.active.is_(True), Person.enroll_state == DONE)
    ).all()
    for p in people:
        if p.id != ctx.person.id:
            ctx.gateway.send(p.phone, body)
    return f"Sent to {len(people) - 1} people."


ADMIN_COMMANDS = {
    "OPEN": admin_open,
    "SUNDAY": admin_sunday,
    "REQUEST": admin_request,
    "ROSTER": admin_roster,
    "FILL": admin_fill,
    "MOVE": admin_move,
    "BROADCAST": admin_broadcast,
}

COMMANDS = {
    "SUB": cmd_sub,
    "CANCEL": cmd_cancel,
    "WHO": cmd_who,
    "CLASS": cmd_class,
    "SUNDAYS": cmd_sundays,
    "MINE": cmd_mine,
    "STATUS": cmd_status,
    "PAUSE": cmd_pause,
    "RESUME": cmd_resume,
    "ADMIN": cmd_admin,
}
