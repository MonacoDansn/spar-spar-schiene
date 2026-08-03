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

# Adaptive Bremse. WICHTIG (gemessen 2026-07-29): Die OeBB liefert 429 auch bei
# 0,5 Anfragen/s - die Ablehnungen sind sporadisch und haengen NICHT an unserem
# Tempo. Sehr langsam zu werden bringt darum nichts ausser Wartezeit. Die Bremse
# bleibt als milde Vorsichtsmassnahme, aber mit engem Deckel; hartnaeckige Faelle
# wandern stattdessen in die Wiederholungs-Warteschlange des Scanners.
THROTTLE_COOLDOWN = 3.0     # innerhalb dieser Zeit zaehlen 429 als ein Ereignis
RECOVER_AFTER = 10.0        # so lange Ruhe -> Tempo darf wieder steigen
RECOVER_STEP_EVERY = 4.0    # Abstand zwischen zwei Beschleunigungsschritten
MAX_INTERVAL = 1.0          # Untergrenze: 1 Anfrage / s (mehr bringt nichts)
# Kurze Wiederholungen: lieber schnell scheitern und den Kandidaten spaeter
# erneut versuchen, als eine Anfrage minutenlang festzuhalten.
RATE_RETRIES = 2
RETRY_WAITS = (1.0, 2.0)


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
        # Diagnose: wie hart bremst die OeBB uns tatsaechlich aus?
        self.rate_hits = 0          # empfangene 429
        self.retry_wait = 0.0       # Sekunden, die deswegen gewartet wurde
        # Verbindung UND Session/Token sind pro Thread: viele parallele Abfragen
        # auf EINER anonymen Session lassen die OeBB-Routing-Engine still
        # degenerieren (leere Ergebnisse) - eigene Session je Worker vermeidet das.
        self._local = threading.local()

    # ---------- intern ----------

    def _throttle(self):
        """Globaler Mindestabstand zwischen Anfragen. Der Startzeitpunkt wird
        unter der Sperre reserviert, geschlafen wird OHNE sie - sonst warten alle
        Worker der Reihe nach am Lock statt parallel zu arbeiten."""
        with self._rate_lock:
            start = max(time.time(), self._last_request + self._min_interval)
            self._last_request = start
        wait = start - time.time()
        if wait > 0:
            time.sleep(wait)

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
        rate_retries = RATE_RETRIES   # 429: kurz wiederholen, dann Warteschlange
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
                with self._rate_lock:
                    self.rate_hits += 1
                self._slow_down()
                if rate_retries > 0:
                    stufe = RATE_RETRIES - rate_retries
                    rate_retries -= 1
                    try:
                        wait = min(float(resp_headers.get("Retry-After", "")), RETRY_WAITS[-1])
                    except (TypeError, ValueError):
                        wait = RETRY_WAITS[min(stufe, len(RETRY_WAITS) - 1)]
                    with self._rate_lock:
                        self.retry_wait += wait
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


def _section_mode(sec):
    """'train' fuer Zug-Abschnitte, sonst 'other' (Bus, Fussweg, Umstieg)."""
    if sec.get("type") == "journey" and (sec.get("category") or {}).get("train"):
        return "train"
    return "other"


def connection_segments(connection):
    """Verbindung als Punktfolge + Verkehrsmittel je Abschnitt.

    Rueckgabe: {"path": [{name, eva}, ...], "modes": [mode_leg0, mode_leg1, ...]}
    wobei modes[i] das Verkehrsmittel zwischen path[i] und path[i+1] ist.
    Nur 'train'-Abschnitte werden spaeter entlang der Gleise geroutet; Bus/Fussweg
    bleiben gerade Linien (sonst snappt der Bahn-Router sie auf fremde Gleise).
    """
    path = []
    modes = []
    for sec in connection.get("sections") or []:
        frm = sec.get("from") or {}
        to = sec.get("to") or {}
        if not frm.get("name") or not to.get("name"):
            continue
        if not path:
            path.append({"name": frm["name"], "eva": frm.get("esn")})
        elif path[-1].get("eva") != frm.get("esn"):
            # Luecke (selten): Verbindungspunkt einfuegen, Bein als 'other'
            path.append({"name": frm["name"], "eva": frm.get("esn")})
            modes.append("other")
        path.append({"name": to["name"], "eva": to.get("esn")})
        modes.append(_section_mode(sec))
    return {"path": path, "modes": modes}


def connection_path(connection):
    """Nur die Punktfolge (Rueckwaertskompatibel)."""
    return connection_segments(connection)["path"]


def booking_url(connection, lang="de", adults=1):
    """Deep-Link in den OeBB-Ticketshop, der Von/Nach/Datum/Zeit vorausfuellt.

    Parameter-Namen/Format aus dem Shop-Bundle (web.main.js) verifiziert:
    stationOrigEva/Name, stationDestEva/Name (eva = esn der Verbindung, NICHT die
    Shop-Stationsnummer!), outwardDate=YYYY-MM-DD, outwardTime=HH:MM, numberOfAdults.
    Faellt bei fehlender esn auf die Ticketshop-Startseite zurueck.
    """
    frm = connection.get("from") or {}
    to = connection.get("to") or {}
    base = f"https://shop.oebbtickets.at/{lang}/ticket"
    if not frm.get("esn") or not to.get("esn"):
        return base
    dep = frm.get("departure") or ""  # z.B. 2026-07-18T08:30:00.000
    params = {
        "stationOrigEva": frm["esn"],
        "stationOrigName": frm.get("name", ""),
        "stationDestEva": to["esn"],
        "stationDestName": to.get("name", ""),
        "numberOfAdults": adults,
    }
    if len(dep) >= 16:
        params["outwardDate"] = dep[:10]
        params["outwardTime"] = dep[11:16]
    return base + "?" + urllib.parse.urlencode(params)


if __name__ == "__main__":
    c = OebbClient()
    neumarkt = {"id": 1250304, "name": "Neumarkt/Wallersee Bahnhof"}
    wien = {"id": 8103000, "name": "Wien Hbf"}
    conns, prices = c.connection_search(neumarkt, wien, datetime_departure="2026-07-18T12:00:00.000")
    for conn in conns:
        p = prices.get(conn["id"], {})
        print(conn["from"]["departure"], "->", conn["to"]["arrival"],
              connection_trains(conn), p.get("price"), "Sparschiene" if p.get("sparschiene") else "")
