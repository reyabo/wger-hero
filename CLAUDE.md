# CLAUDE.md — wger-hero

Verbindliche Leitplanken für alle Claude-Code-Sitzungen in diesem Repository.
Antwortsprache: Deutsch. Code, Bezeichner und Commit-Nachrichten: Englisch.

## Was das Projekt ist

Selbstgehostete Gamification-Schicht über einer wger-Instanz. FastAPI mit
server-gerendertem Jinja2, SQLAlchemy 2.0, SQLite mit WAL, httpx, pydantic-settings.
Läuft in Produktion als Container `wger-hero` auf einem Debian-Server, intern Port 5000,
veröffentlicht auf 8091, hinter Caddy. Datenbank unter `/srv/data/wger-hero/wger_hero.db`.
Einbenutzerbetrieb.

## Leitidee — nicht aufweichen

Die README positioniert das Projekt ausdrücklich als regelbasierte, prüfbare Alternative
zu KI-lastigen Habit-Apps. Der Funktionsumfang wird erweitert, dieser Kern bleibt:

- Jede XP-, Level-, Streak- und Attributberechnung ist **deterministisch, sichtbar und
  nachträglich prüfbar**. Ein LLM vergibt niemals XP, bewertet keinen Fortschritt und
  gewichtet keine Belohnung.
- Ein LLM darf ausschließlich **Text vorschlagen**, den der Nutzer annimmt oder verwirft.
- Bei `LLM_ENABLED=false` — der Vorgabe — funktioniert die App vollständig.
- Keine externen CDNs, kein Tracking, keine Analytics. Zusätzliche Frontend-Bibliotheken
  werden mitgeliefert oder gar nicht genutzt.

## Harte Regeln

1. **wger ist nur lesend.** `app/wger_client.py` sendet ausschließlich GET. Kein POST,
   PUT oder DELETE gegen die wger-API, kein direkter Zugriff auf die wger-Datenbank.
2. **Keine destruktiven Operationen.** Kein `DROP`, kein `TRUNCATE`, kein Löschen von
   Datenbankdateien oder Volumes.
3. **Migrationen additiv und reversibel.** Ab Einführung von Alembic hat jede Revision ein
   funktionierendes `downgrade()`. Bestandsdaten und bestehende XP-Historie bleiben
   unverändert.
4. **Keine Secrets im Repository.** Neue Konfiguration immer in `.env.example` dokumentieren.
   Nie einen echten Token, Hash oder Schlüssel committen.
5. **Kein zusätzlicher Dienst** (Postgres, Redis, Broker, Suchindex) ohne vorherige
   Rückfrage. Der Zielserver hat 4 Kerne und 16 GB RAM und betreibt bereits wger,
   Firefly III, FitTrackee und Caddy. SQLite bleibt.
6. **Kein Framework-Wechsel** im Frontend ohne separate schriftliche Begründung.
7. **Bestehende Tests bleiben grün.** Kein Commit hinterlässt die App in einem nicht
   startfähigen Zustand.

## Grenzen der Cloud-Umgebung

Die Sitzung läuft in einer isolierten VM ohne Zugriff auf die Produktivinstanz. Es gibt
keine erreichbare wger-Instanz, keine Produktivdatenbank und keinen Zugriff auf den Server.

- Verifiziere ausschließlich über die Testsuite mit gemocktem wger-Client.
- Behaupte nie, etwas gegen eine echte wger-Instanz geprüft zu haben.
- Deploy-Schritte gehören nach `docs/DEPLOY.md`, sie werden nicht ausgeführt.

## Tests

```bash
pip install -e ".[dev]"
WGER_BASE_URL=https://wger.example.com python -m pytest
```

Alle Tests nutzen In-Memory-SQLite und einen gemockten wger-Client. Neue Fachlogik gehört
in ein Modul, das ohne Datenbank unit-testbar ist — so wie `app/xp.py` und `app/rewards.py`.

## Arbeitsweise je Sitzung

Eine Sitzung bearbeitet **genau einen Schritt** aus `docs/ROADMAP.md`.

1. Betroffene Dateien lesen, bevor du etwas änderst.
2. Tests zuerst, dann Implementierung.
3. Volle Testsuite ausführen.
4. Den Schritt in `docs/ROADMAP.md` abhaken.
5. Kleine, nachvollziehbare Commits.
6. Abschlussbericht: was geändert, warum, welche Migration, welche neue Env-Variable,
   was der Nutzer vor dem Deploy manuell prüfen muss.

Wenn ein Schritt größer wird als geplant oder eine Annahme nicht trägt: anhalten und
nachfragen, statt den Umfang eigenmächtig auszuweiten.

## Bestandsaufnahme (Stand vor dem Umbau)

**Module:** `main.py`, `config.py`, `database.py`, `models.py`, `wger_client.py`, `sync.py`,
`xp.py`, `habits.py`, `quests.py`, `stats.py`, `rewards.py`, `achievements.py`,
`seed_defaults.py`, `diagnostics.py`.

**Modelle:** `HeroProfile`, `HeroStat`, `StatXpEvent`, `XpEvent`, `SyncEvent`, `Habit`,
`HabitCompletion`, `Quest`, `Achievement`, `ApiCheckEvent`.

**Mechanik:** 10 Attribute in `stats.STATS`; 10 Kategorien in `rewards.CATEGORIES` mit
prozentualer Attributverteilung; XP aus Dauer × Aufwand; globales XP treibt das Level
(`1000 + level × 250`), Attribut-XP getrennt; Quests `manual` / `habit_count` /
`workout_count` über `daily` / `weekly` / `monthly` / `once`; Habits mit Wiederholung und
Zielzahl; Achievements; lesende wger-Synchronisation mit Deduplizierung über `source_hash`;
`/healthz`.

**Bekannte Lücken:** keine Streaks (`four-week-streak` ist Platzhalter), keine Ziele oder
Meilensteine, kein Journal, keine Fokus-Sessions, keine Zeitachse, keine Authentifizierung,
kein Alembic (`database._ADDED_COLUMNS` ist eine handgeführte `ALTER TABLE`-Liste),
`stats.build_radar()` ohne Darstellung, kein PWA-Manifest.
