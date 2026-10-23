"""Every piece of outbound copy.

One file so wording can be iterated as a whole, tone stays consistent, and a
test can assert that member-facing messages fit a single 160-character SMS
segment. Admin listings are exempt -- they are inherently long, and admins
opted into that.
"""

from __future__ import annotations

from datetime import date, time

SEGMENT = 160

ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}


def clock_time(t: time) -> str:
    """08:00 -> '8am', 21:30 -> '9:30pm'. For copy that mentions quiet hours,
    so a changed window never leaves the texts describing the old one."""
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}{suffix}" if t.minute == 0 else f"{hour}:{t.minute:02d}{suffix}"


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


def request_ack_after_hours(class_name: str | None, d: date, resume: time) -> str:
    what = class_name or "your class"
    return (
        f"Got it - sub needed for {what} on {pretty_date(d)}. "
        f"It's after hours, so I'll start asking at {clock_time(resume)}."
    )


def request_ack_late(class_name: str | None, d: date, resume: time) -> str:
    """Two segments, deliberately. Honesty beats brevity here.

    A teacher who asks during Saturday quiet hours has left about an hour of
    Sunday morning to work with. Saying so now is the whole feature -- the
    failure is acceptable because it's disclosed, not discovered at 8:55am.
    """
    what = class_name or "your class"
    return (
        f"Got it - sub needed for {what} on {pretty_date(d)}. It's after hours so I "
        f"can't start asking until {clock_time(resume)}, which may not be enough time. I'd suggest "
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


def request_on_behalf(org: str, admin_name: str, class_name: str | None, d: date) -> str:
    """Tell a teacher an admin asked for a sub on their behalf."""
    what = class_name or "your class"
    return (
        f"{org}: {admin_name} asked me to find a sub for {what} on "
        f"{pretty_date(d)}. I'll text you when someone accepts."
    )


def filled_requester(org: str, sub_name: str, teacher_name: str, d: date) -> str:
    """Close the loop for the admin who filed a request for someone else."""
    return (
        f"{org}: {sub_name} will sub for {teacher_name} on {pretty_date(d)}. "
        "Both have each other's number."
    )


def superseded(org: str) -> str:
    return f"{org}: Never mind - that Sunday is covered now. Thanks!"


def unfilled_teacher(org: str, class_name: str | None, d: date) -> str:
    what = class_name or "your class"
    return (
        f"{org}: I couldn't find a sub for {what} on {pretty_date(d)} - everyone "
        "available has been asked. You'll need to arrange someone directly."
    )


def cancelled(d: date) -> str:
    return f"Withdrawn: your sub request for {pretty_date(d)}. Anyone who was asked has been told."


NOT_SUNDAY = "{given} is a {weekday}. Did you mean {suggestion}? Reply with the date."
NO_SUCH_REQUEST = "I don't have a sub request from you for that date."
NEED_CLASS = "What class do you teach? Reply CLASS followed by the name."


def class_set(name: str) -> str:
    return f"Your class is now {name}. Future requests will use it."


def sundays_set(mask: int) -> str:
    return f"Updated - you're available {pretty_sundays(mask)}."


PAUSED = "Paused. You won't be contacted until you text RESUME."
PAUSED_TOO_LATE = (
    "You're paused, so that request has moved on to someone else. "
    "Text RESUME to be asked again."
)
RESUMED = "Welcome back! You're active again."
def stopped(org: str) -> str:
    return (
        f"{org}: You have been unsubscribed and will not receive any more "
        "messages. Reply START to rejoin."
    )

UNKNOWN = "Sorry, I didn't understand. Text HELP for what I can do."

def quiet_broadcast(start: time, end: time) -> str:
    return (
        f"Broadcasts only go out {clock_time(end)}-{clock_time(start)}. "
        "Try again in the morning."
    )


# -- safety switches -------------------------------------------------------

HALTED_CONFIRM = (
    "HALTED. No texts will go out to anyone until ADMIN UNHALT. "
    "ADMIN OPEN, ROSTER, WITHDRAW and BAN still work."
)
HALTED_REPLY = "Service is halted - nothing is being sent. ADMIN UNHALT to resume."
ADMIN_HALTED_BLOCK = (
    "Blocked while halted: that would start texting people. ADMIN UNHALT first."
)


def halt_status(by: str, reason: str | None) -> str:
    why = f" ({reason})" if reason else ""
    return f"SERVICE HALTED by {by}{why}. ADMIN UNHALT to resume."


def halted_notice(org: str, admin: str, reason: str | None) -> str:
    why = f": {reason}" if reason else ""
    return f"{org}: {admin} HALTED the service{why}. Nothing will be sent until ADMIN UNHALT."


def unhalted_notice(org: str, admin: str) -> str:
    return f"{org}: {admin} resumed the service. Texting is back on."


def banned_notice(org: str, admin: str, who: str, reason: str | None) -> str:
    why = f": {reason}" if reason else ""
    return f"{org}: {admin} banned {who}{why}. ADMIN UNBAN to reverse."


def unbanned_notice(org: str, admin: str, who: str) -> str:
    return f"{org}: {admin} unbanned {who}."


def cancelled_on_behalf(org: str, admin: str, d: date) -> str:
    return f"{org}: {admin} cancelled your sub request for {pretty_date(d)}."


# -- help ------------------------------------------------------------------

HELP_COMMON = ["HELP - this list", "STATUS - where things stand"]
HELP_TEACHER = [
    "SUB <date> - request a sub",
    "WITHDRAW <date> - no longer need a sub",
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
    "ADMIN SUB <date> <teacher> - request for a teacher",
    "ADMIN OPEN - what still needs a sub",
    "ADMIN HALT - emergency stop all texting",
    "ADMIN HELP - full guide for leaders",
]

ADMIN_USAGE = (
    "ADMIN SUB <date> <teacher> | WITHDRAW <date> <teacher> | OPEN | SUNDAY <date> | "
    "REQUEST <id> | ROSTER | FILL <date> <name> | MOVE <old> <new> | "
    "BROADCAST <msg> | BAN <who> | UNBAN <who> | HALT | UNHALT. "
    "Text ADMIN HELP for a plain-English guide."
)


def admin_guide(org: str, quiet_start: time, quiet_end: time) -> str:
    """Plain-English walkthrough for non-technical leaders. Long by design."""
    waking = f"{clock_time(quiet_end)}-{clock_time(quiet_start)}"
    quiet = f"{clock_time(quiet_start)}-{clock_time(quiet_end)}"
    return "\n".join(
        [
            f"{org} leader guide. Start every command with ADMIN.",
            "",
            "ADMIN SUB 10/18 nursery - find a sub for a teacher. Name them by "
            "first name, last name, class, or last 4 digits of their phone. "
            "Add a note after, e.g. ADMIN SUB 10/18 jayn out of town",
            "ADMIN OPEN - every request still waiting for a sub",
            "ADMIN SUNDAY 10/18 - all requests for that Sunday",
            "ADMIN REQUEST 12 - who was asked about request #12 and what they said",
            "ADMIN ROSTER - everyone signed up, with their number for commands",
            "ADMIN FILL 10/18 sam - record a sub you arranged in person, so the "
            "rotation stays fair",
            "ADMIN MOVE <old phone> <new phone> - someone got a new number",
            f"ADMIN BROADCAST <message> - text everyone ({waking} only)",
            "ADMIN WITHDRAW 10/18 nursery - take back a teacher's request",
            "",
            "Any command + HELP explains it, e.g. ADMIN OPEN HELP.",
            "",
            "If something goes wrong:",
            "ADMIN HALT - emergency stop. Nothing is sent to anyone until "
            "ADMIN UNHALT. You can still use OPEN, ROSTER, WITHDRAW and BAN.",
            "ADMIN BAN jayn being rude - the service ignores that person "
            "completely until ADMIN UNBAN. Use a full phone number for "
            "someone who never signed up. Admins can't be banned by text.",
            "",
            "Subs are asked a few at a time, longest-since-served first, with "
            f"more asked as Sunday gets closer. Nothing is sent {quiet}. You are "
            "texted automatically when a request cannot be filled.",
        ]
    )


HELP_FOOTER = "Msg&data rates may apply. Reply STOP to opt out."
HELP_HINT = "Any command + HELP explains it, e.g. SUB HELP."


# -- per-command help --------------------------------------------------------
#
# One text each. Each says what the command does, gives one real example and
# names the one gotcha, if there is one. Looked up by the canonical command
# name; aliases resolve to it first.

COMMAND_HELP = {
    "SUB": (
        "SUB asks for a substitute for a Sunday; your class is on file. "
        "e.g. SUB 10/18, or SUB 10/18 lesson is in the closet. "
        "Must be a Sunday. Ask early."
    ),
    "WITHDRAW": (
        "WITHDRAW 10/18 takes back a request you no longer need; anyone asked "
        "is told. Careful: CANCEL by itself unsubscribes you. Always include the date."
    ),
    "WHO": (
        "WHO lists your upcoming requests and who is subbing for each, with "
        "their number. Nothing to add after it."
    ),
    "CLASS": (
        "CLASS changes the class on file for your requests. e.g. CLASS CTR 7. "
        "Past requests keep the class they had."
    ),
    "SUNDAYS": (
        "SUNDAYS sets which Sundays of the month you can sub. e.g. SUNDAYS 1 3 5, "
        "or SUNDAYS ALL. You are only asked about Sundays you chose."
    ),
    "MINE": (
        "MINE lists the Sundays you have agreed to sub, with the teacher and "
        "class. Nothing to add after it."
    ),
    "STATUS": (
        "STATUS sums up where things stand for you: open requests, who is "
        "subbing, and any request waiting on your YES or NO."
    ),
    "PAUSE": (
        "PAUSE stops you being asked for a while, e.g. a holiday. A request you "
        "are holding moves to the next person. RESUME when you are back."
    ),
    "RESUME": "RESUME ends a PAUSE so you are asked again.",
    "YES": (
        "YES accepts the request you were just sent; the first YES wins and "
        "you get the teacher's number. If someone beat you to it, you are told."
    ),
    "NO": (
        "NO declines the request you were just sent so it moves to the next "
        "person at once. Silence is not a no, but a quick NO is kinder."
    ),
    "STOP": (
        "STOP leaves the service for good: no more texts. Text START to come "
        "back. To take a break instead, use PAUSE."
    ),
    "HELP": "HELP lists your commands. " + HELP_HINT,
}

# Which role a command belongs to, for a friendly note when the wrong role
# asks. Commands missing here are for everyone.
COMMAND_ROLE = {
    "SUB": "teacher", "WITHDRAW": "teacher", "WHO": "teacher", "CLASS": "teacher",
    "SUNDAYS": "substitute", "MINE": "substitute", "YES": "substitute", "NO": "substitute",
}


def command_help(name: str, person) -> str | None:
    text = COMMAND_HELP.get(name)
    if text is None:
        return None
    role = COMMAND_ROLE.get(name)
    if role == "teacher" and not person.is_teacher:
        return f"{name} is a teacher command. {text}"
    if role == "substitute" and not person.is_substitute:
        return f"{name} is a substitute command. {text}"
    return text


def admin_command_help(name: str, quiet_start: time, quiet_end: time) -> str | None:
    waking = f"{clock_time(quiet_end)}-{clock_time(quiet_start)}"
    texts = {
        "SUB": (
            "ADMIN SUB <date> <teacher> [note] files a request for a teacher. "
            "Name them by first or last name, class, last 4 digits, or #number "
            "from ROSTER. e.g. ADMIN SUB 10/18 nursery out of town"
        ),
        "WITHDRAW": (
            "ADMIN WITHDRAW <date> <teacher> takes back a teacher's request; "
            "they and anyone asked are told. e.g. ADMIN WITHDRAW 10/18 nursery"
        ),
        "OPEN": (
            "ADMIN OPEN lists every request still waiting for a sub, soonest "
            "first, with its #number. Nothing to add after it."
        ),
        "SUNDAY": (
            "ADMIN SUNDAY <date> lists every request for that Sunday and how "
            "each stands. e.g. ADMIN SUNDAY 10/18"
        ),
        "REQUEST": (
            "ADMIN REQUEST <#> shows who was asked about one request, when, and "
            "what they said. Get the # from ADMIN OPEN. e.g. ADMIN REQUEST 12"
        ),
        "ROSTER": (
            "ADMIN ROSTER lists everyone enrolled: #number, roles (T teacher, "
            "S sub, A admin), class, phone. Use the #number in other commands."
        ),
        "FILL": (
            "ADMIN FILL <date> <name> records a sub you arranged in person so "
            "turns stay fair; both are texted. e.g. ADMIN FILL 10/18 sam"
        ),
        "MOVE": (
            "ADMIN MOVE <old phone> <new phone> moves someone to a new number "
            "and keeps their history. e.g. ADMIN MOVE 8015551234 8015556789"
        ),
        "BROADCAST": (
            f"ADMIN BROADCAST <message> texts everyone active, {waking} only. "
            "e.g. ADMIN BROADCAST Primary meeting moved to 10am"
        ),
        "BAN": (
            "ADMIN BAN <who> [reason] makes the service ignore that person until "
            "ADMIN UNBAN. Name, class, last 4 digits, or a full phone number for "
            "a stranger. Admins can't be banned."
        ),
        "UNBAN": (
            "ADMIN UNBAN <who> lifts a ban; they come back exactly as they were. "
            "e.g. ADMIN UNBAN jeff"
        ),
        "HALT": (
            "ADMIN HALT [reason] stops ALL texting until ADMIN UNHALT. OPEN, "
            "ROSTER, WITHDRAW and BAN still work. Other admins are told. For emergencies."
        ),
        "UNHALT": (
            "ADMIN UNHALT resumes the service after a HALT and tells you who "
            "texted while it was stopped."
        ),
        "HELP": (
            "ADMIN HELP is the leader guide. Any admin command + HELP explains "
            "it, e.g. ADMIN OPEN HELP."
        ),
    }
    return texts.get(name)


def help_for(person, org: str) -> str:
    """Role-aware: people only see commands they can actually use.

    Every variant is topped with the brand and tailed with the rate and
    opt-out disclosure, which is what carriers check a HELP reply for.
    """
    lines: list[str] = [f"{org} Substitute Finder:"]
    if person.is_teacher:
        lines += HELP_TEACHER
    if person.is_substitute:
        lines += HELP_SUB
    lines += HELP_COMMON
    if person.is_admin:
        lines += HELP_ADMIN
    lines.append(HELP_HINT)
    lines.append(HELP_FOOTER)
    return "\n".join(lines)




# -- admin pushes ----------------------------------------------------------


def admin_unfilled(org: str, class_name: str | None, d: date, teacher: str) -> str:
    return (
        f"{org}: UNFILLED - {class_name or 'class'} on {pretty_date(d)} "
        f"({teacher}). Nobody left to ask."
    )




def admin_last_call(org: str, items: list[str], quiet_start: time, quiet_end: time) -> str:
    body = "; ".join(items)
    return (
        f"{org}: Still no sub for {body}. I stop texting at {clock_time(quiet_start)} "
        f"and resume at {clock_time(quiet_end)} - you may want to make some calls tonight."
    )


def admin_digest(org: str, items: list[str]) -> str:
    return f"{org}: Open for tomorrow - " + "; ".join(items)
