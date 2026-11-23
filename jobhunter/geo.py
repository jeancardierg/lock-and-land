"""Free place search and maps for the Job criteria page. No keys, no accounts, no billing.

- Place search and "what's here?": Photon (photon.komoot.io, OpenStreetMap data), made for search-as-you-type.
- Towns within a radius of home: the Overpass API (OpenStreetMap).
- Map tiles: OpenStreetMap's tile server, fetched through this app so each tile is downloaded once and kept.

Every answer is cached in the data folder (cache/geo.json and cache/tiles/), requests are spaced out, and they
carry an app User-Agent, as those services' usage policies ask. Offline, cached answers still work and anything
else fails with a clear message; typing a place by hand always works."""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import requests

from .branding import APP_NAME

UA = f"{APP_NAME.replace(' & ', 'And')}/0.4 (desktop job-search app; https://github.com/jeancardierg/lock-and-land)"
PHOTON = "https://photon.komoot.io"
OVERPASS = "https://overpass-api.de/api/interpreter"
OVERPASS_URLS = [OVERPASS, OVERPASS, "https://overpass.kumi.systems/api/interpreter"]
TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DAY = 86400
PLACE_TYPES = {"city", "town", "village", "municipality", "suburb", "borough", "hamlet", "county", "state",
               "region", "province", "country", "district", "locality"}


class GeoError(RuntimeError):
    pass


_lock = threading.Lock()
_last: dict[str, float] = {}
_mem: dict | None = None


def _home() -> Path:
    from .config import HOME
    return HOME


def _cache_file() -> Path:
    return _home() / "cache" / "geo.json"


def _cache() -> dict:
    global _mem
    if _mem is None:
        try:
            _mem = json.loads(_cache_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _mem = {}
    return _mem


def _cached(key: str, max_age: int, fetch):
    c = _cache()
    hit = c.get(key)
    if hit and time.time() - hit["t"] < max_age:
        return hit["v"]
    try:
        v = fetch()
    except (requests.RequestException, ValueError) as e:
        if hit:  # offline or the service is busy: an older answer beats none
            return hit["v"]
        raise GeoError("The free map service didn't answer (busy, or no internet). Try again in a minute, or type "
                       "places by hand") from e
    with _lock:
        c[key] = {"t": time.time(), "v": v}
        if len(c) > 3000:  # keep the file small: drop the oldest
            for k in sorted(c, key=lambda k: c[k]["t"])[:500]:
                c.pop(k, None)
        try:
            _cache_file().parent.mkdir(parents=True, exist_ok=True)
            _cache_file().write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return v


def _get(url: str, service: str, gap: float, **kw) -> requests.Response:
    with _lock:  # one request at a time per service, spaced out
        wait = _last.get(service, 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[service] = time.time()
    kw.setdefault("timeout", 20)
    r = requests.request(kw.pop("method", "GET"), url, headers={"User-Agent": UA}, **kw)
    r.raise_for_status()
    return r


def _place(f: dict) -> dict | None:
    p = f.get("properties") or {}
    lon, lat = (f.get("geometry") or {}).get("coordinates") or [None, None]
    name = p.get("name") or p.get("city")
    if not name or lat is None:
        return None
    kind = p.get("osm_value") or p.get("type") or ""
    parts = [name] + [x for x in (p.get("county") if kind in ("village", "hamlet", "suburb") else None,
                                  p.get("state"), p.get("country")) if x and x != name]
    return {"name": name, "label": ", ".join(dict.fromkeys(parts)), "country": p.get("country") or "",
            "kind": kind, "lat": round(lat, 5), "lon": round(lon, 5)}


def search(q: str, limit: int = 8) -> list[dict]:
    """Places (cities, towns, regions, countries) matching what you typed."""
    q = " ".join((q or "").split())[:80]
    if len(q) < 2:
        return []

    def fetch():
        r = _get(f"{PHOTON}/api/", "photon", 0.35, params={"q": q, "limit": 15, "osm_tag": "place", "lang": "en"})
        out, seen = [], set()
        for f in r.json().get("features") or []:
            pl = _place(f)
            if pl and pl["kind"] in PLACE_TYPES and pl["label"] not in seen:
                seen.add(pl["label"])
                out.append(pl)
        return out[:limit]
    return _cached(f"s:{q.lower()}", 30 * DAY, fetch)


def reverse(lat: float, lon: float) -> dict | None:
    """The town at a point on the map."""
    lat, lon = round(float(lat), 3), round(float(lon), 3)

    def fetch():
        r = _get(f"{PHOTON}/reverse", "photon", 0.35,
                 params={"lat": lat, "lon": lon, "limit": 1, "lang": "en", "osm_tag": "place"})
        fs = r.json().get("features") or []
        if not fs:
            r = _get(f"{PHOTON}/reverse", "photon", 0.35, params={"lat": lat, "lon": lon, "limit": 1, "lang": "en"})
            fs = r.json().get("features") or []
        if not fs:
            return None
        p = fs[0].get("properties") or {}
        name = p.get("city") or p.get("town") or p.get("village") or p.get("name")
        return {"name": name, "label": ", ".join(x for x in (name, p.get("state"), p.get("country")) if x),
                "country": p.get("country") or "", "lat": lat, "lon": lon} if name else None
    return _cached(f"r:{lat},{lon}", 90 * DAY, fetch)


def _km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def towns(lat: float, lon: float, km: float) -> list[dict]:
    """Cities and towns (and villages, for small areas) within km of a point, nearest first."""
    lat, lon, km = round(float(lat), 3), round(float(lon), 3), max(1.0, min(float(km), 150.0))
    kinds = "city|town|village" if km <= 25 else "city|town"
    query = (f'[out:json][timeout:25];node["place"~"^({kinds})$"]["name"](around:{int(km * 1000)},{lat},{lon});'
             "out body 400;")

    def fetch():
        r = None
        for i, url in enumerate(OVERPASS_URLS):  # Overpass is often busy (429/502/504): retry, then a mirror
            try:
                r = _get(url, "overpass", 1.0 + 2 * i, method="POST", data={"data": query}, timeout=40)
                break
            except requests.RequestException:
                if i == len(OVERPASS_URLS) - 1:
                    raise
        out = []
        for el in r.json().get("elements") or []:
            t = el.get("tags") or {}
            name = t.get("name")
            if not name or "lat" not in el:
                continue
            try:
                pop = int(str(t.get("population") or "0").replace(".", "").replace(",", "").split()[0])
            except ValueError:
                pop = 0
            alt = [t[k] for k in ALT_KEYS if t.get(k)]
            alt = [x.strip() for a in alt for x in a.split(";") if x.strip() and x.strip() != name]
            out.append({"name": name, "kind": t.get("place"), "population": pop, "alt": list(dict.fromkeys(alt)),
                        "km": round(_km(lat, lon, el["lat"], el["lon"]), 1)})
        out.sort(key=lambda x: x["km"])
        return out[:300]
    found = _cached(f"t:{lat},{lon},{int(km)}", 90 * DAY, fetch)
    _remember_aliases(found)
    return found


ALT_KEYS = ("name:es", "name:ca", "name:eu", "name:gl", "name:en", "short_name", "alt_name", "official_name",
            "old_name")


def _aliases_file() -> Path:
    return _home() / "cache" / "town_aliases.json"


def aliases() -> dict[str, list[str]]:
    """Other names OpenStreetMap gave for towns found on the map (folded name -> names). No network."""
    try:
        return json.loads(_aliases_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember_aliases(found: list[dict]) -> None:
    from .places import fold
    have = aliases()
    new = {fold(t["name"]): t["alt"] for t in found if t.get("alt") and have.get(fold(t["name"])) != t["alt"]}
    if not new:
        return
    have.update(new)
    try:
        _aliases_file().parent.mkdir(parents=True, exist_ok=True)
        _aliases_file().write_text(json.dumps(have, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def tile(z: int, x: int, y: int) -> bytes:
    """One OpenStreetMap tile, from the cache when we have it (kept 30 days)."""
    if not (0 <= z <= 17 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise GeoError("No such tile")
    f = _home() / "cache" / "tiles" / str(z) / str(x) / f"{y}.png"
    if f.exists() and time.time() - f.stat().st_mtime < 30 * DAY:
        return f.read_bytes()
    try:
        data = _get(TILES.format(z=z, x=x, y=y), "tiles", 0.0, timeout=15).content
    except requests.RequestException as e:
        if f.exists():
            return f.read_bytes()
        raise GeoError("Map tiles need internet") from e
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    except OSError:
        pass
    return data
