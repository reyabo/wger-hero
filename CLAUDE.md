CLAUDE.md — wger-hero
Verbindliche Leitplanken für alle Claude-Code-Sitzungen in diesem Repository. Antwortsprache: Deutsch. Code, Bezeichner und Commit-Nachrichten: Englisch.
Was das Projekt ist
Selbstgehostete Gamification-Schicht über einer wger-Instanz. FastAPI mit server-gerendertem Jinja2, SQLAlchemy 2.0, SQLite mit WAL, httpx, pydantic-settings. Läuft in Produktion als Container `wger-hero` auf einem Debian-Server, intern Port 5000, veröffentlicht auf 8091, hinter Caddy. Datenbank unter `/srv/data/wger-hero/wger_hero.db`. Einbenutzerbetrieb.
Der Kern ist ein prüfbares Hauptbuch über selbst definierten Aufwand. Der Wert liegt im Bestand — `HabitCompletion`, `XpEvent`, `StatXpEvent`, `SyncEvent` —, nicht im Funktionsumfang. Was den Bestand schützt, korrigierbar macht und auswertbar hält, hat Vorrang vor jeder neuen Erfassungsfläche.
Leitidee — nicht aufweichen

* Keine KI-Funktionen. Keine Modellaufrufe, keine externen KI-APIs, keine generierten Empfehlungen. Das ist eine bewusste Produktentscheidung, kein offener Punkt.
* Jede XP-, Level- und Attributberechnung ist deterministisch, sichtbar und nachträglich prüfbar.
* Keine externen CDNs, kein Tracking, keine Analytics. Zusätzliche Frontend-Bibliotheken werden mitgeliefert oder gar nicht genutzt.
* Kein Produktivitätsdruck. Mechaniken, die Verlust erzeugen, brauchen eine ausdrückliche Begründung.

Harte Regeln

1. wger ist nur lesend. `app/wger_client.py` sendet ausschließlich GET. Kein POST, PUT oder DELETE gegen die wger-API, kein direkter Zugriff auf die wger-Datenbank.
2. Der Bestand wird nie gelöscht, nur ergänzt. Korrekturen entstehen durch Gegenbuchungen und Markierungen, nicht durch `DELETE` oder Überschreiben. Kein `DROP`, kein `TRUNCATE`.
3. Migrationen additiv und reversibel. Ab Einführung von Alembic hat jede Revision ein funktionierendes `downgrade()`. Bestehende XP-Historie bleibt unverändert.
4. Keine Secrets im Repository. Neue Konfiguration immer in `.env.example` dokumentieren. Nie einen echten Token, Hash oder Schlüssel committen.
5. Kein zusätzlicher Dienst (Postgres, Redis, Broker, Suchindex) ohne vorherige Rückfrage. Der Zielserver hat 4 Kerne und 16 GB RAM und betreibt bereits wger, Firefly III, FitTrackee und Caddy. SQLite bleibt.
6. Kein Framework-Wechsel im Frontend, keine neue Laufzeitabhängigkeit ohne Rückfrage.
7. Bestehende Tests bleiben grün. Kein Commit hinterlässt die App in einem nicht startfähigen Zustand.
8. Zurückhaltung ist erwünscht. Das Projekt hat rund 6.800 Zeilen und wird von einer Person gepflegt. Vorhandenes verbessern schlägt Neues danebenstellen. Wenn ein Schritt größer wird als beschrieben: anhalten und nachfragen.

Grenzen der Cloud-Umgebung
Die Sitzung läuft in einer isolierten VM ohne Zugriff auf die Produktivinstanz. Es gibt keine erreichbare wger-Instanz, keine Produktivdatenbank und keinen Zugriff auf den Server.

* Verifiziere ausschließlich über die Testsuite mit gemocktem wger-Client.
* Behaupte nie, etwas gegen eine echte wger-Instanz geprüft zu haben.
* Deploy-Schritte gehören nach `docs/DEPLOY.md`, sie werden nicht ausgeführt.

Tests

```bash
pip install -e ".[dev]"
WGER_BASE_URL=https://wger.example.com python -m pytest

```

Alle Tests nutzen In-Memory-SQLite und einen gemockten wger-Client. Neue Fachlogik gehört in ein Modul, das ohne Datenbank unit-testbar ist — so wie `app/xp.py` und `app/rewards.py`.
Arbeitsweise je Sitzung
Eine Sitzung bearbeitet genau einen Schritt aus `docs/ROADMAP.md`.

1. Betroffene Dateien lesen, bevor du etwas änderst.
2. Tests zuerst, dann Implementierung.
3. Volle Testsuite ausführen.
4. Den Schritt in `docs/ROADMAP.md` abhaken.
5. Kleine, nachvollziehbare Commits.
6. Abschlussbericht: was geändert, warum, welche Migration, welche neue Env-Variable, was der Nutzer vor dem Deploy manuell prüfen muss.

Bestandsaufnahme (Stand vor dem Umbau)
Module: `main.py`, `config.py`, `database.py`, `models.py`, `wger_client.py`, `sync.py`, `xp.py`, `habits.py`, `quests.py`, `stats.py`, `rewards.py`, `achievements.py`, `seed_defaults.py`, `diagnostics.py`.
Modelle: `HeroProfile`, `HeroStat`, `StatXpEvent`, `XpEvent`, `SyncEvent`, `Habit`, `HabitCompletion`, `Quest`, `Achievement`, `ApiCheckEvent`.
Mechanik: 10 Attribute in `stats.STATS`; 10 Kategorien in `rewards.CATEGORIES` mit prozentualer Attributverteilung; XP aus Dauer × Aufwand; globales XP treibt das Level (`1000 + level × 250`), Attribut-XP getrennt; Quests `manual` / `habit_count` / `workout_count` über `daily` / `weekly` / `monthly` / `once`; Habits mit Wiederholung und Zielzahl; Achievements; lesende wger-Synchronisation mit Deduplizierung über `source_hash`; `/healthz`.
Bekannte Lücken:

* Kein Alembic — `database._ADDED_COLUMNS` ist eine handgeführte `ALTER TABLE`-Liste
* Bestand nicht korrigierbar: `habits.py` setzt `completed_at=now` fest, kein Undo, kein Nachtragen
* Kein Export der Historie
* Kein automatischer Sync, nur `POST /sync` von Hand
* Keine Auswertung über längere Zeiträume; `stats.build_radar()` existiert ohne Darstellung
* Keine Streak- oder Konsistenzlogik; Achievement `four-week-streak` ist Platzhalter
* Keine Authentifizierung
* Oberfläche nicht für kleine Displays ausgelegt
