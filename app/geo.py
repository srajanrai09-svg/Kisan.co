"""Location features.

PostGIS finds nearby farms. OSRM gives road distance and travel time.
If either service is down, the code falls back to straight-line maths so the app keeps working.
"""
import logging
import math
import re

import httpx

from . import config, db
from .cache import cache

log = logging.getLogger("geo")

AVG_SPEED_KMH = 35.0      # used for estimates when OSRM is not reachable
ROAD_FACTOR = 1.35        # road distance is usually ~35% longer than a straight line
ANY_DISTANCE_M = 20_000_000


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlng = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- pincodes
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")


def resolve_pincode(pin, online=True):
    """Centre point of a pincode: first from farms registered there, then (optionally) OpenStreetMap."""
    row = db.get_pincode(pin)
    if row:
        return {"lat": row["lat"], "lng": row["lng"], "source": row["source"]}
    if not (online and config.GEOCODE_ONLINE) or cache.get(f"pinmiss:{pin}"):
        return None
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search",
                      params={"postalcode": pin, "country": "India", "format": "json", "limit": 1},
                      headers={"User-Agent": "LocalFarmerMarket/1.0"}, timeout=3)
        r.raise_for_status()
        hit = r.json()
        if hit:
            lat, lng = float(hit[0]["lat"]), float(hit[0]["lon"])
            db.set_pincode(pin, lat, lng, "nominatim")
            return {"lat": lat, "lng": lng, "source": "nominatim"}
    except Exception as e:
        log.info("Pincode lookup failed (%s)", e.__class__.__name__)
    cache.set(f"pinmiss:{pin}", 1, ttl=600)
    return None


# ---------------------------------------------------------------- PostGIS
def _pg():
    import psycopg

    return psycopg.connect(config.PG_DSN, connect_timeout=2)


def sync_postgis():
    """Copy the farms from SQLite into PostGIS. Returns True if PostGIS is available."""
    try:
        with _pg() as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            c.execute(
                """CREATE TABLE IF NOT EXISTS farms (
                     id INTEGER PRIMARY KEY, name TEXT NOT NULL, area TEXT,
                     geog geography(Point, 4326) NOT NULL)"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS farms_geog_idx ON farms USING GIST (geog)")
            farms = db.farms()
            for f in farms:
                c.execute(
                    """INSERT INTO farms (id, name, area, geog)
                       VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)
                       ON CONFLICT (id) DO UPDATE
                       SET name = EXCLUDED.name, area = EXCLUDED.area, geog = EXCLUDED.geog""",
                    (f["id"], f["name"], f["area"], f["lng"], f["lat"]),
                )
            c.execute("DELETE FROM farms WHERE NOT (id = ANY(%s::int[]))", ([f["id"] for f in farms],))
        log.info("PostGIS synced")
        return True
    except Exception as e:
        log.warning("PostGIS unavailable (%s). Using straight-line search instead.", e.__class__.__name__)
        return False


def postgis_up():
    """Quick health check: can we connect and is PostGIS installed?"""
    try:
        with _pg() as c:
            c.execute("SELECT PostGIS_Version()").fetchone()
        return True
    except Exception:
        return False


def _nearby_postgis(lat, lng, radius_km, limit):
    radius_m = radius_km * 1000 if radius_km else ANY_DISTANCE_M
    with _pg() as c:
        rows = c.execute(
            """SELECT id, ST_Distance(geog, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) / 1000.0 AS km
               FROM farms
               WHERE ST_DWithin(geog, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
               ORDER BY km LIMIT %s""",
            (lng, lat, lng, lat, radius_m, limit),
        ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def _nearby_python(lat, lng, radius_km, limit):
    out = []
    for f in db.farms():
        d = haversine_km(lat, lng, f["lat"], f["lng"])
        if not radius_km or d <= radius_km:
            out.append((f["id"], d))
    out.sort(key=lambda x: x[1])
    return dict(out[:limit])


def nearby_farms(lat, lng, radius_km=50, limit=20, roads=True):
    """Nearest farms first. Each item has km (straight line), product_count and, if roads=True, road_km / eta_min."""
    key = f"nearby:{round(lat, 3)}:{round(lng, 3)}:{radius_km}:{limit}:{int(roads)}"
    hit = cache.get(key)
    if hit is not None:
        return hit

    try:
        found, engine = _nearby_postgis(lat, lng, radius_km, limit), "postgis"
    except Exception:
        found, engine = _nearby_python(lat, lng, radius_km, limit), "haversine"

    by_id = {f["id"]: f for f in db.farms()}
    counts = db.product_counts()
    farms = []
    for fid, km in found.items():
        f = by_id.get(fid)
        if f and counts.get(fid, 0) > 0:
            farms.append({"id": fid, "name": f["name"], "area": f["area"], "lat": f["lat"], "lng": f["lng"],
                          "pincode": f.get("pincode", ""), "km": round(km, 1), "product_count": counts.get(fid, 0),
                          "search": engine})
    if roads and farms:
        add_road_info(lat, lng, farms)
    cache.set(key, farms, ttl=600)
    return farms


# ---------------------------------------------------------------- OSRM
def _estimate(km):
    road_km = km * ROAD_FACTOR
    return round(road_km, 1), max(1, round(road_km / AVG_SPEED_KMH * 60))


def add_road_info(lat, lng, farms):
    """Add road_km, eta_min and routing ('osrm' or 'estimate') to each farm using one OSRM table call."""
    try:
        coords = f"{lng},{lat};" + ";".join(f"{f['lng']},{f['lat']}" for f in farms)
        dest = ";".join(str(i) for i in range(1, len(farms) + 1))
        r = httpx.get(
            f"{config.OSRM_URL}/table/v1/driving/{coords}",
            params={"sources": "0", "destinations": dest, "annotations": "duration,distance"},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "Ok":
            raise ValueError(data.get("code"))
        for f, dur, dist in zip(farms, data["durations"][0], data["distances"][0]):
            if dur is None or dist is None:  # not reachable by road
                f["road_km"], f["eta_min"] = _estimate(f["km"])
                f["routing"] = "estimate"
            else:
                f["road_km"], f["eta_min"], f["routing"] = round(dist / 1000, 1), max(1, round(dur / 60)), "osrm"
    except Exception as e:
        log.warning("OSRM unavailable (%s). Using estimates.", e.__class__.__name__)
        for f in farms:
            f["road_km"], f["eta_min"] = _estimate(f["km"])
            f["routing"] = "estimate"


def route(lat1, lng1, lat2, lng2):
    """Driving route between two points, with a GeoJSON line the page can draw."""
    key = f"route:{round(lat1, 4)}:{round(lng1, 4)}:{round(lat2, 4)}:{round(lng2, 4)}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        r = httpx.get(
            f"{config.OSRM_URL}/route/v1/driving/{lng1},{lat1};{lng2},{lat2}",
            params={"overview": "full", "geometries": "geojson", "steps": "false"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise ValueError(data.get("code"))
        best = data["routes"][0]
        result = {"distance_km": round(best["distance"] / 1000, 1), "duration_min": max(1, round(best["duration"] / 60)),
                  "geometry": best["geometry"], "routing": "osrm"}
    except Exception as e:
        log.warning("OSRM route unavailable (%s). Using estimate.", e.__class__.__name__)
        km = haversine_km(lat1, lng1, lat2, lng2)
        road_km, eta = _estimate(km)
        result = {"distance_km": road_km, "duration_min": eta, "routing": "estimate",
                  "geometry": {"type": "LineString", "coordinates": [[lng1, lat1], [lng2, lat2]]}}
    cache.set(key, result, ttl=3600)
    return result

