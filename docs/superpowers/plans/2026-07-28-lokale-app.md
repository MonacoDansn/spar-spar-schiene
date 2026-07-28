# „Spar Spar Schiene Lokal" — Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zweite Android-App mit eingebettetem Python (Chaquopy), die den unveränderten Scanner lokal am Handy ausführt — ohne Render, ohne Anmeldung.

**Architecture:** Gradle-Modul `android/applocal`; Sync-Tasks kopieren Python-Code, `public/` und `stations.json` beim Bauen aus dem Repo-Root. `LocalMainActivity` kopiert Assets nach `filesDir`, setzt `SPAR_DATA_DIR`/`SPAR_PUBLIC_DIR`, startet `server.main()` im Thread und lädt `http://127.0.0.1:8325`. Lokale `ScanWatchService`-Variante ohne Auth.

**Tech Stack:** Chaquopy 16.x (AGP 8.5.2, minSdk 26, arm64-v8a), Python 3 Stdlib, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-07-28-lokale-app-design.md`

**Risiko:** Kein Android-SDK lokal — Chaquopy-Gradle-Details (Plugin-Version vs. AGP, `python.srcDirs` auf Build-Verzeichnis, `buildPython`) werden über CI-Iterationen verifiziert; Pushes dafür sind abgesegnet.

---

### Task 1: Env-Overrides für Daten-/Public-Verzeichnis (TDD)

**Files:** Test `tests/test_eta.py` (erweitern) · Modify `stations.py:8-10`, `scanner.py:33,81`, `server.py:21`

- [ ] Test: `SPAR_DATA_DIR`/`SPAR_PUBLIC_DIR` setzen, Module mit `importlib.reload` neu laden, `stations.DATA_DIR`, `scanner.PLACES_CACHE_FILE`, `server.PUBLIC` müssen auf die Override-Pfade zeigen; danach Env aufräumen + nochmal reloaden (Defaults zurück).
- [ ] Rot laufen lassen → Implementierung:
  - `stations.py`: `DATA_DIR = os.environ.get("SPAR_DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")`
  - `scanner.py`: `PLACES_CACHE_FILE = os.path.join(stationsdb.DATA_DIR, "places_cache.json")`, `BUS_CACHE_FILE` analog (Import ist schon da)
  - `server.py`: `PUBLIC = os.environ.get("SPAR_PUBLIC_DIR") or os.path.join(os.path.dirname(__file__), "public")`
- [ ] Grün + manueller Kurztest: Server mit beiden Env-Vars auf Kopien starten, `/` und `/api/stations?q=Wien` funktionieren.
- [ ] Commit `"Daten-/Public-Verzeichnis per Env uebersteuerbar (fuer lokale App)"`

### Task 2: Modul-Skelett `android/applocal`

**Files:** Create `android/applocal/build.gradle`, `android/applocal/src/main/AndroidManifest.xml`, Ressourcen (ic_fg, ic_launcher, colors) · Modify `android/settings.gradle` (+`include ':applocal'`), `android/build.gradle` (+Chaquopy-Plugin `com.chaquo.python` Version 16.0.0, apply false)

- [ ] build.gradle: applicationId `at.sparsparschiene.local`, versionName `0.1-experimental`, `ndk { abiFilters "arm64-v8a" }`, Chaquopy `defaultConfig { buildPython "python3" }`, `sourceSets.main { python.srcDirs = ["$buildDir/spar-python"]; assets.srcDirs += ["$buildDir/spar-assets"] }`, zwei `Sync`-Tasks (Python-Dateien; public/ + data/stations.json) mit `preBuild.dependsOn`.
- [ ] Manifest: INTERNET, POST_NOTIFICATIONS, FOREGROUND_SERVICE(+DATA_SYNC), `usesCleartextTraffic="true"` (WebView → http://127.0.0.1), LocalMainActivity (launchMode singleTop), ScanWatchService (dataSync).
- [ ] Commit `"applocal: Modul-Skelett mit Chaquopy und Quell-Sync aus Repo-Root"`

### Task 3: LocalMainActivity + Bridge

**Files:** Create `android/applocal/src/main/java/at/sparsparschiene/local/LocalMainActivity.java`

- [ ] Ablauf onCreate: WebView (JS, DOM-Storage, SparBridge wie Haupt-App, aber Service-Intent nur mit jobId), Splash-HTML via `loadData`, Boot-Thread: Assets `public/` + `data/stations.json` nach `filesDir/spar/...` kopieren (vorhandene Cache-JSONs NICHT überschreiben), `os.environ` via Chaquopy setzen (SPAR_DATA_DIR, SPAR_PUBLIC_DIR, HOST=127.0.0.1, PORT=8325), einmalig (static Flag) `server.main()` in Java-Thread, Socket-Poll auf Port (30 s), dann `loadUrl`. Fehler → Fehler-HTML mit Meldung.
- [ ] Zurück-Taste/Menü minimal: „Neu laden".
- [ ] Commit `"applocal: LocalMainActivity startet eingebetteten Python-Server"`

### Task 4: Lokale ScanWatchService-Variante

**Files:** Create `android/applocal/src/main/java/at/sparsparschiene/local/ScanWatchService.java`

- [ ] Kopie der Haupt-App-Klasse mit: package `at.sparsparschiene.local`, `BASE_URL = "http://127.0.0.1:8325"` fix, keine user/pass-Extras/Header. Alle Review-Fixes (NOTIF_DONE_ID, worker==me-Guard, 401/403 → hier nur 404/200 relevant, error/cancelled-Auswertung) übernehmen.
- [ ] Commit `"applocal: Live-Benachrichtigung gegen lokalen Server"`

### Task 5: CI + README

**Files:** Modify `.github/workflows/android.yml`, `README.md`

- [ ] Workflow: `actions/setup-python@v5` (3.11) vor dem Gradle-Schritt; `gradle assembleDebug` baut beide Module; `applocal/build/outputs/apk/debug/applocal-debug.apk` → `SparSparSchieneLokal.apk`; Release `apk-latest` bekommt beide APKs; Artifact-Upload beide.
- [ ] README: Abschnitt „Lokale App (experimentell)": was sie ist, ~30 MB, keine Anmeldung, Akku-Optimierung-Hinweis, Download-Ort.
- [ ] Commit `"applocal: CI-Build + README"`

### Task 6: CI-Iteration bis grün + Abnahme

- [ ] Push, Actions beobachten; Chaquopy-/Gradle-Fehler fixen und erneut pushen, bis Build grün und beide APKs im Release liegen.
- [ ] Python-Regression: kompletter Unit-Test-Lauf + lokaler Server-Start unverändert ok.
- [ ] User testet APK am Gerät (Installation, Scan, Benachrichtigung).
