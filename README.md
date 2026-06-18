# wger-hero

A small self-hosted gamification layer for [wger](https://github.com/wger-project/wger).

`wger-hero` reads workout data from a wger instance and turns completed training into XP, levels, quests, streaks and achievements.

wger remains the source of truth for workouts, exercises, routines and logs.
wger-hero only adds a lightweight game layer on top.

## Status

Early planning / prototype.

## Goals

* Read workout logs from a wger instance via API
* Award XP for completed workouts
* Track levels, streaks, quests and achievements
* Store local gamification state in SQLite
* Provide a small self-hosted web dashboard
* Run easily with Docker Compose

## Non-Goals

This project does not aim to replace wger.

It should not:

* create or edit workouts in wger in the first version
* modify wger exercise data
* require public internet exposure
* send data to third parties
* include analytics or tracking
* store API tokens in Git

## Planned Features

### Dashboard

* Current level
* Total XP
* XP progress toward next level
* Weekly training progress
* Active quests
* Recent XP events
* Unlocked achievements

### Quest System

Possible quest types:

* Complete a workout
* Complete a weekly training target
* Complete a specific routine
* Complete conditioning work
* Maintain a consistency streak

### XP Categories

Suggested attributes:

* Strength
* Push
* Pull
* Legs
* Stamina
* Mobility
* Discipline

### Achievements

Example achievements:

* First Workout
* Full Week Completed
* Three Workout Week
* Consistency Streak
* Conditioning Completed
* New Personal Best

## Suggested Stack

* Python
* FastAPI
* Jinja2 templates
* SQLite
* httpx
* pydantic
* pytest
* Docker
* Docker Compose

## Configuration

Example `.env`:

```dotenv
WGER_BASE_URL=https://your-wger-instance.example
WGER_API_TOKEN_FILE=/run/secrets/wger_api_token
DATABASE_URL=sqlite:////data/wger_hero.sqlite
APP_SECRET_KEY=change-me
SYNC_INTERVAL_MINUTES=60
TIMEZONE=UTC
DEFAULT_WEEKLY_WORKOUT_TARGET=3
```

Alternatively, the API token may be provided directly through an environment variable:

```dotenv
WGER_API_TOKEN=your-token-here
```

Using a token file is recommended for deployments.

## Docker

The app should listen on port `5000` inside the container.

Example Docker Compose setup:

```yaml
services:
  wger-hero:
    build: .
    container_name: wger-hero
    restart: unless-stopped
    environment:
      WGER_BASE_URL: ${WGER_BASE_URL}
      WGER_API_TOKEN_FILE: /run/secrets/wger_api_token
      DATABASE_URL: sqlite:////data/wger_hero.sqlite
      APP_SECRET_KEY: ${APP_SECRET_KEY}
      TIMEZONE: ${TIMEZONE:-UTC}
      DEFAULT_WEEKLY_WORKOUT_TARGET: ${DEFAULT_WEEKLY_WORKOUT_TARGET:-3}
    volumes:
      - ./data:/data
      - ./secrets/wger_api_token.txt:/run/secrets/wger_api_token:ro
    ports:
      - "8091:5000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz').status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
```

Start:

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8091
```

## Development

Create a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run tests:

```bash
pytest
```

Run locally:

```bash
uvicorn app.main:app --reload
```

## Data Storage

wger-hero stores local gamification data in SQLite.

The database contains:

* hero profile
* XP events
* quest progress
* achievements
* sync history

The sync history is used to prevent awarding XP multiple times for the same workout log.

## Security

Never commit:

* API tokens
* `.env` files
* SQLite databases
* personal workout exports
* logs containing private workout data
* screenshots with private information

Recommended:

* use token files or Docker secrets
* run behind a reverse proxy if exposing the app beyond localhost
* restrict access to trusted users only

## Level Formula

Initial simple formula:

```text
Level 1 starts at 0 XP.
XP needed for next level = 1000 + current_level * 250
```

This formula is intentionally simple and may change later.

## First Version Acceptance Criteria

A first usable version should:

1. connect to a wger instance using an API token
2. fetch recent workout logs
3. normalize workout data internally
4. award XP once per completed workout
5. avoid duplicate XP through stable sync IDs or hashes
6. store gamification state in SQLite
7. show a simple dashboard
8. provide a manual sync button
9. expose `/healthz`
10. run with Docker Compose
11. include tests for XP, levels, quests and deduplication

## Design Principle

wger-hero should reward consistency, not reckless overtraining.

The app should never encourage training through pain, ignoring form breakdown or chasing volume at all costs. Technical work, recovery and honest logging should be rewarded too.

## License

MIT
