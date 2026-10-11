# Ward Substitute Teacher SMS

SMS-only substitute teacher finder for a small congregation. Members interact
with it entirely by text — no app, no login, no web UI.

Design rationale lives in [PLAN.md](PLAN.md). This file is how to run it.

## Quick start (no phone number needed)

Phases 1–4 of the build are fully exercisable offline, which is what makes the
10–15 day A2P registration wait free.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest            # 172 tests, ~0.6s
```

Drive real conversations against a local database:

```bash
export TZ=America/Denver
S=".venv/bin/python -m cli.subctl --db ./data/scratch.db"

$S simulate +15551110001 hi
$S simulate +15551110001 yes          # consent gate comes first
$S simulate +15551110001 "Tom Teacher"
$S simulate +15551110001 teacher
$S simulate +15551110001 male
$S simulate +15551110001 "3rd grade"
$S simulate +15551110001 yes

$S admin +15551110001          # seed the first admin
$S simulate +15551110001 "SUB 9/6"
$S rank 1                      # who's next in line, and why
$S people
```

`simulate` runs the real router against a fake gateway and prints what would
have been texted. It and `tick` refuse to run against the configured live
database unless you pass `--live`: they write real rows but send nothing, which
would leave the running service acting on conversations that never happened.
Point them at a scratch copy with `--db`.

## Running it

```bash
cp .env.example .env      # fill in TZ; Twilio can stay blank for now
docker compose up -d --build
```

The database lands in `./data/substitute.db` on a mounted volume. Backups are
written nightly to `./data/backups/` with `VACUUM INTO` (safe on a live DB,
unlike `cp`) and pruned after 30 days.

## Inbound: polling vs webhook

**No public domain is required.** `INBOUND_MODE` picks how messages reach the
app, and the default needs no inbound network access whatsoever.

### `INBOUND_MODE=poll` (default, recommended for home hosting)

The app asks Twilio for new messages every `POLL_SECONDS`. Every connection is
outbound HTTPS, exactly like sending already is. That means:

- no domain, no DNS, no certificate
- no tunnel, no port forwarding, no dynamic-DNS
- no public endpoint to secure — in poll mode the `/sms` route isn't
  registered at all, so there is nothing to attack
- works behind CGNAT, and survives your ISP changing your IP

The container needs no published ports:

```bash
docker compose up -d          # remove the `ports:` block entirely
```

Cost is latency: a member's reply is processed within one poll interval rather
than instantly. At 20 seconds, against a scheduler whose tightest deadline is a
45-minute batch, that is not a real cost.

**Silence Twilio's default auto-reply.** A new number's SMS webhook points at
Twilio's demo URL, which answers every inbound text with "Thanks for the
message. Configure your number's SMS URL..." before the app ever replies. In
poll mode nothing registers a webhook, so that default stays. Point the
number's Messaging URL at an empty TwiML document instead:

```
https://twimlets.com/echo?Twiml=%3CResponse%3E%3C%2FResponse%3E   (GET)
```

Set it in the console under the number's Messaging configuration, or via the
API with `incoming_phone_numbers(sid).update(sms_url=..., sms_method="GET")`.

Correctness comes from the same `MessageSid` dedupe that guards webhook
retries, so overlapping poll windows are harmless. The first poll against an
existing number **absorbs history without acting on it** — otherwise switching
a live number to polling would replay every message the account ever received.

### `INBOUND_MODE=webhook`

Twilio POSTs to you. Instant, but needs a public HTTPS URL. Point a Cloudflare
Tunnel at `127.0.0.1:8080`, set that URL as both the Twilio webhook and
`PUBLIC_URL`, and keep `VALIDATE_SIGNATURE=1`.

`PUBLIC_URL` must match what Twilio is configured to POST to exactly —
signature validation hashes the URL, so any mismatch rejects every request.

Choose webhook if you want instant replies and already have a domain on
Cloudflare. Otherwise polling is strictly less to own and less to break.

### Two things that will bite you

**`TZ` is load-bearing.** Quiet hours, `nth_sunday()`, and "tomorrow" all
resolve in local church time. A container left on UTC computes the wrong nth
Sunday for any Saturday-evening request.

**One worker only.** A second uvicorn worker means a second APScheduler
sending every batch twice. The `CMD` pins `--workers 1`; leave it.

## Commands members can text

`HELP` is role-aware — people only see what they can actually use.

| Role | Commands |
|---|---|
| Everyone | `HELP` `STATUS` `PAUSE` `RESUME` `STOP` |
| Teachers | `SUB <date> [note]` `CANCEL <date>` `WHO` `CLASS <name>` |
| Substitutes | `YES` `NO` `SUNDAYS 1 3`/`ALL` `MINE` |
| Admins | `ADMIN HELP` `SUB <date> <teacher>` `CANCEL <date> <teacher>` `OPEN` `SUNDAY <date>` `REQUEST <id>` `ROSTER` `FILL` `MOVE` `BROADCAST` `BAN` `UNBAN` `HALT` `UNHALT` |

### Leader guide

This is the same material `ADMIN HELP` returns by text, with room to breathe.
Hand it to whoever will be running the service.

**Becoming an admin.** There is no text command for it, on purpose: admins
can see every phone number and text the whole group. Enroll by text first,
then on the server:

```bash
docker compose exec substitute python -m cli.subctl admin +18015551234
# add --remove to revoke
```

Admin is independent of role. An admin can also be a teacher, a substitute,
both, or neither.

**Every admin command starts with `ADMIN`.** Dates are month/day, e.g.
`10/18`, and must be a Sunday; the reply suggests the nearest one if not.

| Text | What it does |
|---|---|
| `ADMIN HELP` | The plain-English guide |
| `ADMIN SUB 10/18 nursery` | Find a sub for a teacher. Name them by **first name, last name, class, last 4 digits of their phone, or `#number`** from the roster. Anything after the name is a note the subs will see: `ADMIN SUB 10/18 jayn out of town` |
| `ADMIN OPEN` | Every request still waiting for a sub, soonest first |
| `ADMIN SUNDAY 10/18` | All requests for that Sunday and how each stands |
| `ADMIN REQUEST 12` | Who was asked about request #12, when, and what they said |
| `ADMIN ROSTER` | Everyone signed up: `#number`, role flags, class, phone |
| `ADMIN FILL 10/18 sam` | Record a sub you arranged in person, so the rotation stays fair |
| `ADMIN MOVE <old> <new>` | Someone changed phone numbers; keeps their history |
| `ADMIN BROADCAST <msg>` | Text everyone who is active. 8am–9pm only |
| `ADMIN CANCEL 10/18 nursery` | Withdraw a teacher's request; the teacher and anyone asked are told |
| `ADMIN HALT [reason]` / `ADMIN UNHALT` | Emergency stop for the whole service. See below |
| `ADMIN BAN <who> [reason]` / `ADMIN UNBAN <who>` | Ignore one person completely. See below |

**When a name matches more than one person**, the reply lists the candidates
with their numbers, and you resend with the number:

```
More than one match:
#2 JaynAnn Thomas (Nursery)
#5 Jenny Thomas (CTR 6b)
Reply with the number, e.g. ADMIN SUB 10/18 #5
```

Whole words and whole class names win over partial matches, so `6a` finds
CTR 6a without also catching CTR 6b, while `ctr` lists both.

**What happens after `ADMIN SUB`.** The teacher is texted that you asked on
their behalf. You get a "Got it" naming the class and teacher. Subs are then
asked exactly as if the teacher had texted `SUB` themselves. When someone
accepts, the teacher gets the sub's name and number, and you get a one-line
"will sub for" note. If nobody is left to ask, both of you are told.

**What you are texted without asking.** A request that cannot be filled.
On Saturdays, a 5pm digest of anything still open for tomorrow and an 8pm
last call. A reply from a sub the system could not understand.

**Things the service will refuse, and why.**

- `ADMIN SUB` for someone who texted `STOP` or `PAUSE`. They asked not to be
  texted, and an admin cannot consent on their behalf. They can text `RESUME`
  or `START` themselves.
- `ADMIN BROADCAST` between 9pm and 8am. Nothing proactive is sent then.
- A request for a date that is not a Sunday, or is in the past.

**If something goes wrong.** Two switches exist for the day a bug or a
person misbehaves and you are nowhere near a computer:

- **`ADMIN HALT [reason]`** stops the whole service. Nothing is sent to
  anyone until `ADMIN UNHALT`, enforced at the sending layer so it holds
  even if the fault is upstream. The fill loop pauses. Admins can still use
  `OPEN`, `SUNDAY`, `REQUEST`, `ROSTER`, `CANCEL`, `BAN` and `UNBAN`;
  anything that would start new texting is refused. Texts from members
  during a halt are dropped, and `UNHALT` tells you who went unanswered so
  you can follow up. The switch is stored in the database, so a restart
  does not undo it. `STOP` is still confirmed while halted.
- **`ADMIN BAN <who> [reason]`** makes the service ignore one person
  entirely: no replies, no offers, pending offers released, open requests
  withdrawn. Give a full phone number for someone who never enrolled. Only
  `STOP` still gets its confirmation. Admins cannot be banned by text; revoke
  admin at the server first. `ADMIN UNBAN <who>` restores them as they were.

Both notify every other admin. The same switches exist at the server as
`subctl halt`, `unhalt`, `ban`, and `status`.

**Two commands that keep the data honest and are easy to forget:**

- **`ADMIN FILL`** — people arrange swaps in the hallway. If those never reach
  the database, the fairness ordering slowly becomes fiction.
- **`ADMIN MOVE`** — people are keyed on phone number. Without this, someone
  who changes numbers re-enrolls as a stranger and silently loses their served
  history.

## How it behaves

- Teacher texts `SUB 3/15`; the class comes from their profile, snapshotted
  onto the request so history doesn't rewrite itself when they change classes.
- Substitutes are ranked by **last served date**, then **last asked**, then
  total serves. The second key is why a member who never replies doesn't
  monopolise the front of every batch — silence is never penalised, but it
  isn't rewarded with permanent first place either.
- Batches of 3 with a 12h window far out, narrowing to everyone-at-once with
  45 minutes on the day. First `YES` wins via a conditional `UPDATE`.
- **Nothing proactive is sent 9pm–8am.** Direct replies are never delayed.
  Offer TTLs pause overnight, so a 6h window opened at 8:50pm expires at 2pm
  the next day rather than burning through the roster at 3am.
- A request filed during Saturday quiet hours is told up front that it probably
  won't fill and that they should call someone directly. Failing to fill that
  is an acceptable outcome, not a bug.

## Layout

```
app/
  quiet.py         quiet-hours + TTL arithmetic (densest edge cases)
  ranking.py       eligibility and fairness ordering
  fill.py          batches, offers, expiry, the one-minute tick
  conversation.py  inbound routing + onboarding FSM
  commands.py      member and admin commands
  messages.py      every piece of outbound copy
  clock.py         the only place datetime.now() is allowed
cli/subctl.py
tests/
```

## Monitoring

Set `HEARTBEAT_URL` to an Uptime Kuma push-monitor URL and the poller pings it
after every successful Twilio fetch. Give the monitor a short window (two
minutes with a few retries) and it will alert when polling stops for any
reason: container down, scheduler stalled, internet out, or credentials
revoked. A plain "is the container running" check misses most of those.

For a usage graph at a glance, set `ACTIVITY_URL` to a second push monitor.
Every minute the app reports messages sent plus received in the trailing 24
hours as that monitor's `ping` value, so Kuma's response-time chart becomes
a messages-per-day chart and "Ping (Current)" reads as today's volume. Give it
a five-minute window so it also alerts if the scheduler tick stops.

For backups, have whatever job copies `data/backups/` off the machine ping a
second push monitor with a window of about 26 hours, so a silent copy failure
also alerts. Test a restore once with `subctl --db <snapshot> people`.

## Policies

`docs/` holds the public Privacy Policy and Terms required for A2P 10DLC
campaign registration, served via GitHub Pages
(Settings → Pages → branch `master`, folder `/docs`):

- `docs/privacy.html`
- `docs/terms.html`

They are plain static HTML with no build step. The privacy policy contains the
"no mobile information will be shared with third parties" clause carriers look
for — don't reword it.

If you edit these, check the live pages afterwards. A policy URL that returns
200 but renders a placeholder is worse than no URL at all: it reaches a carrier
reviewer looking finished.

## Before going live

1. **Upgrade the Twilio account to paid.** Trial accounts cannot register for
   A2P 10DLC at all, so this blocks everything else. Put the church's card on
   it with auto-recharge and a backup payment method.
2. File A2P 10DLC registration (~$19, 10–15 day review) — see PLAN.md §8.
   Publish `docs/` to GitHub Pages first; the form requires both URLs.
3. Set `ORG_NAME` and `TZ` in `.env`.
4. If using `INBOUND_MODE=webhook`, set `VALIDATE_SIGNATURE=1` and a correct
   `PUBLIC_URL`. In `poll` mode neither applies — there is no endpoint.
5. Seed at least one admin with `subctl admin <phone>`. No `ADMIN` command
   works until one exists.
6. Pilot with 3–4 people and one real Sunday before the group rollout.

## Licence

MIT — see [LICENSE](LICENSE).
