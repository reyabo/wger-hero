# DEPLOY — wger-hero

Deployment auf dem Zielserver. **Diese Schritte werden nicht von Claude Code
ausgeführt** — die Entwicklungsumgebung hat keinen Zugriff auf den Server, die
Produktivdatenbank oder eine echte wger-Instanz.

Alle Beispiele leiten ihre Ausgabe in zeitgestempelte Dateien um. Gib niemals
Secrets aus: keine Tokens, keine Hashes, keine Session-Keys.

```bash
TS=$(date +%Y-%m-%d_%H-%M-%S)
LOG=/srv/data/wger-hero/deploy-$TS
mkdir -p "$(dirname "$LOG")"
```

Umgebungsannahmen laut Projektvorgabe: Container `wger-hero`, intern Port 5000,
Host-Port 8091, hinter Caddy, Datenbank `/srv/data/wger-hero/wger_hero.db`.
Die konkrete Caddy- und Compose-Konfiguration des Servers ist hier **nicht**
dokumentiert, weil sie in diesem Repository nicht vorliegt — nicht erfinden,
sondern auf dem Server nachsehen.

---

## 1. Bestandsprüfung

```bash
docker compose ps                          > "$LOG-01-ps.txt"        2>&1
docker compose exec wger-hero python -c "import app; print('ok')" \
                                           > "$LOG-02-import.txt"    2>&1
sqlite3 /srv/data/wger-hero/wger_hero.db ".tables" \
                                           > "$LOG-03-tables.txt"    2>&1
sqlite3 /srv/data/wger-hero/wger_hero.db \
  "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version';" \
                                           > "$LOG-04-alembic.txt"   2>&1
```

Ist `alembic_version` **nicht** vorhanden, ist die Datenbank noch nicht
übernommen → Abschnitt 3a. Ist sie vorhanden → Abschnitt 3b.

## 2. Sicherung inklusive WAL-Zustand

Die Datenbank läuft im WAL-Modus. Ein blosses `cp` der `.db`-Datei ist **nicht**
konsistent, weil die zuletzt geschriebenen Seiten noch in `-wal` liegen können.
`.backup` schreibt einen konsistenten Stand, auch bei laufendem Container.

```bash
sqlite3 /srv/data/wger-hero/wger_hero.db \
  ".backup '/srv/data/wger-hero/wger_hero.db.bak-$TS'" \
                                           > "$LOG-05-backup.txt"    2>&1
ls -l /srv/data/wger-hero/wger_hero.db.bak-$TS \
                                           >> "$LOG-05-backup.txt"   2>&1
sqlite3 "/srv/data/wger-hero/wger_hero.db.bak-$TS" "PRAGMA integrity_check;" \
                                           > "$LOG-06-integrity.txt" 2>&1
```

`integrity_check` muss `ok` liefern. Erst dann weiter.

## 3. Migration

Migration ist ein **expliziter** Schritt. Die Anwendung migriert weder beim
Import noch beim Start — das ist durch Tests abgesichert.

### 3a. Bestehende Datenbank übernehmen (einmalig)

Die Datenbank hat bereits alle Tabellen. Die Baseline-Revision darf hier
**nicht** ausgeführt werden — sie würde an bereits existierenden Tabellen
scheitern. Stattdessen wird der Stand gestempelt:

```bash
docker compose exec wger-hero alembic stamp 0001_baseline \
                                           > "$LOG-07-stamp.txt"     2>&1
docker compose exec wger-hero alembic current \
                                           >> "$LOG-07-stamp.txt"    2>&1
```

Danach einmal Abschnitt 3b, um spätere Revisionen anzuwenden.

### 3b. Migration anwenden

```bash
docker compose exec wger-hero alembic current \
                                           > "$LOG-08-before.txt"    2>&1
docker compose exec wger-hero alembic upgrade head \
                                           > "$LOG-09-upgrade.txt"   2>&1
docker compose exec wger-hero alembic current \
                                           >> "$LOG-09-upgrade.txt"  2>&1
```

### 3c. Nach Revision 0003 (Quest-Abschlüsse)

`quest_completions` startet leer. Bereits abgeschlossene Quests bekommen
**keinen** nachträglichen Datensatz — es wird keine Historie erfunden. Sie
werden dennoch nicht erneut belohnt, weil `completed_at` und `active` weiter
greifen. Prüfen, dass die Tabelle existiert und der Unique-Index steht:

```bash
docker compose exec wger-hero sqlite3 /data/wger_hero.db \
  "SELECT count(*) FROM quest_completions;
   SELECT name FROM sqlite_master WHERE type='index'
     AND name='ix_quest_completions_dedup_key';" \
                                           > "$LOG-09b-completions.txt"  2>&1
```

Erwartet: `0` und der Indexname. Fehlt der Index, ist die Migration
unvollständig — dann nicht weitermachen, sondern Abschnitt 8 (Rückweg).

### 3d. Nach Revision 0004 (Pausenzeiträume der Ziele)

`goal_pause_intervals` startet ebenfalls leer. Pausen, die **vor** dieser
Revision begonnen haben, bekommen keinen nachträglichen Datensatz — auch hier
wird keine Historie erfunden. Ein bereits pausiertes Ziel bleibt pausiert und
wird weiterhin nicht bewertet; seine zurückliegenden Wochen zählen so wie
bisher. Erst ab dem nächsten Pausieren entsteht ein Intervall.

Prüfen, dass die Tabelle und der **partielle** Unique-Index existieren:

```bash
docker compose exec wger-hero sqlite3 /data/wger_hero.db \
  "SELECT count(*) FROM goal_pause_intervals;
   SELECT sql FROM sqlite_master WHERE type='index'
     AND name='ux_goal_pause_open';" \
                                           > "$LOG-09c-pauses.txt"     2>&1
```

Erwartet: `0` und eine Indexdefinition, die auf `WHERE ended_at IS NULL` endet.
Fehlt der Zusatz, wäre pro Ziel nur **eine** Pause überhaupt möglich — dann
nicht weitermachen, sondern Abschnitt 8 (Rückweg).

### 3e. Nach Revision 0005 (Wochenplanung der Gewohnheiten)

`habit_schedule_days` startet leer. Bestehende Gewohnheiten bekommen **keine**
Planung — sie bleiben flexibel und erscheinen wie bisher an jedem Tag. Weder
Gewohnheiten noch Completions werden verändert.

```bash
docker compose exec wger-hero sqlite3 /data/wger_hero.db \
  "SELECT count(*) FROM habit_schedule_days;
   SELECT count(*) FROM habit_completions;
   SELECT sql FROM sqlite_master WHERE type='table'
     AND name='habit_schedule_days';" \
                                           > "$LOG-09d-schedule.txt"    2>&1
```

Erwartet: `0`, die unveränderte Anzahl der Completions und eine Tabellen-
definition mit `UNIQUE (habit_id, iso_weekday)`. Fehlt der Unique-Zusatz, könnte
ein Wochentag doppelt gespeichert werden — dann nicht weitermachen, sondern
Abschnitt 8 (Rückweg). Rückweg dieser Revision:
`alembic downgrade 0004_goal_pause_intervals` plus Backup; das Löschen der
Planungstabelle lässt Gewohnheiten und Completions unberührt (getestet).

## 4. Container neu bauen

```bash
git -C /pfad/zu/wger-hero pull origin main > "$LOG-10-pull.txt"      2>&1
docker compose up -d --build               > "$LOG-11-build.txt"     2>&1
docker compose ps                          >> "$LOG-11-build.txt"    2>&1
```

## 5. Healthcheck

```bash
curl -fsS http://127.0.0.1:8091/healthz    > "$LOG-12-health.txt"    2>&1
```

Erwartet: `{"status":"ok"}`.

## 6. Funktionsprüfung

Im Browser, angemeldet:

- Übersicht lädt, XP und Level plausibel
- Sync auslösen, keine neuen Fehler
- Habits und Quests sichtbar, Fortschritt unverändert
- `/japanese` lädt, letzter Import unverändert
- `/stats` zeigt das Radar
- `/settings` zeigt Token-Status ohne Wert

Datenbestand gegenprüfen:

```bash
sqlite3 /srv/data/wger-hero/wger_hero.db \
  "SELECT level,total_xp FROM hero_profile;
   SELECT count(*) FROM xp_events;
   SELECT count(*) FROM habit_completions;
   SELECT count(*) FROM japanese_save_imports;" \
                                           > "$LOG-13-counts.txt"    2>&1
```

Die Zahlen müssen zum Stand vor der Migration passen.

## 7. Logprüfung

```bash
docker compose logs --tail=200 wger-hero   > "$LOG-14-logs.txt"      2>&1
grep -iE "error|traceback|exception" "$LOG-14-logs.txt" \
                                           > "$LOG-15-errors.txt"    2>&1 || true
```

`$LOG-15-errors.txt` sollte leer sein. Prüfe zusätzlich, dass **kein** Token,
Hash oder Session-Key im Log steht.

## 8. Rückweg

Zuerst der reguläre Weg über Alembic:

```bash
docker compose exec wger-hero alembic downgrade -1 \
                                           > "$LOG-16-downgrade.txt" 2>&1
docker compose up -d --build               >> "$LOG-16-downgrade.txt" 2>&1
```

Wenn das nicht reicht, zurück auf das Backup. Container vorher stoppen, damit
keine Schreibvorgänge laufen. Die alte Datei wird **nicht gelöscht**, sondern
zur Seite gelegt:

```bash
docker compose stop wger-hero              > "$LOG-17-restore.txt"   2>&1
mv /srv/data/wger-hero/wger_hero.db \
   /srv/data/wger-hero/wger_hero.db.pre-restore-$TS \
                                           >> "$LOG-17-restore.txt"  2>&1
rm -f /srv/data/wger-hero/wger_hero.db-wal /srv/data/wger-hero/wger_hero.db-shm
cp "/srv/data/wger-hero/wger_hero.db.bak-$TS" \
   /srv/data/wger-hero/wger_hero.db        >> "$LOG-17-restore.txt"  2>&1
docker compose start wger-hero             >> "$LOG-17-restore.txt"  2>&1
curl -fsS http://127.0.0.1:8091/healthz    >> "$LOG-17-restore.txt"  2>&1
```

Die `-wal`/`-shm`-Dateien gehören zur alten Datenbank und müssen weg, sonst
mischt SQLite den alten WAL-Inhalt in die wiederhergestellte Datei.

---

## Secrets erzeugen

Werden ausserhalb des Repositories erzeugt und schreibgeschützt eingebunden.
Nie committen, nie ins Log schreiben.

```bash
# Session-Schlüssel
umask 077
openssl rand -hex 32 > /srv/secrets/hero_session_secret

# Passwort-Hash (Argon2), Passwort interaktiv, nicht in der Shell-History
python - <<'EOF' > /srv/secrets/hero_password_hash
from argon2 import PasswordHasher
from getpass import getpass
print(PasswordHasher().hash(getpass("Passwort: ")), end="")
EOF

chmod 400 /srv/secrets/hero_session_secret /srv/secrets/hero_password_hash
```

Einbindung read-only per Compose, Pfade über `AUTH_PASSWORD_HASH_FILE` und
`SESSION_SECRET_FILE`. Details in `.env.example`.
