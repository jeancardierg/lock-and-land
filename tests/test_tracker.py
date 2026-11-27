"""Tracker tab: every job you applied to, with a stage you change as things move."""
from __future__ import annotations

from pathlib import Path

from .test_phase2 import cfg_for_tests, client, db, fake, job, ready  # noqa: F401


def _applied(db, title: str, ext: str) -> int:  # noqa: F811
    db.upsert_job(job(external_id=ext, title=title))
    jid = max(j["id"] for j in db.jobs())
    db.move(jid, "filtered", reason="test")
    db.move(jid, "submitted", actor="jean", reason="test")
    return jid


def test_tracker_lists_applied_jobs_only(client, db, fake, tmp_path):  # noqa: F811
    c, _ = client
    open_ = ready(db, fake, tmp_path)          # letter ready, not applied
    a = _applied(db, "Service desk analyst", "2")
    b = _applied(db, "IT support engineer", "3")
    d = c.get("/api/tracker").json()
    ids = [j["id"] for j in d["jobs"]]
    assert set(ids) == {a, b} and open_ not in ids
    assert all(j["track"] == "applied" for j in d["jobs"])
    assert d["stages"] == ["applied", "interview", "offer", "rejected", "withdrawn"]
    assert d["counts"]["applied"] == 2 and d["counts"]["interview"] == 0
    assert all(len(j["changed"]) == 10 for j in d["jobs"])


def test_stage_change_saves_and_is_logged(client, db, fake, tmp_path):  # noqa: F811
    c, _ = client
    jid = _applied(db, "Service desk analyst", "2")
    today = db.submitted_today()
    r = c.post(f"/api/jobs/{jid}/stage", json={"stage": "interview"})
    assert r.status_code == 200 and r.json()["done"] == "Stage set to interview."
    j = db.job(jid)
    assert j["status"] == "interview" and j["state"] == "submitted"   # the pipeline still sees it as applied
    ev = db.events(jid)[-1]
    assert (ev["from_state"], ev["to_state"], ev["actor"]) == ("submitted", "submitted", "jean")
    assert ev["reason"] == "stage: applied -> interview"
    assert db.submitted_today() == today      # a stage change is not a new application (daily cap)
    assert c.get("/api/tracker").json()["counts"]["interview"] == 1
    n = len(db.events(jid))
    assert c.post(f"/api/jobs/{jid}/stage", json={"stage": "interview"}).json()["done"] == "No change."
    assert len(db.events(jid)) == n


def test_stage_refuses_unknown_stage_and_unapplied_jobs(client, db, fake, tmp_path):  # noqa: F811
    c, _ = client
    jid = _applied(db, "Service desk analyst", "2")
    assert c.post(f"/api/jobs/{jid}/stage", json={"stage": "hired!"}).status_code == 400
    assert c.post(f"/api/jobs/{jid}/stage", json={}).status_code == 400
    open_ = ready(db, fake, tmp_path)
    r = c.post(f"/api/jobs/{open_}/stage", json={"stage": "interview"})
    assert r.status_code == 409 and "I applied" in r.json()["detail"]
    assert db.job(open_)["state"] == "needs_review"
    assert c.post("/api/jobs/99999/stage", json={"stage": "offer"}).status_code == 404


def test_old_tracker_stages_show_up(client, db, fake, tmp_path):  # noqa: F811
    """Stages set in the old dashboard's Tracker are still in the database and appear as they were."""
    c, _ = client
    jid = _applied(db, "Service desk analyst", "2")
    db.conn.execute("UPDATE jobs SET status='offer' WHERE id=?", (jid,))
    db.conn.commit()
    row = next(j for j in c.get("/api/tracker").json()["jobs"] if j["id"] == jid)
    assert row["track"] == "offer"


def test_page_has_the_tracker_tab():
    html = (Path(__file__).resolve().parents[1] / "jobhunter" / "review" / "index.html").read_text(encoding="utf-8")
    assert "tab('tracker')" in html and "/api/tracker" in html and "/stage`" in html
