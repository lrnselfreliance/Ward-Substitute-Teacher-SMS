from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.clock import FrozenClock
from app.config import Config
from app.conversation import Router
from app.db import make_engine, make_session_factory
from app.fill import Filler
from app.gateway import FakeGateway
from app.models import DONE, MALE, Person

TZ = ZoneInfo("America/Denver")


class App:
    """Test harness: a whole app wired to a fake gateway and a frozen clock."""

    def __init__(self) -> None:
        self.config = Config(db_path=":memory:", timezone=TZ, validate_signature=False)
        engine = make_engine(":memory:")
        self.session_factory = make_session_factory(engine)
        self.clock = FrozenClock(datetime(2026, 3, 2, 15, 0, tzinfo=TZ))
        self.gateway = FakeGateway(clock=self.clock)
        self.filler = Filler(
            self.session_factory, self.gateway, self.clock, self.config
        )
        self.router = Router(
            self.session_factory, self.gateway, self.clock, self.config, self.filler
        )

    # -- time ---------------------------------------------------------------

    def at(self, stamp: str) -> None:
        """Set the clock using *local* wall time, e.g. '2026-03-07 20:00'."""
        naive = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
        self.clock.set(naive.replace(tzinfo=TZ))

    def advance(self, **kwargs) -> None:
        self.clock.advance(**kwargs)

    # -- driving ------------------------------------------------------------

    def sms(self, phone: str, body: str, sid: str | None = None) -> None:
        self.router.handle(phone, body, sid)

    def tick(self) -> None:
        self.filler.tick()

    # -- setup --------------------------------------------------------------

    def enroll(
        self,
        phone: str,
        name: str,
        *,
        gender: str = MALE,
        teacher: bool = False,
        substitute: bool = True,
        class_name: str | None = None,
        sundays: int = 0b11111,
        admin: bool = False,
    ) -> int:
        with self.session_factory() as session:
            person = Person(
                phone=phone,
                name=name,
                gender=gender,
                is_teacher=teacher,
                is_substitute=substitute,
                is_admin=admin,
                class_name=class_name,
                sundays=sundays,
                active=True,
                enroll_state=DONE,
                created_at=self.clock.now(),
                consent_at=self.clock.now(),
            )
            session.add(person)
            session.commit()
            return person.id

    # -- assertions ---------------------------------------------------------

    def outbox(self, phone: str) -> list[str]:
        return [m.body for m in self.gateway.to(phone)]

    def last(self, phone: str) -> str:
        msgs = self.outbox(phone)
        assert msgs, f"no messages sent to {phone}"
        return msgs[-1]

    def count(self, phone: str) -> int:
        return len(self.outbox(phone))

    def clear(self) -> None:
        self.gateway.clear()

    def texted(self, *phones: str) -> set[str]:
        """Which of the given phones received anything since the last clear."""
        return {m.to for m in self.gateway.sent} & set(phones)


@pytest.fixture
def app() -> App:
    return App()


TEACHER = "+15550000001"
SUBS = [f"+1555000{i:04d}" for i in range(100, 120)]


@pytest.fixture
def staffed(app: App):
    """A male teacher plus six male substitutes, all available every Sunday."""
    app.enroll(TEACHER, "Tom Teacher", teacher=True, substitute=False,
               class_name="3rd grade")
    for i, phone in enumerate(SUBS[:6]):
        app.enroll(phone, f"Sub{i}")
    return app
