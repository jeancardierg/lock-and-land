"""Free maps and place search for Job criteria: answers are cached, offline falls back to the cache, and saving a
map pick keeps the home point and radius."""
import shutil
from pathlib import Path

import pytest
import requests
import yaml

from jobhunter import criteria, geo

ROOT = Path(__file__).resolve().parent.parent


class Resp:
    def __init__(self, data=None, content=b""):
        self.data, self.content = data, content

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_home", lambda: tmp_path)
    monkeypatch.setattr(geo, "_mem", None)
    monkeypatch.setattr(geo, "_last", {})
    return tmp_path


def test_search_is_cached_and_works_offline_from_cache(home, monkeypatch):
    calls = []

    def fake(method, url, headers=None, **kw):
        calls.append((url, kw.get("params"), headers))
        return Resp({"features": [
            {"geometry": {"coordinates": [2.1686, 41.3874]},
             "properties": {"name": "Barcelona", "osm_value": "city", "state": "Catalonia", "country": "Spain"}},
            {"geometry": {"coordinates": [0, 0]}, "properties": {"name": "Barcelona Street", "osm_value": "street"}}]})
    monkeypatch.setattr(requests, "request", fake)
    r = geo.search("Barcel")
    assert r == [{"name": "Barcelona", "label": "Barcelona, Catalonia, Spain", "country": "Spain", "kind": "city",
                  "lat": 41.3874, "lon": 2.1686}]
    assert "photon.komoot.io" in calls[0][0] and "LockAndLand" in calls[0][2]["User-Agent"]
    monkeypatch.setattr(geo, "_mem", None)  # a fresh start reads the cache file

    def offline(*a, **k):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(requests, "request", offline)
    assert geo.search("barcel") == r and len(calls) == 1
    with pytest.raises(geo.GeoError):
        geo.search("Madrid")


def test_towns_nearest_first(home, monkeypatch):
    def fake(method, url, headers=None, **kw):
        assert method == "POST" and "around:20000,41.387,2.169" in kw["data"]["data"]
        return Resp({"elements": [
            {"lat": 41.54, "lon": 2.10, "tags": {"name": "Sabadell", "place": "city", "population": "216.520"}},
            {"lat": 41.36, "lon": 2.10, "tags": {"name": "L'Hospitalet de Llobregat", "place": "city"}},
            {"lat": 41.4, "lon": 2.2, "tags": {"place": "town"}}]})
    monkeypatch.setattr(requests, "request", fake)
    t = geo.towns(41.3874, 2.1686, 20)
    assert [x["name"] for x in t] == ["L'Hospitalet de Llobregat", "Sabadell"] and t[1]["population"] == 216520


def test_towns_retries_when_overpass_is_busy(home, monkeypatch):
    tries = []

    def flaky(method, url, headers=None, **kw):
        tries.append(url)
        if len(tries) < 3:
            raise requests.HTTPError("502 Bad Gateway")
        return Resp({"elements": [{"lat": 41.45, "lon": 2.24, "tags": {"name": "Badalona", "place": "city"}}]})
    monkeypatch.setattr(requests, "request", flaky)
    monkeypatch.setattr(geo.time, "sleep", lambda s: None)
    assert [t["name"] for t in geo.towns(41.387, 2.169, 15)] == ["Badalona"] and "kumi" in tries[2]


def test_tiles_cached_and_bounded(home, monkeypatch):
    n = []
    monkeypatch.setattr(requests, "request", lambda *a, **k: n.append(1) or Resp(content=b"PNG"))
    assert geo.tile(5, 15, 12) == b"PNG" and geo.tile(5, 15, 12) == b"PNG" and len(n) == 1
    with pytest.raises(geo.GeoError):
        geo.tile(5, 40, 12)


def test_routes_and_static(monkeypatch):
    from fastapi.testclient import TestClient
    from jobhunter.review.app import app
    c = TestClient(app, base_url="http://localhost")
    assert c.get("/static/leaflet/leaflet.js").status_code == 200
    assert c.get("/static/%2e%2e/connections.py").status_code == 404
    monkeypatch.setattr(geo, "search", lambda q: [{"name": "Badalona"}])
    assert c.get("/api/geo/search?q=bada").json() == [{"name": "Badalona"}]

    def down(*a):
        raise geo.GeoError("Couldn't reach the free map service")
    monkeypatch.setattr(geo, "towns", down)
    assert c.get("/api/geo/towns?lat=41&lon=2&km=10").status_code == 503


def test_saving_a_map_pick(tmp_path, monkeypatch):
    shutil.copy2(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    monkeypatch.setattr(criteria, "HOME", tmp_path)
    criteria.apply({"roles": "Materials engineer", "home_city": "Sabadell", "commute_towns": "Terrassa, Barcelona",
                    "home_geo": {"lat": 41.54321, "lon": 2.1, "km": 500}}, tmp_path / "config.yaml")
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert raw["rules"]["home_geo"] == {"lat": 41.5432, "lon": 2.1, "km": 150}
    assert raw["rules"]["home_cities"] == ["sabadell", "terrassa", "barcelona"]
    v = criteria.from_config(raw)
    assert v["home_geo"]["km"] == 150 and "Spanish" in v["language_options"] and "EU" in v["region_options"]
