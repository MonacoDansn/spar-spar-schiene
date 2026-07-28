"""Scan-Logik: findet guenstigere Tickets, die die Soll-Verbindung enthalten.

Phasen:
  A: alle Kandidaten-Einstiege A (Halbkreis hinter Start) -> Ziel testen
  B: Start -> alle Kandidaten-Ziele B (Halbkreis hinter Ziel) testen
  C: alle Kombinationen A x B der Ueberlebenden aus A und B

Ein Kandidat 'passt', wenn seine Verbindung die Zuege der Soll-Verbindung
als Teilfolge enthaelt -> man sitzt im selben Zug, steigt aber am eigenen
Bahnhof ein/aus. Das Ticket beginnt/endet nur woanders.
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import oebb
import stations as stationsdb

JOBS = {}
JOBS_LOCK = threading.Lock()

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
PLACES_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "places_cache.json")
_places_cache = None
_places_lock = threading.Lock()


def _osm_places(lat, lon, radius_km):
    """Alle Staedte/Doerfer im Umkreis via OpenStreetMap (Overpass), gecacht."""
    global _places_cache
    key = f"{lat:.2f},{lon:.2f},{int(radius_km)}"
    with _places_lock:
        if _places_cache is None:
            try:
                with open(PLACES_CACHE_FILE, encoding="utf-8") as f:
                    _places_cache = json.load(f)
            except (OSError, ValueError):
                _places_cache = {}
        if key in _places_cache:
            return _places_cache[key]
    query = (f'[out:json][timeout:50];'
             f'node["place"~"^(city|town|village)$"](around:{int(radius_km * 1000)},{lat},{lon});out;')
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    result = None
    last_err = None
    for url in OVERPASS_URLS:  # bei 504/Ueberlastung auf Spiegel-Server ausweichen
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"User-Agent": "SparSparSchiene/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                result = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            last_err = e
    if result is None:
        raise last_err
    places = []
    for el in result.get("elements", []):
        name = (el.get("tags") or {}).get("name")
        if name and el.get("lat") and el.get("lon"):
            places.append({"name": name, "lat": el["lat"], "lon": el["lon"]})
    with _places_lock:
        _places_cache[key] = places
        try:
            with open(PLACES_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(_places_cache, f, ensure_ascii=False)
        except OSError:
            pass
    return places

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


BUS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "bus_cache.json")
_bus_cache = None
_bus_cache_lock = threading.Lock()


def _load_bus_cache():
    global _bus_cache
    with _bus_cache_lock:
        if _bus_cache is None:
            try:
                with open(BUS_CACHE_FILE, encoding="utf-8") as f:
                    _bus_cache = json.load(f)
            except (OSError, ValueError):
                _bus_cache = {}
        return _bus_cache


def _save_bus_cache():
    with _bus_cache_lock:
        try:
            with open(BUS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(_bus_cache, f, ensure_ascii=False)
        except OSError:
            pass


def town_from_station(name):
    """'Hallwang-Elixhausen Bahnhof' -> 'Hallwang-Elixhausen'"""
    n = re.sub(r"\s*\(.*?\)", "", name)
    n = re.sub(r"\s+(Hauptbahnhof|Westbahnhof|Ostbahnhof|Bahnhofstra\S+|Bahnhof|Bahnhst|Bahnst|Hbf|Bf)$",
               "", n, flags=re.I)
    return n.strip()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000")


def _parse_iso(s):
    return datetime.fromisoformat(s.replace(".000", ""))


class ScanJob:
    def __init__(self, params):
        self.id = uuid.uuid4().hex[:12]
        self.params = params
        self.events = []
        self.cond = threading.Condition()
        self.cancelled = False
        self.finished = False
        self.results = {}  # key -> result dict (dedupe, cheapest wins)
        # Defaults per Umgebung drosselbar: Render-Free hat nur 0,1 CPU und teilt
        # sich die Ausgangs-IP mit anderen Kunden - dort gelten sanftere Werte.
        default_rps = float(os.environ.get("SPAR_MAX_RPS", "24"))
        self.client = oebb.OebbClient(max_rps=float(params.get("maxRps", default_rps)))
        self.skip_travel_action = False
        self.timeout_streak = 0  # Zeitueberschreitungen in Folge (Abbruch-Kriterium)
        self.stats_lock = threading.Lock()
        self.stats = {"queries": 0, "empty": 0, "conns": 0, "matches": 0, "price_fail": 0}
        self.eta_tracker = EtaTracker()
        self.phase_state = None    # letzter Fortschritt inkl. ETA (fuer Snapshot/App)
        self.pending_phases = {}   # bekannte, noch nicht gestartete Phasen: name -> total
        self.c_known = False       # Umfang von Phase C bekannt -> ETA nicht mehr "mind."
        self.thread = threading.Thread(target=self._run_safe, daemon=True)

    def _stat(self, **kwargs):
        with self.stats_lock:
            for k, v in kwargs.items():
                self.stats[k] += v

    def _stats_line(self):
        s = dict(self.stats)
        return (f"Statistik: {s['queries']} Abfragen ({s['empty']} leer), "
                f"{s['conns']} Verbindungen, {s['matches']} Zug-Matches, "
                f"{s['price_fail']} Preisfehler")

    # ---------- Events ----------

    def emit(self, etype, data):
        with self.cond:
            self.events.append({"seq": len(self.events), "type": etype,
                                "ts": time.strftime("%H:%M:%S"), "data": data})
            self.cond.notify_all()

    def events_since(self, seq, timeout=25):
        with self.cond:
            if len(self.events) <= seq and not self.finished:
                self.cond.wait(timeout)
            return self.events[seq:]

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

    # ---------- Hilfen ----------

    def _search(self, from_st, to_st, dep=None, arr=None):
        """Verbindungssuche, optional ohne travelAction (wenn Probe erfolgreich war)."""
        if self.skip_travel_action:
            ta = None
        else:
            ta = self.client.travel_action(from_st, to_st, dep or arr)
        conns = self.client.timetable(ta, from_st, to_st,
                                      datetime_departure=dep, datetime_arrival=arr)
        self._stat(queries=1, conns=len(conns), empty=1 if not conns else 0)
        return conns

    def _add_result(self, ticket_from, ticket_to, conn, price_info, soll, phase):
        trains = oebb.connection_trains(conn)
        key = (ticket_from["id"], ticket_to["id"], tuple(trains))
        price = price_info.get("price")
        if price is None:
            return
        entry = {
            "reduced": price_info.get("reduced", False),
            "reducedScope": price_info.get("reducedScope"),
            "ticketFrom": ticket_from["name"],
            "ticketTo": ticket_to["name"],
            "ticketFromId": ticket_from["id"],
            "ticketToId": ticket_to["id"],
            "price": price,
            "sparschiene": price_info.get("sparschiene", False),
            "sollPrice": soll["price"],
            "saving": round((soll["price"] or 0) - price, 2) if soll["price"] is not None else None,
            "trains": trains,
            "sollTrains": soll["trains"],
            "boardTime": soll["dep"],
            "alightTime": soll["arr"],
            "ticketDep": conn["from"]["departure"],
            "ticketArr": conn["to"]["arrival"],
            "phase": phase,
            "lat": ticket_from.get("lat"),
            "lon": ticket_from.get("lon"),
        }
        old = self.results.get(key)
        if old is None or price < old["price"]:
            self.results[key] = entry
            self.emit("result", entry)

    def _match_and_price(self, conns, soll_list):
        """Findet Verbindungen, die eine Soll-Verbindung abdecken, und holt Preise."""
        matches = []  # (conn, soll)
        for conn in conns:
            trains = oebb.connection_trains(conn)
            for soll in soll_list:
                if oebb.covers(trains, soll["trains"]):
                    matches.append((conn, soll))
                    break
        if not matches or self.cancelled:
            return []
        self._stat(matches=len(matches))
        price_map = self.client.prices([c["id"] for c, _ in matches])
        out = []
        for conn, soll in matches:
            info = price_map.get(conn["id"])
            if info and not info["error"] and info["price"] is not None:
                out.append((conn, soll, info))
        self._stat(price_fail=len(matches) - len(out))
        return out

    # ---------- Ablauf ----------

    def _run_safe(self):
        try:
            self._run()
        except Exception as e:
            self.emit("error", {"message": str(e)})
        finally:
            self.finished = True
            with self.cond:
                self.cond.notify_all()

    def _run(self):
        p = self.params
        start = p["from"]   # {id, name, lat, lon}
        dest = p["to"]
        dep_str = p["datetime"]
        if len(dep_str) == 16:
            dep_str += ":00.000"

        # ---- Soll-Verbindungen ----
        self.emit("phase", {"name": "soll", "label": "Soll-Verbindungen laden"})
        conns, price_map = self.client.connection_search(start, dest, datetime_departure=dep_str)
        soll_list = []
        for conn in conns:
            info = price_map.get(conn["id"]) or {}
            trains = oebb.connection_trains(conn)
            if not trains:
                continue
            soll_list.append({
                "trains": trains,
                "dep": conn["from"]["departure"],
                "arr": conn["to"]["arrival"],
                "price": info.get("price"),
                "sparschiene": info.get("sparschiene", False),
                "fromName": conn["from"]["name"],
                "toName": conn["to"]["name"],
            })
        if not soll_list:
            raise RuntimeError("Keine Soll-Verbindungen gefunden. Datum/Stationen pruefen.")
        self.emit("soll", {"connections": soll_list})

        # Probe: geht es ohne travelAction? (spart 1/3 der Requests)
        try:
            test = self.client.timetable(None, start, dest, datetime_departure=dep_str, count=1)
            if test:
                pm = self.client.prices([test[0]["id"]])
                if pm and not next(iter(pm.values()))["error"]:
                    self.skip_travel_action = True
                    self.emit("log", {"msg": "Optimierung aktiv: travelAction wird uebersprungen"})
        except Exception:
            pass

        # Zeitanker: die Engine liefert pro Abfrage nur ~6 Verbindungen. Bei grosser
        # Zeitspanne der Soll-Verbindungen wird darum mit ZWEI Ankern gesucht
        # (frueheste + spaeteste), sonst faellt ein Teil aus dem Fenster.
        arr_times = sorted(_parse_iso(s["arr"]) for s in soll_list)
        dep_times = sorted(_parse_iso(s["dep"]) for s in soll_list)
        arr_anchors = [_iso(arr_times[-1] + timedelta(minutes=1))]
        if arr_times[-1] - arr_times[0] > timedelta(minutes=45):
            arr_anchors.append(_iso(arr_times[0] + timedelta(minutes=1)))
        dep_anchors = [_iso(dep_times[0] - timedelta(minutes=1))]
        if dep_times[-1] - dep_times[0] > timedelta(minutes=45):
            dep_anchors.append(_iso(dep_times[-1] - timedelta(minutes=1)))
        first_dep = dep_times[0]

        # ---- Kandidaten ermitteln ----
        r_start = float(p.get("radiusStart", 40))
        r_dest = float(p.get("radiusDest", 40))
        origins = stationsdb.candidates_behind(start["lat"], start["lon"], dest["lat"], dest["lon"], r_start)
        dests = stationsdb.candidates_behind(dest["lat"], dest["lon"], start["lat"], start["lon"], r_dest)
        if p.get("autoBus", True):
            self._augment_bus(origins, "Einstieg-Seite", start, dest, r_start)
            self._augment_bus(dests, "Ziel-Seite", dest, start, r_dest)
            _save_bus_cache()
        _merge_extras(origins, p.get("extraOrigins") or [])
        _merge_extras(dests, p.get("extraDests") or [])
        self.emit("candidates", {"origins": origins, "dests": dests})
        self.pending_phases = {"A": len(origins), "B": len(dests)}

        start_self = dict(start, dist_km=0)
        dest_self = dict(dest, dist_km=0)

        # ---- Phase A: fruehere Einstiege ----
        surviving_origins = []
        self._phase_scan("A", "Einstiegs-Bahnhoefe testen", origins,
                         lambda a: self._try_pair(a, dest_self, arrs=arr_anchors,
                                                  soll_list=soll_list, phase="A"),
                         on_survivor=lambda a, dep, best, spar, ref:
                             surviving_origins.append(dict(a, dep=dep, bestPrice=best, spar=spar, refPrice=ref)))

        # ---- Phase B: weitere Ziele ----
        surviving_dests = []
        self._phase_scan("B", "Ziel-Bahnhoefe testen", dests,
                         lambda b: self._try_pair(start_self, b, deps=dep_anchors,
                                                  soll_list=soll_list, phase="B"),
                         on_survivor=lambda b, dep, best, spar, ref:
                             surviving_dests.append(dict(b, bestPrice=best, spar=spar, refPrice=ref)))

        # ---- Phase C: Kombinationen (Einstieg != Ausstieg) ----
        combo_origins, combo_dests = surviving_origins, surviving_dests
        if p.get("comboMode", "attractive") != "all":
            def attractive(x):
                # attraktiv = Sparschiene gefunden ODER dort schon mind. so guenstig wie die
                # gematchte Soll-Verbindung selbst
                return x.get("spar") or (x.get("refPrice") is not None and x.get("bestPrice") is not None
                                         and x["bestPrice"] <= x["refPrice"] + 0.01)

            combo_origins = [a for a in surviving_origins if attractive(a)]
            combo_dests = [b for b in surviving_dests if attractive(b)]
            self.emit("log", {"msg": f"Attraktiv-Filter: {len(combo_origins)}/{len(surviving_origins)} Einstiege, "
                                     f"{len(combo_dests)}/{len(surviving_dests)} Ziele (Sparschiene oder <= Soll-Preis)"})
        combos = [(a, b) for a in combo_origins for b in combo_dests]
        self.pending_phases["C"] = len(combos)
        self.c_known = True
        self.emit("log", {"msg": f"Phase C: {len(combos)} Kombinationen aus "
                                 f"{len(combo_origins)} Einstiegen x {len(combo_dests)} Zielen"})
        self._phase_scan("C", "Kombinationen testen", combos,
                         lambda ab: self._try_pair(
                             ab[0], ab[1],
                             deps=[_iso(_parse_iso(ab[0]["dep"]) - timedelta(minutes=10))],
                             soll_list=soll_list, phase="C"),
                         on_survivor=None)

        self.emit("log", {"msg": self._stats_line()})
        results = sorted(self.results.values(), key=lambda r: r["price"])
        self.emit("done", {"count": len(results)})

    def _phase_scan(self, phase_id, label, items, worker, on_survivor):
        self.emit("phase", {"name": phase_id, "label": label, "total": len(items)})
        self.pending_phases.pop(phase_id, None)
        if not items:
            return
        done = 0
        found = 0
        default_workers = int(os.environ.get("SPAR_WORKERS", "20"))
        with ThreadPoolExecutor(max_workers=int(self.params.get("workers", default_workers))) as pool:
            futures = {pool.submit(worker, item): item for item in items}
            for fut in as_completed(futures):
                if self.cancelled:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError("Scan abgebrochen")
                item = futures[fut]
                done += 1
                self.eta_tracker.record()
                try:
                    hits = fut.result()
                    self.timeout_streak = 0
                except Exception as e:
                    msg = str(e)
                    if msg == "nicht buchbar":
                        self.emit("log", {"msg": f"uebersprungen (nicht buchbar): {self._item_name(item)}"})
                    elif msg.startswith("Zeitueberschreitung"):
                        self.timeout_streak += 1
                        self.emit("log", {"msg": f"Zeitueberschreitung: {self._item_name(item)} - uebersprungen "
                                                 f"({self.timeout_streak} in Folge)"})
                        if self.timeout_streak >= 10:
                            self.cancelled = True
                            pool.shutdown(wait=False, cancel_futures=True)
                            raise RuntimeError(
                                "Abbruch: Die OeBB-API antwortet nicht mehr (10 Zeitueberschreitungen "
                                "in Folge). Bitte in ein paar Minuten erneut versuchen - "
                                "bisherige Ergebnisse bleiben erhalten.")
                    else:
                        self.emit("log", {"msg": f"Fehler bei {self._item_name(item)}: {msg[:160]}"})
                    hits = []
                if hits:
                    found += 1
                    if on_survivor is not None:
                        # dep-Zeit, Bestpreis, Sparschiene-Flag und Referenzpreis merken (fuer Phase C).
                        # Teilstrecken-Angebote (reduced) zaehlen NICHT als attraktiv -
                        # ihr Preis deckt die Relation ja nicht ab.
                        conn = hits[0][0]
                        full = [h for h in hits if not h[2].get("reduced")]
                        best = min((h[2]["price"] for h in full), default=None)
                        spar = any(h[2]["sparschiene"] for h in full)
                        refs = [h[1]["price"] for h in full if h[1]["price"] is not None]
                        ref = min(refs) if refs else None
                        target = item if not isinstance(item, tuple) else item[0]
                        on_survivor(target, conn["from"]["departure"], best, spar, ref)
                self._emit_progress(phase_id, label, done, len(items), found)

    # ---------- Bushaltestellen automatisch ergaenzen ----------

    def _augment_bus(self, candidates, label, center, other, radius_km):
        """Fuegt pro Ort im Halbkreis eine Bushaltestelle als Kandidat hinzu.

        Orte kommen flaechendeckend aus OpenStreetMap (alle Staedte/Doerfer im Gebiet),
        nicht nur entlang der Bahnlinien. Zusaetzlich werden die Gemeindenamen der
        Bahnhof-Kandidaten beruecksichtigt.
        """
        towns = {}
        for c in candidates:
            if c.get("bus"):
                continue
            t = town_from_station(c["name"])
            if len(t) >= 3 and t.lower() not in towns:
                towns[t.lower()] = (t, c)
        if radius_km >= 1:
            try:
                places = _osm_places(center["lat"], center["lon"], radius_km)
            except Exception as e:
                places = []
                self.emit("log", {"msg": f"OSM-Ortsabfrage fehlgeschlagen ({label}): {str(e)[:80]}"})
            in_area = 0
            for pl in places:
                if not stationsdb.in_half_disc(center["lat"], center["lon"],
                                               other["lat"], other["lon"], radius_km,
                                               pl["lat"], pl["lon"]):
                    continue
                in_area += 1
                key = pl["name"].lower()
                if key not in towns:
                    towns[key] = (pl["name"], pl)
            self.emit("log", {"msg": f"{in_area} Orte aus OpenStreetMap im Gebiet ({label})"})
        items = list(towns.values())
        if not items:
            return
        self.emit("phase", {"name": "bus", "label": f"Bushaltestellen suchen ({label})", "total": len(items)})
        have = {c["id"] for c in candidates}
        done = 0
        added = 0
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {pool.submit(self._find_bus_stop, t, near): t for t, near in items}
            for fut in as_completed(futures):
                if self.cancelled:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError("Scan abgebrochen")
                done += 1
                try:
                    stop = fut.result()
                except Exception:
                    stop = None
                if stop and stop["id"] not in have:
                    have.add(stop["id"])
                    candidates.append(stop)
                    added += 1
                self.eta_tracker.record()
                self._emit_progress("bus", f"Bushaltestellen suchen ({label})",
                                    done, len(items), added)
        self.emit("log", {"msg": f"{added} Bushaltestellen ergaenzt ({label})"})

    def _find_bus_stop(self, town, near_station):
        cache = _load_bus_cache()
        # Ortsnamen sind nicht eindeutig (z.B. "Brunn") -> Region in den Key aufnehmen
        key = f"{town.lower()}|{near_station['lat']:.1f},{near_station['lon']:.1f}"
        if key in cache:
            return cache[key]
        stop = None
        try:
            results = self.client.search_stations(town)
        except Exception:
            results = []
        first_word = town.lower().split()[0].split("-")[0]
        for s in results:
            name = s.get("name") or ""
            number = s.get("number")
            # Heuristik: kleine Nummern (< 1 Mio) sind Bus-/Lokalhaltestellen
            if not name or not number or number >= 1_000_000:
                continue
            if first_word not in name.lower():
                continue
            lat, lon = s["latitude"] / 1e6, s["longitude"] / 1e6
            if stationsdb.haversine_km(near_station["lat"], near_station["lon"], lat, lon) > 8:
                continue
            stop = {"name": name, "id": number, "lat": lat, "lon": lon,
                    "dist_km": None, "bus": True}
            break
        with _bus_cache_lock:
            _bus_cache[key] = stop
        return stop

    @staticmethod
    def _item_name(item):
        if isinstance(item, tuple):
            return f"{item[0]['name']} -> {item[1]['name']}"
        return item.get("name", "?")

    def _try_pair(self, from_st, to_st, soll_list, phase, deps=None, arrs=None):
        """WICHTIG: Preise muessen SOFORT nach jeder timetable-Abfrage geholt werden.
        Der prices-Endpoint bepreist nur Verbindungen aus der jeweils LETZTEN
        timetable-Abfrage der Session - eine weitere Abfrage (z.B. zweiter
        Zeitanker) ueberschreibt den Kontext und die aelteren Verbindungen
        blieben still ohne Preis."""
        all_hits = []
        seen = set()
        anchors = [("dep", d) for d in deps or []] + [("arr", a) for a in arrs or []]
        for kind, anchor in anchors:
            if self.cancelled:
                return all_hits
            conns = self._search(from_st, to_st,
                                 dep=anchor if kind == "dep" else None,
                                 arr=anchor if kind == "arr" else None)
            fresh = [c for c in conns if c["id"] not in seen]
            seen.update(c["id"] for c in fresh)
            hits = self._match_and_price(fresh, soll_list)
            for conn, soll, info in hits:
                self._add_result(from_st, to_st, conn, info, soll, phase)
            all_hits.extend(hits)
        return all_hits


def _merge_extras(candidates, extras):
    """Manuell hinzugefuegte Haltestellen (z.B. Bus) in die Kandidatenliste mischen."""
    have = {c["id"] for c in candidates}
    for e in extras:
        if not e.get("id") or e["id"] in have:
            continue
        candidates.append({
            "name": e.get("name", "?"),
            "id": e["id"],
            "lat": e.get("lat"),
            "lon": e.get("lon"),
            "dist_km": None,
            "extra": True,
        })
        have.add(e["id"])


def start_scan(params):
    job = ScanJob(params)
    with JOBS_LOCK:
        # Fertige alte Jobs aufraeumen (nur die 3 juengsten behalten): die Instanz
        # kann wochenlang laufen, sonst wachsen Events/Ergebnisse im RAM unbegrenzt.
        finished = [k for k, j in JOBS.items() if j.finished]
        for k in finished[:-3]:
            del JOBS[k]
        JOBS[job.id] = job
    job.thread.start()
    return job


def get_job(job_id):
    with JOBS_LOCK:
        return JOBS.get(job_id)


def jobs_snapshot():
    with JOBS_LOCK:
        return list(JOBS.values())
