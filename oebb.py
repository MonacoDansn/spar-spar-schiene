"""Client fuer die inoffizielle OeBB-Ticketshop-API (shop.oebbtickets.at).

Verifizierter Ablauf (Stand 2026-07):
  1. GET  /api/domain/v4/init                  -> anonymer AccessToken (JWT, ~5 min gueltig)
  2. POST /api/offer/v2/travelActions          -> travelActionId fuer eine Relation
  3. POST /api/hafas/v4/timetable              -> Verbindungen (wichtig: vollstaendiger Body
     mit entryPointId, sortType, filter und echtem Passagier-Objekt, sonst haengt Schritt 4!)
  4. GET  /api/offer/v1/prices?connectionIds[] -> Preise inkl. Sparschiene-Kennzeichnung
"""
import http.client
import json
import ssl
import threading
import time
import urllib.parse

HOST = "shop.oebbtickets.at"
BASE_PATH = "/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SparSparSchiene/1.0"

PASSENGER_ADULT = {
    "me": False,
    "remembered": False,
    "markedForDeath": False,
    "challengedFlags": {
        "hasHandicappedPass": False,
        "hasAssistanceDog": False,
        "hasWheelchair": False,
        "hasAttendant": False,
    },
    "cards": [],
    "relations": [],
    "type": "ADULT",
    "id": 1751500000000,
}

DEFAULT_FILTER = {
    "regionaltrains": False, "direct": False, "changeTime": False,
    "wheelchair": False, "bikes": False, "trains": False,
    "motorail": False, "droppedConnections": False,
}
DEBUG_FILTER = {
    "noAggregationFilter": False, "disableAggregation": False,
    "disableEqclassFilter": False, "disableReductionFilter": False,
}


class OebbError(Exception):
    pass


class OebbTimeout(OebbError):
    """Eine Abfrage hat das Zeitlimit ueberschritten."""
    pass


# Zeitlimits pro Abfragetyp (Sekunden). Normale Antworten kommen in < 1s;
# der bekannte Pathologie-Fall (Offer-Engine haengt) laeuft serverseitig in 60s ins 504.
TIMEOUT_DEFAULT = 15
TIMEOUT_PRICES = 20

# Adaptive Bremse: 429-Meldungen kommen bei paralleler Suche im Schwung herein -
# alle melden aber DASSELBE Limit. Ohne Cooldown verdoppelt jede einzelne Meldung
# die Wartezeit (10 Worker = Faktor 1024) und die Suche kriecht nur noch.
THROTTLE_COOLDOWN = 3.0     # innerhalb dieser Zeit zaehlen 429 als ein Ereignis
RECOVER_AFTER = 10.0        # so lange Ruhe -> Tempo darf wieder steigen
RECOVER_STEP_EVERY = 4.0    # Abstand zwischen zwei Beschleunigungsschritten
MAX_INTERVAL = 5.0          # Notfall-Untergrenze: 1 Anfrage / 5 s


class OebbClient:
    """Thread-sicherer API-Client: Keep-Alive-Verbindung pro Thread,
    Token-Verwaltung, globales Rate-Limit mit adaptiver 429-Bremse."""

    def __init__(self, max_rps=24.0):
        self._token_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._min_interval = 1.0 / max_rps
        self._base_interval = self._min_interval
        self._last_request = 0.0
        self._last_throttle = 0.0   # letzte Bremsung (fuer Cooldown/Erholung)
        self._last_recover = 0.0
        # Verbindung UND Session/Token sind pro Thread: viele parallele Abfragen
        # auf EINER anonymen Session lassen die OeBB-Routing-Engine still
        # degenerieren (leere Ergebnisse) - eigene Session je Worker vermeidet das.
        self._local = threading.local()

    # ---------- intern ----------

    def _throttle(self):
        with self._rate_lock:
            wait = self._last_request + self._min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()

    def _slow_down(self):
        """Nach HTTP 429: Tempo halbieren - aber hoechstens einmal pro Cooldown.
        Parallele Worker melden dasselbe Limit; ohne diese Sperre faellt die Rate
        nach einem einzigen Drossel-Schwung sofort auf die Untergrenze."""
        with self._rate_lock:
            now = time.time()
            if now - self._last_throttle < THROTTLE_COOLDOWN:
                return
            self._last_throttle = now
            self._min_interval = min(self._min_interval * 2.0, MAX_INTERVAL)

    def _recover_speed(self):
        """Nach einer ruhigen Phase zuegig wieder beschleunigen (Halbierung je
        Schritt). Frueher ~0.98 pro Erfolg - das brauchte hunderte Anfragen und
        die Suche blieb praktisch dauerhaft im Schneckentempo."""
        with self._rate_lock:
            if self._min_interval <= self._base_interval:
                return
            now = time.time()
            if now - self._last_throttle < RECOVER_AFTER:
                return
            if now - self._last_recover < RECOVER_STEP_EVERY:
                return
            self._last_recover = now
            self._min_interval = max(self._min_interval * 0.5, self._base_interval)

    def _conn(self, timeout):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = http.client.HTTPSConnection(HOST, timeout=timeout)
            self._local.conn = c
        return c

    def _raw(self, method, pathq, headers, body, timeout=TIMEOUT_DEFAULT):
        """Ein HTTP-Roundtrip ueber die Keep-Alive-Verbindung des Threads.

        - Zeitueberschreitung: Verbindung sofort verwerfen und OebbTimeout werfen
          (KEIN stiller zweiter Versuch hier - das entscheidet der Aufrufer).
        - Kaputte/abgelaufene Keep-Alive-Verbindung: einmal frisch verbinden.
        """
        for fresh in (False, True):
            conn = self._conn(timeout)
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            try:
                conn.request(method, pathq, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, data, dict(resp.getheaders())
            except TimeoutError:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None
                raise OebbTimeout(f"Zeitueberschreitung nach {timeout}s")
            except (http.client.HTTPException, ssl.SSLError, OSError):
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None
                if fresh:
                    raise

    def _get_token(self):
        token = getattr(self._local, "token", None)
        token_time = getattr(self._local, "token_time", 0.0)
        if token is None or time.time() - token_time > 240:
            status, raw, _ = self._raw("GET", BASE_PATH + "/domain/v4/init",
                                       {"User-Agent": UA, "Channel": "inet", "Lang": "de"}, None)
            if status != 200:
                raise OebbError(f"Token-Abruf fehlgeschlagen: HTTP {status}")
            token = json.loads(raw.decode("utf-8"))["accessToken"]
            self._local.token = token
            self._local.token_time = time.time()
        return token

    def _request(self, method, path, body=None, query=None, timeout=TIMEOUT_DEFAULT, retries=2):
        pathq = BASE_PATH + path
        if query:
            pathq += "?" + query
        # Getrennte Budgets, damit sich die Fehlerarten nicht gegenseitig die
        # Wiederholungen wegnehmen und jeder Endpfad seine korrekte Meldung wirft.
        http_attempts = 0     # Netzwerk-/5xx-Fehler: max. `retries` Wiederholungen
        timeout_retries = 1   # Zeitueberschreitungen: genau 1 Wiederholung
        auth_retries = 2      # 401: Token erneuern
        rate_retries = 5      # 429: geduldig wiederholen - kein Kandidat geht verloren
        while True:
            self._throttle()
            try:
                headers = {
                    "User-Agent": UA,
                    "AccessToken": self._get_token(),
                    "Channel": "inet",
                    "Lang": "de",
                    "Accept": "application/json",
                }
                data = None
                if body is not None:
                    headers["Content-Type"] = "application/json"
                    data = json.dumps(body).encode("utf-8")
                status, raw, resp_headers = self._raw(method, pathq, headers, data, timeout=timeout)
            except OebbTimeout:
                # Haengende Abfrage: Tempo drosseln, 1x wiederholen, dann klar melden
                self._slow_down()
                if timeout_retries > 0:
                    timeout_retries -= 1
                    continue
                raise OebbError(f"Zeitueberschreitung bei {path} "
                                f"(Abfrage brach nach {timeout}s ab, 1x wiederholt)")
            except OSError as e:
                http_attempts += 1
                if http_attempts <= retries:
                    time.sleep(1.5 * http_attempts)
                    continue
                raise OebbError(f"Netzwerkfehler bei {path}: {e}")
            if status == 200:
                self._recover_speed()
                return json.loads(raw.decode("utf-8"))
            detail = raw.decode("utf-8", errors="replace")[:300]
            if status == 401:
                # Token abgelaufen -> erneuern und wiederholen (Session ist pro Thread)
                self._local.token = None
                auth_retries -= 1
                if auth_retries >= 0:
                    continue
                raise OebbError(f"HTTP 401 bei {path}: {detail}")
            if '"code":12018' in detail:
                # Relation nicht routbar (aufgelassene Haltestelle etc.) -> kein Retry
                raise OebbError("nicht buchbar")
            if status == 429:
                # OeBB bittet um langsameres Tempo: Rate halbieren und geduldig
                # wiederholen (Retry-After-Header respektieren, wenn vorhanden).
                self._slow_down()
                rate_retries -= 1
                if rate_retries >= 0:
                    try:
                        wait = min(float(resp_headers.get("Retry-After", "")), 30.0)
                    except (TypeError, ValueError):
                        wait = min(2.0 ** (5 - rate_retries), 10.0)
                    time.sleep(wait)
                    continue
                raise OebbError(f"OeBB-Server ueberlastet (HTTP 429) bei {path} - "
                                f"trotz Tempodrosselung keine Antwort, bitte spaeter erneut versuchen")
            if status in (500, 502, 503, 504):
                http_attempts += 1
                if http_attempts <= retries:
                    time.sleep(1.5 * http_attempts)
                    continue
            raise OebbError(f"HTTP {status} bei {path}: {detail}")

    # ---------- oeffentlich ----------

    def search_stations(self, name, count=15):
        q = urllib.parse.urlencode({"name": name, "count": count})
        return self._request("GET", "/hafas/v1/stations", query=q)

    def travel_action(self, from_station, to_station, datetime_str):
        body = {
            "from": {"number": from_station["id"], "name": from_station["name"]},
            "to": {"number": to_station["id"], "name": to_station["name"]},
            "datetime": datetime_str,
            "passengers": [PASSENGER_ADULT],
        }
        data = self._request("POST", "/offer/v2/travelActions", body=body)
        actions = data.get("travelActions") or []
        if not actions:
            raise OebbError("Keine travelAction erhalten")
        return actions[0]["id"]

    def timetable(self, travel_action_id, from_station, to_station,
                  datetime_departure=None, datetime_arrival=None, count=6):
        body = {
            "travelActionId": travel_action_id,
            "reverse": False,
            "filter": DEFAULT_FILTER,
            "debugFilter": DEBUG_FILTER,
            "passengers": [PASSENGER_ADULT],
            "entryPointId": "timetable",
            "sortType": "DEPARTURE",
            "count": count,
            "from": {"number": from_station["id"], "name": from_station["name"]},
            "to": {"number": to_station["id"], "name": to_station["name"]},
        }
        if datetime_departure:
            body["datetimeDeparture"] = datetime_departure
        else:
            body["datetimeArrival"] = datetime_arrival
        data = self._request("POST", "/hafas/v4/timetable", body=body)
        return data.get("connections") or []

    def prices(self, connection_ids):
        if not connection_ids:
            return {}
        parts = ["connectionIds%5B%5D=" + cid for cid in connection_ids]
        q = "&".join(parts) + "&sortType=DEPARTURE"
        data = self._request("GET", "/offer/v1/prices", query=q, timeout=TIMEOUT_PRICES)
        result = {}
        for offer in data.get("offers") or []:
            note = offer.get("specialNote") or {}
            # reducedScope = Preis gilt nur fuer eine TEILSTRECKE der Verbindung!
            scope = offer.get("reducedScope") or []
            scope_txt = "; ".join(
                f"{(s.get('from') or {}).get('name', '?')} → {(s.get('to') or {}).get('name', '?')}"
                for s in scope) or None
            result[offer["connectionId"]] = {
                "price": offer.get("price"),
                "sparschiene": note.get("de") == "Sparschiene",
                "error": bool(offer.get("offerError")),
                "reduced": bool(scope),
                "reducedScope": scope_txt,
            }
        return result

    def connection_search(self, from_station, to_station,
                          datetime_departure=None, datetime_arrival=None,
                          count=6, with_prices=True):
        """Komfort: travelAction + timetable (+ prices) in einem Schritt."""
        dt = datetime_departure or datetime_arrival
        ta = self.travel_action(from_station, to_station, dt)
        conns = self.timetable(ta, from_station, to_station,
                               datetime_departure=datetime_departure,
                               datetime_arrival=datetime_arrival, count=count)
        price_map = {}
        if with_prices and conns:
            price_map = self.prices([c["id"] for c in conns])
        return conns, price_map


def connection_trains(connection):
    """Liste der Zuege einer Verbindung, z.B. ['IC 547', 'RJX 165']."""
    trains = []
    for sec in connection.get("sections") or []:
        if sec.get("type") != "journey":
            continue
        cat = sec.get("category") or {}
        label = f"{cat.get('displayName') or cat.get('shortName') or ''} {cat.get('number') or ''}".strip()
        if label:
            trains.append(label)
    return trains


def covers(candidate_trains, soll_trains):
    """True, wenn die Soll-Zuege als Teilfolge in der Kandidaten-Verbindung stecken."""
    if not soll_trains:
        return False
    it = iter(candidate_trains)
    return all(t in it for t in soll_trains)


if __name__ == "__main__":
    c = OebbClient()
    neumarkt = {"id": 1250304, "name": "Neumarkt/Wallersee Bahnhof"}
    wien = {"id": 8103000, "name": "Wien Hbf"}
    conns, prices = c.connection_search(neumarkt, wien, datetime_departure="2026-07-18T12:00:00.000")
    for conn in conns:
        p = prices.get(conn["id"], {})
        print(conn["from"]["departure"], "->", conn["to"]["arrival"],
              connection_trains(conn), p.get("price"), "Sparschiene" if p.get("sparschiene") else "")
