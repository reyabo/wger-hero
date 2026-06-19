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
