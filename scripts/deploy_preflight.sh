#!/usr/bin/env bash
# Read-only preflight before a wger-hero update.
#
# Answers the four questions that decide whether an update is safe to start:
# is the working tree clean, which compose files exist, do the auth secrets
# exist, and — the one that actually bites — does the effective compose
# configuration still mount them into the container.
#
# The failure this exists to prevent: `docker compose up -d --build` without the
# local `-f` files recreates the container without their mounts. The secrets are
# untouched on disk, but the app answers 503 "Auth not configured" on every
# path, including /healthz. Recreating secrets at that point would replace the
# password and drop every session for nothing.
#
# READS ONLY. It creates nothing, changes nothing, deletes nothing, and starts
# no container. It never prints the content of a secret — only whether a file
# exists, whether it is non-empty, and its mode, owner and size.
#
# Unlike backup.sh this does NOT use `set -e`: a preflight should survey
# everything and report all findings at once, not stop at the first one. It
# reports its verdict through the exit code instead.
#
#   bash scripts/deploy_preflight.sh
#   DC="docker compose -f docker-compose.yml -f docker-compose.auth.yml" \
#     bash scripts/deploy_preflight.sh
#
# Exit codes:  0 = ready   1 = usage/environment problem   2 = do not deploy
#
set -uo pipefail

# The compose invocation, as a string that is word-split on purpose so callers
# can pass the full -f set. Compose itself only ever loads docker-compose.yml
# and docker-compose.override.yml automatically.
DC="${DC:-docker compose}"
CONTAINER="${CONTAINER:-wger-hero}"

SECRET_FILES="${SECRET_FILES:-secrets/hero_password_hash secrets/hero_session_secret}"
SECRET_TARGETS="${SECRET_TARGETS:-/run/secrets/hero_password_hash /run/secrets/hero_session_secret}"

problems=0
notes=0

fail() { printf 'FEHLER   %s\n' "$*"; problems=$((problems + 1)); }
warn() { printf 'HINWEIS  %s\n' "$*"; notes=$((notes + 1)); }
okay() { printf 'OK       %s\n' "$*"; }
head_() { printf '\n== %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. Working tree
# ---------------------------------------------------------------------------
check_repository() {
  head_ "Repository"
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    fail "kein Git-Repository — im Repositoryverzeichnis ausführen"
    return
  fi
  local dirty
  dirty="$(git status --porcelain 2>/dev/null)"
  if [ -n "$dirty" ]; then
    warn "lokale Änderungen vorhanden — ein Pull würde sie berühren:"
    printf '%s\n' "$dirty" | sed 's/^/         /'
  else
    okay "Arbeitsverzeichnis sauber"
  fi
  okay "HEAD $(git rev-parse --short HEAD 2>/dev/null || echo unbekannt)"
}

# ---------------------------------------------------------------------------
# 2. Which compose files are present
# ---------------------------------------------------------------------------
# Compose auto-loads exactly these two; everything else needs an explicit -f.
is_auto_loaded() {
  case "$(basename "$1")" in
    docker-compose.yml|docker-compose.yaml|compose.yml|compose.yaml) return 0 ;;
    docker-compose.override.yml|docker-compose.override.yaml) return 0 ;;
    compose.override.yml|compose.override.yaml) return 0 ;;
    *) return 1 ;;
  esac
}

check_compose_files() {
  head_ "Compose-Dateien"
  local found=0 file
  for file in docker-compose*.yml docker-compose*.yaml compose*.yml compose*.yaml; do
    [ -e "$file" ] || continue
    found=$((found + 1))
    if is_auto_loaded "$file"; then
      okay "$file (wird automatisch geladen)"
    elif printf '%s' "$DC" | grep -qF -- "$file"; then
      okay "$file (explizit in DC)"
    else
      fail "$file existiert, steht aber NICHT in DC — beim nächsten 'up' gehen seine Mounts verloren"
    fi
  done
  [ "$found" -gt 0 ] || fail "keine Compose-Datei gefunden"
}

# ---------------------------------------------------------------------------
# 3. Do the secrets exist — metadata only, never the value
# ---------------------------------------------------------------------------
check_secret_files() {
  head_ "Auth-Secrets auf dem Host"
  local file
  for file in $SECRET_FILES; do
    if [ -s "$file" ]; then
      okay "$(stat -c 'vorhanden: %n mode=%a owner=%U group=%G size=%s' "$file" 2>/dev/null || echo "vorhanden: $file")"
    elif [ -e "$file" ]; then
      fail "$file ist LEER — die Anwendung wird sie ablehnen"
    else
      warn "$file fehlt (kein Fehler, falls AUTH_ENABLED=false)"
    fi
  done
}

# ---------------------------------------------------------------------------
# 4. The question that matters: are they mounted
# ---------------------------------------------------------------------------
check_effective_mounts() {
  head_ "Effektive Compose-Konfiguration"
  local config target
  # Only the mount targets are inspected. The full output resolves the service
  # environment and would show .env values, so it is never printed wholesale.
  if ! config="$($DC config 2>/dev/null)"; then
    warn "'$DC config' nicht ausführbar (kein Docker in dieser Umgebung?) — Mounts ungeprüft"
    return
  fi
  for target in $SECRET_TARGETS; do
    if printf '%s' "$config" | grep -qF -- "$target"; then
      okay "$target wird gemountet"
    else
      fail "$target wird NICHT gemountet — Container käme ohne Zugriffsschutz hoch (503)"
    fi
  done
}

# ---------------------------------------------------------------------------
main() {
  printf 'wger-hero deploy preflight — nur lesend\n'
  printf 'DC=%s\n' "$DC"

  check_repository
  check_compose_files
  check_secret_files
  check_effective_mounts

  head_ "Ergebnis"
  if [ "$problems" -gt 0 ]; then
    printf 'NICHT DEPLOYEN: %s Fehler, %s Hinweise\n' "$problems" "$notes"
    return 2
  fi
  printf 'bereit: 0 Fehler, %s Hinweise\n' "$notes"
  return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
