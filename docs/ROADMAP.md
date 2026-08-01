# ROADMAP — wger-hero

Sequenzierung des Umbaus zum persönlichen Quest-System (Einbenutzerbetrieb).
Jede Sitzung bearbeitet **genau einen Schritt**. Reihenfolge ist bindend, weil
spätere Schritte auf früheren aufsetzen.

Legende: `[ ]` offen · `[x]` erledigt · `[~]` teilweise

## Ausgangsstand

- Basis: `f9c4c5e` auf `feature/data-driven-routine-quests`, **394 Tests grün**.
- **Abweichung:** `main` steht auf `be2bbce` mit **350 Tests**. Die drei Commits
  `e5f9182`, `7acb440`, `f9c4c5e` (match_text, workout_variety, Zeitraum-Quests)
  sind nicht gemergt. Das Epic setzt darauf auf — dieser Strang muss vor dem
  Deployment nach `main`.
- Kein `docs/`-Verzeichnis vorhanden gewesen; mit dieser Datei angelegt.

---

## 0 — Vorbereitung

- [x] `git status` sauber, Testsuite grün, Ausgangsstand dokumentiert
- [x] `docs/ROADMAP.md` angelegt

## 1 — Alembic  ✅

Ziel: additive, reversible Migrationen; `_ADDED_COLUMNS` als dokumentierte
Übergangslösung erhalten, bis die Übernahme bestehender Datenbanken getestet ist.

- [x] `alembic` als Projektabhängigkeit
- [x] `alembic.ini` + `migrations/env.py` gegen `app.models.Base.metadata`
- [x] Baseline-Revision, die das aktuelle Schema exakt abbildet
- [x] Jede Revision mit funktionierendem `downgrade()`
- [x] **Keine** automatische Migration beim Import oder Start
- [x] Test: Upgrade auf bestehender DB, Downgrade, Bestand unverändert
- [x] `docs/DEPLOY.md`: Bestandsprüfung, WAL-konsistente Sicherung, Migration,
      Neubau, Healthcheck, Funktions- und Logprüfung, Rückweg

Migration: ja (Baseline). Rollback: `alembic downgrade` + Backup.

## 2 — Zugriffsschutz  ✅

- [x] Argon2-Hash aus Secret-Datei, Session-Key aus Secret-Datei
- [x] Login / POST-Logout, keine Registrierung, keine Passwortänderung
- [x] Signierte Sitzung, Cookie `HttpOnly` + `SameSite=Lax` + `Secure` (Prod)
- [x] Login-Versuche begrenzen (in-process, kein zusätzlicher Dienst)
- [x] CSRF für **alle** zustandsändernden Formulare, auch bestehende
- [x] Öffentlich nur: `/healthz`, `/login`, statische Assets, Manifest, SW, Offline
- [x] Neue Variablen: `AUTH_ENABLED`, `AUTH_PASSWORD_HASH_FILE`,
      `SESSION_SECRET_FILE`, `SESSION_MAX_AGE_SECONDS`, `COOKIE_SECURE`,
      `APP_TIMEZONE`
- [x] Tests: ungeschützt/geschützt, Login, Fehlpasswort, Logout, manipulierte
      Sitzung, CSRF, Cookie-Flags, `AUTH_ENABLED=false`

Migration: nein. Neue Secrets: ja (nicht committen).

## 3 — Ziele und Meilensteine  ✅

- [x] Modell `Goal` (Slug, Titel, Beschreibung, Status, Sortierung, Kurzlabel)
- [x] Additive Felder `goal_id`, `is_milestone`, `sort_order` an Habit/Quest
- [x] `/goals`, Detailseite, anlegen, bearbeiten, pausieren, reaktivieren,
      archivieren (**nie** löschen)
- [x] Pausiertes Ziel: keine Mahnung, kein XP-Verlust, Historie bleibt
- [x] Tests: CRUD, Statuswechsel, Archivierung ohne Datenverlust, Zuordnung

Migration: ja (additiv).

## 4 — Zusätzliche Quest-Quellen  ✅

- [x] `japanese_session_count` — nur zählbare Imports (nicht Baseline,
      Duplikat, historisch, Warnung, `STATUS`, abgebrochen); Regel zentral und
      DB-frei testbar
- [x] Stabile Habit-Bindung für `habit_count` (`match_text` bleibt Fallback)
- [x] Append-only `QuestCompletion` (Quest, Zeitraum, Zeitpunkt, XP, Stat-XP,
      Dedup-Schlüssel) — wiederholbare Quests nie doppelt belohnen
- [x] Tests: Nichtzählung je Sonderfall, Dedup, Bestandsquests unverändert

Migration: `0003_quest_completions`, additiv, mit getestetem `downgrade()`.

**Cutover für historische Abschlüsse.** Die Tabelle startet leer. Quests, die
vor dieser Revision abgeschlossen wurden, haben keinen Completion-Datensatz —
es wird keine Historie erfunden. Sie werden trotzdem nicht erneut belohnt,
weil die bestehenden Wächter (`completed_at`, `active`) unverändert greifen.
Ab dieser Revision ist der Datensatz die maßgebliche Quelle für „schon
belohnt". Für Streaks und Meilensteine in Schritt 5 zählt entsprechend erst,
was ab dem Cutover erfasst wurde.

## 5 — Momentum und Streaks  ✅

- [x] Streak: aufeinanderfolgende vollständig erfüllte Perioden, KW Mo–So,
      `Europe/Berlin`; laufende Woche zählt nur bei Erfüllung; bester Streak bleibt
- [x] Momentum: 40/30/20/10 über die letzten vier **abgeschlossenen** Wochen,
      Erfüllungsgrad je Woche auf 0–100 gedeckelt, Ergebnis 0–100
- [x] Laufende Woche nie als Fehlschlag; pausierte Zeiträume neutral; fehlende
      Daten sichtbar neutral
- [x] `GoalPauseInterval`: Pausen werden historisch erfasst statt den heutigen
      Status rückwirkend anzuwenden; jede Überlappung neutralisiert die ganze
      Woche; höchstens ein offenes Intervall je Ziel (partieller Unique-Index)
- [x] Modul ohne Datenbank unit-testbar; Formel im UI erklärt
- [x] Tests: KW-Grenzen, DST, pausierte Wochen, Übererfüllung, Datenlücken

Migration: `0004_goal_pause_intervals`, additiv, mit getestetem `downgrade()`.
Die Tabelle startet leer; Pausen vor dieser Revision werden nicht erfunden.

## 6 — Tages- und Wochenansicht

- [ ] `/today`: Held, XP, heutige und offene flexible Habits, Schnellabschluss
      mit CSRF, Wochenquests, drei Hauptziele, letzte XP, Pause-Hinweis
- [ ] `/week`: Mo–So, Habits je Tag, wger-Trainings, bestätigte Japanisch-Sessions,
      Wochenquests, Zielkarten mit Momentum/Streak, Navigation, Zukunft nur lesend
- [ ] Habits: optionale Wochenplanung (`mon`…`sun`), ohne Angabe weiter flexibel
- [ ] Tests: beide Ansichten, geplante vs. flexible Habits, Zeitzone

Migration: ja (additives Habit-Feld).

## 7 — Mobile PWA

- [ ] `manifest.webmanifest`, lokale Icons inkl. maskierbar
- [ ] Service Worker: **nur** statische Assets, Icons, Manifest, Offline-Seite;
      keine authentifizierten Seiten, keine POSTs, keine Fachdaten;
      dynamische Routen `network-only`
- [ ] Neutrale Offline-Seite, mobile Navigation, große Touch-Ziele, Fokuszustände
- [ ] `Cache-Control` für authentifizierte Seiten
- [ ] Tests: Manifest erreichbar, SW cached nichts Dynamisches

Migration: nein. Keine externen CDNs.

## 8 — Starter-Kampagne

- [ ] Idempotent, stabile Slugs, keine Duplikate, keine Überschreibung
- [ ] Konflikt bei gleichem Namen mit abweichender Konfiguration **melden**
- [ ] Vorhandene äquivalente Quest wiederverwenden (kein zweiter „Week Warrior“)
- [ ] Vorhandenes `CONTROL`-Programm migrieren statt duplizieren
- [ ] Ziel A Kraftpfad, Ziel B Weg des Japanischen, Ziel C Körperkontrolle
      (neutrale Bezeichnungen, keine intimen Details)
- [ ] Einstellungen: Vorschau → Bestätigung → Anlage → `/today`
- [ ] CLI: `python -m app.seed_programs starter`
- [ ] Sicherheits-/Pausenhinweis auf der Zielseite Körperkontrolle
- [ ] Tests: Idempotenz, Konfliktmeldung, kein Duplikat von Week Warrior/CONTROL,
      keine doppelte Japanisch-Belohnung

Migration: nein (nur Daten).

## 9 — Oberfläche

- [ ] Navigation: Heute, Woche, Ziele, Gewohnheiten, Quests, Japanisch,
      Attribute, Erfolge, Einstellungen
- [ ] Zielkarten mit Fortschritt, Momentum, Streaks, nächstem Meilenstein, Status
- [ ] Sachliche, nicht wertende Texte (kein „gescheitert“, „Strafe“, „verloren“)

## 10 — Dokumentation

- [ ] `README.md`, `.env.example`, `docs/DEPLOY.md`, optional `docs/PRIVACY.md`
- [ ] `CLAUDE.md` nur bei neuen verbindlichen Architekturregeln
- [ ] Momentum-/Streak-Formeln, PWA-Cache-Grenzen, Secret-Erzeugung,
      Backup vor Migration, Verhalten bei pausierten Zielen

---

## Ausdrücklich nicht Teil dieses Umbaus

Journal · freie intime Sitzungsprotokolle · Fokus-Timer · Kalender-Sync ·
Push-Erinnerungen · soziale Funktionen · externe Analyse-/Trackingdienste ·
Coaching- oder Textgenerierung · KI in der Bewertungslogik
