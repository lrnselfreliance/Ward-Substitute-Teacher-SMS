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
export DB_PATH=./data/substitute.db TZ=America/Denver
S=".venv/bin/python -m cli.subctl"

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
have been texted.

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
| Admins | `ADMIN OPEN` `SUNDAY <date>` `REQUEST <id>` `ROSTER` `FILL` `MOVE` `BROADCAST` |

Two admin commands keep the data honest and are easy to forget:

- **`ADMIN FILL <date> <name>`** — record a swap arranged in the hallway. If
  those never reach the database, the fairness ordering slowly becomes fiction.
- **`ADMIN MOVE <old> <new>`** — `person` is keyed on phone number. Without
  this, someone who changes numbers re-enrolls as a stranger and silently loses
  their entire served history.

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
