#!/usr/bin/env bash
# Read-only acceptance smoke test for wger-hero.
#
# Run this ON the server AFTER a deployment. It only reads: no page is posted
# to, no habit is completed, the starter campaign is NOT activated, no migration
# is applied, and the database is opened with sqlite3 -readonly. Output goes to
# a TXT file in the current directory.
#
# No token, password hash, session secret or .env content is ever printed, and
# /settings is deliberately not fetched.
#
#   bash scripts/smoke_full.sh
#
set -uo pipefail

CONTAINER="${CONTAINER:-wger-hero}"
# HTTPS name, because a service worker is only registered over HTTPS or
# localhost — testing the plain 8091 port would not prove installability.
BASE="${BASE:-https://wger-hero.example.invalid}"
# On Fairbrook the database file is /data/wger_hero.sqlite. Verify with
#   docker exec wger-hero printenv DATABASE_URL
# before trusting this default.
DB="${DB:-/data/wger_hero.sqlite}"
OUT="wger-hero-smoke-$(date +%F_%H-%M-%S).txt"

exec > "$OUT" 2>&1

echo "=== wger-hero smoke test (read-only) ==="
date -Iseconds
echo "container=$CONTAINER base=$BASE db=$DB"

echo
echo "--- 1. Health and login ---"
curl -sS -o /dev/null -w 'healthz: HTTP %{http_code}\n' "$BASE/healthz"
curl -sS "$BASE/healthz"; echo
curl -sS -o /dev/null -w 'login:   HTTP %{http_code}\n' "$BASE/login"

echo
echo "--- 2. Application pages ---"
# With AUTH_ENABLED=true these redirect to /login (303). That IS the expected
# result and proves they are protected; with auth off they answer 200. Any
# other status is a problem.
for path in /today /week /goals /habits /quests /japanese /settings/starter; do
  curl -sS -o /dev/null -w "$path: HTTP %{http_code} -> %{redirect_url}\n" "$BASE$path"
done

echo
echo "--- 3. Protected pages must not be cacheable ---"
for path in /today /week; do
  echo -n "$path: "
  curl -sS -o /dev/null -D - "$BASE$path" | grep -i '^cache-control' || echo "(kein Header)"
done

echo
echo "--- 4. PWA assets ---"
for path in /manifest.webmanifest /sw.js /offline \
            /static/icons/icon.svg /static/icons/icon-maskable.svg \
            /static/style.css; do
  curl -sS -o /dev/null -w "$path: HTTP %{http_code} (%{content_type})\n" "$BASE$path"
done

echo
echo "--- 5. Manifest and service worker boundary ---"
curl -sS "$BASE/manifest.webmanifest" \
  | python3 -c 'import json,sys; m=json.load(sys.stdin); print("\n".join(f"{k}: {m[k]}" for k in ("name","short_name","start_url","scope","display")))'
echo "cached paths declared by the worker:"
curl -sS "$BASE/sw.js" | grep -E "CACHE_VERSION|'/offline'|'/static/|'/manifest" || true

echo
echo "--- 6. Alembic revision ---"
docker exec "$CONTAINER" python -m alembic current 2>&1 | tail -5
echo "expected head: 0006_optional_learning_metrics"

echo
echo "--- 7. Database integrity and foreign keys (read-only) ---"
docker exec "$CONTAINER" sqlite3 -readonly "$DB" \
  "PRAGMA integrity_check;
   PRAGMA foreign_key_check;
   SELECT 'goals',                count(*) FROM goals;
   SELECT 'habits',               count(*) FROM habits;
   SELECT 'habit_schedule_days',  count(*) FROM habit_schedule_days;
   SELECT 'habit_completions',    count(*) FROM habit_completions;
   SELECT 'quests',               count(*) FROM quests;
   SELECT 'quest_completions',    count(*) FROM quest_completions;
   SELECT 'goal_pause_intervals', count(*) FROM goal_pause_intervals;
   SELECT 'japanese_imports',     count(*) FROM japanese_save_imports;
   SELECT 'xp_events',            count(*) FROM xp_events;"

echo
echo "--- 8. Japanese SAVE parser against the deployed code (pure, no write) ---"
# parse_save(raw: str) -> JapaneseSave; it touches neither session nor database.
docker exec "$CONTAINER" python - <<'PY'
from app.japanese_saves import parse_save

MINIMAL = """=== 状態 SAVE ===
Datum: 2026-08-01 | Streak: 4
Charakter: Lv 2 (見習い) | 433 / 1000 XP
語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
=== END SAVE ==="""

FULL = MINIMAL.replace(
    "Charakter:",
    "WaniKani-Level: 2\nBunpro-Level: N5\nGrammatikpunkte im SRS: 37\nCharakter:",
)

for label, block in (("minimal", MINIMAL), ("mit Lernstaenden", FULL)):
    s = parse_save(block)
    print("%-18s wanikani=%s bunpro=%s srs=%s" % (
        label, s.wanikani_level, s.bunpro_level, s.bunpro_points))
    print("%-18s hash=%s" % ("", s.normalized_hash()))
PY

echo
echo "--- 9. Starter campaign preview (dry-run, writes nothing) ---"
docker exec "$CONTAINER" python -m app.seed_programs starter --dry-run

echo
echo "The campaign was NOT activated and no habit was completed."
echo "For activation see docs/DEPLOY.md section 18a; for the manual UI"
echo "acceptance see docs/UI_CHECKLIST.md."
echo "=== done ==="
