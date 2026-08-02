# wger-hero

A small, self-hosted **habit RPG**. Define your own habits and quests, complete them, and earn XP, stats, and achievements.

It is a transparent, rule-based alternative to AI-heavy habit/quest apps:

- **You** define what counts. The app never decides what you should value.
- No AI coach, no generated daily plans, no productivity pressure, no external AI APIs.
- Every reward follows visible rules, and every XP event is auditable.

Your [wger](https://wger.de) instance is one **automatic** data source (workout XP). On top of that you track **manual habits** (reading, language learning, mobility, recovery, project work, …) and your own **custom quests**. wger stays the source of truth for workouts — wger-hero only ever reads it.

## Features

- **Manual habits** — repeatable, user-defined actions with their own XP and stat rewards
- **Custom quests** — `manual`, `habit_count`, and `workout_count` goals over daily/weekly/monthly/once periods
- **Global XP vs. stat XP** — global XP drives your level; stat XP grows 10 attributes (data ready for a future radar screen)
- Reads wger via the REST API (read-only) and awards XP for completed workouts, conditioning, and logged RIR
- Simple level progression: `1000 + level × 250 XP` per level
- Achievements (First Blood, Triple Threat, …)
- Accidental double-click protection on habit completion
- Clean server-rendered dashboard — no external CDN, no analytics, no tracking
- Deduplication: syncing the same workout twice never awards XP twice
- `/healthz` endpoint for container health checks

## Quick Start (Docker)

### 1. Clone and configure

```bash
git clone https://github.com/reyabo/wger-hero
cd wger-hero
cp .env.example .env
```

Edit `.env`:

```env
WGER_BASE_URL=https://wger.yourdomain.com
HERO_NAME=YourName
```

### 2. Set up the API token

Generate a token in wger at **Settings → API → Generate new token**.

Create a secrets directory (excluded from git):

```bash
mkdir secrets
echo "your_wger_api_token_here" > secrets/wger_api_token.txt
chmod 600 secrets/wger_api_token.txt
```

The Docker Compose file mounts this as a read-only secret at `/run/secrets/wger_api_token`.

### 3. Create the data directory

```bash
sudo mkdir -p /srv/data/wger-hero
```

### 4. Start the app

```bash
docker compose up -d --build
```

The app is available at `http://localhost:8091`.

### 5. Sync your workouts

Open the dashboard and click **Sync Now**, or POST to `/sync`:

```bash
curl -X POST http://localhost:8091/sync
```

## Running Locally (without Docker)

```bash
# Install dependencies
pip install -e ".[dev]"

# Set required env vars
export WGER_BASE_URL=https://wger.yourdomain.com
export WGER_API_TOKEN=your_token_here

# Override database path (avoids needing /data)
export DATABASE_URL=sqlite:///./wger_hero_dev.db

# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8091 --reload
```

## Running Tests

```bash
WGER_BASE_URL=https://wger.example.com python -m pytest
```

All tests use in-memory SQLite and mocked wger clients — no real wger instance required.

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `WGER_BASE_URL` | (required) | Base URL of your wger instance |
| `WGER_API_TOKEN` | — | API token (env var, least preferred) |
| `WGER_API_TOKEN_FILE` | — | Path to a file containing the token |
| `HERO_NAME` | `Hero` | Display name for your character |
| `DATABASE_URL` | `sqlite:////data/wger_hero.db` | SQLite database path |
| `APP_ENV` | `production` | Environment label |
| `WGER_FETCH_EXERCISE_LOGS` | `true` | Set `false` to skip `/api/v2/log/` + exercise catalog (older wger) |
| `SYNC_FROM_DATE` | — | Only sync workouts on/after this date (`YYYY-MM-DD`), enforced locally |

Token resolution order (highest priority first):
1. `WGER_API_TOKEN_FILE` env var → reads file at that path
2. Docker secret at `/run/secrets/wger_api_token` (auto-detected)
3. `WGER_API_TOKEN` env var

## Caddy Reverse Proxy

Add to your `Caddyfile`:

```
hero.example.com {
    reverse_proxy localhost:8091
}
```

## Habits & Custom Quests

Everything is server-rendered and defined by you — no AI, no hidden weighting.

### Habits (`/habits`)

A habit is a repeatable action you complete for XP. Create one at `/habits/new`:

| Field | Meaning |
|---|---|
| Title / description | What the habit is |
| Recurrence | `daily` · `weekly` · `monthly` · `flexible` |
| Target count | How many completions make up a full period |
| Base XP reward | Global XP awarded per completion |
| Stat rewards | XP added to specific attributes per completion |
| Feste Wochentage | **Optional** weekday plan — see below |

Completing a habit creates an auditable completion record, awards global XP **and** stat XP, writes the XP events, and updates your hero. A second click within a couple of seconds is ignored so you never double-award by accident. Inactive habits cannot be completed.

#### Optional weekday planning

A habit may be tied to weekdays, stored as **ISO weekdays** (`1` = Monday …
`7` = Sunday) in `habit_schedule_days`. Numbers, not German labels, so the data
stays language-independent and validatable; anything outside 1–7 is refused on
the server, not only in the browser.

- **No selection = flexible.** A habit without a plan keeps behaving exactly as
  before: it is available every day and appears under "Jederzeit möglich".
- Several weekdays per habit are normal; the same weekday can never be stored
  twice (unique constraint on `(habit_id, iso_weekday)`).
- Renaming, editing or deactivating a habit leaves the plan and every recorded
  completion untouched. Clearing the selection removes the *plan*, never the
  history.
- **A missed planned day costs nothing.** No negative XP, no penalty, no
  automatically created completion. The plan says what was intended, the
  completions say what happened.

### Custom quests (`/quests`)

A quest is a larger goal with a period. Create one at `/quests/new`:

| Type | Progress source |
|---|---|
| `manual` | You mark it complete yourself |
| `habit_count` | Number of habit completions in the period (optionally filtered by a *match text* against habit titles) |
| `workout_count` | Number of synced wger workouts in the period |

Periods are `daily` · `weekly` · `monthly` · `once`. Quests can carry their own stat rewards and can be marked **repeatable** to re-arm for the next period after completion. The built-in seeded quests (Week Warrior, HOME HERO × SUPERMOVER 3) keep working unchanged.

## Stats

Global XP (your level) and stat XP (your attributes) are tracked separately. There are 10 stats; stat totals are stored per attribute and surfaced on the dashboard (the radar chart is intentionally not built yet — the data is prepared for it):

| Key | Display (DE) | Key | Display (DE) |
|---|---|---|---|
| `strength` | Stärke | `technique` | Technik |
| `endurance` | Ausdauer | `discipline` | Disziplin |
| `dexterity` | Geschicklichkeit | `knowledge` | Wissen |
| `mobility` | Beweglichkeit | `creativity` | Kreativität |
| `body_control` | Körperkontrolle | `recovery` | Regeneration |

## Japanese SAVE Import

Paste the coach's SAVE block on `/japanese`, review the preview, confirm. The
import is the **single reward channel** for Japanese sessions — the seeded
"Japanisch lernen" habit is no longer created, because it would reward the same
session twice.

### The required core

A SAVE is valid with just these three lines between the markers:

```
=== 状態 SAVE ===
Datum: 2026-08-01 | Streak: 4
Charakter: Lv 2 (見習い) | 433 / 1000 XP
語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
=== END SAVE ===
```

They stay mandatory for a reason: the date drives the baseline / historical
classification, the character line drives the level progress, and the five
scores *are* the snapshot. Everything else is optional and simply absent when
the coach did not report it.

### Optional learning metrics

Three independent lines. Each may appear alone, in any combination, or not at
all:

```
WaniKani-Level: 2
Bunpro-Level: N5
Grammatikpunkte im SRS: 37
```

| Line | Accepted |
|---|---|
| `WaniKani-Level` | a non-negative integer — `2` yes, `zwei` no |
| `Bunpro-Level` | one of `N1` … `N5`; anything else is refused |
| `Grammatikpunkte im SRS` | any non-negative integer, `0` included; `-1` and `viele` are refused |

**Absent is not zero.** A missing line stores `None` — "the SAVE did not say" —
and is shown as `—` / *Nicht angegeben*. A written `0` stores `0` and is shown
as `0`. The two are different facts, they hash differently, and no value is
ever carried over from an earlier import to fill a gap.

If a line is written but its number is missing or unreadable, that is a
validation error, not "unset" — a typo should be corrected, not swallowed.

### Combined legacy line

The older single-line spelling keeps working, with every part after the
WaniKani level optional:

```
WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS
WaniKani: Lv 2 | Bunpro: N5, 1 Grammatikpunkt im SRS
WaniKani: Lv 2 | Bunpro: N5, 5 Punkte
WaniKani: Lv 2 | Bunpro: N5
WaniKani: Lv 2
Bunpro: N5
```

`Lv`, `Lv.` and `Level` are interchangeable, singular and plural both work, the
suffix `im SRS` is optional, and whitespace around `|` and `,` is optional.

Both spellings feed the **same three fields**, so this:

```
WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS
```

and this:

```
WaniKani-Level: 2
Bunpro-Level: N5
Grammatikpunkte im SRS: 10
```

produce identical values, identical normalized text and the identical duplicate
hash.

### When both spellings appear

A SAVE may contain both. If every stated value agrees, it is accepted. If two
statements contradict each other, the SAVE is refused with a message naming the
field and both values:

```
Widersprüchliche Angaben für „Grammatikpunkte im SRS“: 10 und 37.
```

Neither side is quietly preferred — a contradiction is a question only the user
can answer.

### Optional coach notes

```
Aktueller Grammatikpunkt: これ
Debuffs: keine
Neue Vokabeln heute: keine
Tagesquest: Erfüllt – Mini-Boss „Partikel-Golem“ besiegt.
```

Every one of them may be missing, and an empty value counts as missing. Absent
stores `None`; no placeholder is invented and nothing is copied from a previous
import. None of them influences XP, classification or duplicate detection.

### Session mode and completion

`Session-Modus` and `Session-Abschluss` are one statement written on two lines.
Both present enables the deterministic session reward; both absent keeps the
legacy level-delta evaluation. **Exactly one of them is a validation error** —
half a statement cannot be evaluated deterministically, and guessing the other
half is precisely what this app does not do:

```
„Session-Modus“ und „Session-Abschluss“ müssen gemeinsam angegeben werden.
```

An unknown *value* in either line is something else: the SAVE still imports as
a snapshot, but the reward becomes a warning case with 0 XP, exactly as before.

### Full example

```
=== 状態 SAVE ===
Datum: 2026-08-01 | Streak: 4
WaniKani-Level: 2
Bunpro-Level: N5
Grammatikpunkte im SRS: 37
Charakter: Lv 2 (見習い) | 433 / 1000 XP
Session-Modus: START
Session-Abschluss: vollständig
Session-XP: 60
語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
Aktueller Grammatikpunkt: これ
Debuffs: keine
Neue Vokabeln heute: keine
Tagesquest: Erfüllt – Mini-Boss „Partikel-Golem“ besiegt.
=== END SAVE ===
```

### Reward rules (deterministic, computed by wger-hero)

XP comes from **session mode and completion only**. The five competence scores
are a factual snapshot; their deltas never influence XP or attributes.

| Mode | Full session |
|---|---|
| `MINI` | 15 XP |
| `START`, `START_VOICE`, `GENKI`, `IRODORI`, `SCHWACH` | 40 XP |
| `BOSS` | 80 XP |
| `STATUS` | always 0 XP |

`START VOICE`, `START-VOICE` and `START_VOICE` are accepted alike.

| Completion | Result |
|---|---|
| `vollständig` | full value |
| `teilweise` | `min(15, full)` |
| `abgebrochen` | 0 XP |
| `keine Leistung` | 0 XP |

**`Session-XP:` is only a coach-reported comparison value.** For a save with mode
and completion, wger-hero ignores it for the reward and shows the difference in
the preview. In the example above the actual reward is therefore **40 XP**, not 60.

Attribute XP reuses the project's canonical learning split
(`knowledge_learning` → Wissen 70 %, Disziplin 20 %, Technik 10 %), so the stat
XP of a session always sums to exactly its global XP. No physical attribute is
ever touched.

| Session | Global | Wissen | Disziplin | Technik |
|---|---|---|---|---|
| `MINI`, vollständig | +15 | +10 | +3 | +2 |
| `START`, vollständig | +40 | +28 | +8 | +4 |
| `BOSS`, vollständig | +80 | +56 | +16 | +8 |

The first import is always a **baseline**: 0 global XP and 0 attribute XP.
Duplicate and back-dated saves never award anything.

Saves without the two session lines fall back to the previous level-bar delta and
are marked *Legacy-Berechnung*. That path exists for backward compatibility only:
it still grants **global XP**, but **no attribute XP** — its amount comes from the
coach-maintained progress bar rather than a wger-hero rule, so only fully
specified sessions move the radar.

## XP Rules (automatic, from wger)

| Event | XP | Attribute |
|---|---|---|
| Workout completed | +100 | Strength |
| Conditioning/finisher detected | +25 | Conditioning |
| RIR logged on any set | +10 | Mindfulness |
| Quest completed (Week Warrior) | +200 | Strength |
| Quest completed (HOME HERO Full Week) | +200 | Strength |
| Achievement unlocked | +50 | Glory |

Manual habits and custom quests award the XP and stat rewards **you** assign to them.

## Level Formula

```
XP to next level = 1000 + current_level × 250
```

Level 1 → 2: 1,250 XP  
Level 2 → 3: 1,500 XP  
Level 10 → 11: 3,500 XP

## Project Structure

```
wger-hero/
├── app/
│   ├── main.py         # FastAPI routes
│   ├── config.py       # Settings (pydantic-settings)
│   ├── database.py     # SQLAlchemy engine + init
│   ├── models.py       # ORM models
│   ├── wger_client.py  # wger API client (httpx)
│   ├── sync.py         # Fetch → normalize → award XP
│   ├── xp.py           # XP rules + level formula
│   ├── habits.py       # Manual habit logic + completion rewards
│   ├── quests.py       # Quest seeding, creation + progress
│   ├── stats.py        # 10-stat registry + stat-XP rewards
│   ├── achievements.py # Achievement unlock logic
│   ├── templates/      # Jinja2 HTML templates
│   └── static/         # CSS (no external CDN)
├── tests/
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── pyproject.toml
```

## What Still Needs Live Verification

The wger API client is designed to be easy to adapt. Verify against your live instance:

| Item | Candidate | Notes |
|---|---|---|
| Completed sessions | `/api/v2/workoutsession/` | Check fields: `id`, `date`, `workout`, `notes` |
| Exercise logs | `/api/v2/log/` | Check fields: `exercise`, `reps`, `weight`, `rir` |
| Routines | `/api/v2/routine/` | May not exist on older wger — client handles 404 gracefully |
| Exercise names | `/api/v2/exercise/` | Names may be in a `translations` list, not a top-level `name` field |
| Token format | `Authorization: Token <value>` | Verify this is correct (not `Bearer`) |

Test connectivity:

```bash
curl -H "Authorization: Token YOUR_TOKEN" \
  https://wger.yourdomain.com/api/v2/workoutsession/?format=json
```

## Security

- API tokens are never logged or committed
- No data is sent to third parties
- No analytics or external tracking
- Raw API payloads are not stored — only a sanitized summary
- `.env` and `secrets/` are in `.gitignore`

## Quest Completions and Deduplication

Every rewarded quest period writes one append-only `QuestCompletion` row. Rows
are never edited or deleted — not through the UI, not by the services.

**Dedup key.** Derived deterministically from the quest id, its period type and
the canonical start of the evaluated window, computed in `APP_TIMEZONE`:

```
quest:17:weekly:2026-07-27      # Monday of that week
quest:22:monthly:2026-08-01     # 1st of that month
quest:31:once                   # a one-off quest has exactly one key
```

The date is normalized to the start of the period, so re-arming a repeatable
quest mid-week cannot produce a second key for the same calendar week.

**The guarantee lives in the database.** `dedup_key` carries a unique index. A
preceding "already rewarded?" query alone would leave a window in which two
near-simultaneous completions both pass; the insert is attempted *before* any XP
is touched, and if it loses that race the whole unit of work is rolled back. No
partial XP can survive a failed completion.

**Historic completions are not invented.** The table starts empty at revision
`0003`. Quests finished earlier have no row and are not retro-filled; they are
still not re-rewarded, because the existing `completed_at` / `active` guards
continue to apply.

## Stable Habit Binding

`habit_count` quests can bind to one habit through `Quest.habit_id`. It takes
precedence over `match_text`, which remains the fallback for quests created
before the field existed. A stable id survives renaming and archiving, where a
title match would silently start counting the wrong rows — or none at all.

## Japanese Sessions as a Quest Source

`japanese_session_count` counts confirmed Japanese sessions whose **`save_date`**
falls inside the quest window, so importing a few days late still credits the
right week.

One rule, in `quests.japanese_import_counts()`. An import counts only when
wger-hero itself scored it from session mode and completion **and** actually paid
out:

| Excluded | Why |
|---|---|
| `reward_calculation != deterministic_session` | baseline, historical, warning and the legacy level-bar path — no confirmed session mode |
| `classification != progress` | duplicates and back-dated imports |
| `xp_awarded == 0` | `STATUS`, aborted and no-performance sessions |

Duplicates never reach the check at all — they produce no row.

**No double reward.** The SAVE import stays the direct reward for a session. The
quest only ever adds its own configured bonus when the period target is met; it
never re-awards session XP, never modifies import rows and never recalculates
attributes.

## Today and Week

Two read-only operational views. Both join what already exists — habits, their
optional plan, recorded completions, quests and goals — and introduce no second
reward, quest or momentum engine.

### `/today`

The current day in `APP_TIMEZONE`: weekday and date, the habits planned for
today with their status (`offen` / `erledigt` / `pausiert`), the flexible habits
that are available any day, the daily and weekly quests of the running period,
and a link to the week. Each habit carries a quick action that posts to the
existing completion route — same service, same XP rules, same double-click
guard.

### `/week`

Monday–Sunday of the running week, or of any other week via `/week?date=` with
an ISO date. An unparseable value falls back to the current week with a plain
message rather than an error page. Each of the seven days is its own card with
its planned habits, their completion status and the goal behind them; weekly
quests are listed below with counter, target and whether the period was already
rewarded.

Quest progress is shown **only for the running period**. The counters in
`app/quests.py` answer for the current window, so a past week reports "Zähler
nur für den laufenden Zeitraum" instead of a number that would look historical
but is not. Showing a quest never completes it — rewarding stays with the
existing quest logic and its `QuestCompletion` deduplication.

### Timezone, pauses and safety

- Calendar weeks are Monday–Sunday in `APP_TIMEZONE`, derived from
  `momentum.week_start()` and `quests.app_today()`. `app/planning.py` never
  reads a clock: the reference date is always passed in, which tests assert.
- The aggregation is read-only — no XP, no `QuestCompletion`, no commit. Also
  asserted by a test.
- Pauses come from the recorded `GoalPauseInterval` history through the shared
  `goal_progress.overlaps_pause()`. A paused item is marked neutrally and never
  as a failure. Habits of completed or archived goals are not presented as
  current work; their history stays intact.
- The return target of a quick action is validated against an internal
  allowlist (`safe_next`), so a crafted `next` cannot bounce the browser to
  another host.

## Progressive Web App

wger-hero installs as a PWA on phone and desktop. Everything it needs is local —
no CDN, no external font, no third-party script.

| File | Served at | Purpose |
|---|---|---|
| `app/static/manifest.webmanifest` | `/manifest.webmanifest` | name, colours, icons, `start_url` `/today`, scope `/` |
| `app/static/sw.js` | `/sw.js` | the service worker (served from the root so its scope may be `/`) |
| `app/static/icons/*.svg` | `/static/icons/…` | local icons, one `any` and one `maskable` |
| `app/templates/offline.html` | `/offline` | the static fallback page |

**Installation needs HTTPS** (or `localhost`) — browsers refuse to register a
service worker otherwise. Use the Caddy reverse proxy for this; the direct
`http://…:8091` port is for debugging, not a supported installation path.

### What the service worker caches — and what it must never touch

It caches exactly one thing: the fixed, versioned list in `STATIC_ASSETS`
(stylesheet, icons, manifest, offline page). There is no "cache every GET"
strategy and no runtime caching.

Never cached, by construction:

- every dynamic or authenticated page — `/`, `/login`, `/today`, `/week`,
  `/goals`, `/habits`, `/quests`, `/japanese`, `/settings`, `/stats`,
  `/achievements`
- every non-GET request, so no form submission, login or SAVE import
- responses the server marks `Cache-Control: no-store, private`, which is every
  authenticated page
- anything from another origin

Why so strict: a cached authenticated page would outlive a logout inside the
browser profile. Pages are network-only, full stop. The offline page therefore
carries no goals, habits, quests or XP — nothing personal can be served from
cache after logout.

**There is no offline write path.** The offline page shows no form and stores
nothing, because silently swallowing an entry the user believed was saved would
be worse than saying "no connection".

`CACHE_VERSION` is bumped whenever an asset changes; the `activate` handler
deletes every cache that does not match it. Registration is defensive: an
unsupported or failing `serviceWorker` never affects the page, and nothing is
logged to the console.

## Starter campaign

Three prepared goals an install can adopt — never automatically. Preview it at
**Einstellungen → Starter-Kampagne** (`/settings/starter`) or from the CLI:

```bash
python -m app.seed_programs starter --dry-run   # preview, writes nothing
python -m app.seed_programs starter             # activate
```

Web page and CLI call the same two functions in `app/starter.py`, so a preview
can never disagree with what an activation does.

### What it contains

| Goal | Weekly quest | Habits | Milestones |
|---|---|---|---|
| **Kraftpfad** (`kraftpfad`) | *Dreifachschlag* — 3 wger workouts per week (`workout_count`) | — | first counted workout · 4 fulfilled weeks · 12 fulfilled weeks |
| **Weg des Japanischen** (`weg-des-japanischen`) | *Fünf Schriftrollen* — 5 SRS reviews per week (`habit_count`, bound by habit id) · *Zwei Gespräche mit dem Sensei* — 2 confirmed sessions per week (`japanese_session_count`) | *SRS-Review*, planned Mon–Fri | first review · 20 reviews · 8 confirmed sessions · 4 weeks with both goals |
| **Körperkontrolle** (`koerperkontrolle`, short label *Routine K*) | *Der Fünfer-Rhythmus* — all five planned routines in one week | the five existing CONTROL routines, planned Mon, Tue, Wed, Thu, Sat | the four existing CONTROL stages |

Friday and Sunday stay deliberately free in Routine K.

**Reuse instead of duplication.** If the seeded `week-warrior` quest exists it
*is* "three workouts per week", so it is linked to Kraftpfad rather than
duplicated — its title and reward are never touched, and no second
*Dreifachschlag* is created. The same holds for the five CONTROL routines and
their four stages: they are matched by exact title and only ever gain the goal
link and the weekday plan.

### Rules

- **Idempotent.** Re-running changes nothing; a dry run afterwards reports no
  changes.
- **Matched by exact title and slug**, never by a fuzzy substring.
- **Never overwrites.** A goal, habit or quest the user has edited keeps its
  title, description and status. Nothing is renamed, nothing is deleted.
- **Conflicts are reported, not resolved.** An entry that already belongs to a
  different goal is listed as a conflict and left exactly as it is.
- **No retroactive history.** No XP event, no habit completion, no quest
  completion, no pause interval, no Japanese SAVE import. Milestones start at
  zero and are only ever reached by real later activity.
- **No network.** The campaign never talks to wger or anything else.
- **All or nothing.** If activation fails, the rows this run created are removed
  again, so no half-created goal survives. The browser sees one plain sentence;
  the traceback goes to the log.

### Routine K and privacy

Routine K stores neutral titles only — no intimate free text, no body metrics,
no outcome protocol; a test asserts this over the seeded data. The goal page
shows a factual note: pause on lasting tension or pain, a pause costs no XP and
breaks no streak, and see a doctor if complaints persist. That is a safety note,
not a diagnosis or a treatment recommendation.

## Streaks and Momentum

Two deliberately different answers to "how is it going".

**Streak** is strict: consecutive fully satisfied calendar weeks (Mon–Sun in
`APP_TIMEZONE`). A week counts when every weekly quest of the goal was rewarded
in it. The running week counts only once it is already satisfied — an unfinished
week never ends a streak. The best streak stays visible after a break.

**Momentum** is forgiving on purpose, so one missed week is not a reason to give
up. It is a weighted average over the last four **completed** weeks:

| Week | Weight |
|---|---:|
| last week | 40 % |
| the week before | 30 % |
| two weeks before | 20 % |
| three weeks before | 10 % |

Each week contributes its fulfilment **capped at 100 %** — over-delivering in one
week cannot paper over another. The result is 0–100 and is explained in the UI,
including a per-week breakdown of what counted and what did not.

Rules that keep it from feeling punitive:

- The **running week is never scored**; it cannot be a failure yet.
- **Paused weeks are removed** from the calculation and the remaining weights are
  renormalized, so a deliberate break neither helps nor hurts.
- **Missing history is not a failure** — weeks before a goal existed are treated
  the same way and reported as "keine Daten".
- With nothing scorable, momentum is **`None`** ("noch nicht genug Daten"), never
  `0`, because zero reads as failure.
- Nothing here can produce negative XP or a negative value.

The scoring rules live in `app/momentum.py` and are pure — dates and numbers in,
numbers out, no database and no clock of their own. `app/goal_progress.py` is the
thin layer that maps stored `QuestCompletion` history onto them.

### Which weeks count as paused

Pausing a goal writes a `GoalPauseInterval` — a start, and an end once the goal
leaves the paused state. Momentum and streaks read those intervals, **never the
goal's current status applied backwards**. That distinction matters in both
directions: resuming a goal must not turn its old break into a row of failed
weeks, and pausing today must not retroactively excuse a week that really was
missed while the goal was running.

The neutralisation rule is deliberately generous and applied in one place
(`goal_progress.week_was_paused`):

> Any overlap of a pause interval with a calendar week neutralises the whole week.

So a pause that starts on Wednesday does not leave Monday and Tuesday behind as
an unfinished week. In momentum a neutral week is removed and the remaining
weights are renormalized; in a streak it is **skipped**, so it neither extends
nor breaks the run. The running week stays outside momentum regardless.

Bookkeeping rules:

- `active → paused` opens exactly one interval; pausing again changes nothing.
- Leaving `paused` closes the open interval — including `completed` and
  `archived`, because a goal in those states is not on a break either. No status
  other than `paused` may leave an interval running.
- At most one open interval per goal, enforced by a **partial unique index**
  (`ux_goal_pause_open … WHERE ended_at IS NULL`) rather than by Python alone.
  Several finished breaks per goal are normal.
- Intervals are append-only: no edit and no delete route exists. Breaks taken
  before revision `0004_goal_pause_intervals` have no row and none is invented —
  those weeks are scored from the quest history exactly as before.
- Stored timestamps are naive UTC; the calendar day they belong to is resolved
  once, centrally, via `quests.app_date_of()` in `APP_TIMEZONE`.
