#!/usr/bin/env bash
# Automatic, WAL-consistent SQLite backup for wger-hero.
#
# Takes an online backup through SQLite's own backup API, verifies the result
# with PRAGMA integrity_check, and removes automatic backups older than the
# retention window. Safe to run unattended from cron.
#
# This is the one script in scripts/ that WRITES — it creates and deletes backup
# files. It still never touches the live database, and it never deletes a file
# it did not create itself: rotation is confined to its own directory AND its
# own filename pattern, so a deployment snapshot (backup-*, offline-*, vor-*)
# or a forensic fehlgeschlagen-* copy can never be reached.
#
# It prints no token, no password hash, no session secret and no .env content.
#
# Unlike the smoke scripts, this uses `set -e`: a smoke test should survey
# everything and keep going, but a backup that failed must never be followed by
# a rotation that deletes good older copies.
#
#   bash scripts/backup.sh
#   KEEP_DAYS=30 bash scripts/backup.sh
#
set -euo pipefail

CONTAINER="${CONTAINER:-wger-hero}"
# Container path of the live database. On Fairbrook this is wger_hero.sqlite;
# the repository default in docker-compose.yml says wger_hero.db. Verify with
#   docker exec wger-hero printenv DATABASE_URL
DB="${DB:-/data/wger_hero.sqlite}"
# A dedicated directory, so rotation can never wander into the shared data
# directory where the live database and the hand-made snapshots live.
BACKUP_DIR_IN_CONTAINER="${BACKUP_DIR_IN_CONTAINER:-/data/backups}"
BACKUP_DIR_ON_HOST="${BACKUP_DIR_ON_HOST:-/srv/data/wger-hero/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

# A dedicated prefix, so rotation can never match a deployment snapshot even if
# one were somehow moved into this directory.
PREFIX="auto-backup-"


backup_filename() {
    printf '%s%s.sqlite\n' "$PREFIX" "$1"
}

# The single most important rule in this script: what rotation is allowed to
# delete. Only this script's own name shape matches — the literal prefix, a
# fully digit-shaped timestamp, and the .sqlite suffix. wger_hero.sqlite,
# its -wal and -shm companions, backup-*, offline-*, vor-* and fehlgeschlagen-*
# all fail it.
backup_name_matches() {
    case "$1" in
        "$PREFIX"[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9]-[0-9][0-9]-[0-9][0-9].sqlite)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}


fail() {
    echo "FEHLER: $*" >&2
    exit 1
}


main() {
    local ts name target_in_container target_on_host size removed

    ts="$(date +%F_%H-%M-%S)"
    name="$(backup_filename "$ts")"
    target_in_container="$BACKUP_DIR_IN_CONTAINER/$name"
    target_on_host="$BACKUP_DIR_ON_HOST/$name"

    # A stopped container must be a loud failure, never a silent skip: a backup
    # that quietly did not happen is worse than one that visibly failed.
    docker exec "$CONTAINER" true \
        || fail "Container '$CONTAINER' ist nicht erreichbar — keine Sicherung erstellt."

    docker exec "$CONTAINER" mkdir -p "$BACKUP_DIR_IN_CONTAINER" \
        || fail "Sicherungsverzeichnis konnte nicht angelegt werden."

    if [ -e "$target_on_host" ]; then
        echo "Sicherung $name existiert bereits — nichts zu tun."
        exit 0
    fi

    # Paths go through the environment, never interpolated into the Python
    # source: a path containing a quote would otherwise become code.
    # The stdlib sqlite3 module is used rather than the sqlite3 CLI, which the
    # image does not install — Connection.backup() is the same WAL-consistent
    # online backup, and it needs no extra package.
    docker exec -e SRC="$DB" -e DST="$target_in_container" "$CONTAINER" python - <<'PY' \
        || fail "Die Sicherung konnte nicht erstellt werden."
import os
import sqlite3

src = sqlite3.connect(f"file:{os.environ['SRC']}?mode=ro", uri=True)
dst = sqlite3.connect(os.environ["DST"])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
PY

    # Verified in a separate call, so a crash in the writer cannot be mistaken
    # for a pass.
    docker exec -e DST="$target_in_container" "$CONTAINER" python - <<'PY' \
        || fail "Die Integritätsprüfung der Sicherung ist fehlgeschlagen."
import os
import sqlite3
import sys

con = sqlite3.connect(f"file:{os.environ['DST']}?mode=ro", uri=True)
try:
    result = con.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    con.close()
if result != "ok":
    sys.stderr.write(f"integrity_check: {result}\n")
    sys.exit(1)
PY

    [ -f "$target_on_host" ] \
        || fail "Sicherung $name ist auf dem Host nicht aufgetaucht — Pfade prüfen."

    size="$(wc -c < "$target_on_host")"
    # One SQLite page. A valid database is never smaller, so anything below it
    # is a truncated write.
    if [ "$size" -lt 4096 ]; then
        rm -f "$target_on_host"
        fail "Sicherung $name war nur $size Bytes groß — verworfen, nichts rotiert."
    fi

    # Rotation runs only after every check above passed. Never delete an old,
    # good backup to make room for a bad new one.
    [ -n "$BACKUP_DIR_ON_HOST" ] && [ "$BACKUP_DIR_ON_HOST" != "/" ] \
        || fail "Unplausibles Sicherungsverzeichnis — es wird nichts gelöscht."
    [ -d "$BACKUP_DIR_ON_HOST" ] \
        || fail "Sicherungsverzeichnis fehlt — es wird nichts gelöscht."
    [ ! -e "$BACKUP_DIR_ON_HOST/$(basename "$DB")" ] \
        || fail "Im Sicherungsverzeichnis liegt die Live-Datenbank — es wird nichts gelöscht."

    # Four independent restrictions, any one of which alone would be enough:
    # no recursion, no directories, only this script's own name shape, and only
    # past the retention window. -print before -delete makes every removal
    # visible in the cron mail.
    removed="$(find "$BACKUP_DIR_ON_HOST" \
        -mindepth 1 -maxdepth 1 \
        -type f \
        -name "${PREFIX}[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9]-[0-9][0-9]-[0-9][0-9].sqlite" \
        -mtime +"$KEEP_DAYS" \
        -print -delete | wc -l)"

    echo "OK $name, $size Bytes, integrity_check ok, $removed alte Sicherung(en) entfernt"
}


# Sourceable without side effects, so the naming rules can be tested directly.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
