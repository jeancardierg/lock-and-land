"""Notification settings: per alert and channel, instant / hourly / daily / off, quiet hours, no repeats."""
from datetime import datetime

import pytest

from jobhunter import connections as C
from jobhunter import notify


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "_deliver", lambda cfg, ch, t, b, path="/": out.append((ch, t, b)) or True)
    from jobhunter.config import DATA
    for f in (notify.QUEUE, notify.STATE):
        (DATA / f).unlink(missing_ok=True)
    yield out
    for f in (notify.QUEUE, notify.STATE):
        (DATA / f).unlink(missing_ok=True)


def cfg(**n):
    return {"notify": n, "thresholds": {"draft_score": 70}}


def test_defaults_and_bad_values():
    p = notify.prefs(cfg(events={"waiting": {"desktop": False, "telegram": "weekly"}}))
    assert p["events"]["strong_jobs"] == {"label": "Strong new job matches", "desktop": "instant", "telegram": "instant"}
    assert p["events"]["waiting"]["desktop"] == "off" and p["events"]["waiting"]["telegram"] == "instant"
    assert p["strong_score"] == 70 and p["digest_time"] == "09:00" and not p["quiet_hours"]["enabled"]


def test_quiet_hours_wrap_midnight():
    p = notify.prefs(cfg(quiet_hours={"enabled": True, "start": "22:00", "end": "08:00"}))
    assert notify.in_quiet(p, datetime(2026, 11, 23, 23, 30)) and notify.in_quiet(p, datetime(2026, 11, 23, 7, 59))
    assert not notify.in_quiet(p, datetime(2026, 11, 23, 8, 0)) and not notify.in_quiet(p, datetime(2026, 11, 23, 15))


def test_modes_per_channel(sent, monkeypatch):
    c = cfg(events={"waiting": {"desktop": "off", "telegram": "instant"},
                    "strong_jobs": {"desktop": "hourly", "telegram": "daily"}}, digest_time="09:00")
    notify.send(c, "Lock & Land: your turn", "1 waiting", event="waiting")
    assert sent == [("telegram", "Lock & Land: your turn", "1 waiting")]
    notify.send(c, "Lock & Land: 2 strong matches", "a", event="strong_jobs")
    notify.send(c, "Lock & Land: 1 strong match", "b", event="strong_jobs")
    assert len(sent) == 1  # both queued
    r = notify.flush(c, now=datetime(2026, 11, 23, 8, 5))  # hourly due (first time), daily not yet
    assert r["sent"] == {"desktop": 2} and sent[-1][0] == "desktop" and "2 updates" in sent[-1][1]
    assert notify.flush(c, now=datetime(2026, 11, 23, 8, 30))["sent"] == {}
    r = notify.flush(c, now=datetime(2026, 11, 23, 9, 1), summary=lambda: "3 new jobs")
    assert r["sent"] == {"telegram": 3}  # two strong-match alerts + daily summary in one digest
    assert "Daily summary: 3 new jobs" in sent[-1][2]
    assert notify.flush(c, now=datetime(2026, 11, 23, 9, 6), summary=lambda: "x")["sent"] == {}  # once a day


def test_quiet_hours_hold_then_release(sent):
    c = cfg(quiet_hours={"enabled": True, "start": "00:00", "end": "23:59"})
    notify.send(c, "Lock & Land: database problem", "check", event="problems")
    assert sent == []
    assert notify.flush(c, now=datetime(2026, 11, 23, 12))["quiet"]
    r = notify.flush(c, now=datetime(2026, 11, 23, 23, 59, 30))
    assert r["sent"] == {"desktop": 1, "telegram": 1}


def test_save_settings_api(sent):
    from fastapi.testclient import TestClient
    from jobhunter.review.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8502")
    h = {"X-Jobhunter": "1"}
    body = {"events": {"recruiter": {"desktop": "hourly", "telegram": "off"}}, "strong_score": 85,
            "digest_time": "07:30", "quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00"}}
    r = c.post("/api/connections/notify", json=body, headers=h)
    assert r.status_code == 200
    n = c.get("/api/connections").json()["notify"]
    assert n["events"]["recruiter"] == {"label": "New recruiter messages", "desktop": "hourly", "telegram": "off"}
    assert n["strong_score"] == 85 and n["digest_time"] == "07:30" and n["quiet_hours"]["enabled"]
    assert c.post("/api/connections/notify", json={"events": {"nope": {}}}, headers=h).status_code == 400
    assert c.post("/api/connections/notify", json={"events": {"daily_summary": {"desktop": "hourly"}}},
                  headers=h).status_code == 400
    assert c.post("/api/connections/notify", json={"digest_time": "9am"}, headers=h).status_code == 400
    assert c.post("/api/connections/notify", json=body).status_code == 403
    C._edit_config(lambda raw: raw.get("notify", {}).pop("events", None) or raw["notify"].pop("quiet_hours", None))


def test_tick_announces_once_and_not_on_first_run(sent, monkeypatch):
    from jobhunter import tasks
    from jobhunter.db import DB, now
    import tempfile, os
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    monkeypatch.setattr(tasks, "_ctx", lambda: (cfg(strong_score=80), db))
    db.upsert_job({"source": "x", "external_id": "1", "title": "Endpoint Engineer", "company": "Acme"})
    db.update_job(1, score=90)
    tasks.notify_tick.call_local()  # first run: remembers what exists, says nothing (the daily summary may go)
    assert not [s for s in sent if s[1] != "Daily summary"]
    db.upsert_job({"source": "x", "external_id": "2", "title": "Sysadmin", "company": "Beta"})
    db.update_job(2, score=85)
    db.upsert_job({"source": "x", "external_id": "3", "title": "Weak", "company": "C"})
    db.update_job(3, score=40)
    tasks.notify_tick.call_local()
    strong = [s for s in sent if "strong match" in s[1]]
    assert len(strong) == 2 and "85 · Sysadmin, Beta" in strong[0][2]  # desktop + telegram
    tasks.notify_tick.call_local()
    assert len([s for s in sent if "strong match" in s[1]]) == 2  # not repeated
