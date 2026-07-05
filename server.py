"""Spar Spar Schiene - Web-Server (nur Python-Standardbibliothek).

Lokal:   python server.py            -> http://localhost:8325
Hosting: PORT/HOST via Umgebungsvariablen; optionaler Passwortschutz
         ueber SPAR_PASSWORD (HTTP Basic Auth, Benutzername egal).
"""
import base64
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import oebb
import scanner
import stations as stationsdb

PORT = int(os.environ.get("PORT", "8325"))
HOST = os.environ.get("HOST", "127.0.0.1")
PASSWORD = os.environ.get("SPAR_PASSWORD")
PUBLIC = os.path.join(os.path.dirname(__file__), "public")

shared_client = oebb.OebbClient(max_rps=8)

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # ruhig bleiben

    # ---------- Antwort-Helfer ----------

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
        elif path.startswith("/api/scan/") and path.endswith("/events"):
            job_id = path.split("/")[3]
            self._sse(job_id, int((qs.get("seq") or ["0"])[0]))
        elif path.startswith("/api/scan/"):
            job_id = path.split("/")[3]
            job = scanner.get_job(job_id)
            if not job:
                self._json({"error": "unbekannter Scan"}, 404)
                return
            self._json({"finished": job.finished,
                        "results": sorted(job.results.values(), key=lambda r: r["price"])})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._json({"error": "ungueltiges JSON"}, 400)
                return

        if path == "/api/scan":
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


def main():
    print("Lade Stationsdatenbank...")
    n = len(stationsdb.load_stations())
    print(f"{n} Stationen bereit.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Spar Spar Schiene laeuft: http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}"
          + (" (Passwortschutz aktiv)" if PASSWORD else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Beendet.")


if __name__ == "__main__":
    main()
