# DEPLOY — wger-hero

Verbindlicher Ablauf für ein Update der Produktivinstanz. Jeder Schritt schreibt
sein Ergebnis in eine Datei, damit im Fehlerfall nachvollziehbar ist, wo es
gehakt hat.

**Nichts hiervon wird aus einer Entwicklungssitzung heraus ausgeführt.** Diese
Datei ist die Anleitung für den Betreiber am Server.

---

## 0. Vorbereitung und Annahmen

```bash
TS=$(date +%F_%H-%M-%S)
LOG=/srv/data/wger-hero/deploy-$TS
mkdir -p /srv/data/wger-hero
cd /pfad/zu/wger-hero
```

Der Container heißt `wger-hero`, läuft intern auf Port 5000, veröffentlicht auf
8091, hinter Caddy.

### Den Datenbankpfad zuerst feststellen

Der Pfad steht **an genau einer Stelle** und wird hier als Variable gesetzt, weil
er im Repository und auf dem Server abweichen kann:

```bash
# Auf Fairbrook:
DB_IN_CONTAINER=/data/wger_hero.sqlite
DB_ON_HOST=/srv/data/wger-hero/wger_hero.sqlite
```

> **Vor dem ersten Befehl prüfen.** Die `docker-compose.yml` im Repository setzt
> als Vorgabe `DATABASE_URL=sqlite:////data/wger_hero.db` — mit `.db`. Ein
> Eintrag unter `environment:` **überschreibt** `.env`, die Vorgabe gewinnt also
> gegen eine dort gesetzte Variable. Weicht der Server davon ab, muss seine
> `docker-compose.yml` den Pfad selbst setzen. Erst nachsehen, dann arbeiten:
>
> ```bash
> docker compose exec wger-hero printenv DATABASE_URL
> docker compose exec wger-hero ls -l /data
> ```
>
> Die tatsächliche Ausgabe ist maßgeblich, nicht diese Datei.

---

## 1. Aktuellen Containerzustand prüfen

```bash
docker compose ps                          > "$LOG-01-ps.txt"          2>&1
docker compose logs --tail=200 wger-hero  >> "$LOG-01-ps.txt"          2>&1
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8091/healthz \
                                          >> "$LOG-01-ps.txt"          2>&1
```

Erwartet: Container `running`, Healthcheck `200`. Läuft die Instanz schon vorher
nicht, wird nicht deployt, sondern erst die Ursache gesucht.

## 2. Git-Stand prüfen

```bash
git fetch origin                           > "$LOG-02-git.txt"         2>&1
git status --porcelain                    >> "$LOG-02-git.txt"         2>&1
git log --oneline -5 origin/main          >> "$LOG-02-git.txt"         2>&1
```

Erwartet: sauberes Arbeitsverzeichnis. Lokale Änderungen am Server sind ein
Warnsignal — sie gehen beim Pull verloren oder erzeugen einen Konflikt.

## 3. SQLite-Integrität prüfen

```bash
docker compose exec wger-hero sqlite3 -readonly "$DB_IN_CONTAINER" \
  "PRAGMA integrity_check;
   PRAGMA foreign_key_check;
   SELECT count(*) FROM xp_events;
   SELECT count(*) FROM habit_completions;
   SELECT count(*) FROM quest_completions;
   SELECT count(*) FROM japanese_save_imports;" \
                                           > "$LOG-03-integrity.txt"   2>&1
```

Erwartet: `ok`, keine Fremdschlüsselverletzung, plausible Zahlen. Diese Zahlen
sind später der Vergleichsmaßstab.

## 4. Konsistentes Online-Backup erstellen

`.backup` ist WAL-konsistent — im Gegensatz zu einem `cp` der Datei, das mitten
in einer Transaktion einen unbrauchbaren Stand einfängt.

```bash
docker compose exec wger-hero sqlite3 "$DB_IN_CONTAINER" \
  ".backup '/data/backup-$TS.sqlite'"      > "$LOG-04-backup.txt"      2>&1
ls -l /srv/data/wger-hero/backup-$TS.sqlite \
                                          >> "$LOG-04-backup.txt"      2>&1
docker compose exec wger-hero sqlite3 -readonly "/data/backup-$TS.sqlite" \
  "PRAGMA integrity_check;"               >> "$LOG-04-backup.txt"      2>&1
```

Erwartet: Datei vorhanden, nicht leer, `ok`. **Ohne geprüftes Backup wird nicht
weitergemacht.**

## 5. Altes Image als Rollback taggen

```bash
docker image inspect wger-hero-wger-hero:latest --format '{{.Id}}' \
                                           > "$LOG-05-image.txt"       2>&1
docker tag wger-hero-wger-hero:latest wger-hero:rollback-$TS \
                                          >> "$LOG-05-image.txt"       2>&1
docker image ls | grep wger-hero          >> "$LOG-05-image.txt"       2>&1
```

Damit existiert ein benanntes Ziel für Abschnitt 19.

## 6. Neues Image bauen

```bash
git pull origin main                       > "$LOG-06-pull.txt"        2>&1
docker compose build                      >> "$LOG-06-pull.txt"        2>&1
```

Der Build kopiert `app/`, `alembic.ini` **und** `migrations/` ins Image — ohne
die beiden letzten wäre der Migrationsbefehl in Abschnitt 7 und 10 im Container
nicht ausführbar.

## 7. Migration auf einer Kopie testen

Erst auf einer Kopie, nie zuerst auf den Echtdaten.

```bash
cp /srv/data/wger-hero/backup-$TS.sqlite \
   /srv/data/wger-hero/migrationstest-$TS.sqlite \
                                           > "$LOG-07-migtest.txt"     2>&1
docker compose run --rm \
  -e DATABASE_URL="sqlite:////data/migrationstest-$TS.sqlite" \
  wger-hero python -m alembic current     >> "$LOG-07-migtest.txt"     2>&1
docker compose run --rm \
  -e DATABASE_URL="sqlite:////data/migrationstest-$TS.sqlite" \
  wger-hero python -m alembic upgrade head \
                                          >> "$LOG-07-migtest.txt"     2>&1
docker compose run --rm \
  -e DATABASE_URL="sqlite:////data/migrationstest-$TS.sqlite" \
  wger-hero python -m alembic current     >> "$LOG-07-migtest.txt"     2>&1
docker compose exec wger-hero sqlite3 -readonly \
  "/data/migrationstest-$TS.sqlite" "PRAGMA integrity_check;" \
                                          >> "$LOG-07-migtest.txt"     2>&1
```

Erwartet am Ende: Revision `0006_optional_learning_metrics (head)` und `ok`.
Schlägt hier etwas fehl, wird **nicht** produktiv migriert.

**Erstübernahme einer Bestandsdatenbank:** ist noch nie mit Alembic gearbeitet
worden, existiert keine `alembic_version`-Tabelle. Dann **nicht** `upgrade`,
sondern erst `alembic stamp 0001_baseline` und danach `alembic upgrade head`.
Ein `upgrade` über ein bestehendes Schema scheitert laut — das ist Absicht und
durch einen Test abgesichert.

## 8. Container stoppen

```bash
docker compose stop wger-hero              > "$LOG-08-stop.txt"        2>&1
docker compose ps                         >> "$LOG-08-stop.txt"        2>&1
```

## 9. Finales Offline-Backup erstellen

Zwischen Abschnitt 4 und jetzt kann noch geschrieben worden sein. Bei
gestopptem Container ist die Datei ruhig:

```bash
cp /srv/data/wger-hero/wger_hero.sqlite \
   /srv/data/wger-hero/offline-$TS.sqlite  > "$LOG-09-offline.txt"     2>&1
ls -l /srv/data/wger-hero/*.sqlite*       >> "$LOG-09-offline.txt"     2>&1
```

`-wal` und `-shm` gehören zur Datenbank. Bei gestopptem Container sind sie in
der Regel bereits eingearbeitet; existieren sie noch, werden sie **mitkopiert**
und nicht einzeln gelöscht.

## 10. Produktive Migration ausführen

```bash
docker compose run --rm wger-hero python -m alembic current \
                                           > "$LOG-10-migrate.txt"     2>&1
docker compose run --rm wger-hero python -m alembic upgrade head \
                                          >> "$LOG-10-migrate.txt"     2>&1
docker compose run --rm wger-hero python -m alembic current \
                                          >> "$LOG-10-migrate.txt"     2>&1
```

Die Anwendung migriert **nie** von selbst — weder beim Import noch beim Start.
Das ist eine feste Architekturregel und durch einen Test abgesichert.

## 11. Neuen Container starten

```bash
docker compose up -d                       > "$LOG-11-up.txt"          2>&1
docker compose ps                         >> "$LOG-11-up.txt"          2>&1
docker compose logs --tail=100 wger-hero  >> "$LOG-11-up.txt"          2>&1
```

## 12. Healthcheck

```bash
sleep 5
curl -sS http://127.0.0.1:8091/healthz     > "$LOG-12-health.txt"      2>&1
curl -sS -o /dev/null -w 'login: %{http_code}\n' \
  http://127.0.0.1:8091/login             >> "$LOG-12-health.txt"      2>&1
```

## 13. Datenbankintegrität

```bash
docker compose exec wger-hero sqlite3 -readonly "$DB_IN_CONTAINER" \
  "PRAGMA integrity_check;"                > "$LOG-13-integrity.txt"   2>&1
```

## 14. Fremdschlüssel- und Bestandsprüfung

```bash
docker compose exec wger-hero sqlite3 -readonly "$DB_IN_CONTAINER" \
  "PRAGMA foreign_key_check;
   SELECT count(*) FROM xp_events;
   SELECT count(*) FROM habit_completions;
   SELECT count(*) FROM quest_completions;
   SELECT count(*) FROM goal_pause_intervals;
   SELECT count(*) FROM habit_schedule_days;
   SELECT count(*) FROM japanese_save_imports;" \
                                           > "$LOG-14-counts.txt"      2>&1
```

Die Zahlen müssen zu Abschnitt 3 passen. Eine gesunkene Zahl ist ein Abbruch­
grund — dann Abschnitt 19.

## 15. PWA-Smoke-Test

```bash
for path in /manifest.webmanifest /sw.js /offline \
            /static/icons/icon.svg /static/icons/icon-maskable.svg \
            /static/style.css; do
  curl -sS -o /dev/null -w "$path: %{http_code}\n" "https://DEINE-DOMAIN$path"
done                                       > "$LOG-15-pwa.txt"         2>&1
```

Erwartet: überall `200`. Die Installation als App setzt **HTTPS** voraus, also
den Weg über Caddy — über den direkten Port 8091 registriert kein Browser einen
Service Worker. Der Port ist zum Debuggen da, nicht als Installationsweg.

## 16. SAVE-Smoke-Test

Rein rechnend, ohne Schreibzugriff:

```bash
docker compose exec wger-hero python - <<'PY' > "$LOG-16-save.txt" 2>&1
from app.japanese_saves import parse_save
BLOCK = """=== 状態 SAVE ===
Datum: 2026-08-01 | Streak: 4
WaniKani-Level: 2
Bunpro-Level: N5
Grammatikpunkte im SRS: 37
Charakter: Lv 2 (見習い) | 433 / 1000 XP
語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
=== END SAVE ==="""
s = parse_save(BLOCK)
print("wanikani_level:", s.wanikani_level)
print("bunpro_level:  ", s.bunpro_level)
print("bunpro_points: ", s.bunpro_points)
print("hash:          ", s.normalized_hash())
PY
```

## 17. Starter-Dry-run

```bash
docker compose exec wger-hero python -m app.seed_programs starter --dry-run \
                                           > "$LOG-17-starter-dry.txt" 2>&1
```

Der Dry-run schreibt nichts. Die eigentliche Aktivierung ist ein getrennter,
bewusster Schritt — siehe Abschnitt 18.

## 18. Manuelle UI-Abnahme

Die manuelle UI-Abnahme ist der letzte Schritt vor der Freigabe.

Siehe [docs/UI_CHECKLIST.md](UI_CHECKLIST.md). Kurz: Login, `/today`, `/week`,
ein Habit abschließen und die Reaktion prüfen, ein pausiertes Ziel ansehen, die
Japanisch-Vorschau mit fehlenden Werten öffnen, Starter-Vorschau, Offline-Seite.

---

## 18a. Starter-Kampagne aktivieren (optional, getrennt)

Nicht Teil des Deployments. Reihenfolge:

```bash
# 1. Dry-run
docker compose exec wger-hero python -m app.seed_programs starter --dry-run \
                                           > "$LOG-20-starter-dry.txt"  2>&1
# 2. Vorschau prüfen — besonders Zeilen mit "Konflikt"
less "$LOG-20-starter-dry.txt"
# 3. Sichern
docker compose exec wger-hero sqlite3 "$DB_IN_CONTAINER" \
  ".backup '/data/vor-starter-$TS.sqlite'" > "$LOG-21-starter-backup.txt" 2>&1
# 4. Aktivieren
docker compose exec wger-hero python -m app.seed_programs starter \
                                           > "$LOG-22-starter-apply.txt" 2>&1
# 5. Ergebnis prüfen
less "$LOG-22-starter-apply.txt"
# 6. Abnahme: erneuter Dry-run muss "Keine Änderungen nötig" melden
docker compose exec wger-hero python -m app.seed_programs starter --dry-run \
                                           > "$LOG-23-starter-recheck.txt" 2>&1
```

Schritt 6 ist die eigentliche Abnahme. Meldet er noch offene Änderungen, nicht
erneut aktivieren, sondern die Ausgabe prüfen. Derselbe Weg steht in der
Oberfläche unter **Einstellungen → Starter-Kampagne**.

---

## Revisionsspezifische Prüfungen

Nach einem Update, das eine dieser Revisionen zum ersten Mal anwendet.

### Nach `0003_quest_completions`

`quest_completions` startet leer. Vorher abgeschlossene Quests bekommen keinen
nachträglichen Datensatz — es wird keine Historie erfunden. Sie werden trotzdem
nicht erneut belohnt, weil `completed_at` und `active` weiter greifen.

```bash
docker compose exec wger-hero sqlite3 -readonly "$DB_IN_CONTAINER" \
  "SELECT count(*) FROM quest_completions;
   SELECT name FROM sqlite_master WHERE type='index'
     AND name='ix_quest_completions_dedup_key';"
```

Erwartet: `0` und der Indexname. Fehlt der Index, ist die Migration
unvollständig — dann Abschnitt 19.

### Nach `0004_goal_pause_intervals`

`goal_pause_intervals` startet leer. Pausen, die vorher begonnen haben, bekommen
keinen Datensatz. Ein bereits pausiertes Ziel bleibt pausiert; erst beim
nächsten Pausieren entsteht ein Intervall.

```bash
docker compose exec wger-hero sqlite3 -readonly "$DB_IN_CONTAINER" \
  "SELECT count(*) FROM goal_pause_intervals;
   SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_goal_pause_open';"
```

Erwartet: `0` und eine Indexdefinition, die auf `WHERE ended_at IS NULL` endet.
Fehlt der Zusatz, wäre pro Ziel nur **eine** Pause überhaupt möglich.

### Nach `0005_habit_schedule_days`

`habit_schedule_days` startet leer. Bestehende Gewohnheiten bekommen keine
Planung — sie bleiben flexibel und erscheinen wie bisher an jedem Tag.

```bash
docker compose exec wger-hero sqlite3 -readonly "$DB_IN_CONTAINER" \
  "SELECT count(*) FROM habit_schedule_days;
   SELECT count(*) FROM habit_completions;
   SELECT sql FROM sqlite_master WHERE type='table' AND name='habit_schedule_days';"
```

Erwartet: `0`, die unveränderte Zahl der Completions und
`UNIQUE (habit_id, iso_weekday)` in der Tabellendefinition.

### Nach `0006_optional_learning_metrics`

`japanese_save_imports.wanikani_level` und `.bunpro_points` werden nullable.
Bestandswerte bleiben **unverändert**; eine bestehende `0` wird **nicht**
rückwirkend zu `NULL`. Erst neue Imports ohne die jeweilige Zeile speichern
`NULL` — „nicht angegeben".

```bash
docker compose exec wger-hero sqlite3 -readonly "$DB_IN_CONTAINER" \
  "SELECT count(*) FROM japanese_save_imports;
   SELECT count(*) FROM japanese_save_imports WHERE bunpro_points IS NULL;
   SELECT sql FROM sqlite_master WHERE type='table' AND name='japanese_save_imports';"
```

Erwartet: die unveränderte Zahl der Imports, direkt nach der Migration `0`
NULL-Werte, und eine Definition, in der `wanikani_level` und `bunpro_points`
**kein** `NOT NULL` mehr tragen.

> **Der Rückweg hinter `0006` ist verlustbehaftet.**
> `alembic downgrade 0005_habit_schedule_days` stellt `NOT NULL` wieder her und
> braucht dafür in jeder Zeile einen Wert. Zeilen, die „nicht angegeben"
> gespeichert haben, werden zu `0`. Gezählte Werte bleiben unangetastet, aber
> die Unterscheidung zwischen „nicht angegeben" und einer echten `0` ist danach
> **weg** und lässt sich aus der Datenbank nicht rekonstruieren.
>
> **Für einen vollständig datentreuen Rollback hinter
> `0006_optional_learning_metrics` ist das vor dem Deployment erstellte
> SQLite-Backup zu verwenden. Ein Alembic-Downgrade allein stellt die
> ursprüngliche NULL-Semantik nicht vollständig wieder her.**

---

## 19. Rückweg

Es gibt zwei verschiedene Rückwege. Welcher gebraucht wird, hängt davon ab, ob
die Datenbank schon verändert wurde.

### 19a. Nur die Anwendung zurückrollen

Wenn die Migration **nicht** gelaufen ist oder die alte Anwendung mit dem neuen
Schema noch arbeiten kann — additive Revisionen erlauben das in der Regel:

```bash
docker compose stop wger-hero              > "$LOG-30-rollback.txt"    2>&1
docker tag wger-hero:rollback-$TS wger-hero-wger-hero:latest \
                                          >> "$LOG-30-rollback.txt"    2>&1
docker compose up -d --no-build           >> "$LOG-30-rollback.txt"    2>&1
curl -sS http://127.0.0.1:8091/healthz    >> "$LOG-30-rollback.txt"    2>&1
```

Die Datenbank bleibt unangetastet. Das ist der schonende Weg und der erste, den
man versucht.

### 19b. Die Datenbank zurückrollen

Nur wenn die Daten selbst beschädigt sind oder hinter `0006` zurückgegangen
werden muss.

```bash
# 1. Container stoppen
docker compose stop wger-hero              > "$LOG-31-restore.txt"     2>&1

# 2. Den fehlgeschlagenen Stand separat sichern — nicht überschreiben.
mv /srv/data/wger-hero/wger_hero.sqlite \
   /srv/data/wger-hero/fehlgeschlagen-$TS.sqlite \
                                          >> "$LOG-31-restore.txt"     2>&1
# -wal und -shm gehören dazu: mitnehmen, damit der Stand analysierbar bleibt.
for suffix in -wal -shm; do
  [ -f "/srv/data/wger-hero/wger_hero.sqlite$suffix" ] && \
  mv "/srv/data/wger-hero/wger_hero.sqlite$suffix" \
     "/srv/data/wger-hero/fehlgeschlagen-$TS.sqlite$suffix"
done                                      >> "$LOG-31-restore.txt"     2>&1

# 3. Backup prüfen, bevor es eingespielt wird.
sqlite3 -readonly "/srv/data/wger-hero/offline-$TS.sqlite" \
  "PRAGMA integrity_check;"               >> "$LOG-31-restore.txt"     2>&1

# 4. Einspielen
cp /srv/data/wger-hero/offline-$TS.sqlite \
   /srv/data/wger-hero/wger_hero.sqlite   >> "$LOG-31-restore.txt"     2>&1

# 5. Altes Image starten
docker tag wger-hero:rollback-$TS wger-hero-wger-hero:latest \
                                          >> "$LOG-31-restore.txt"     2>&1
docker compose up -d --no-build           >> "$LOG-31-restore.txt"     2>&1
curl -sS http://127.0.0.1:8091/healthz    >> "$LOG-31-restore.txt"     2>&1
```

**Nie** die produktive Datei ohne geprüftes Backup überschreiben, und **nie**
den fehlgeschlagenen Stand einfach löschen: er ist die einzige Quelle für die
Frage, was schiefgegangen ist.

---

## Smoke-Test-Skripte

Rein lesend, nicht Teil des Deployments, jederzeit ausführbar:

```bash
bash scripts/smoke_full.sh          # Healthcheck, PWA, Seiten, DB, Starter-Dry-run
bash scripts/smoke_today_week.sh    # Tages- und Wochenlogik
bash scripts/smoke_pwa_starter.sh   # PWA-Assets und Starter-Vorschau
```

Jedes schreibt seine Ausgabe in eine TXT-Datei, gibt keine Secrets aus,
aktiviert nichts und ändert nichts.
