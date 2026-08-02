# Manuelle Prüfliste — Oberfläche

Für die Abnahme nach einer UI-Änderung. **Lokal ausführen**, nicht auf der
Produktivinstanz: mehrere Punkte schließen eine Gewohnheit ab und schreiben
damit XP.

```bash
pip install -e ".[dev]"
WGER_BASE_URL=https://wger.example.com DATABASE_URL=sqlite:///./local.db \
AUTH_ENABLED=false uvicorn app.main:app --reload --port 5000
```

Danach `http://127.0.0.1:5000`. Für die Login-Punkte `AUTH_ENABLED=true` mit
einer lokalen Hash- und Secret-Datei setzen — siehe README, Abschnitt
„Authentifizierung".

Die Smalltalk-Breite prüft man am schnellsten mit den Responsive-Werkzeugen des
Browsers bei 360 px.

---

## Login

- [ ] Desktop: Karte zentriert, Sigil und Titel sichtbar, nur das Passwortfeld
- [ ] 360 px: nichts läuft über, kein horizontales Scrollen
- [ ] Tab-Reihenfolge: Passwortfeld → Anmelden, Fokusring deutlich sichtbar
- [ ] Falsches Passwort: verständliche Meldung, **kein** Hash, kein Pfad, kein
      Traceback
- [ ] Kein externes Bild und keine externe Schrift geladen (Netzwerk-Tab)

## `/today`

- [ ] Hero-Bereich steht oben: Name, Level, XP-Leiste, Gesamt-XP
- [ ] Datum und Wochentag korrekt (Europe/Berlin)
- [ ] Geplante und flexible Gewohnheiten sind getrennt
- [ ] Ein offener Eintrag steht auf `offen` — als **Wort**, nicht nur farbig
- [ ] Ein erledigter Eintrag steht auf `erledigt`
- [ ] Leerzustand ist ein neutraler Satz, kein Vorwurf
- [ ] 360 px: eine lesbare Spalte, Schaltflächen gut treffbar

## Gewohnheit abschließen

- [ ] „Erledigt eintragen" → Erfolgsmeldung erscheint **einmal**
- [ ] Die Karte hebt sich kurz hervor und steht danach auf `erledigt`
- [ ] Die XP-Leiste im Hero-Bereich reagiert
- [ ] Die Meldung nennt die Gewohnheit
- [ ] Bei einer Gewohnheit mit XP steht die **tatsächliche** Zahl da
- [ ] Bei 0 XP steht **kein** `+0 XP`, sondern „Fortschritt gespeichert"
- [ ] **Seite neu laden:** die Meldung ist weg, es entsteht kein zweiter
      Abschluss
- [ ] Zweimal schnell hintereinander klicken: nur ein Abschluss, nur eine
      Meldung

## Reduzierte Bewegung

Systemeinstellung „Bewegung reduzieren" aktivieren (oder in den
Entwicklerwerkzeugen `prefers-reduced-motion: reduce` erzwingen).

- [ ] Keine Animation, kein Pulsieren, kein Einfliegen
- [ ] Der Abschluss ist **trotzdem** eindeutig: Rahmen und Fläche der Karte
      wechseln sichtbar
- [ ] Die Erfolgsmeldung erscheint weiterhin

## `/week`

- [ ] Montag bis Sonntag, sieben Tageskarten
- [ ] Der heutige Tag ist hervorgehoben
- [ ] Geplante Gewohnheiten stehen auf dem richtigen Tag
- [ ] Vor- und Zurücknavigation funktioniert
- [ ] Ein ungültiges `?date=` zeigt eine Meldung und die aktuelle Woche
- [ ] Wochenquests mit Zähler und Zielwert
- [ ] Eine vergangene Woche behauptet **keine** historische Questzahl
- [ ] 360 px: eine Spalte, **kein** horizontales Scrollen

## Ziele

- [ ] Zielkarte zeigt Status, Momentum, aktuelle und beste Serie
- [ ] Detailseite zeigt zusätzlich die Woche-für-Woche-Aufschlüsselung
- [ ] „Wie wird Momentum berechnet?" lässt sich aufklappen und erklärt die
      Pausenregel
- [ ] Ein pausiertes Ziel ist **neutral** markiert — nirgends „gescheitert"
- [ ] Ein Ziel ohne Historie zeigt „—" statt `0 %`
- [ ] Erfasste Pausen werden mit Zeitraum aufgelistet
- [ ] Ein Meilenstein ist als solcher erkennbar

## Quests

- [ ] Eine manuelle Quest sagt „manuell zu bestätigen"
- [ ] Eine automatische Quest sagt „zählt automatisch"
- [ ] „Der Fünfer-Rhythmus" steht auf **manuell** — das ist die tatsächliche
      Implementierung
- [ ] Der Fortschrittsbalken passt zum Zähler

## Japanisch

- [ ] Vorschau mit dem Minimalformat (nur Datum, Charakter, fünf Werte) wird
      angenommen
- [ ] Fehlende numerische Werte erscheinen als `0`
- [ ] Fehlendes Bunpro-Level erscheint als „Nicht angegeben" — **nicht** als
      `0` und **nicht** als erfundenes Level
- [ ] Ein explizit importiertes `0` sieht genauso aus wie ein fehlender Wert
      (das ist die gewollte Darstellungsregel)
- [ ] Widersprüchliche Doppelangaben zeigen eine verständliche Meldung ohne
      Regex und ohne Traceback
- [ ] Detailseite eines Imports zeigt dieselben Werte

## Starter-Kampagne

- [ ] `/settings/starter` zeigt die Vorschau, ohne etwas zu schreiben
- [ ] Jede Zeile nennt ihre Aktion: neu, ergänzt, wiederverwendet, keine
      Änderung, Konflikt
- [ ] Ziele, Gewohnheiten, Wochentage, Quests und Meilensteine sind sichtbar
- [ ] Der Sicherheitshinweis zu Routine K ist sachlich formuliert
- [ ] Nach einer Aktivierung meldet ein erneuter Aufruf keine Änderungen mehr

## PWA und Offline

- [ ] `/manifest.webmanifest` liefert JSON mit `start_url` `/today`
- [ ] `/offline` sieht aus wie die App, zeigt aber **keine** Nutzerdaten und
      **kein** Formular
- [ ] Installation im Browser wird angeboten (nur über HTTPS oder localhost)
- [ ] Im Netzwerk-Tab: keine Anfrage an einen fremden Host

## Übergreifend

- [ ] Tab-Durchlauf über eine ganze Seite: jeder Fokus ist sichtbar
- [ ] Jede Statusangabe steht auch als Text da, nicht nur als Farbe
- [ ] Überschriften sind semantisch (`h1`, dann `h2`)
- [ ] Keine Seite scrollt bei 360 px horizontal
