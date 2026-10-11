"""Admin CLI.

`subctl simulate` is the workhorse: it drives real conversations against the
real code with a fake gateway, so the whole app can be exercised long before a
phone number exists.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.clock import SystemClock, to_local
from app.commands import normalize_phone
from app.config import Config
from app.conversation import Router
from app.db import make_engine, make_session_factory
from app.fill import Filler
from app.gateway import FakeGateway, GuardedGateway, TwilioGateway
from app.halt import clear_halt, halted, set_halt
from app.messages import describe
from app.models import DONE, Person, Request
from app.ranking import eligible


def _wire(config: Config, *, real_sms: bool = False):
    """real_sms=True sends through Twilio when configured. Everything else
    uses the fake, which prints instead of sending."""
    engine = make_engine(config.db_path)
    session_factory = make_session_factory(engine)
    clock = SystemClock()
    if real_sms and config.twilio_configured:
        inner = TwilioGateway(
            config.twilio_account_sid, config.twilio_auth_token, config.twilio_from
        )
    else:
        inner = FakeGateway(clock=clock)
    gateway = GuardedGateway(inner, session_factory, config.db_path)
    filler = Filler(session_factory, gateway, clock, config)
    router = Router(session_factory, gateway, clock, config, filler)
    return session_factory, gateway, clock, filler, router


def cmd_people(args, config):
    session_factory, *_ = _wire(config)
    with session_factory() as session:
        people = session.scalars(select(Person).order_by(Person.name)).all()
        if not people:
            print("nobody enrolled")
            return
        for p in people:
            state = "" if p.enroll_state == DONE else f" [{p.enroll_state}]"
            admin = " ADMIN" if p.is_admin else ""
            paused = "" if p.active else " PAUSED"
            print(f"{p.id:>3} {p.phone:<15} {describe(p)}{admin}{paused}{state}")


def cmd_requests(args, config):
    session_factory, *_ = _wire(config)
    with session_factory() as session:
        rows = session.scalars(
            select(Request).order_by(Request.service_date.desc())
        ).all()
        for r in rows:
            who = f" -> {r.filled_by.name}" if r.filled_by else ""
            print(
                f"#{r.id:<4} {r.service_date} {r.status:<9} "
                f"{r.class_name or '?'} ({r.teacher.name}){who}"
            )


def cmd_offers(args, config):
    session_factory, *_ = _wire(config)
    with session_factory() as session:
        request = session.get(Request, args.request_id)
        if not request:
            sys.exit("no such request")
        print(f"#{request.id} {request.service_date} {request.status}")
        for o in sorted(request.offers, key=lambda o: o.sent_at):
            local = to_local(o.sent_at, config.timezone).strftime("%Y-%m-%d %H:%M")
            print(f"  {o.person.name:<20} {o.status:<11} asked {local}")


def cmd_rank(args, config):
    """Show who would be asked next, and why. Makes fairness auditable."""
    session_factory, *_ = _wire(config)
    with session_factory() as session:
        request = session.get(Request, args.request_id)
        if not request:
            sys.exit("no such request")
        for i, person in enumerate(eligible(session, request), 1):
            last = session.scalar(
                select(Request.service_date)
                .where(Request.filled_by_id == person.id)
                .order_by(Request.service_date.desc())
                .limit(1)
            )
            print(f"{i:>2}. {person.name:<20} last served: {last or 'never'}")


def cmd_admin(args, config):
    session_factory, *_ = _wire(config)
    phone = normalize_phone(args.phone)
    with session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == phone))
        if not person:
            sys.exit(f"nobody at {phone}")
        person.is_admin = not args.remove
        session.commit()
        print(f"{person.name}: admin={person.is_admin}")


def cmd_halt(args, config):
    session_factory, *_ = _wire(config)
    with session_factory() as session:
        set_halt(session, "server", " ".join(args.reason) or None, SystemClock().now())
        session.commit()
        print("HALTED. Nothing will be sent until `subctl unhalt` or ADMIN UNHALT.")


def cmd_unhalt(args, config):
    session_factory, *_ = _wire(config)
    with session_factory() as session:
        clear_halt(session, SystemClock().now())
        session.commit()
        print("resumed")


def cmd_status(args, config):
    session_factory, *_ = _wire(config)
    with session_factory() as session:
        state = halted(session)
        print(f"halted by {state.by}: {state.reason or '-'} (since {state.at})"
              if state else "running")
        banned = session.scalars(select(Person).where(Person.banned.is_(True))).all()
        for p in banned:
            print(f"banned: {p.phone} {p.name or '?'} ({p.ban_reason or '-'})")


def cmd_ban(args, config):
    # Real SMS: banning withdraws their offers and requests, and the people
    # already asked must actually be told to stand down.
    session_factory, gateway, clock, filler, router = _wire(config, real_sms=True)
    phone = normalize_phone(args.phone)
    with session_factory() as session:
        person = session.scalar(select(Person).where(Person.phone == phone))
        if person is None:
            person = Person(phone=phone, enroll_state="ask_consent", active=False,
                            sundays=0, created_at=clock.now())
            session.add(person)
            session.flush()
        person.banned = not args.remove
        person.banned_at = None if args.remove else clock.now()
        person.ban_reason = None if args.remove else (" ".join(args.reason) or None)
        person.banned_by_id = None
        if person.banned:
            filler.withdraw(session, person)
        session.commit()
        print(f"{person.name or phone}: banned={person.banned}")


def _refuse_live(args, config) -> None:
    """simulate and tick write real rows but send through a fake gateway.
    Against the database the server is using, that leaves state the server
    then acts on (or thinks it already did). Require an explicit opt-in."""
    if args.db is None and not args.live:
        sys.exit(
            f"refusing to run against the configured database ({config.db_path}).\n"
            "Pass --db <copy.db> to use a scratch copy, or --live if you really "
            "mean the running service's data."
        )


def cmd_simulate(args, config):
    """Send a message as if it arrived from a phone, and print the replies."""
    _refuse_live(args, config)
    session_factory, gateway, clock, filler, router = _wire(config)
    phone = normalize_phone(args.phone)
    router.handle(phone, " ".join(args.text))
    for msg in gateway.sent:
        print(f"-> {msg.to}: {msg.body}")


def cmd_tick(args, config):
    _refuse_live(args, config)
    session_factory, gateway, clock, filler, router = _wire(config)
    filler.tick()
    for msg in gateway.sent:
        print(f"-> {msg.to}: {msg.body}")
    if not gateway.sent:
        print("(nothing to do)")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="subctl")
    parser.add_argument("--db", default=None, help="override DB_PATH")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("people").set_defaults(func=cmd_people)
    sub.add_parser("requests").set_defaults(func=cmd_requests)
    p = sub.add_parser("tick", help="run one scheduler tick (scratch DB only unless --live)")
    p.add_argument("--live", action="store_true")
    p.set_defaults(func=cmd_tick)
    sub.add_parser("status", help="halt state and bans").set_defaults(func=cmd_status)
    sub.add_parser("unhalt", help="lift an emergency stop").set_defaults(func=cmd_unhalt)

    p = sub.add_parser("halt", help="emergency stop: send nothing to anyone")
    p.add_argument("reason", nargs="*")
    p.set_defaults(func=cmd_halt)

    p = sub.add_parser("ban", help="ignore a phone number entirely")
    p.add_argument("phone")
    p.add_argument("reason", nargs="*")
    p.add_argument("--remove", action="store_true", help="unban")
    p.set_defaults(func=cmd_ban)

    p = sub.add_parser("offers")
    p.add_argument("request_id", type=int)
    p.set_defaults(func=cmd_offers)

    p = sub.add_parser("rank", help="who would be asked next for a request")
    p.add_argument("request_id", type=int)
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("admin", help="grant or revoke admin")
    p.add_argument("phone")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_admin)

    p = sub.add_parser("simulate", help="drive a conversation locally (scratch DB only unless --live)")
    p.add_argument("--live", action="store_true")
    p.add_argument("phone")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_simulate)

    args = parser.parse_args(argv)
    config = Config.from_env()
    if args.db:
        config = Config(**{**config.__dict__, "db_path": args.db})
    args.func(args, config)


if __name__ == "__main__":
    main()
