# Substitute — SMS-only substitute teacher finder

A small service that fills substitute-teacher slots for a church group entirely over SMS.
No app, no login, no web UI for members. Dozens of users.

## Decisions locked in

| Question | Decision |
|---|---|
| Who starts a request | The teacher texts in (e.g. `SUB 3/15`) |
| Ask pacing | Small batches of ~3, first accept wins, window narrows as the date nears |
| Stack | Python + SQLite, Docker container, DB on a mounted volume |
| SMS | Twilio, self-hosted webhook |
| Fairness | Last-served date; non-responders stay in the queue |
| Class | Set once per teacher at onboarding, changeable by text |
| Roles | teacher / substitute / both, plus admin |

---

## 1. Domain model

### Person
One row per phone number. A person may be teacher, substitute, or both; independently,
they may be an admin.

```
person
  id                integer pk
  phone             text unique        -- E.164, +15551234567
  name              text
  gender            text               -- 'M' | 'F'
  is_teacher        bool
  is_substitute     bool
  is_admin          bool               -- orthogonal to the above
  class_name        text null          -- teachers only, e.g. "3rd grade"
  sundays           integer            -- bitmask, bit 0 = 1st Sunday .. bit 4 = 5th
  active            bool               -- false after STOP or PAUSE
  enroll_state      text               -- see §4; 'done' when enrolled
  consent_at        timestamp
  created_at        timestamp
```

`sundays` as a 5-bit mask makes eligibility a bitwise AND. `ALL` = 31.

### Request
A teacher needs cover for one date.

```
request
  id                integer pk
  teacher_id        -> person
  service_date      date               -- must be a Sunday
  class_name        text               -- SNAPSHOT of teacher.class_name at creation
  note              text null          -- optional extra from the teacher
  status            text               -- open | filled | cancelled | unfilled
  filled_by         -> person null
  filled_at         timestamp null
  created_at        timestamp
```

**`class_name` is copied onto the request, not joined from the teacher.** If a teacher
moves from 3rd grade to 5th grade in the fall, last spring's records must still read "3rd
grade." Joining live would silently rewrite history.

### Offer
One text asking one substitute about one request. The audit trail.

```
offer
  id                integer pk
  request_id        -> request
  person_id         -> person
  sent_at           timestamp
  expires_at        timestamp
  status            text               -- pending | accepted | declined | expired | superseded
  responded_at      timestamp null
  raw_reply         text null          -- what they actually typed, for parser debugging
```

Two derived values, computed from these tables rather than stored as counters that drift:

- `last_served_date` = `MAX(request.service_date)` where `filled_by = person.id`
- `last_asked_at` = `MAX(offer.sent_at)` for that person

---

## 2. Eligibility and ranking

Eligible for request `R` when **all** hold:

1. `active` and `is_substitute`
2. `gender == R.teacher.gender`
3. `sundays` bit for `nth_sunday(R.service_date)` is set
4. No prior offer for `R` — never ask the same person twice about the same date
5. Not already `filled_by` on another request for the same `service_date`
6. No other **pending** offer outstanding right now (see §4)

```sql
ORDER BY last_served_date ASC NULLS FIRST,   -- never served goes first
         last_asked_at    ASC NULLS FIRST,   -- see below
         times_served     ASC,
         (person.id * 2654435761 + request.id) % 1000   -- stable tiebreak
```

### Why `last_asked_at` is the second key

You asked that a substitute who never responds still return to the queue for future
Sundays — never penalized, never dropped. Agreed, and that's the right call socially.

But it breaks naive LRU. A member who never replies keeps `last_served_date = NULL`
forever, so they sort into the **very front of every batch, permanently**. They'd occupy
one of three slots in every request the system ever makes, while people who would actually
say yes wait behind them.

Sorting by `last_asked_at` within each served-tier fixes it without any penalty concept.
Being asked moves you to the back of *your own tier*; it never removes you, never marks
you, and you cycle back around as soon as everyone else in that tier has been asked. A
chronic non-responder gets asked exactly as often as anyone else at their service level —
just not first every single time.

No "strikes," no deactivation. Silence is treated as a non-event, exactly as you specified.

`nth_sunday(d) = ((d.day - 1) // 7) + 1`. Fifth Sundays occur in only 4–5 months a year —
say so in the onboarding prompt.

---

## 3. The fill loop

```
teacher texts "SUB 3/15"
  └─ create request, class_name copied from teacher's profile
  └─ reply: "Got it — sub needed for 3rd grade, Sun Mar 15. I'll text you when I find someone."
  └─ send batch #1: rank eligible → take N → create pending offers → send

on inbound YES:
  UPDATE request SET status='filled', filled_by=? WHERE id=? AND status='open'
  ├─ rowcount 1 → they won
  │    ├─ text sub:     teacher's name + phone + date + class
  │    ├─ text teacher: sub's name + phone
  │    └─ sibling pending offers → 'superseded', "already filled, thanks"
  └─ rowcount 0 → beaten; "That one just got filled — thanks anyway."

on inbound NO:
  mark declined; if no pending offers remain and request still open → next batch

on timer tick (every minute):
  expire offers past expires_at        (no penalty — see §2)
  open request with zero pending       → next batch
  no eligible people left              → status='unfilled'
                                       → text teacher, text all admins
```

The conditional `UPDATE ... WHERE status='open'` is the entire concurrency story. First
accept wins atomically; no locking to reason about.

### Batch size and response window

The window narrows as the date approaches — early on, fairness is affordable; on Saturday
night, filling the slot is all that matters.

| Days until service | Batch size | Offer TTL |
|---|---|---|
| 8+ | 3 | 12 h |
| 3–7 | 3 | 6 h |
| 2 | 4 | 3 h |
| 1 (Saturday) | all remaining | 90 min |
| Day of | all remaining | 45 min |

### Quiet hours

**No sub request ever goes out between 9pm and 8am local.** A teacher texting `SUB` at
10pm creates the request immediately; the first batch sends at 8am. No exceptions, not
even day-of.

### Late requests may simply fail — and that's fine

A teacher who requests a sub during Saturday-night quiet hours has left the system roughly
one hour of Sunday morning to work with. **Failing to fill that is an acceptable outcome,
not a bug.** The app still tries at 8am, but it makes no heroic effort and pages nobody at
midnight.

This is a load-bearing simplification. It's what lets the design drop the 6am-override
path, the emergency admin wake-up, and every "but what if" branch that would otherwise
grow around the last twelve hours before a service. The system is a fair, patient
scheduler — not an incident-response pager.

What it *must* do is be honest at the moment of the request, so the teacher isn't relying
on something that probably won't come through:

> "Got it — sub needed for 3rd grade tomorrow. It's after hours, so I can't start asking
> until 8am, which may not be enough time. I'd suggest calling someone directly tonight.
> I'll still try, and I'll text you the moment anyone accepts."

That message is the whole feature. The failure is acceptable *because* it's disclosed
immediately rather than discovered at 8:55am.

Boundaries are config (`QUIET_START=21:00`, `QUIET_END=08:00`); adjust the evening edge to
taste.

Three rules make this behave:

1. **TTL clocks pause during quiet hours.** An offer sent at 8:50pm with a 6h window must
   not expire at 2:50am while its recipient is asleep — that would burn through the whole
   substitute list overnight and mark everyone as non-responsive. Elapsed time counts only
   during waking hours.
2. **Quiet hours gate *proactive* messages only** — offers, digests, broadcasts,
   reminders. A **direct reply to an inbound message is never delayed.** If someone texts
   the service at 11pm, the service answers at 11pm: onboarding questions, `HELP`,
   `STATUS`, error messages. Silence would just look broken.
3. **Tell the teacher what's happening.** The acknowledgment must say *when* asking
   starts, or a teacher who texts at 10pm and sees nothing overnight will assume the
   service is dead and start calling people manually:

   > "Got it — sub needed for 3rd grade, Sun Mar 15. It's after hours, so I'll start
   > asking at 8am and text you as soon as someone accepts."

**One open call:** when a sub accepts at 11pm, the confirmation to the teacher is
proactive, not a reply. I'd send it immediately anyway — it closes a loop the teacher
opened and is worrying about, and good news at 11pm beats uncertainty. Flag if you'd
rather it hold until 8am.

---

## 4. The conversation engine

Every inbound message routes through one function. Order matters:

```
1. STOP/UNSUBSCRIBE/HELP        → compliance + help, always wins
2. unknown phone                → begin onboarding
3. onboarding in progress       → feed the enroll FSM
4. explicit command keyword     → command parser
5. pending offer for this phone → yes/no parser
6. otherwise                    → "Sorry, I didn't understand. Text HELP for options."
```

Commands beating yes/no (4 before 5) lets a teacher with a pending offer still text
`SUB 3/22` without it reading as a decline.

### Onboarding FSM

```
ASK_NAME     "Welcome! What's your name?"
ASK_ROLE     "Are you a TEACHER or a SUBSTITUTE? (reply BOTH if both)"
ASK_GENDER   "Are you MALE or FEMALE? (subs are matched to teachers of the same gender)"
ASK_CLASS    "What class do you teach?"                        [teachers only]
ASK_SUNDAYS  "Which Sundays can you sub? Reply like 1 3 5, or ALL.
              (Only some months have a 5th Sunday.)"           [substitutes only]
CONFIRM      "Got it: Jane Doe, teacher & sub, female, 3rd grade, 1st/3rd Sundays.
              Correct? YES/NO"
DONE
```

`NO` at CONFIRM restarts at ASK_NAME. Answers persist as they're given, so a dropped
conversation resumes in place.

### Parsing

- **Yes**: `y yes yeah yep yup sure ok okay i can 1 👍`
- **No**: `n no nope cant can't busy sorry unable 2 👎`
- **Unrecognized reply to an offer**: reprompt once — "Sorry, reply YES or NO." A second
  unrecognized reply leaves it pending to expire, and flags an admin.
- **Dates**: `3/15`, `3/15/26`, `march 15`, `this sunday`, `next sunday`. Never guess a
  non-Sunday — "Mar 16 is a Monday. Did you mean Sun Mar 15?"

### One pending offer per person

Eligibility rule 6 means nobody holds two outstanding offers at once, so a bare `YES` is
never ambiguous. Cost: someone sitting on a stale offer skips the next request until it
expires. TTL bounds it; acceptable at this scale.

---

## 5. Commands, and role-aware HELP

`HELP` returns only what the sender can actually use, assembled from their role flags.
An admin who also teaches gets all three blocks.

**Everyone**
| Text | Effect |
|---|---|
| `HELP` | This list |
| `STATUS` | Your current situation, phrased for your role |
| `PAUSE` / `RESUME` | Temporarily stop being contacted |
| `STOP` | Full opt-out |

**Teachers**
| Text | Effect |
|---|---|
| `SUB <date> [note]` | Request a sub for that Sunday |
| `CANCEL <date>` | Cancel; pending offerees are told |
| `WHO` | Who's subbing for you, and when |
| `CLASS <name>` | Change your class |

**Substitutes**
| Text | Effect |
|---|---|
| `YES` / `NO` | Answer the current request |
| `SUNDAYS 1 3` / `SUNDAYS ALL` | Update availability |
| `MINE` | Dates you've committed to |

**Admins**
| Text | Effect |
|---|---|
| `ADMIN SUNDAY <date>` | Every request that date + fill status |
| `ADMIN OPEN` | All unfilled/in-progress requests, soonest first |
| `ADMIN REQUEST <id>` | Full offer history: who was asked, who declined, who's pending |
| `ADMIN ROSTER` | Everyone, with role, class, availability, last served |
| `ADMIN FILL <date> <name>` | Record a fill arranged in person |
| `ADMIN MOVE <old> <new>` | Repoint a member to a new phone number |
| `ADMIN BROADCAST <msg>` | Message everyone active |

Two of these matter more than they look:

- **`ADMIN FILL`** — people arrange swaps in the hallway. If those never reach the
  database, the fairness ordering slowly becomes fiction.
- **`ADMIN MOVE`** — `person` is keyed on phone. Without this, a member who changes
  numbers re-onboards as a stranger and silently loses their served history.

### Admins don't have to ask

Pull commands cover "what's the state of things," but admins shouldn't have to poll for
the case that matters. Push automatically:

- A request goes `unfilled` (nobody left to ask) → text every admin immediately
- A request is created for a date less than 48h out → text admins, since that likely needs
  a human working the phones in parallel
- **Saturday 5pm** → digest of any still-open requests for tomorrow
- **Saturday 8pm** → any request still open gets an alert: *"Still no sub for 3rd grade
  tomorrow. I stop texting at 9pm and resume at 8am — you may want to make some calls
  tonight."*

The 8pm alert exists for requests that have been **open for days** and the system failed
to fill — that's a genuine miss worth a human's evening. A request created *during*
Saturday quiet hours doesn't trigger it: the teacher was already told at request time that
it probably wouldn't fill (§3), so there's nothing new to report and no reason to page
anyone. Sort on `created_at`, not just `status`.

---

## 6. Application layout

```
substitute/
  app/
    main.py            FastAPI: POST /sms, GET /health
    config.py          env-driven settings
    db.py              engine, session, schema migrations
    models.py          SQLAlchemy models
    conversation.py    inbound router + onboarding FSM
    parsers.py         yes/no, dates, sundays, names
    commands.py        member + admin command handlers
    ranking.py         eligibility + ordering
    fill.py            batches, offers, expiry, the timer tick
    messages.py        every piece of outbound copy
    gateway.py         TwilioGateway + FakeGateway
    clock.py           the only place datetime.now() is allowed
  cli/subctl.py
  tests/
  data/                → volume mount, holds substitute.db
  Dockerfile
  compose.yml
```

**`messages.py` holding all copy in one file** is worth the indirection here. You'll
iterate wording constantly, tone consistency matters when it's a church group reading it,
and it gives you one place to assert every message fits in a single 160-character segment.

- **FastAPI + uvicorn**, single worker (see below)
- **SQLAlchemy 2.0** over SQLite
- **APScheduler** in-process for the one-minute tick
- **pytest** with `FakeGateway` and a frozen clock

---

## 7. Docker, SQLite, and deployment

```yaml
# compose.yml
services:
  substitute:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: America/Denver          # adjust — see below
    volumes:
      - ./data:/data              # substitute.db lives here
    ports:
      - "127.0.0.1:8080:8080"     # tunnel terminates in front of this
```

Dockerfile: `python:3.14-slim`, non-root user owning `/data`, `uvicorn app.main:app
--workers 1`.

**Run exactly one worker.** Two workers means two APScheduler instances racing to send the
same batch twice, plus SQLite write contention. One worker is enormously more than enough
for dozens of users, and it makes the concurrency story trivial.

**SQLite settings:** WAL mode, `busy_timeout=5000`, foreign keys on. A local bind mount is
fine for WAL. **Never put this DB on NFS or a network share** — WAL locking breaks in ways
that corrupt rather than error.

**`TZ` is load-bearing, not cosmetic.** Quiet hours, `nth_sunday()`, and "tomorrow" all
depend on local church time. Store every timestamp in UTC; convert only for display and
scheduling decisions. A container defaulting to UTC would compute the wrong nth-Sunday for
any Saturday-evening request.

**Backups:** nightly `VACUUM INTO /data/backups/substitute-YYYYMMDD.db` from the scheduler
(safe on a live DB, unlike `cp`), keep 30 days, and copy off-box. The whole DB is a few
hundred KB — this is nearly free, and it's the roster plus the entire fairness history.

### Inbound: polling removes the public-URL requirement

**No domain is required.** Two ways for messages to reach the app:

| | `poll` (default) | `webhook` |
|---|---|---|
| Direction | App calls Twilio | Twilio calls app |
| Needs public URL | No | Yes |
| Needs domain / cert / tunnel | No | Yes |
| Attack surface | None (no route registered) | Public POST endpoint |
| Works behind CGNAT | Yes | No |
| Latency | One poll interval (~20s) | Instant |

Polling lists messages with `direction == "inbound"` and a `DateSent` lower
bound, then feeds each into the same router. Every connection is outbound
HTTPS — identical to sending, which the app already does. Nothing about the
home network changes.

Two details make it safe:

- **Overlapping windows are harmless.** The poll window deliberately overlaps;
  the same `MessageSid` dedupe that guards webhook retries absorbs the repeats.
- **The first poll absorbs history without acting on it.** Otherwise pointing
  the poller at an existing number would replay every message the account ever
  received — re-running onboarding and re-answering old offers for everyone at
  once.

Latency is the only cost, and it is nominal here: the tightest deadline in the
system is a 45-minute batch window.

### Exposing the webhook (only if you choose webhook mode)

**Cloudflare Tunnel** — no port forwarding, no dynamic DNS, TLS handled, home IP stays
private. Caddy + Let's Encrypt if you'd rather own the chain.

Worth naming plainly: this is used Saturday night and Sunday morning. A home power or
internet blip then takes it down exactly when it matters. A $5/mo VPS removes that failure
mode; the app is identical either way, only the tunnel target changes.

### Non-negotiables for a public webhook

1. **Validate `X-Twilio-Signature` on every request.** The endpoint is internet-reachable;
   without this, anyone who finds it can impersonate any member's phone number. Single
   most important security line in the project.
2. **Idempotency** — Twilio retries on non-2xx. Key on `MessageSid`, ignore duplicates.
3. Return 204 fast; do sending work after the response.

---

## 8. A2P 10DLC — start this first

US carriers require registration for application-to-person SMS on a 10-digit long code;
Twilio has blocked unregistered A2P traffic since 2023.

**Consent and registration are different systems.** Members texting in first is a flawless
consent record — but carriers can't see it. They see traffic from a long code and filter on
heuristics. The Campaign Registry is the channel for saying "this is a church scheduling
tool." Classification is by *transport*, not content: anything API-sent is A2P.

The failure mode is silent and partial — some carriers drop, others deliver, the API may
report success. You'd find out as "the Verizon subs never got asked."

| Item | Cost |
|---|---|
| Brand registration (Sole Prop *or* Low Volume Standard) | $4 one-time |
| Campaign vetting | $15 one-time |
| Campaign monthly | ~$1.50–$10/mo |
| Carrier surcharge | $0.003–$0.005 per message |
| **Campaign review time** | **currently 10–15 days** |

With a church EIN, register **Low Volume Standard** — same $4, better throughput, tied to
the organization rather than to you. Without one, **Sole Proprietor** works but needs OTP
verification against your personal phone and caps at 3,000 segments/day. Projected volume
is ~200 messages/month, so limits are irrelevant.

### Number continuity

The service number is permanent — members are told it once, ever. Twilio does not
auto-release numbers; even a suspended account keeps them 90 days. Rotation is a spam
tactic (snowshoeing) that filtering hunts for; one stable number building reputation is an
asset.

- Auto-recharge **with a backup payment method** — an expired card going unnoticed is the
  only realistic way to lose the number.
- Account owned and billed by **the church, not a personal card**, or continuity is
  coupled to one volunteer.
- Upgrade off the Twilio trial before onboarding anyone.

**The one thing that would force a number change:** 10DLC long code and toll-free are
different numbers. Decide before publicizing. Recommended: local 10DLC. Don't hand the
number out until the campaign is approved.

### Consent record

The member texting in first *is* the opt-in. Store `consent_at` and the verbatim first
message. Honor `STOP` immediately and permanently.

---

## 9. Testing

The conversation engine is where the bugs will live, so make it the cheap thing to test.

- `FakeGateway` records outbound messages instead of calling Twilio
- `FrozenClock` injected everywhere; no `datetime.now()` outside `clock.py`
- Table-driven parser tests over dozens of real-world spellings
- Scenario tests that read like transcripts:

```python
def test_first_accept_wins(app):
    app.sms("+1555TEACH", "SUB 3/15")
    assert app.outbox_to("+1555SUB1").last.contains("3rd grade")
    app.sms("+1555SUB2", "yes")
    app.sms("+1555SUB1", "yes")
    assert app.outbox_to("+1555SUB1").last.contains("just got filled")
    assert app.outbox_to("+1555TEACH").last.contains("+1555SUB2")

def test_silence_does_not_penalize(app):
    app.sms("+1555TEACH", "SUB 3/15")
    app.advance(hours=13)                      # SUB1 never replies, offer expires
    app.sms("+1555TEACH", "SUB 3/22")
    assert app.outbox_to("+1555SUB1").count == 2   # asked again, no penalty
```

```python
def test_night_request_holds_until_morning(app):
    app.at("2026-03-09 22:00")                 # Monday 10pm
    app.sms("+1555TEACH", "SUB 3/15")
    assert app.outbox_to("+1555TEACH").last.contains("8am")
    assert app.outbox_to("+1555SUB1").count == 0
    app.at("2026-03-10 07:59"); app.tick()
    assert app.outbox_to("+1555SUB1").count == 0
    app.at("2026-03-10 08:00"); app.tick()
    assert app.outbox_to("+1555SUB1").count == 1

def test_replies_are_never_delayed(app):
    app.at("2026-03-09 23:30")
    app.sms("+1555NEW", "hi")                  # unknown number, onboarding
    assert app.outbox_to("+1555NEW").count == 1
```

- Property test on the ranker: after N simulated fills, the spread between most- and
  least-used substitute stays within 1
- A test that no offer is ever sent with a timestamp inside quiet hours — assert it over
  the whole simulated-year property run, not just one case
- A test that every string in `messages.py` fits one SMS segment

---

## 10. Build order

| Phase | Deliverable |
|---|---|
| **0** | File A2P 10DLC registration. Before any code. |
| **1** | Schema, `subctl`, ranking + tests. No SMS at all. |
| **2** | Conversation engine against `FakeGateway`: onboarding, parsers, all commands, role-aware HELP. Fully testable offline. |
| **3** | Fill loop: batches, narrowing windows, expiry, quiet hours, admin push alerts. |
| **4** | Docker + compose + volume + backups. Still `FakeGateway`. |
| **5** | Twilio wiring, signature validation, idempotency, Cloudflare Tunnel. |
| **6** | Pilot with 3–4 real people and one real Sunday before the group rollout. |

Phases 1–4 are the whole product and need no phone number, which is what makes the 10–15
day registration wait free.

---

## 11. Open questions

1. **A teacher with two classes.** `class_name` is a single field, so `SUB 3/15` is
   unambiguous for the 95% case. If someone genuinely covers two rooms, they'd need
   `SUB 3/15 <class>` and a real `class` entity. Worth building only if it actually occurs.
2. **Recurring absences.** "I'm out all of July" is four requests. Add `SUB 7/5 7/12 7/19`
   multi-date parsing or an `AWAY <range>` command later.
3. **No-shows.** A sub who accepts and doesn't appear still counts as served, pushing them
   to the back of the line — backwards. Needs `ADMIN NOSHOW <date>` if it ever happens.
4. **Gender matching is a hard constraint.** With no override, a women's class with zero
   available female subs goes `unfilled` and the teacher is told. Confirm that's desired
   rather than an admin-overridable soft rule.
5. **Who are the initial admins?** Bootstrapping needs at least one admin seeded via
   `subctl` before anyone can use `ADMIN` commands.
