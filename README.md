# 🚆 Spar Spar Schiene

Findet günstigere ÖBB-Tickets auf deiner Strecke. Die ÖBB-Sparschiene-Preise hängen
von Start- und Zielbahnhof ab — oft ist ein Ticket **über deine Strecke hinaus**
(früherer Start / späteres Ziel) billiger als das direkte Ticket. Du steigst trotzdem
an deinem Bahnhof ein und aus; nur das Ticket beginnt/endet woanders.

## Start

```
python server.py
```

Dann im Browser: **http://localhost:8325**
(oder einfach `Start-SparSparSchiene.bat` doppelklicken)

Keine Installation nötig — nur Python 3 (Standardbibliothek).

## Bedienung

1. **Von / Nach**: deine tatsächliche Einstiegs- und Ausstiegsstation
2. **Datum / Abfahrt ab**: gewünschter Reisezeitpunkt (Sparschiene: bis 6 Monate im Voraus)
3. **Radius hinter Einstieg**: Halbkreis *hinter* deinem Startbahnhof (weggedreht vom Ziel),
   in dem frühere Ticket-Startbahnhöfe gesucht werden
4. **Radius hinter Ziel**: Halbkreis hinter deinem Ziel für spätere Ticket-Endbahnhöfe
5. **Extra-Haltestellen**: einzelne Haltestellen (auch Bus) manuell als Kandidaten hinzufügen
6. **Bushaltestellen automatisch** (Standard: an): alle Orte im Halbkreis werden
   flächendeckend aus OpenStreetMap geholt (Overpass-API, `data/places_cache.json`) —
   nicht nur entlang der Bahnlinien. Pro Ort wird über die ÖBB-Suche eine Bushaltestelle
   ergänzt (gecacht in `data/bus_cache.json`)
7. **Kombinationen**: „Nur vermutlich attraktive" (Standard — nur Bahnhöfe kombinieren,
   an denen schon Sparschiene oder ein Preis ≤ Soll-Preis gefunden wurde) oder „Alle testen"
8. **Scan starten** — Ergebnisse erscheinen live; Sortierung wählbar (Preis aufsteigend /
   Ersparnis absteigend). Über die Checkboxen bei den Soll-Verbindungen lässt sich die
   Ergebnisliste auf bestimmte Züge filtern.
9. Während des Scans zeigen Fortschrittsbalken **und Restzeit-Schätzung** den
   Stand; die Ergebnisse sind in *Frühere Abfahrtsbahnhöfe*, *Spätere
   Ankunftsbahnhöfe* und *Kreuzverbindungen* gegliedert.

## Wie es funktioniert

Scan in 3 Phasen (spart massiv Abfragen):

- **Phase A**: jeder Kandidaten-Bahnhof A im Start-Halbkreis wird als Ticket-Start
  getestet (A → dein Ziel). Treffer = die Verbindung enthält *deine* Züge.
- **Phase B**: analog jeder Kandidat B im Ziel-Halbkreis (dein Start → B).
- **Phase C**: alle Kombinationen A × B der Phase-A/B-Treffer.

Ein Treffer bedeutet: Die Züge deiner Soll-Verbindung (z.B. `IC 647`) stecken als
Teilfolge in der gefundenen Verbindung — du sitzt also im selben Zug.

## Technik

- **Backend**: Python 3, nur Standardbibliothek (`server.py`, `scanner.py`, `oebb.py`, `stations.py`)
- **Frontend**: statisches HTML/JS mit Leaflet-Karte (`public/`)
- **Stationsdaten**: Trainline-EU-Datensatz (`data/stations_full.csv`, ~18.000 Stationen
  mit ÖBB-IDs); wird beim ersten Start zu `data/stations.json` gefiltert.
  Aktualisieren: neue CSV von
  <https://raw.githubusercontent.com/trainline-eu/stations/master/stations.csv>
  nach `data/stations_full.csv` laden und `data/stations.json` löschen.

### ÖBB-API (inoffiziell, verifiziert 2026-07)

Basis: `https://shop.oebbtickets.at/api` — Header: `AccessToken`, `Channel: inet`, `Lang: de`

| Schritt | Endpoint |
|---|---|
| Anonymer Token (~5 min gültig) | `GET /domain/v4/init` |
| Stationssuche (Name oder ESN-Nummer) | `GET /hafas/v1/stations?name=...` |
| Offer-Kontext | `POST /offer/v2/travelActions` |
| Verbindungen | `POST /hafas/v4/timetable` |
| Preise (+ Sparschiene-Kennung) | `GET /offer/v1/prices?connectionIds[]=...` |

**Wichtig**: Der `timetable`-Body braucht `entryPointId: "timetable"`, `sortType`,
`filter` und ein vollständiges Passagier-Objekt mit `type: "ADULT"` — sonst hängt
der `prices`-Aufruf serverseitig (60s → 504). Station-IDs: sowohl Shop-Nummern
(z.B. `1250304`) als auch ESN (z.B. `8100134`) werden akzeptiert.

**Kritisch (Session-Kontext)**: `prices` bepreist nur Verbindungen aus der jeweils
**letzten** `timetable`-Abfrage derselben Session (Token). Eine weitere
`timetable`-Abfrage überschreibt den Kontext — ältere Verbindungs-IDs fehlen dann
**stillschweigend** in der prices-Antwort (kein Fehler!). Konsequenzen im Code:
Preise werden sofort nach jeder timetable-Abfrage geholt, und jeder Worker-Thread
hat seine **eigene Session** (Token pro Thread), damit parallele Worker sich nicht
gegenseitig den Kontext zerstören.

**Fehlerbehandlung**: Zeitlimit 15 s pro Abfrage (Preise 20 s). Bei Überschreitung:
Tempo drosseln, 1× wiederholen, dann Kandidat überspringen (Log-Zeile mit Zähler).
Bei 10 Zeitüberschreitungen in Folge bricht der Scan mit klarer Meldung ab;
bisherige Ergebnisse bleiben erhalten. Am Scan-Ende steht eine Statistik-Zeile im
Protokoll (Abfragen/Verbindungen/Matches/Preisfehler) — »Preisfehler« sollte 0 sein,
sonst stimmt etwas mit dem Session-Handling nicht.

## Online-Hosting (Render.com, kostenlos)

Das Repo enthält ein fertiges `render.yaml` (Blueprint):

1. Auf <https://render.com> mit dem GitHub-Konto anmelden (kostenlos, keine Kreditkarte)
2. **New + → Blueprint** → dieses Repo auswählen
3. Beim Deploy nach `SPAR_PASSWORD` gefragt → Wunsch-Passwort setzen (schützt die
   Seite per HTTP Basic Auth; Benutzername egal)
4. Fertig — die App läuft unter `https://spar-spar-schiene.onrender.com`
   (Free-Tier: schläft nach 15 min Leerlauf, erster Aufruf danach dauert ~30–60 s)

Umgebungsvariablen: `PORT` (setzt Render automatisch), `HOST=0.0.0.0`,
`SPAR_PASSWORD` (leer = kein Schutz, z.B. lokal), `SPAR_WORKERS` / `SPAR_MAX_RPS`
(Scan-Parallelität/Tempo; auf Render gedrosselt, da Free-Tier nur 0,1 CPU hat).

### Stabilität auf dem Free-Tier

Render legt Free-Instanzen nach 15 min ohne **eingehende** Anfragen schlafen —
auch mitten im Scan, denn ein offener SSE-Stream zählt nicht zuverlässig als
Traffic. Alle Scan-Jobs leben nur im RAM und sind nach einem Neustart weg.
Dagegen sind drei Maßnahmen eingebaut:

1. **Selbst-Ping während Scans** (`server.py`): solange ein Scan läuft, ruft der
   Server alle 4 min seine eigene öffentliche URL (`RENDER_EXTERNAL_URL`, von
   Render gesetzt) auf — das zählt als Traffic und verhindert den Spin-down
   mitten im Scan. Intervall: `SPAR_KEEPALIVE_SECS` (Sekunden).
2. **Keep-alive-Workflow** (`.github/workflows/keepalive.yml`): pingt den Server
   alle 10 min via GitHub Actions an, damit es gar nicht erst zu Kaltstarts
   kommt. Kann jederzeit gelöscht/deaktiviert werden; GitHub schaltet geplante
   Workflows nach 60 Tagen ohne Repo-Aktivität selbst ab.
3. **Saubere Fehlermeldung im Frontend**: startet der Server trotzdem neu
   (Deploy, Absturz), meldet die App „Scan verloren (Server-Neustart)" statt
   endlos weiterzuladen — vorher fror die Oberfläche einfach ein.

## Android-App (APK)

Der Ordner `android/` enthält eine schlanke WebView-App; GitHub Actions baut bei
jedem Push automatisch die APK und veröffentlicht sie als Release **`apk-latest`**
(→ Releases-Seite des Repos, Datei `SparSparSchiene.apk`).

- Beim ersten Start fragt die App nach der **Server-URL**: die Render-URL
  (voreingestellt) oder daheim `http://<PC-IP>:8325`
- **Live-Benachrichtigung**: Während eines Scans zeigt die App eine
  System-Benachrichtigung mit Fortschritt und Restzeit — auch bei
  ausgeschaltetem Display (die App fragt beim ersten Scan nach der
  Benachrichtigungs-Berechtigung).
- Passwort wird einmal eingegeben und gespeichert (Menü: Zugangsdaten löschen)
- Installation am Handy: APK herunterladen → Installation aus unbekannter Quelle
  erlauben → installieren. Da die APK debug-signiert ist, bei App-Updates vorher
  die alte Version deinstallieren.

## Lokale App (experimentell)

`SparSparSchieneLokal.apk` (Release `apk-latest`) läuft **komplett ohne
Server**: ein eingebetteter Python-Interpreter ([Chaquopy](https://chaquo.com/chaquopy/))
führt den unveränderten `server.py` direkt am Handy aus, der WebView zeigt die
gewohnte Oberfläche von `127.0.0.1:8325`.

- **Keine Anmeldung, keine Server-URL** — App öffnen und loslegen
- ÖBB-Anfragen gehen direkt vom Handy aus (eigene IP statt Cloud)
- ~30 MB (Python-Runtime, nur arm64); parallel zur Haupt-App installierbar
- Scans laufen, solange App oder Benachrichtigungs-Service leben; bei sehr
  langen Scans die App ggf. von der Akku-Optimierung ausnehmen
- Caches (`places_cache`, `bus_cache`) liegen im App-Speicher

## Hinweise

- Inoffizielles Hobby-Tool; die ÖBB kann die Schnittstelle jederzeit ändern.
- Rate-Limit: max. 8 Requests/s (in `oebb.py` konfigurierbar).
- Tickets gelten offiziell ab dem aufgedruckten Startbahnhof. Späteres Zusteigen im
  selben Zug ist gängige Praxis, aber ohne Rechtsanspruch (z.B. bei Zugausfall vor
  deinem Bahnhof besteht kein Beförderungsanspruch ab dort).
- Preise ohne Gewähr — vor dem Kauf im ÖBB Ticketshop prüfen.
