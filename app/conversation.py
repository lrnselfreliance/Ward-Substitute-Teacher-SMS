"""Inbound message routing and the onboarding state machine.

Routing order matters and is fixed:

    1. STOP / START / HELP        compliance and help always win
    2. unknown phone              begin onboarding
    3. onboarding in progress     feed the FSM
    4. explicit command keyword   command parser
    5. pending offer              yes/no parser
    6. otherwise                  "I didn't understand"

Step 4 preceding step 5 is deliberate: a teacher holding a pending offer can
still text SUB 3/22 without it reading as a decline.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import messages as M
from .commands import COMMANDS, Ctx
from .models import (
    ASK_CLASS,
    ASK_GENDER,
    ASK_CONSENT,
    ASK_NAME,
    ASK_ROLE,
    ASK_SUNDAYS,
    CONFIRM,
    DONE,
    EXPIRED,
    PENDING,
    SUPERSEDED,
    Offer,
    Person,
    ProcessedMessage,
)
from .parsers import (
    clean_name,
    normalize,
    parse_gender,
    parse_role,
    parse_sundays,
    parse_yes_no,
    split_command,
)

log = logging.getLogger(__name__)

STOP_WORDS = {"stop", "unsubscribe", "cancel me", "quit", "end", "stopall"}
START_WORDS = {"start", "unstop", "resubscribe"}


class Router:
    def __init__(self, session_factory, gateway, clock, config, filler) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.clock = clock
        self.config = config
        self.filler = filler

    # -- entry point --------------------------------------------------------

    def handle(self, phone: str, body: str, sid: str | None = None) -> None:
        with self.session_factory() as session:
            try:
                if sid and self._already_seen(session, sid):
                    return
                reply = self._route(session, phone, body or "")
                if reply:
                    self.gateway.send(phone, reply)
                session.commit()
            except Exception:
                session.rollback()
                log.exception("failed handling message from %s", phone)
                self.gateway.send(phone, M.UNKNOWN)

    def _already_seen(self, session: Session, sid: str) -> bool:
        if session.get(ProcessedMessage, sid):
            return True
        session.add(ProcessedMessage(sid=sid, received_at=self.clock.now()))
        session.flush()
        return False

    # -- routing ------------------------------------------------------------

    def _route(self, session: Session, phone: str, body: str) -> str | None:
        text = normalize(body)
        person = session.scalar(select(Person).where(Person.phone == phone))

        # 1. compliance
        if text in STOP_WORDS:
            if person:
                person.active = False
                self._release_pending(session, person)
            return M.STOPPED
        if text in START_WORDS:
            if person:
                person.active = True
                return M.RESUMED
            person = self._create(session, phone, body)
            return self._consent_prompt()
        if text == "help":
            if person and person.enrolled:
                return M.help_for(person)
            if person:
                return self._prompt_for(person)
            person = self._create(session, phone, body)
            return self._consent_prompt()

        # 2. brand new number
        if person is None:
            self._create(session, phone, body)
            return self._consent_prompt()

        # 3. mid-onboarding
        if not person.enrolled:
            return self._onboard(session, person, body)

        # 4. explicit command
        head, arg = split_command(body)
        handler = COMMANDS.get(head)
        if handler:
            ctx = Ctx(
                session=session,
                person=person,
                arg=arg,
                filler=self.filler,
                gateway=self.gateway,
                clock=self.clock,
                config=self.config,
            )
            return handler(ctx)

        # 5. answer to an outstanding offer
        offer = session.scalar(
            select(Offer).where(
                Offer.person_id == person.id, Offer.status == PENDING
            )
        )
        if offer:
            return self._answer_offer(session, offer, body)

        # A YES arriving just after the slot was filled, or after the offer
        # expired, deserves a real explanation rather than "I didn't
        # understand" -- from their side they answered a question we asked.
        if parse_yes_no(body) is not None:
            recent = session.scalar(
                select(Offer)
                .where(
                    Offer.person_id == person.id,
                    Offer.status.in_([SUPERSEDED, EXPIRED]),
                    Offer.sent_at >= self.clock.now() - timedelta(days=3),
                )
                .order_by(Offer.sent_at.desc())
            )
            if recent:
                return M.TOO_LATE

        # 6. no idea
        return M.UNKNOWN

    # -- helpers ------------------------------------------------------------

    def _create(self, session: Session, phone: str, body: str) -> Person:
        now = self.clock.now()
        person = Person(
            phone=phone,
            enroll_state=ASK_CONSENT,
            created_at=now,
            # consent_at is stamped when they actually say YES, not merely
            # when they first text -- that is the moment the reviewer and any
            # future audit care about.
            consent_at=None,
            consent_text=body,
            active=True,
            sundays=0,
        )
        session.add(person)
        session.flush()
        return person

    def _release_pending(self, session: Session, person: Person) -> None:
        offers = session.scalars(
            select(Offer).where(Offer.person_id == person.id, Offer.status == PENDING)
        ).all()
        for offer in offers:
            offer.status = "expired"

    def _answer_offer(self, session: Session, offer: Offer, body: str) -> str:
        answer = parse_yes_no(body)
        if answer is None:
            if not offer.reprompted:
                offer.reprompted = True
                return M.BAD_YESNO
            # Second unparseable reply: leave it pending to expire naturally
            # and flag a human rather than guessing.
            offer.person.needs_attention = True
            self.filler.notify_admins(
                session,
                f"Couldn't parse a reply from {offer.person.name}: \"{body.strip()}\"",
            )
            return M.BAD_YESNO
        if answer:
            return self.filler.accept(session, offer, body)
        self.filler.decline(session, offer, body)
        request = offer.request
        if not session.scalar(
            select(Offer).where(
                Offer.request_id == request.id, Offer.status == PENDING
            )
        ):
            self.filler.send_next_batch(session, request)
        return "Thanks for letting me know."

    # -- onboarding ---------------------------------------------------------

    def _consent_prompt(self) -> str:
        return M.consent_prompt(self.config.org_name)

    def _prompt_for(self, person: Person) -> str:
        return {
            ASK_CONSENT: self._consent_prompt(),
            ASK_NAME: M.ASK_NAME_PROMPT,
            ASK_ROLE: M.ASK_ROLE,
            ASK_GENDER: M.ASK_GENDER,
            ASK_CLASS: M.ASK_CLASS,
            ASK_SUNDAYS: M.ASK_SUNDAYS,
            CONFIRM: M.confirm(person),
        }.get(person.enroll_state, self._consent_prompt())

    def _after_gender(self, person: Person) -> str:
        return ASK_CLASS if person.is_teacher else ASK_SUNDAYS

    def _after_class(self, person: Person) -> str:
        return ASK_SUNDAYS if person.is_substitute else CONFIRM

    def _onboard(self, session: Session, person: Person, body: str) -> str:
        state = person.enroll_state

        if state == ASK_CONSENT:
            answer = parse_yes_no(body)
            if answer is None:
                return self._consent_prompt()
            if not answer:
                person.active = False
                return M.CONSENT_DECLINED
            person.active = True
            person.consent_at = self.clock.now()
            person.enroll_state = ASK_NAME
            return M.ASK_NAME_PROMPT

        if state == ASK_NAME:
            name = clean_name(body)
            if not name:
                return M.ASK_NAME_PROMPT
            person.name = name
            person.enroll_state = ASK_ROLE
            return M.ASK_ROLE

        if state == ASK_ROLE:
            role = parse_role(body)
            if role is None:
                return M.BAD_ROLE
            person.is_teacher, person.is_substitute = role
            person.enroll_state = ASK_GENDER
            return M.ASK_GENDER

        if state == ASK_GENDER:
            gender = parse_gender(body)
            if gender is None:
                return M.BAD_GENDER
            person.gender = gender
            person.enroll_state = self._after_gender(person)
            return M.ASK_CLASS if person.enroll_state == ASK_CLASS else M.ASK_SUNDAYS

        if state == ASK_CLASS:
            name = clean_name(body)
            if not name:
                return M.ASK_CLASS
            person.class_name = name
            person.enroll_state = self._after_class(person)
            return (
                M.ASK_SUNDAYS
                if person.enroll_state == ASK_SUNDAYS
                else M.confirm(person)
            )

        if state == ASK_SUNDAYS:
            mask = parse_sundays(body)
            if mask is None:
                return M.BAD_SUNDAYS
            person.sundays = mask
            person.enroll_state = CONFIRM
            return M.confirm(person)

        if state == CONFIRM:
            answer = parse_yes_no(body)
            if answer is None:
                return M.BAD_YESNO
            if not answer:
                person.name = None
                person.gender = None
                person.class_name = None
                person.sundays = 0
                person.is_teacher = person.is_substitute = False
                person.enroll_state = ASK_NAME
                return M.RESTART
            person.enroll_state = DONE
            session.flush()
            return M.ENROLLED_TEACHER if person.is_teacher else M.ENROLLED_SUB

        return self._consent_prompt()
