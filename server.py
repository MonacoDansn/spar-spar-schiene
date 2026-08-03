"""Spar Spar Schiene - Web-Server (nur Python-Standardbibliothek).

Lokal:   python server.py            -> http://localhost:8325
Hosting: PORT/HOST via Umgebungsvariablen; optionaler Passwortschutz
         ueber SPAR_PASSWORD (HTTP Basic Auth, Benutzername egal).
"""
import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import oebb
import scanner
import stations as stationsdb

PORT = int(os.environ.get("PORT", "8325"))
HOST = os.environ.get("HOST", "127.0.0.1")
PASSWORD = os.environ.get("SPAR_PASSWORD")
PUBLIC = os.environ.get("SPAR_PUBLIC_DIR") or os.path.join(os.path.dirname(__file__), "public")
# Render setzt RENDER_EXTERNAL_URL automatisch; lokal bleibt sie leer.
EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
KEEPALIVE_SECS = int(os.environ.get("SPAR_KEEPALIVE_SECS", "240"))

shared_client = oebb.OebbClient(max_rps=8)

_geo_cache = {}    # eva -> {name, lat, lon} | None (fuer Streckenanzeige)
_route_cache = {}  # "eva,eva,..." -> [[lat,lon],...] | None (Bahntrassen-Geometrie)

# Bahn-Routing ueber OpenStreetMap-Gleise (OSRM-Zugprofil). Snappt die Halte an die
# echten Gleise und liefert die Trassen-Geometrie. Community-Dienst -> bei Ausfall
# faellt die Anzeige auf die gerade Linie zurueck.
OSRM_RAIL = "https://signal.eu.org/osm/eu/route/v1/train/"


def _resolve_eva(eva):
    """eva-Stationsnummer -> {name, lat, lon} (gecacht)."""
    if eva not in _geo_cache:
        try:
            res = shared_client.search_stations(eva)
            hit = next((s for s in res if str(s.get("number")) == eva), None) \
                or (res[0] if res else None)
            _geo_cache[eva] = ({"name": hit.get("name") or hit.get("meta"),
                                "lat": hit["latitude"] / 1e6,
                                "lon": hit["longitude"] / 1e6} if hit else None)
        except Exception:
            _geo_cache[eva] = None
    return _geo_cache[eva]


def _rail_leg(a, b):
    """Bahn-Routing zwischen ZWEI Punkten -> ([[lat,lon],...], distanz_m) oder (None,None).
    Nur zwei Wegpunkte -> der Router kann keinen Umweg ueber einen falschen
    Zwischenpunkt nehmen."""
    url = f"{OSRM_RAIL}{a['lon']},{a['lat']};{b['lon']},{b['lat']}?overview=full&geometries=geojson&steps=false"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SparSparSchiene/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("code") != "Ok" or not data.get("routes"):
            return None, None
        rt = data["routes"][0]
        geo = rt.get("geometry", {}).get("coordinates", [])
        return [[lat, lon] for lon, lat in geo], rt.get("distance")
    except Exception:
        return None, None


def _build_route(stops, modes):
    """Segmentweise Strecke: Zug-Beine entlang der Gleise, Bus/Fussweg gerade.
    Verwirft implausible Router-Ergebnisse (schlechter Gleis-Snap) -> gerade Linie."""
    segs = []
    for i in range(len(stops) - 1):
        a, b = stops[i], stops[i + 1]
        if not a or not b:
            continue
        mode = modes[i] if i < len(modes) else "other"
        straight = [[a["lat"], a["lon"]], [b["lat"], b["lon"]]]
        line = straight
        if mode == "train":
            geo, dist = _rail_leg(a, b)
            direct_km = stationsdb.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            # Plausibel, wenn die Gleisstrecke nicht absurd viel laenger ist als Luftlinie
            if geo and dist is not None and dist / 1000.0 <= direct_km * 2.5 + 5:
                line = geo
        segs.append({"mode": mode, "line": line})
    return segs

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# ---------- Debug-Berichte (fluechtig im RAM, Abruf nur per Geheim-Kennung) ----------

DEBUG_REPORTS = {}          # id -> text (Einfuege-Reihenfolge = Alter)
DEBUG_MAX_REPORTS = 20
DEBUG_MAX_CHARS = 100_000
_debug_lock = threading.Lock()


def store_debug_report(text):
    """Bericht ablegen; gibt die zufaellige Abruf-Kennung zurueck.
    Zusaetzlich ins Log drucken - auf Render als Backup einsehbar."""
    import uuid
    rid = uuid.uuid4().hex[:12]
    with _debug_lock:
        DEBUG_REPORTS[rid] = text[:DEBUG_MAX_CHARS]
        while len(DEBUG_REPORTS) > DEBUG_MAX_REPORTS:
            DEBUG_REPORTS.pop(next(iter(DEBUG_REPORTS)))
    print(f"=== Debug-Bericht {rid} ===\n{text[:DEBUG_MAX_CHARS]}\n=== Ende {rid} ===")
    return rid


def get_debug_report(rid):
    with _debug_lock:
        return DEBUG_REPORTS.get(rid)


def job_snapshot(job, light):
    """Scan-Zustand als JSON-faehiges Dict; light = ohne grosses results-Array."""
    snap = {"finished": job.finished, "phase": job.phase_state,
            "resultCount": len(job.results),
            "error": job.error, "cancelled": job.cancelled}
    if not light:
        snap["results"] = sorted(job.results.values(), key=lambda r: r["price"])
    return snap


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # ruhig bleiben

    # ---------- Antwort-Helfer ----------

    def _json(self, obj, status=200, cors=False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cors:
            # Debug-Endpunkte: auch aus der lokalen App (Origin 127.0.0.1) nutzbar
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, relpath):
        path = os.path.normpath(os.path.join(PUBLIC, relpath.lstrip("/")))
        if not path.startswith(PUBLIC) or not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- Routen ----------

    def _check_auth(self):
        """HTTP Basic Auth, aktiv sobald SPAR_PASSWORD gesetzt ist."""
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Basic "):
            try:
                userpw = base64.b64decode(header[6:]).decode("utf-8")
                if userpw.split(":", 1)[1] == PASSWORD:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Spar Spar Schiene"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if self.path.startswith("/api/debug/"):
            # Ohne Auth: Kennung ist ein 12-stelliges Zufallsgeheimnis
            report = get_debug_report(self.path.split("/")[3].split("?")[0])
            if report is None:
                self._json({"error": "unbekannte Kennung"}, 404, cors=True)
                return
            body = report.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/health":
            # Ohne Auth: prueft, ob die OeBB-API von diesem Server aus erreichbar ist
            try:
                shared_client._get_token()
                self._json({"status": "ok", "oebb": True})
            except Exception as e:
                self._json({"status": "error", "oebb": False, "detail": str(e)[:200]}, 502)
            return
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._file("index.html")
        elif path.startswith("/static/"):
            self._file(path[len("/static/"):])
        elif path == "/api/stations":
            q = (qs.get("q") or [""])[0]
            if len(q) < 2:
                self._json([])
                return
            try:
                res = shared_client.search_stations(q)
                out = []
                for s in res:
                    name = s.get("name") or s.get("meta")
                    if not name:
                        continue
                    out.append({
                        "id": s["number"],
                        "name": name,
                        "lat": s["latitude"] / 1e6,
                        "lon": s["longitude"] / 1e6,
                    })
                self._json(out)
            except Exception as e:
                self._json({"error": str(e)}, 502)
        elif path == "/api/geocode":
            # eva-Stationsnummern -> Koordinaten (fuer Streckenanzeige), gecacht
            evas = [e for e in (qs.get("evas") or [""])[0].split(",") if e]
            out = {e: _resolve_eva(e) for e in evas[:60] if _resolve_eva(e)}
            self._json(out)
        elif path == "/api/route":
            # Bahntrassen-Geometrie: geordnete eva-Liste + Verkehrsmittel je Bein (modes)
            evas = [e for e in (qs.get("evas") or [""])[0].split(",")][:60]
            modes = [m for m in (qs.get("modes") or [""])[0].split(",")]
            key = ",".join(evas) + "|" + ",".join(modes)
            resolved = [_resolve_eva(e) if e else None for e in evas]
            stops = [s for s in resolved if s]
            if key not in _route_cache:
                _route_cache[key] = _build_route(resolved, modes)
            self._json({"stops": stops, "segments": _route_cache[key]})
        elif path == "/api/history":
            self._json(scanner.list_history())
        elif path.startswith("/api/history/"):
            rec = scanner.get_history(path.split("/")[3])
            self._json(rec if rec else {"error": "nicht gefunden"}, 200 if rec else 404)
        elif path.startswith("/api/scan/") and path.endswith("/events"):
            job_id = path.split("/")[3]
            self._sse(job_id, int((qs.get("seq") or ["0"])[0]))
        elif path.startswith("/api/scan/"):
            job_id = path.split("/")[3]
            job = scanner.get_job(job_id)
            if not job:
                self._json({"error": "unbekannter Scan"}, 404)
                return
            self._json(job_snapshot(job, light="light" in qs))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)

        if path == "/api/debug":
            # Ohne Auth (die lokale App hat kein Passwort); streng groessenbegrenzt
            raw = self.rfile.read(min(length, DEBUG_MAX_CHARS * 4)) if length else b""
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                self._json({"error": "leerer Bericht"}, 400, cors=True)
                return
            self._json({"id": store_debug_report(text)}, cors=True)
            return

        if not self._check_auth():
            return
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._json({"error": "ungueltiges JSON"}, 400)
                return

        if path == "/api/soll":
            for field in ("from", "to", "datetime"):
                if field not in body:
                    self._json({"error": f"Feld fehlt: {field}"}, 400)
                    return
            try:
                soll = scanner.build_soll_list(shared_client, body["from"], body["to"],
                                               body["datetime"])
                self._json({"connections": soll})
            except Exception as e:
                self._json({"error": str(e)}, 502)
        elif path == "/api/scan":
            for field in ("from", "to", "datetime"):
                if field not in body:
                    self._json({"error": f"Feld fehlt: {field}"}, 400)
                    return
            job = scanner.start_scan(body)
            self._json({"jobId": job.id})
        elif path.startswith("/api/scan/") and path.endswith("/cancel"):
            job = scanner.get_job(path.split("/")[3])
            if job:
                job.cancelled = True
                self._json({"ok": True})
            else:
                self._json({"error": "unbekannter Scan"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    # ---------- Server-Sent Events ----------

    def _sse(self, job_id, start_seq):
        job = scanner.get_job(job_id)
        if not job:
            self._json({"error": "unbekannter Scan"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        seq = start_seq
        try:
            while True:
                events = job.events_since(seq, timeout=20)
                if not events:
                    if job.finished:
                        break
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for ev in events:
                    seq = ev["seq"] + 1
                    payload = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if job.finished and ev["type"] in ("done", "error"):
                    break
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


def _keep_awake():
    """Render Free legt die Instanz nach 15 min ohne EINGEHENDE Anfragen schlafen -
    auch mitten im Scan (offene SSE-Streams zaehlen nicht zuverlaessig als Traffic).
    Solange ein Scan laeuft, haelt ein Selbst-Ping ueber die oeffentliche URL
    (durch Renders Proxy = eingehender Traffic) die Instanz wach."""
    while True:
        time.sleep(KEEPALIVE_SECS)
        if not any(not j.finished for j in scanner.jobs_snapshot()):
            continue
        try:
            req = urllib.request.Request(EXTERNAL_URL + "/api/health",
                                         headers={"Connection": "close"})
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            print("Keep-alive-Ping gesendet (Scan laeuft).")
        except Exception as e:
            print(f"Keep-alive-Ping fehlgeschlagen: {e}")


def main():
    print("Lade Stationsdatenbank...")
    n = len(stationsdb.load_stations())
    print(f"{n} Stationen bereit.")
    if EXTERNAL_URL:
        threading.Thread(target=_keep_awake, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Spar Spar Schiene laeuft: http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}"
          + (" (Passwortschutz aktiv)" if PASSWORD else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Beendet.")


if __name__ == "__main__":
    main()
