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

Completing a habit creates an auditable completion record, awards global XP **and** stat XP, writes the XP events, and updates your hero. A second click within a couple of seconds is ignored so you never double-award by accident. Inactive habits cannot be completed.

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

### Preferred format

```
=== 状態 SAVE ===
Datum: 2026-07-31 | Streak: 4
WaniKani: Lv 1 | Bunpro: N5, 5 Punkte
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

### Accepted wording variants

The WaniKani/Bunpro line tolerates the coach's alternative phrasing. All four
lines below are read identically (WaniKani level `2`, Bunpro level `N5`,
`10` Bunpro points):

```
WaniKani: Lv 2 | Bunpro: N5, 10 Punkte
WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS
WaniKani: Lv 2|Bunpro: N5, 1 Grammatikpunkt im SRS
WaniKani: Level 2 | Bunpro: N5, 10 Grammatikpunkte im SRS
```

`Lv`, `Lv.` and `Level` are interchangeable, `Punkt`/`Punkte`,
`Grammatikpunkt`/`Grammatikpunkte` and the optional suffix `im SRS` are all
accepted, and whitespace around `|` and `,` is optional. The number itself
still has to be a non-negative integer — spelled-out numbers, a missing Bunpro
count and negative values are rejected with a German error naming the line.

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
