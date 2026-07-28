# Design: „Spar Spar Schiene Lokal" — serverlose Experimental-App

Datum: 2026-07-28 · Status: vom User genehmigt (Variante „Eingebettetes Python")

## Ziel

Eine zweite, experimentelle Android-App, die **komplett ohne Render/Server**
auskommt: der komplette Scanner läuft lokal am Handy. Zusatzanforderung:
**keinerlei Anmeldung** beim Öffnen — App starten und loslegen.

## Ansatz

[Chaquopy](https://chaquo.com/chaquopy/) (MIT-Lizenz) bettet einen
CPython-Interpreter in die App ein. Der **unveränderte** `server.py` (nur
Standardbibliothek) läuft in einem Hintergrund-Thread auf `127.0.0.1:8325`;
der WebView lädt die gewohnte Oberfläche von dort. ÖBB-Anfragen gehen direkt
vom Handy aus. Kein CORS (gleicher Origin), kein Basic-Auth (SPAR_PASSWORD
ungesetzt), kein URL-Dialog (fix localhost).

## Komponenten

### 1. Python (Repo-Root, nützt beiden Welten)

Daten- und Web-Verzeichnis per Umgebungsvariable übersteuerbar — Code steckt
im APK (nicht beschreibbar), Caches müssen in den App-Speicher:

- `stations.py`: `DATA_DIR = os.environ.get("SPAR_DATA_DIR") or <bisheriger Pfad>`
- `scanner.py`: Cache-Pfade leiten sich von `stationsdb.DATA_DIR` ab
- `server.py`: `PUBLIC = os.environ.get("SPAR_PUBLIC_DIR") or <bisheriger Pfad>`

Ohne gesetzte Variablen verhält sich alles exakt wie bisher (Render/lokal PC).

### 2. Neues Gradle-Modul `android/applocal`

- App-ID `at.sparsparschiene.local`, Label „Spar Spar Schiene Lokal",
  Version `0.1-experimental`; parallel zur Haupt-App installierbar.
- Chaquopy-Plugin, nur `arm64-v8a` (APK ~25–30 MB).
- **Ein Code-Stand**: Gradle-`Sync`-Tasks kopieren beim Bauen
  `server.py/scanner.py/oebb.py/stations.py` (→ Python-Quellen) sowie
  `public/` und `data/stations.json` (→ Assets) aus dem Repo-Root ins
  Build-Verzeichnis. Nichts wird doppelt gepflegt.
- `LocalMainActivity`: zeigt „Lokaler Server startet…", kopiert beim Start
  `public/` und `stations.json` aus den Assets nach `filesDir` (Caches
  `places_cache.json`/`bus_cache.json` bleiben dabei erhalten), setzt
  `SPAR_DATA_DIR`/`SPAR_PUBLIC_DIR`/`HOST`/`PORT`, startet `server.main()`
  in einem Thread (einmalig pro Prozess), wartet per Socket-Poll auf den
  Port (max. 30 s) und lädt dann `http://127.0.0.1:8325`. Fehlerfall:
  Klartext-Fehlerseite. Kein Auth-/URL-Dialog.
- `ScanWatchService` (lokale Variante der Haupt-App-Klasse, ohne
  Auth/URL-Extras): Live-Benachrichtigung wie gehabt gegen localhost;
  hält als Foreground-Service zugleich den Prozess (und damit den
  laufenden Scan) bei Display-aus am Leben.
- JS-Brücke `window.SparApp` identisch zur Haupt-App (app.js bleibt gleich).

### 3. CI (`.github/workflows/android.yml`)

- Runner bekommt Python 3.x (`setup-python`) für den Chaquopy-Build.
- Baut beide Module; Release `apk-latest` erhält zusätzlich
  `SparSparSchieneLokal.apk`.

## Fehlerbehandlung

- Portstart schlägt fehl / Python-Fehler → Fehlerseite mit Meldung statt
  weißem Bildschirm.
- Doppelstart des Servers (Activity-Recreate) → statisches Flag verhindert
  zweiten `bind`.
- Lange Scans bei Display-aus: Foreground-Service hält den Prozess; Hinweis
  im README, die App bei Bedarf von der Akku-Optimierung auszunehmen (Doze
  kann Netzwerk drosseln).

## Verifikation

- Python-Teil lokal voll testbar: Unit-Test für die Env-Overrides; manueller
  Lauf des Servers mit `SPAR_DATA_DIR`/`SPAR_PUBLIC_DIR` auf Kopien.
- App-Teil: **kein Android-SDK lokal** — Gate ist der CI-Build (grün +
  APK-Artefakt); Funktionstest am Gerät durch den User. Build wird ggf. in
  mehreren Push-Iterationen stabilisiert (vom User abgesegnet).

## Bewusst NICHT enthalten (YAGNI)

- Kein Ersatz der Haupt-App; Render-Betrieb bleibt unverändert.
- Kein Offline-Betrieb (ÖBB-API und Kartenkacheln brauchen Internet).
- Keine iOS-Variante, kein Play-Store-Release (debug-signiert wie bisher).
