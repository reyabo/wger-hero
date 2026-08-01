#!/usr/bin/env bash
# Read-only smoke test for the day and week views.
#
# Run this ON the server AFTER deploying step 6. It only reads: no write goes to
# the production database, no habit is completed, no quest is evaluated, no
# migration is applied. Output goes to a TXT file in the current directory.
#
# Nothing here prints a token, a password hash, a session secret or any part of
# a .env file. /settings is deliberately not fetched.
#
#   bash scripts/smoke_today_week.sh
#
set -uo pipefail

CONTAINER="${CONTAINER:-wger-hero}"
BASE="${BASE:-http://127.0.0.1:8091}"
DB="${DB:-/data/wger_hero.db}"
OUT="wger-hero-smoke-$(date +%F_%H-%M-%S).txt"

exec > "$OUT" 2>&1

echo "=== wger-hero smoke test (read-only) ==="
date -Iseconds
echo "container=$CONTAINER base=$BASE"

echo
echo "--- 1. Healthcheck ---"
curl -sS -o /dev/null -w 'healthz: HTTP %{http_code}\n' "$BASE/healthz"
curl -sS "$BASE/healthz"
echo

echo
echo "--- 2. Login page (public) ---"
curl -sS -o /dev/null -w 'login: HTTP %{http_code}\n' "$BASE/login"

echo
echo "--- 3. /today and /week ---"
# With AUTH_ENABLED=true these must redirect to /login (303) rather than render;
# that IS the expected result and proves the pages are protected. With auth off
# they answer 200. Both outcomes are fine, an error status is not.
for path in /today /week "/week?date=$(date +%F)" "/week?date=quatsch"; do
  curl -sS -o /dev/null -w "$path: HTTP %{http_code} -> %{redirect_url}\n" "$BASE$path"
done

echo
echo "--- 4. Alembic revision ---"
docker exec "$CONTAINER" python -m alembic current 2>&1 | tail -5
echo "expected head: 0005_habit_schedule_days"

echo
echo "--- 5. Database integrity (read-only) ---"
docker exec "$CONTAINER" sqlite3 -readonly "$DB" \
  "PRAGMA integrity_check;
   PRAGMA foreign_key_check;
   SELECT 'habits',                count(*) FROM habits;
   SELECT 'habit_completions',     count(*) FROM habit_completions;
   SELECT 'habit_schedule_days',   count(*) FROM habit_schedule_days;
   SELECT 'goal_pause_intervals',  count(*) FROM goal_pause_intervals;
   SELECT 'quest_completions',     count(*) FROM quest_completions;
   SELECT 'xp_events',             count(*) FROM xp_events;"

echo
echo "--- 6. Planning logic against the live schema (read-only) ---"
# Every call below is a pure read. The signatures are the ones in the code:
#   quests.app_today()                     -> date        (no arguments)
#   momentum.week_start(day)               -> date        (needs a date)
#   planning.week_days(day)                -> list[date]  (needs a date)
#   planning.week_plan(db, day)            -> WeekPlan    (session + date)
#   planning.today_plan(db, day)           -> dict        (session + date)
#   database._get_session_factory()()      -> Session     (module-private,
#                                                    the only session factory
#                                                    the app exposes)
docker exec "$CONTAINER" python - <<'PY'
from app.database import _get_session_factory
from app.momentum import week_start
from app.planning import today_plan, week_days, week_plan
from app.quests import app_today

day = app_today()
print("app_today:", day, "| week_start:", week_start(day))
print("week_days:", [d.isoformat() for d in week_days(day)])

db = _get_session_factory()()
try:
    data = today_plan(db, day)
    plan = data["plan"]
    print("today: planned=%d flexible=%d quests=%d"
          % (len(plan.planned), len(plan.flexible), len(data["quests"])))
    week = week_plan(db, day, today=day)
    print("week: %s .. %s days=%d quests=%d"
          % (week.monday, week.sunday, len(week.days), len(week.quests)))
    for d in week.days:
        print("  %s %-10s geplant=%d erledigt=%d"
              % (d.day, d.weekday_label, len(d.planned), d.done_count))
finally:
    db.close()
PY

echo
echo "=== done ==="
