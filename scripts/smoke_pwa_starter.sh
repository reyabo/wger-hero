#!/usr/bin/env bash
# Read-only smoke test for the PWA and the starter campaign.
#
# Run this ON the server AFTER deploying steps 7 and 8. It only reads: no page
# is posted to, the starter campaign is NOT activated, no migration is applied
# and the database is opened read-only. Output goes to a TXT file.
#
# To activate the campaign later, follow section 4a of docs/DEPLOY.md — dry-run,
# check the preview, back up, activate, verify with a second dry-run.
#
# Nothing here prints a token, a password hash, a session secret or any part of
# a .env file.
#
#   bash scripts/smoke_pwa_starter.sh
#
set -uo pipefail

CONTAINER="${CONTAINER:-wger-hero}"
# Use the HTTPS name: a service worker is only registered over HTTPS or
# localhost, so testing the plain 8091 port would not prove installability.
BASE="${BASE:-https://wger-hero.example.invalid}"
DB="${DB:-/data/wger_hero.db}"
OUT="wger-hero-smoke-pwa-$(date +%F_%H-%M-%S).txt"

exec > "$OUT" 2>&1

echo "=== wger-hero PWA/starter smoke test (read-only) ==="
date -Iseconds
echo "container=$CONTAINER base=$BASE"

echo
echo "--- 1. Healthcheck and login page ---"
curl -sS -o /dev/null -w 'healthz: HTTP %{http_code}\n' "$BASE/healthz"
curl -sS -o /dev/null -w 'login:   HTTP %{http_code}\n' "$BASE/login"

echo
echo "--- 2. PWA assets (all public, all local) ---"
for path in /manifest.webmanifest /sw.js /offline \
            /static/icons/icon.svg /static/icons/icon-maskable.svg \
            /static/style.css; do
  curl -sS -o /dev/null -w "$path: HTTP %{http_code} (%{content_type})\n" "$BASE$path"
done

echo
echo "--- 3. Manifest content ---"
# Only the structural fields; the manifest contains no configuration by design.
curl -sS "$BASE/manifest.webmanifest" \
  | python3 -c 'import json,sys; m=json.load(sys.stdin); print("\n".join(f"{k}: {m[k]}" for k in ("name","short_name","start_url","scope","display")))'

echo
echo "--- 4. Service worker cache boundary ---"
curl -sS "$BASE/sw.js" | grep -E "CACHE_VERSION|'/offline'|'/static/|'/manifest" || true
echo "(the list above is the complete set of cached paths)"
curl -sS -o /dev/null -D - "$BASE/sw.js" | grep -Ei 'cache-control|service-worker-allowed' || true

echo
echo "--- 5. Protected pages must not be cacheable ---"
# With AUTH_ENABLED=true these redirect to /login (303) — that is the expected
# result and proves they are protected. Without auth they answer 200.
for path in /today /week /settings /settings/starter; do
  curl -sS -o /dev/null -w "$path: HTTP %{http_code} -> %{redirect_url}\n" "$BASE$path"
  curl -sS -o /dev/null -D - "$BASE$path" | grep -i '^cache-control' || true
done

echo
echo "--- 6. Alembic revision ---"
docker exec "$CONTAINER" python -m alembic current 2>&1 | tail -5
echo "expected head: 0005_habit_schedule_days (steps 7 and 8 add no migration)"

echo
echo "--- 7. Database integrity (read-only) ---"
docker exec "$CONTAINER" sqlite3 -readonly "$DB" \
  "PRAGMA integrity_check;
   PRAGMA foreign_key_check;
   SELECT 'goals',                count(*) FROM goals;
   SELECT 'habits',               count(*) FROM habits;
   SELECT 'habit_schedule_days',  count(*) FROM habit_schedule_days;
   SELECT 'quests',               count(*) FROM quests;
   SELECT 'quest_completions',    count(*) FROM quest_completions;
   SELECT 'goal_pause_intervals', count(*) FROM goal_pause_intervals;
   SELECT 'xp_events',            count(*) FROM xp_events;"

echo
echo "--- 8. Starter campaign preview (dry-run, writes nothing) ---"
# plan_starter(db) takes a session and returns a StarterPlan; it never writes.
# The CLI wrapper below is the same code path the settings page uses.
docker exec "$CONTAINER" python -m app.seed_programs starter --dry-run

echo
echo "The campaign was NOT activated. See docs/DEPLOY.md section 4a for that."
echo "=== done ==="
