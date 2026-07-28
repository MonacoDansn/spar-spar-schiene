"""Stationsdatenbank: laedt Trainline-EU-Datensatz, filtert Stationen mit OeBB-IDs,
bietet Radius-/Halbkreis-Suche fuer Kandidaten-Bahnhoefe."""
import csv
import json
import math
import os

# Uebersteuerbar fuer die lokale Android-App: Code liegt dort im (nicht
# beschreibbaren) APK, Daten/Caches muessen in den App-Speicher.
DATA_DIR = os.environ.get("SPAR_DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")
RAW_CSV = os.path.join(DATA_DIR, "stations_full.csv")
CACHE_JSON = os.path.join(DATA_DIR, "stations.json")

# Laender rund um Oesterreich, in denen Sparschiene-Relationen sinnvoll sind
COUNTRIES = {"AT", "DE", "IT", "HU", "CZ", "SK", "SI", "CH", "HR", "PL", "NL", "BE", "FR", "DK"}

_stations = None


def _build_cache():
    stations = []
    with open(RAW_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            if row.get("country") not in COUNTRIES:
                continue
            obb_id = (row.get("obb_id") or "").strip()
            lat = row.get("latitude")
            lon = row.get("longitude")
            if not obb_id or not lat or not lon:
                continue
            try:
                stations.append({
                    "name": row["name"],
                    "id": int(obb_id),
                    "lat": float(lat),
                    "lon": float(lon),
                    "country": row["country"],
                })
            except ValueError:
                continue
    with open(CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(stations, f, ensure_ascii=False)
    return stations


def load_stations():
    global _stations
    if _stations is None:
        if os.path.exists(CACHE_JSON):
            with open(CACHE_JSON, encoding="utf-8") as f:
                _stations = json.load(f)
        else:
            _stations = _build_cache()
    return _stations


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def in_half_disc(center_lat, center_lon, other_lat, other_lon, radius_km, lat, lon):
    """True, wenn (lat, lon) im Halbkreis um center liegt, weggedreht von other."""
    d = haversine_km(center_lat, center_lon, lat, lon)
    if d > radius_km or d < 0.05:
        return False
    coslat = math.cos(math.radians(center_lat))
    ox = (other_lon - center_lon) * coslat
    oy = other_lat - center_lat
    sx = (lon - center_lon) * coslat
    sy = lat - center_lat
    return sx * ox + sy * oy <= 0


def candidates_behind(center_lat, center_lon, other_lat, other_lon, radius_km):
    """Alle Stationen im Umkreis von center, die im Halbkreis 'weggedreht' von other liegen.

    Halbkreis-Kriterium: Vektor (Station - Center) zeigt nicht Richtung Other,
    d.h. Skalarprodukt mit (Other - Center) <= 0.
    """
    result = []
    # Equirektangulare Projektion fuer Richtungsvektoren (lokal ausreichend genau)
    coslat = math.cos(math.radians(center_lat))
    ox = (other_lon - center_lon) * coslat
    oy = other_lat - center_lat
    for s in load_stations():
        d = haversine_km(center_lat, center_lon, s["lat"], s["lon"])
        if d > radius_km or d < 0.05:
            continue
        sx = (s["lon"] - center_lon) * coslat
        sy = s["lat"] - center_lat
        if sx * ox + sy * oy <= 0:
            entry = dict(s)
            entry["dist_km"] = round(d, 1)
            result.append(entry)
    result.sort(key=lambda x: x["dist_km"])
    return result


if __name__ == "__main__":
    st = load_stations()
    print("Stationen geladen:", len(st))
    c = candidates_behind(47.954675, 13.224806, 48.185, 16.376, 30)
    print("Kandidaten hinter Neumarkt/Wallersee (30km, weg von Wien):", len(c))
    for x in c[:10]:
        print(" ", x["name"], x["dist_km"], "km", x["id"])
