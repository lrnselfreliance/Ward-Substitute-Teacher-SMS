"""Every piece of outbound copy.

One file so wording can be iterated as a whole, tone stays consistent, and a
test can assert that member-facing messages fit a single 160-character SMS
segment. Admin listings are exempt -- they are inherently long, and admins
opted into that.
"""

from __future__ import annotations

from datetime import date

SEGMENT = 160

ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}


def pretty_date(d: date) -> str:
    return d.strftime("%a %b %-d")


def pretty_sundays(mask: int) -> str:
    if mask == 0b11111:
        return "all Sundays"
    days = [ORDINALS[i + 1] for i in range(5) if mask & (1 << i)]
    if not days:
        return "no Sundays"
    return "/".join(days) + " Sundays"


def describe(person) -> str:
    bits = [person.name or "?"]
    roles = []
    if person.is_teacher:
        roles.append("teacher")
    if person.is_substitute:
        roles.append("sub")
    if roles:
        bits.append(" & ".join(roles))
    bits.append("male" if person.gender == "M" else "female")
    if person.is_teacher and person.class_name:
        bits.append(person.class_name)
    if person.is_substitute:
        bits.append(pretty_sundays(person.sundays))
    return ", ".join(bits)


# -- onboarding ------------------------------------------------------------

def consent_prompt(org: str) -> str:
    """The opt-in moment.

    Deliberately two segments: carriers expect message-frequency and
    rate disclosure plus HELP/STOP wording at the point of consent, and this
    is sent once per person. A campaign reviewer who texts the number sees
    this first, before any information is collected.
    """
    return (
        f"{org} substitute finder. Do you consent to receive texts about "
        "substitute teaching? Msg frequency varies, msg & data rates may apply. "
        "Reply YES to join or STOP to opt out."
    )


CONSENT_DECLINED = (
    "No problem - you won't be texted again. Text this number anytime if you "
    "change your mind."
)

def opted_in(org: str) -> str:
    """Confirmation for someone re-subscribing who is already enrolled."""
    return (
        f"{org}: You are now opted in to substitute teaching texts. "
        "Msg&data rates may apply. Reply HELP for help, STOP to opt out."
    )


def opted_in_new(org: str) -> str:
    """Confirmation plus the first enrollment question, in one segment.

    Every opt-in path lands here -- texting START, texting YES cold, or
    replying YES to the consent prompt -- so the confirmation a carrier
    reviewer sees is the same however they arrive.
    """
    return (
        f"{org}: You are now opted in to substitute teaching texts. "
        "Msg&data rates may apply. Reply HELP for help, STOP to opt out. "
        "What's your name?"
    )


ASK_NAME_PROMPT = "What's your name?"
ASK_ROLE = "Are you a TEACHER or a SUBSTITUTE? (reply BOTH if both)"
ASK_GENDER = "Are you MALE or FEMALE? Subs are matched to teachers of the same gender."
ASK_CLASS = "What class do you teach?"
ASK_SUNDAYS = (
    "Which Sundays can you sub? Reply like 1 3 5, or ALL. "
    "(Only some months have a 5th Sunday.)"
)


def confirm(person) -> str:
    return f"Got it: {describe(person)}. Correct? YES or NO"


ENROLLED_TEACHER = (
    "You're all set. Text SUB and a date when you need a substitute. "
    "Text HELP anytime."
)
ENROLLED_SUB = "You're all set. I'll text you when someone needs a sub. Text HELP anytime."
RESTART = "No problem, let's start over. What's your name?"

BAD_ROLE = "Please reply TEACHER, SUBSTITUTE, or BOTH."
BAD_GENDER = "Please reply MALE or FEMALE."
BAD_SUNDAYS = "Please reply with Sunday numbers like 1 3 5, or ALL."
BAD_YESNO = "Sorry, please reply YES or NO."


# -- requests --------------------------------------------------------------


def request_ack(class_name: str | None, d: date) -> str:
    what = class_name or "your class"
    return f"Got it - sub needed for {what} on {pretty_date(d)}. I'll text you when someone accepts."


def request_ack_after_hours(class_name: str | None, d: date) -> str:
    what = class_name or "your class"
    return (
        f"Got it - sub needed for {what} on {pretty_date(d)}. "
        "It's after hours, so I'll start asking at 8am."
    )


def request_ack_late(class_name: str | None, d: date) -> str:
    """Two segments, deliberately. Honesty beats brevity here.

    A teacher who asks during Saturday quiet hours has left about an hour of
    Sunday morning to work with. Saying so now is the whole feature -- the
    failure is acceptable because it's disclosed, not discovered at 8:55am.
    """
    what = class_name or "your class"
    return (
        f"Got it - sub needed for {what} on {pretty_date(d)}. It's after hours so I "
        "can't start asking until 8am, which may not be enough time. I'd suggest "
        "calling someone directly tonight. I'll still try and will text you if "
        "anyone accepts."
    )


def offer(
    org: str, teacher_name: str, class_name: str | None, d: date, note: str | None
) -> str:
    """The recurring unsolicited message, so it carries brand and STOP.

    Messages a member did not just prompt need to identify the sender; this
    one also repeats the opt-out because it is the message they receive most.
    """
    what = class_name or "a class"
    extra = f" ({note})" if note else ""
    return (
        f"{org}: Can you sub for {teacher_name}'s {what} on {pretty_date(d)}?"
        f"{extra} Reply YES or NO, or STOP to opt out."
    )


def accepted_sub(
    org: str, teacher_name: str, phone: str, class_name: str | None, d: date
) -> str:
    what = class_name or "the class"
    return (
        f"{org}: Thank you! You're subbing {what} on {pretty_date(d)}. "
        f"{teacher_name}: {phone}"
    )


def accepted_teacher(org: str, sub_name: str, phone: str, d: date) -> str:
    return (
        f"{org}: {sub_name} can sub for you on {pretty_date(d)}. "
        f"Reach them at {phone}."
    )


TOO_LATE = "Thanks for replying! That one just got filled by someone else."


def superseded(org: str) -> str:
    return f"{org}: Never mind - that Sunday is covered now. Thanks!"


def unfilled_teacher(org: str, class_name: str | None, d: date) -> str:
    what = class_name or "your class"
    return (
        f"{org}: I couldn't find a sub for {what} on {pretty_date(d)} - everyone "
        "available has been asked. You'll need to arrange someone directly."
    )


def cancelled(d: date) -> str:
    return f"Cancelled your sub request for {pretty_date(d)}."


NOT_SUNDAY = "{given} is a {weekday}. Did you mean {suggestion}? Reply with the date."
NO_SUCH_REQUEST = "I don't have a sub request from you for that date."
NEED_CLASS = "What class do you teach? Reply CLASS followed by the name."


def class_set(name: str) -> str:
    return f"Your class is now {name}. Future requests will use it."


def sundays_set(mask: int) -> str:
    return f"Updated - you're available {pretty_sundays(mask)}."


PAUSED = "Paused. You won't be contacted until you text RESUME."
RESUMED = "Welcome back! You're active again."
STOPPED = "You've been removed and won't be texted again. Text START to rejoin."
UNKNOWN = "Sorry, I didn't understand. Text HELP for what I can do."

QUIET_BROADCAST = "Broadcasts only go out 8am-9pm. Try again in the morning."


# -- help ------------------------------------------------------------------

HELP_COMMON = ["HELP - this list", "STATUS - where things stand", "STOP - opt out"]
HELP_TEACHER = [
    "SUB <date> - request a sub",
    "CANCEL <date> - cancel a request",
    "WHO - who's subbing for you",
    "CLASS <name> - change your class",
]
HELP_SUB = [
    "YES / NO - answer a request",
    "SUNDAYS 1 3 / ALL - set availability",
    "MINE - dates you've committed to",
    "PAUSE / RESUME - pause being asked",
]
HELP_ADMIN = [
    "ADMIN OPEN / SUNDAY <date> / REQUEST <id>",
    "ADMIN ROSTER / FILL / MOVE / BROADCAST",
]


def help_for(person) -> str:
    """Role-aware: people only see commands they can actually use."""
    lines: list[str] = []
    if person.is_teacher:
        lines += HELP_TEACHER
    if person.is_substitute:
        lines += HELP_SUB
    lines += HELP_COMMON
    if person.is_admin:
        lines += HELP_ADMIN
    return "\n".join(lines)


HELP_STRANGER = "Text anything to sign up as a teacher or substitute."


# -- admin pushes ----------------------------------------------------------


def admin_unfilled(org: str, class_name: str | None, d: date, teacher: str) -> str:
    return (
        f"{org}: UNFILLED - {class_name or 'class'} on {pretty_date(d)} "
        f"({teacher}). Nobody left to ask."
    )


def admin_short_notice(org: str, class_name: str | None, d: date, teacher: str) -> str:
    return (
        f"{org}: Short notice - {teacher} needs a sub for "
        f"{class_name or 'class'} on {pretty_date(d)}."
    )


def admin_last_call(org: str, items: list[str]) -> str:
    body = "; ".join(items)
    return (
        f"{org}: Still no sub for {body}. I stop texting at 9pm and resume at "
        "8am - you may want to make some calls tonight."
    )


def admin_digest(org: str, items: list[str]) -> str:
    return f"{org}: Open for tomorrow - " + "; ".join(items)
