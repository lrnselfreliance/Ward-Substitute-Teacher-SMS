"""Data model. Three core tables plus two small bookkeeping ones."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """Store aware UTC datetimes in SQLite without losing the timezone.

    SQLite has no native tz support, so naive values read back would silently
    become "local" and break every comparison. This normalises on the way in
    and re-attaches UTC on the way out.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime reached the database: {value!r}")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


# -- enums as plain strings; SQLite gains nothing from CHECK constraints here --

MALE, FEMALE = "M", "F"

OPEN, FILLED, CANCELLED, UNFILLED = "open", "filled", "cancelled", "unfilled"

PENDING, ACCEPTED, DECLINED, EXPIRED, SUPERSEDED = (
    "pending",
    "accepted",
    "declined",
    "expired",
    "superseded",
)

# Onboarding states. Consent comes first -- nothing is collected before it.
ASK_CONSENT = "ask_consent"
ASK_NAME = "ask_name"
ASK_ROLE = "ask_role"
ASK_GENDER = "ask_gender"
ASK_CLASS = "ask_class"
ASK_SUNDAYS = "ask_sundays"
CONFIRM = "confirm"
DONE = "done"

ALL_SUNDAYS = 0b11111


class Person(Base):
    __tablename__ = "person"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(80))
    gender: Mapped[str | None] = mapped_column(String(1))

    is_teacher: Mapped[bool] = mapped_column(Boolean, default=False)
    is_substitute: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    class_name: Mapped[str | None] = mapped_column(String(80))
    sundays: Mapped[int] = mapped_column(Integer, default=0)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    enroll_state: Mapped[str] = mapped_column(String(20), default=ASK_CONSENT)

    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consent_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    # Set when an unparseable reply needs a human to look at it.
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def enrolled(self) -> bool:
        return self.enroll_state == DONE

    def available_on(self, nth: int) -> bool:
        return bool(self.sundays & (1 << (nth - 1)))

    def __repr__(self) -> str:
        return f"<Person {self.name or '?'} {self.phone}>"


class Request(Base):
    __tablename__ = "request"

    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)
    service_date: Mapped[date] = mapped_column(Date, index=True)

    # Snapshot, not a join: if the teacher moves to another class in the fall,
    # last spring's records must still read the class they taught then.
    class_name: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(12), default=OPEN, index=True)
    filled_by_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    teacher: Mapped[Person] = relationship(foreign_keys=[teacher_id])
    filled_by: Mapped[Person | None] = relationship(foreign_keys=[filled_by_id])
    offers: Mapped[list["Offer"]] = relationship(back_populates="request")

    def __repr__(self) -> str:
        return f"<Request #{self.id} {self.service_date} {self.status}>"


class Offer(Base):
    __tablename__ = "offer"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("request.id"), index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), index=True)

    sent_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(12), default=PENDING, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    raw_reply: Mapped[str | None] = mapped_column(Text)
    reprompted: Mapped[bool] = mapped_column(Boolean, default=False)

    request: Mapped[Request] = relationship(back_populates="offers")
    person: Mapped[Person] = relationship()


class ProcessedMessage(Base):
    """Twilio retries on any non-2xx; dedupe on MessageSid."""

    __tablename__ = "processed_message"

    sid: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Notice(Base):
    """Guards once-per-occasion pushes (Saturday digest, last call)."""

    __tablename__ = "notice"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_notice"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(64))
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime)
