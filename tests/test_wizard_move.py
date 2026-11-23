"""Setup wizard (criteria) and in-app data-folder change."""
import json
from pathlib import Path

import pytest
import yaml

from jobhunter import criteria
from jobhunter.desktop import move


def test_criteria_roundtrip(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"profile": {"name": "X", "location": "Valencia, Spain"},
                                   "search": {"queries": ["nurse"]}}), encoding="utf-8")
    pre = criteria.from_config(yaml.safe_load(cfg.read_text()))
    assert pre["roles"] == ["nurse"] and pre["home_city"] == "Valencia" and not pre["completed"]
    res = criteria.apply({"roles": "Registered nurse\nICU nurse", "title_words": "nurse, enfermer",
                          "skills": "ICU, triage", "home_city": "Valencia", "commute_towns": "Sagunto",
                          "work_mode": "any", "remote_regions": "Spain, EU", "min_salary": "30000",
                          "languages": "Spanish, English", "max_age_days": "10", "seniority": ["mid", "boss"]}, cfg)
    assert res["ok"] and Path(res["backup"]).exists()
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert raw["profile"]["name"] == "X"  # untouched keys survive
    assert raw["search"]["queries"] == ["Registered nurse", "ICU nurse"]
    assert raw["search"]["title_any"] == ["nurse", "enfermer"]
    assert raw["rules"]["home_cities"] == ["valencia", "sagunto"]
    assert raw["preferences"]["work_mode"] == "any"
    assert raw["profile"]["min_salary_eur"] == 30000 and raw["preferences"]["onsite_min_salary_eur"] == 30000
    assert raw["targets"]["seniority"] == ["mid"]
    assert raw["sources"]["himalayas"]["country"] == "Spain"
    assert raw["sources"]["jooble"]["locations"] == ["Valencia", "Spain"]
    assert criteria.done(raw)


def test_criteria_needs_a_role(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("profile: {}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        criteria.apply({"roles": "  "}, cfg)


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "appcfg"
    monkeypatch.setattr(move, "cfg_dir", lambda: d)
    return d


def test_move_copies_and_switches(tmp_path, cfgdir):
    src, dst = tmp_path / "old", tmp_path / "new"
    (src / "data").mkdir(parents=True)
    (src / "config.yaml").write_text("a: 1", encoding="utf-8")
    (src / "data" / "jobhunter.db").write_bytes(b"x" * 1000)
    (src / "data" / "workers.heartbeat").write_text("1")
    move.request(src, dst, "move")
    res = move.run_pending()
    assert res["ok"], res
    assert (dst / "data" / "jobhunter.db").read_bytes() == b"x" * 1000
    assert not (dst / "data" / "workers.heartbeat").exists()
    assert (cfgdir / "home.txt").read_text() == str(dst.resolve())
    assert not src.exists() and Path(res["old_folder_kept_as"]).exists()
    assert move.run_pending() is None and move.last_result()["ok"]


def test_move_refuses_bad_targets(tmp_path, cfgdir):
    src = tmp_path / "old"
    (src / "data").mkdir(parents=True)
    (src / "config.yaml").write_text("a: 1")
    with pytest.raises(ValueError):
        move.request(src, src, "move")
    with pytest.raises(ValueError):
        move.request(src, src / "sub", "move")
    with pytest.raises(ValueError):
        move.request(src, tmp_path / "empty", "use")
    other = tmp_path / "other"
    (other / "data").mkdir(parents=True)
    (other / "data" / "jobhunter.db").write_bytes(b"1")
    with pytest.raises(ValueError):
        move.request(src, other, "move")
    (other / "config.yaml").write_text("b: 2")
    move.request(src, other, "use")
    assert move.run_pending()["ok"] and src.exists()
    assert (cfgdir / "home.txt").read_text() == str(other.resolve())


def test_setup_and_home_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from jobhunter import connections as C
    from jobhunter.review.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8502")
    assert c.get("/setup").status_code == 200
    v = c.get("/api/setup").json()
    assert "roles" in v and "work_modes" in v
    assert c.post("/api/setup", json={"roles": ""}).status_code == 403  # no X-Jobhunter header
    seen = {}
    monkeypatch.setattr(C, "change_home", lambda p, m: seen.update(p=p, m=m) or {"ok": True})
    r = c.post("/api/connections/home", json={"path": str(tmp_path), "mode": "use"}, headers={"X-Jobhunter": "1"})
    assert r.status_code == 200 and seen == {"p": str(tmp_path), "m": "use"}
