# ETA + Android-Benachrichtigung + getrennte Ergebnisse + Mobil-Layout — Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restzeit-Schätzung im Scan, Android-System-Benachrichtigung mit Live-Fortschritt, Ergebnisliste in drei Abschnitte (A/B/C) geteilt, kein horizontales Scrollen am Handy.

**Architecture:** Der Server berechnet ETA aus einem gleitenden Fenster von Item-Abschlüssen und liefert Fortschritt+ETA in `progress`-Events und einem schlanken Snapshot (`?light=1`). Die Weboberfläche zeigt ETA und gruppierte Ergebnisse; unter 640 px werden Tabellen zu CSS-Karten. Die Android-App bekommt eine JS-Brücke + Foreground-Service, der den Snapshot pollt und die Benachrichtigung aktualisiert (unabhängig vom pausierten WebView).

**Tech Stack:** Python 3 Standardbibliothek (+ `unittest`), Vanilla JS/CSS, Android Java (Plattform-APIs, keine Bibliotheken), GitHub Actions (APK).

**Spec:** `docs/superpowers/specs/2026-07-28-eta-benachrichtigung-mobil-design.md`

---

### Task 1: EtaTracker (Server, TDD)

**Files:**
- Test: `tests/test_eta.py` (neu)
- Modify: `scanner.py` (Klasse `EtaTracker` nach den Imports, vor `JOBS`)

- [x] **Step 1: Fehlschlagenden Test schreiben**

`tests/test_eta.py`:

```python
import unittest

from scanner import EtaTracker


class TestEtaTracker(unittest.TestCase):
    def test_zu_wenige_messungen_ergibt_none(self):
        t = EtaTracker(min_samples=5)
        for ts in (1.0, 2.0, 3.0, 4.0):
            t.record(ts)
        self.assertIsNone(t.estimate(10))

    def test_konstante_rate(self):
        t = EtaTracker(min_samples=5)
        for ts in (0.0, 1.0, 2.0, 3.0, 4.0):  # 1 Item/s
            t.record(ts)
        self.assertAlmostEqual(t.estimate(10), 10.0, places=3)

    def test_fenster_begrenzt_alte_messungen(self):
        t = EtaTracker(window=5, min_samples=5)
        for ts in (0.0, 100.0, 101.0, 102.0, 103.0, 104.0):  # 0.0 faellt raus
            t.record(ts)
        self.assertAlmostEqual(t.estimate(4), 4.0, places=3)

    def test_nichts_mehr_offen_ergibt_none(self):
        t = EtaTracker(min_samples=2)
        t.record(1.0)
        t.record(2.0)
        self.assertIsNone(t.estimate(0))


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Test laufen lassen — muss fehlschlagen**

Run (im Repo-Root): `python -m unittest discover -s tests -t . -v`
Expected: `ImportError: cannot import name 'EtaTracker'`

- [x] **Step 3: Minimale Implementierung**

In `scanner.py` nach den Modul-Konstanten (`PLACES_CACHE_FILE`-Block) einfügen:

```python
class EtaTracker:
    """Schaetzt die Restzeit aus einem gleitenden Fenster von Item-Abschluessen."""

    def __init__(self, window=30, min_samples=5):
        self.window = window
        self.min_samples = min_samples
        self._stamps = []
        self._lock = threading.Lock()

    def record(self, ts=None):
        with self._lock:
            self._stamps.append(time.time() if ts is None else ts)
            if len(self._stamps) > self.window:
                self._stamps.pop(0)

    def estimate(self, remaining_items):
        """Sekunden bis fertig oder None (zu wenig Daten / nichts offen)."""
        with self._lock:
            if len(self._stamps) < self.min_samples or remaining_items <= 0:
                return None
            span = self._stamps[-1] - self._stamps[0]
            if span <= 0:
                return None
            rate = (len(self._stamps) - 1) / span
            return remaining_items / rate
```

- [x] **Step 4: Test laufen lassen — muss bestehen**

Run: `python -m unittest discover -s tests -t . -v`
Expected: `OK` (4 Tests)

- [x] **Step 5: Commit**

```bash
git add tests/test_eta.py scanner.py
git commit -m "EtaTracker: Restzeit aus gleitendem Abschluss-Fenster"
```

---

### Task 2: Fortschritts-Zustand + ETA in ScanJob (TDD)

**Files:**
- Test: `tests/test_eta.py` (erweitern)
- Modify: `scanner.py` (`ScanJob.__init__`, neue Methode `_emit_progress`, `_phase_scan`, `_augment_bus`, `_run`)

- [x] **Step 1: Fehlschlagenden Test ergänzen**

In `tests/test_eta.py` anhängen:

```python
from scanner import ScanJob


class TestPhaseState(unittest.TestCase):
    def test_emit_progress_setzt_phase_state_und_event(self):
        job = ScanJob({"maxRps": 1})
        job.pending_phases = {"B": 10}
        for ts in (0.0, 1.0, 2.0, 3.0, 4.0):
            job.eta_tracker.record(ts)
        job._emit_progress("A", "Einstiegs-Bahnhoefe testen", done=5, total=10, found=2)

        st = job.phase_state
        self.assertEqual(st["name"], "A")
        self.assertEqual(st["done"], 5)
        self.assertEqual(st["total"], 10)
        self.assertEqual(st["found"], 2)
        # 5 offen in A + 10 in B = 15 Items bei 1 Item/s
        self.assertEqual(st["eta"], 15)
        self.assertTrue(st["etaMin"])  # Phase C noch unbekannt

        ev = job.events[-1]
        self.assertEqual(ev["type"], "progress")
        self.assertEqual(ev["data"]["eta"], 15)
        self.assertTrue(ev["data"]["etaMin"])

    def test_eta_min_faellt_weg_sobald_c_bekannt(self):
        job = ScanJob({"maxRps": 1})
        job.c_known = True
        for ts in (0.0, 1.0, 2.0, 3.0, 4.0):
            job.eta_tracker.record(ts)
        job._emit_progress("C", "Kombinationen testen", done=1, total=3, found=0)
        self.assertFalse(job.phase_state["etaMin"])
```

- [x] **Step 2: Test laufen lassen — muss fehlschlagen**

Run: `python -m unittest discover -s tests -t . -v`
Expected: `AttributeError: 'ScanJob' object has no attribute 'eta_tracker'` (o. ä.)

- [x] **Step 3: Implementierung**

In `ScanJob.__init__` nach `self.stats = {...}` einfügen:

```python
        self.eta_tracker = EtaTracker()
        self.phase_state = None    # letzter Fortschritt inkl. ETA (fuer Snapshot/App)
        self.pending_phases = {}   # bekannte, noch nicht gestartete Phasen: name -> total
        self.c_known = False       # Umfang von Phase C bekannt -> ETA nicht mehr "mind."
```

Neue Methode in `ScanJob` (unter `events_since`):

```python
    def _emit_progress(self, phase_id, label, done, total, found):
        remaining = (total - done) + sum(self.pending_phases.values())
        eta = self.eta_tracker.estimate(remaining)
        eta_min = not self.c_known
        self.phase_state = {"name": phase_id, "label": label, "done": done,
                            "total": total, "found": found,
                            "eta": None if eta is None else round(eta),
                            "etaMin": eta_min}
        self.emit("progress", {"phase": phase_id, "done": done, "total": total,
                               "found": found, "eta": self.phase_state["eta"],
                               "etaMin": eta_min})
```

In `_phase_scan`:
- nach `self.emit("phase", ...)` einfügen: `self.pending_phases.pop(phase_id, None)`
- vor `hits = fut.result()` (direkt nach `done += 1`): `self.eta_tracker.record()`
- den bisherigen `self.emit("progress", {...})`-Aufruf am Schleifenende ersetzen durch:

```python
                self._emit_progress(phase_id, label, done, len(items), found)
```

In `_augment_bus` den `self.emit("progress", {...})`-Aufruf in der Schleife ersetzen durch:

```python
                self.eta_tracker.record()
                self._emit_progress("bus", f"Bushaltestellen suchen ({label})",
                                    done, len(items), added)
```

In `_run`:
- nach `self.emit("candidates", ...)` einfügen:

```python
        self.pending_phases = {"A": len(origins), "B": len(dests)}
```

- nach `combos = [(a, b) for ...]` (vor dem Phase-C-Log) einfügen:

```python
        self.pending_phases["C"] = len(combos)
        self.c_known = True
```

- [x] **Step 4: Tests laufen lassen — müssen bestehen**

Run: `python -m unittest discover -s tests -t . -v`
Expected: `OK` (6 Tests)

- [x] **Step 5: Commit**

```bash
git add tests/test_eta.py scanner.py
git commit -m "ScanJob: Fortschritts-Zustand mit Restzeit-Schaetzung (ETA)"
```

---

### Task 3: Snapshot-Endpoint mit `phase` und `?light=1` (TDD)

**Files:**
- Test: `tests/test_eta.py` (erweitern)
- Modify: `server.py:128-135` (GET `/api/scan/<id>`)

- [x] **Step 1: Fehlschlagenden Test ergänzen**

Der Handler ist eng mit dem Socket verzahnt; getestet wird die neue, ausgelagerte
Snapshot-Funktion. In `tests/test_eta.py` anhängen:

```python
import server


class TestSnapshot(unittest.TestCase):
    def _job(self):
        job = ScanJob({"maxRps": 1})
        job.phase_state = {"name": "A", "label": "x", "done": 1, "total": 2,
                           "found": 0, "eta": 30, "etaMin": True}
        job.results = {"k": {"price": 10.0}}
        return job

    def test_light_snapshot_ohne_results(self):
        snap = server.job_snapshot(self._job(), light=True)
        self.assertNotIn("results", snap)
        self.assertEqual(snap["resultCount"], 1)
        self.assertEqual(snap["phase"]["eta"], 30)
        self.assertFalse(snap["finished"])

    def test_voller_snapshot_mit_results_und_phase(self):
        snap = server.job_snapshot(self._job(), light=False)
        self.assertEqual(len(snap["results"]), 1)
        self.assertEqual(snap["phase"]["name"], "A")
```

- [x] **Step 2: Test laufen lassen — muss fehlschlagen**

Run: `python -m unittest discover -s tests -t . -v`
Expected: `AttributeError: module 'server' has no attribute 'job_snapshot'`

- [x] **Step 3: Implementierung**

In `server.py` auf Modulebene (unter `MIME = {...}`) einfügen:

```python
def job_snapshot(job, light):
    """Scan-Zustand als JSON-faehiges Dict; light = ohne grosses results-Array."""
    snap = {"finished": job.finished, "phase": job.phase_state,
            "resultCount": len(job.results)}
    if not light:
        snap["results"] = sorted(job.results.values(), key=lambda r: r["price"])
    return snap
```

Den bestehenden Snapshot-Zweig in `do_GET` ersetzen:

```python
        elif path.startswith("/api/scan/"):
            job_id = path.split("/")[3]
            job = scanner.get_job(job_id)
            if not job:
                self._json({"error": "unbekannter Scan"}, 404)
                return
            self._json(job_snapshot(job, light="light" in qs))
```

- [x] **Step 4: Tests laufen lassen — müssen bestehen**

Run: `python -m unittest discover -s tests -t . -v`
Expected: `OK` (8 Tests)

- [x] **Step 5: Commit**

```bash
git add tests/test_eta.py server.py
git commit -m "Snapshot-Endpoint: phase-Objekt und ?light=1 ohne results"
```

---

### Task 4: Weboberfläche — ETA-Anzeige + JS-Brücke

**Files:**
- Modify: `public/app.js` (progress-Handler, `scanBtn.onclick`, `finishScan`)

- [x] **Step 1: ETA-Formatierung und Brücken-Helfer einbauen**

In `public/app.js` unter `fmtPrice` einfügen:

```js
function fmtEta(sec, isMin) {
  if (sec == null) return "";
  const txt = sec < 90 ? `~${Math.max(10, Math.round(sec / 10) * 10)} s`
                       : `~${Math.round(sec / 60)} min`;
  return ` · noch ${isMin ? "mind. " : ""}${txt}`;
}

// Brücke zur Android-App (window.SparApp existiert nur im WebView der App)
function notifyApp(fn, arg) {
  try {
    if (window.SparApp && window.SparApp[fn]) window.SparApp[fn](arg == null ? "" : String(arg));
  } catch (e) { /* App-Brücke optional */ }
}
```

Im `progress`-Case von `handleEvent` die Zeile mit `progress-text` ersetzen:

```js
      document.getElementById("progress-text").textContent =
        `${d.done} / ${d.total} Abfragen · ${d.found} Bahnhöfe mit Treffern` +
        fmtEta(d.eta, d.etaMin);
```

In `scanBtn.onclick` direkt nach `state.jobId = data.jobId;`:

```js
  notifyApp("scanStarted", data.jobId);
```

In `finishScan()` als erste Zeile:

```js
  notifyApp("scanFinished");
```

- [x] **Step 2: Manuell verifizieren**

Server lokal starten (`python server.py`), Scan Salzburg Hbf → Wien Hbf,
Radius 15/15, Bus aus. Expected: Fortschrittstext zeigt nach einigen Sekunden
`… · noch mind. ~X min`; in Phase C ohne „mind.". Keine Konsolen-Fehler.

- [x] **Step 3: Commit**

```bash
git add public/app.js
git commit -m "Web-UI: Restzeit im Fortschritt + JS-Bruecke zur Android-App"
```

---

### Task 5: Weboberfläche — Ergebnisse in drei Abschnitten

**Files:**
- Modify: `public/index.html:97-114` (results-card)
- Modify: `public/app.js` (`renderResults`)

- [x] **Step 1: HTML umbauen**

In `public/index.html` innerhalb `#results-card` die bisherige
`<table id="results-table">…</table>` ersetzen durch drei Gruppen:

```html
    <div class="result-group" id="group-A" style="display:none">
      <h3>🚉 Frühere Abfahrtsbahnhöfe <span class="group-count"></span></h3>
      <table class="results-table">
        <thead><tr>
          <th>Preis</th><th>Ersparnis</th><th>Ticket von</th><th>Ticket nach</th>
          <th>Dein Zug</th><th>Einstieg</th><th></th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="result-group" id="group-B" style="display:none">
      <h3>🏁 Spätere Ankunftsbahnhöfe <span class="group-count"></span></h3>
      <table class="results-table">
        <thead><tr>
          <th>Preis</th><th>Ersparnis</th><th>Ticket von</th><th>Ticket nach</th>
          <th>Dein Zug</th><th>Einstieg</th><th></th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="result-group" id="group-C" style="display:none">
      <h3>✖️ Kreuzverbindungen <span class="group-count"></span></h3>
      <table class="results-table">
        <thead><tr>
          <th>Preis</th><th>Ersparnis</th><th>Ticket von</th><th>Ticket nach</th>
          <th>Dein Zug</th><th>Einstieg</th><th></th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
```

- [x] **Step 2: renderResults auf Gruppen umstellen**

In `public/app.js` den Tabellen-Teil von `renderResults` (ab `const tbody = …`
bis zum Ende der `sorted.forEach`-Schleife) ersetzen durch:

```js
  // Bestpreis global: erstes Ticket der Sortierung, dessen Preis die GANZE Strecke abdeckt
  const best = sorted.find((r) => !r.reduced);

  const rowHtml = (r) => {
    const saving = r.saving != null ? r.saving : 0;
    const savingHtml = saving > 0
      ? `<span class="saving-pos">-${fmtPrice(saving).slice(2)} €</span>`
      : `<span class="saving-neg">${saving < 0 ? "+" + fmtPrice(-saving).slice(2) + " €" : "±0"}</span>`;
    const reducedHtml = r.reduced
      ? `<span class="tag tag-warn" title="${r.reducedScope || ""}">⚠ Teilstrecke</span>` +
        (r.reducedScope ? `<br><small class="warn-text">Preis gilt nur: ${r.reducedScope}</small>` : "")
      : "";
    return `<td class="c-price price">${fmtPrice(r.price)}${r.sparschiene ? '<span class="tag">Sparschiene</span>' : ""}${reducedHtml}</td>` +
      `<td class="c-saving">${savingHtml}</td>` +
      `<td class="c-from">${r.ticketFrom}<br><small>ab ${fmtTime(r.ticketDep)}</small></td>` +
      `<td class="c-to">${r.ticketTo}<br><small>an ${fmtTime(r.ticketArr)}</small></td>` +
      `<td class="c-trains trains">${r.trains.join(" → ")}</td>` +
      `<td class="c-board">${fmtTime(r.boardTime)} <small>(dein Bahnhof)</small></td>` +
      `<td class="c-book"><a href="https://shop.oebbtickets.at/de/ticket" target="_blank">buchen ↗</a></td>`;
  };

  ["A", "B", "C"].forEach((phase) => {
    const group = document.getElementById("group-" + phase);
    const rows = sorted.filter((r) => r.phase === phase);
    group.style.display = rows.length ? "" : "none";
    group.querySelector(".group-count").textContent = `(${rows.length})`;
    const tbody = group.querySelector("tbody");
    tbody.innerHTML = "";
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      if (r === best) tr.classList.add("best");
      tr.innerHTML = rowHtml(r);
      tbody.appendChild(tr);
    });
  });
```

(Die Zeilen mit `result-count` und `results-card` am Ende der Funktion bleiben.)

- [x] **Step 3: Styling für Gruppen-Überschriften**

In `public/style.css` unter `.card h2 {…}`:

```css
.result-group h3 { margin: 18px 0 6px; font-size: 15px; }
.result-group:first-of-type h3 { margin-top: 8px; }
.group-count { color: var(--gray); font-weight: 400; }
```

- [x] **Step 4: Manuell verifizieren**

Scan wie in Task 4. Expected: Ergebnisse erscheinen in „🚉 Frühere
Abfahrtsbahnhöfe" und (nach Phase B/C) in den weiteren Abschnitten; leere
Abschnitte unsichtbar; genau eine grüne Bestpreis-Zeile insgesamt; Zug-Filter
(Checkboxen) und Sortierung wirken über alle Abschnitte.

- [x] **Step 5: Commit**

```bash
git add public/index.html public/app.js public/style.css
git commit -m "Web-UI: Ergebnisse nach Abfahrt/Ankunft/Kreuzverbindung getrennt"
```

---

### Task 6: Mobil-Layout ohne horizontales Scrollen

**Files:**
- Modify: `public/style.css` (Media-Query am Dateiende)
- Modify: `public/app.js` (Klassen an Soll-Tabellen-Zellen)

- [x] **Step 1: Soll-Tabellen-Zellen Klassen geben**

In `handleEvent`, Case `soll`, die `insertAdjacentHTML`-Zeile ersetzen:

```js
        tr.insertAdjacentHTML("beforeend",
          `<td class="s-dep">${fmtDate(c.dep)} ${fmtTime(c.dep)}</td><td class="s-arr">${fmtTime(c.arr)}</td>` +
          `<td class="s-trains trains">${c.trains.join(" → ")}</td>` +
          `<td class="s-price price">${c.price != null ? fmtPrice(c.price) : "–"}${c.sparschiene ? '<span class="tag">Sparschiene</span>' : ""}</td>`);
```

Und beim Checkbox-`td` davor: `td.className = "s-check";`

- [x] **Step 2: Mobile-CSS anhängen**

Am Ende von `public/style.css`:

```css
html, body { max-width: 100%; overflow-x: hidden; }
img, svg { max-width: 100%; }

@media (max-width: 640px) {
  header { padding: 12px 14px; }
  header h1 { font-size: 20px; }
  .subtitle { font-size: 12px; }
  main { margin: 12px auto; padding: 0 10px; gap: 12px; }
  .card { padding: 12px; }
  .card h2 { font-size: 16px; }
  #map { height: 300px; }
  .log { font-size: 11px; }
  button { padding: 10px 16px; font-size: 15px; }

  /* Ergebnis-Tabellen werden zu Karten */
  .results-table, .results-table tbody { display: block; width: 100%; }
  .results-table thead { display: none; }
  .results-table tr {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 2px 10px;
    padding: 10px 2px;
    border-bottom: 1px solid #eee;
  }
  .results-table td { display: block; border: none; padding: 0; font-size: 13px; }
  .results-table td.c-price  { grid-column: 1; grid-row: 1; font-size: 16px; }
  .results-table td.c-saving { grid-column: 2; grid-row: 1; text-align: right; }
  .results-table td.c-from   { grid-column: 1; grid-row: 2; }
  .results-table td.c-to     { grid-column: 2; grid-row: 2; text-align: right; }
  .results-table td.c-trains { grid-column: 1; grid-row: 3; overflow-wrap: anywhere; }
  .results-table td.c-board  { grid-column: 1; grid-row: 4; color: var(--gray); font-size: 12px; }
  .results-table td.c-book   { grid-column: 2; grid-row: 3 / 5; align-self: end; text-align: right; }
  .results-table tr.best { border-left: 4px solid var(--green); padding-left: 6px; }

  /* Soll-Verbindungen kompakt */
  #soll-table, #soll-table tbody { display: block; width: 100%; }
  #soll-table thead { display: none; }
  #soll-table tr {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 2px 8px;
    align-items: center;
    padding: 8px 2px;
    border-bottom: 1px solid #eee;
  }
  #soll-table td { display: block; border: none; padding: 0; font-size: 13px; }
  #soll-table td.s-check  { grid-column: 1; grid-row: 1 / 3; }
  #soll-table td.s-dep    { grid-column: 2; grid-row: 1; }
  #soll-table td.s-arr    { grid-column: 2; grid-row: 1; text-align: right; padding-right: 4px; }
  #soll-table td.s-trains { grid-column: 2; grid-row: 2; overflow-wrap: anywhere; }
  #soll-table td.s-price  { grid-column: 3; grid-row: 1 / 3; text-align: right; }
}
```

Hinweis: `s-dep` und `s-arr` teilen sich Grid-Zelle (Zeile 1, Spalte 2) —
`s-arr` bekommt `justify-self: end` implizit über `text-align: right`; falls
Überlappung auftritt, `grid-template-columns: auto 1fr auto auto` und `s-arr`
in eigene Spalte legen (bei Verifikation prüfen).

- [x] **Step 3: Verifizieren bei 375 px**

Browser-Pane auf 375×812 stellen, Seite laden, Scan starten, Ergebnisse
abwarten. In der Konsole:

```js
document.documentElement.scrollWidth <= document.documentElement.clientWidth
```

Expected: `true` in allen Zuständen (Formular / Scan läuft / Ergebnisse);
Ergebnisse als Karten mit Preis oben links, Ersparnis oben rechts.

- [x] **Step 4: Commit**

```bash
git add public/style.css public/app.js
git commit -m "Mobil-Layout: Karten statt Tabellen, kein horizontales Scrollen"
```

---

### Task 7: Android — JS-Brücke in MainActivity

**Files:**
- Modify: `android/app/src/main/java/at/sparsparschiene/app/MainActivity.java`

- [x] **Step 1: Brücke implementieren**

Neue Imports:

```java
import android.content.pm.PackageManager;
import android.os.Build;
import android.webkit.JavascriptInterface;
```

In `onCreate` nach `webView.getSettings().setDomStorageEnabled(true);`:

```java
        webView.addJavascriptInterface(new SparBridge(), "SparApp");
```

Neue innere Klasse (vor `baseUrl()`):

```java
    /** Von public/app.js aufgerufen (window.SparApp) - startet den Fortschritts-Service. */
    private class SparBridge {
        @JavascriptInterface
        public void scanStarted(String jobId) {
            runOnUiThread(() -> {
                if (Build.VERSION.SDK_INT >= 33 &&
                        checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                                != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(
                            new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 1);
                }
                Intent i = new Intent(MainActivity.this, ScanWatchService.class);
                i.putExtra("jobId", jobId);
                i.putExtra("baseUrl", baseUrl());
                i.putExtra("user", prefs.getString("auth_user", null));
                i.putExtra("pass", prefs.getString("auth_pass", null));
                startForegroundService(i);
            });
        }

        @JavascriptInterface
        public void scanFinished() {
            // Nur Hinweis - der Service erkennt das Ende selbst ueber den Snapshot.
        }
    }
```

- [x] **Step 2: Commit**

```bash
git add android/app/src/main/java/at/sparsparschiene/app/MainActivity.java
git commit -m "Android: JS-Bruecke SparApp startet Fortschritts-Service"
```

---

### Task 8: Android — ScanWatchService + Manifest + Version

**Files:**
- Create: `android/app/src/main/java/at/sparsparschiene/app/ScanWatchService.java`
- Modify: `android/app/src/main/AndroidManifest.xml`
- Modify: `android/app/build.gradle:13-14` (versionCode/-Name)

- [x] **Step 1: Service anlegen**

```java
package at.sparsparschiene.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import android.util.Base64;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Pollt den Scan-Status (GET /api/scan/<id>?light=1) unabhaengig vom WebView
 * und zeigt eine Fortschritts-Benachrichtigung - auch bei Display aus.
 * Der WebView pausiert im Hintergrund, dieser Service nicht.
 */
public class ScanWatchService extends Service {

    private static final String CHANNEL = "scan_progress";
    private static final int NOTIF_ID = 1;
    private static final int POLL_MS = 10_000;
    private static final long MAX_RUNTIME_MS = 30 * 60_000L;

    private volatile Thread worker;

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String jobId = intent != null ? intent.getStringExtra("jobId") : null;
        String baseUrl = intent != null ? intent.getStringExtra("baseUrl") : null;
        if (jobId == null || baseUrl == null) { stopSelf(); return START_NOT_STICKY; }
        String user = intent.getStringExtra("user");
        String pass = intent.getStringExtra("pass");

        createChannel();
        startForeground(NOTIF_ID, build("Scan läuft…", 0, 0, true));

        if (worker != null) worker.interrupt();  // alter Scan -> neuer gewinnt
        worker = new Thread(() -> poll(baseUrl, jobId, user, pass));
        worker.setDaemon(true);
        worker.start();
        return START_NOT_STICKY;
    }

    private void poll(String baseUrl, String jobId, String user, String pass) {
        long startTime = System.currentTimeMillis();
        while (!Thread.currentThread().isInterrupted()
                && System.currentTimeMillis() - startTime < MAX_RUNTIME_MS) {
            try {
                HttpURLConnection c = (HttpURLConnection)
                        new URL(baseUrl + "/api/scan/" + jobId + "?light=1").openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(15000);
                if (user != null && pass != null) {
                    String cred = Base64.encodeToString(
                            (user + ":" + pass).getBytes("UTF-8"), Base64.NO_WRAP);
                    c.setRequestProperty("Authorization", "Basic " + cred);
                }
                int status = c.getResponseCode();
                if (status == 404) {
                    finish("⚠️ Scan verloren (Server-Neustart)");
                    return;
                }
                if (status == 200) {
                    StringBuilder sb = new StringBuilder();
                    try (BufferedReader r = new BufferedReader(
                            new InputStreamReader(c.getInputStream(), "UTF-8"))) {
                        String line;
                        while ((line = r.readLine()) != null) sb.append(line);
                    }
                    JSONObject snap = new JSONObject(sb.toString());
                    if (snap.optBoolean("finished")) {
                        finish("✅ Scan fertig: " + snap.optInt("resultCount") + " Tickets gefunden");
                        return;
                    }
                    JSONObject ph = snap.optJSONObject("phase");
                    if (ph != null) {
                        int done = ph.optInt("done");
                        int total = ph.optInt("total");
                        String txt = phaseLabel(ph.optString("name"))
                                + " · " + done + "/" + total + " Abfragen"
                                + " · " + ph.optInt("found") + " Treffer" + etaText(ph);
                        notify(build(txt, done, total, true));
                    }
                }
            } catch (Exception ignored) {
                // Netzwerkfehler (Funkloch, Server wacht auf): naechster Versuch
            }
            try { Thread.sleep(POLL_MS); } catch (InterruptedException e) { return; }
        }
        stopSelf();
    }

    private static String phaseLabel(String name) {
        switch (name) {
            case "A": return "Abfahrtsbahnhöfe";
            case "B": return "Ankunftsbahnhöfe";
            case "C": return "Kreuzverbindungen";
            case "bus": return "Bushaltestellen";
            default: return "Scan";
        }
    }

    private static String etaText(JSONObject ph) {
        if (ph.isNull("eta")) return "";
        int eta = ph.optInt("eta");
        String t = eta < 90 ? "~" + Math.max(10, eta / 10 * 10) + " s"
                            : "~" + Math.round(eta / 60.0) + " min";
        return " · noch " + (ph.optBoolean("etaMin") ? "mind. " : "") + t;
    }

    private void finish(String message) {
        notify(build(message, 0, 0, false));
        stopForeground(false);  // Benachrichtigung stehen lassen
        stopSelf();
    }

    private void notify(Notification n) {
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(NOTIF_ID, n);
    }

    private Notification build(String text, int done, int total, boolean ongoing) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_fg)
                .setContentTitle("Spar Spar Schiene")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(pi)
                .setOnlyAlertOnce(true)
                .setOngoing(ongoing);
        if (total > 0) b.setProgress(total, done, false);
        return b.build();
    }

    private void createChannel() {
        NotificationChannel ch = new NotificationChannel(CHANNEL,
                "Scan-Fortschritt", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("Live-Fortschritt laufender Ticket-Scans");
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE))
                .createNotificationChannel(ch);
    }

    @Override
    public void onDestroy() {
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }
}
```

- [x] **Step 2: Manifest erweitern**

`android/app/src/main/AndroidManifest.xml` — nach der INTERNET-Permission:

```xml
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC" />
```

Innerhalb `<application>` nach dem `<activity>`-Block:

```xml
        <service
            android:name=".ScanWatchService"
            android:exported="false"
            android:foregroundServiceType="dataSync" />
```

- [x] **Step 3: App-Version anheben**

`android/app/build.gradle`: `versionCode 2`, `versionName "1.1"`.

- [x] **Step 4: Lokaler Syntax-Check (kein Android-SDK lokal)**

Kein lokales Gradle/SDK verfügbar — Gate ist der CI-Build (Task 9).
Sichtprüfung: Imports vollständig, `R.drawable.ic_fg` existiert
(`android/app/src/main/res/drawable/ic_fg.xml`).

- [x] **Step 5: Commit**

```bash
git add android/
git commit -m "Android: Foreground-Service mit Live-Fortschritts-Benachrichtigung"
```

---

### Task 9: README + E2E-Verifikation + Deploy

**Files:**
- Modify: `README.md` (Bedienung/Android-Abschnitt)

- [x] **Step 1: README ergänzen**

Im Abschnitt „Bedienung" nach Punkt 8 anfügen:

```markdown
9. Während des Scans zeigen Fortschrittsbalken **und Restzeit-Schätzung** den
   Stand; die Ergebnisse sind in *Frühere Abfahrtsbahnhöfe*, *Spätere
   Ankunftsbahnhöfe* und *Kreuzverbindungen* gegliedert.
```

Im Abschnitt „Android-App (APK)" als neuen Punkt:

```markdown
- **Live-Benachrichtigung**: Während eines Scans zeigt die App eine
  System-Benachrichtigung mit Fortschritt und Restzeit — auch bei
  ausgeschaltetem Display (die App fragt beim ersten Scan nach der
  Benachrichtigungs-Berechtigung).
```

- [x] **Step 2: Alle Tests + kompletter lokaler E2E-Lauf**

```bash
python -m unittest discover -s tests -t . -v   # erwartet: OK
python -m py_compile server.py scanner.py oebb.py stations.py
```

Dann Server neu starten, Scan im Browser (Desktop-Breite und 375 px) und
prüfen: ETA sichtbar, drei Abschnitte, kein Horizontal-Scroll, `?light=1`
liefert `phase` ohne `results` (curl).

- [x] **Step 3: Commit**

```bash
git add README.md
git commit -m "README: ETA, gegliederte Ergebnisse, Live-Benachrichtigung"
```

- [x] **Step 4: Push (mit User abstimmen) + CI beobachten**

Nach Freigabe: `git push` → Render-Deploy (Web) und GitHub-Actions-APK-Build
(`android/**` geändert → Workflow läuft). Build-Ergebnis prüfen; APK-Release
`apk-latest` muss aktualisiert sein. User installiert die neue APK
(alte Version vorher deinstallieren, Debug-Signatur).
