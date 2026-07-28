# Design: Zeitschätzer, Android-Live-Benachrichtigung, getrennte Ergebnislisten, Mobil-Layout

Datum: 2026-07-28 · Status: vom User genehmigt (Variante A)

## Ziel

1. **Zeitschätzer**: Anzeige, wie lange der Scan voraussichtlich noch dauert.
2. **Android-Live-Benachrichtigung**: System-Benachrichtigung mit Fortschritt
   („92/155 Abfragen · noch ~3 min"), die auch bei Display-aus weiterläuft.
3. **Getrennte Ergebnislisten**: Frühere Abfahrtsbahnhöfe (Phase A), spätere
   Ankunftsbahnhöfe (Phase B) und Kreuzverbindungen (Phase C) als eigene Abschnitte.
4. **Mobil-Layout**: kein horizontales Scrollen mehr am Handy; kompakte Darstellung.

## Architektur-Entscheidung (Variante A)

Der Android-WebView pausiert bei Display-aus — die Webseite kann eine
Benachrichtigung im Hintergrund nicht füttern. Darum berechnet der **Server**
Fortschritt + Restzeit und stellt sie im Snapshot-Endpoint bereit; ein **nativer
Foreground-Service** in der Android-App pollt diesen Endpoint unabhängig vom
WebView und aktualisiert die Benachrichtigung.

## Komponenten

### 1. Server (`scanner.py`, `server.py`)

- `ScanJob` führt einen Fortschritts-Zustand: `phase_state = {name, label, done,
  total, found, started}` plus Zeitstempel-Liste der letzten Item-Abschlüsse
  (max. 30) zur Ratenberechnung.
- **ETA-Formel**: Rate = Abschlüsse der letzten 30 Items / Zeitspanne (erst ab
  5 Abschlüssen). Restzeit = (Rest der aktuellen Phase + Summe der Totals noch
  ausstehender, bereits bekannter Phasen) / Rate. Solange Phase C noch nicht
  gestartet ist (Umfang unbekannt), wird die Restzeit als „mind." markiert
  (`etaMin: true`).
- `progress`-Events erhalten zusätzlich `eta` (Sekunden, `null` wenn noch keine
  Rate) und `etaMin` (bool).
- `GET /api/scan/<id>?light=1`: Snapshot **ohne** das (potenziell große)
  `results`-Array, dafür mit `phase`-Objekt inkl. `eta`/`etaMin` und
  `resultCount`. Der normale Snapshot (ohne `light`) bleibt unverändert plus
  `phase`-Objekt. Auth wie bisher (Basic Auth) — der native Service schickt
  denselben Header.

### 2. Weboberfläche (`public/`)

- **ETA-Anzeige**: im Fortschrittstext: `92 / 155 Abfragen · 14 Bahnhöfe mit
  Treffern · noch ~3 min` (bzw. `mind. ~3 min`, solange Phase C unbekannt).
- **Getrennte Ergebnisse**: statt einer Tabelle drei Abschnitte im
  Ergebnis-Card, jeweils mit Überschrift + Zähler:
  - „🚉 Frühere Abfahrtsbahnhöfe" (phase A)
  - „🏁 Spätere Ankunftsbahnhöfe" (phase B)
  - „✖️ Kreuzverbindungen" (phase C)
  Leere Abschnitte werden ausgeblendet. Sortier-Auswahl und
  Soll-Verbindungs-Filter wirken auf alle drei gemeinsam. Die
  Bestpreis-Hervorhebung gilt global (günstigstes Voll-Ticket über alle
  Abschnitte).
- **JS-Brücke zur App**: Wenn `window.SparApp` existiert (nur in der
  Android-App injiziert), ruft das Frontend `SparApp.scanStarted(jobId)` beim
  Start und `SparApp.scanFinished()` bei done/error/cancel auf. Im Browser ohne
  Brücke: kein Effekt.
- **Mobil-Layout (≤ 640 px)**, rein per CSS-Media-Query:
  - Ergebnis- und Soll-Tabellen werden zu **Karten**: `thead` ausgeblendet,
    jede Zeile ein Block mit 2–3 kompakten Zeilen
    (Preis + Ersparnis + Tags / Ticket von→nach mit Zeiten / Züge + Buchen-Link).
  - `html, body { overflow-x: hidden }` als Sicherheitsnetz; Karte/Log/Chips
    mit `max-width: 100%`.
  - Karte (`#map`) auf 300 px Höhe reduziert, Header/Paddings kompakter.
  - Erfolgskriterium: `document.documentElement.scrollWidth <= clientWidth`
    bei 375 px Breite auf allen Ansichten (Formular, Scan läuft, Ergebnisse).

### 3. Android-App (`android/`)

- **`MainActivity`**: injiziert die JS-Brücke
  (`addJavascriptInterface(bridge, "SparApp")`). `scanStarted(jobId)` fordert
  bei Android 13+ die `POST_NOTIFICATIONS`-Laufzeitberechtigung an und startet
  den `ScanWatchService` mit `jobId`, `baseUrl` und den gespeicherten
  Basic-Auth-Zugangsdaten. `scanFinished()` ist nur ein Hinweis — der Service
  erkennt das Ende selbst.
- **`ScanWatchService`** (Foreground-Service, Typ `dataSync`):
  - pollt alle 10 s `GET <baseUrl>/api/scan/<jobId>?light=1` (Thread, kein WebView).
  - Benachrichtigung (eigener Channel „Scan-Fortschritt", stumm):
    Fortschrittsbalken (`done/total`), Text `Phase C · 92/155 Abfragen ·
    14 Treffer · noch ~3 min`. Tippen öffnet die App.
  - Scan fertig → letzte Benachrichtigung „✅ Scan fertig: 40 Tickets gefunden"
    (ohne Balken, auto-dismiss) und `stopSelf()`.
  - HTTP 404 → „⚠️ Scan verloren (Server-Neustart)" und `stopSelf()`.
    Netzwerkfehler → weiter versuchen (max. 30 min Gesamtlaufzeit als Limit).
- **Manifest**: `POST_NOTIFICATIONS`, `FOREGROUND_SERVICE`,
  `FOREGROUND_SERVICE_DATA_SYNC`, Service-Deklaration mit
  `foregroundServiceType="dataSync"`.
- Die APK wird vom bestehenden CI-Workflow gebaut (Trigger `android/**` greift).

## Fehlerbehandlung

- ETA ist eine Schätzung: bei zu wenig Daten (`eta: null`) zeigt die UI nichts
  an statt Unsinn. Drosselung/429-Bremse verlangsamt die Rate → ETA passt sich
  über das gleitende Fenster automatisch an.
- Service übersteht App-Wechsel; wird die App komplett beendet (Task weggewischt),
  darf der Service weiterlaufen bis Scan-Ende oder Limit.
- Server-Neustart: Frontend zeigt „Scan verloren" (bereits umgesetzt), der
  Service meldet es ebenfalls und beendet sich.

## Tests / Verifikation

- **Server**: lokaler Scan → `progress`-Events enthalten plausibles `eta`;
  `?light=1`-Snapshot enthält `phase`, aber kein `results`.
- **Web**: Browser auf 375 px → kein horizontales Scrollen (scrollWidth-Check)
  in allen drei Zuständen; Ergebnisliste zeigt drei Abschnitte korrekt gruppiert.
- **Android**: Kompilieren via CI (Gate: Build grün). Kein Emulator in dieser
  Umgebung — Funktionstest der Benachrichtigung macht der User am Gerät;
  der Code hält sich an dokumentierte Standard-APIs.

## Bewusst NICHT enthalten (YAGNI)

- Kein Persistieren von Scan-Jobs über Server-Neustarts.
- Keine Web-Push-Notifications für Desktop-Browser.
- Kein Umbau der Karten-/Formular-Bedienung am Handy über das Nötige hinaus.
