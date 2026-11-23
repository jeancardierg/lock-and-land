"""Phase 2: state machine, migration (on a copy of the live database when one is present), pipeline run with
fake models, review gateway API, IMAP parsing. Nothing here may send or submit."""
import json
import shutil
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from jobhunter import gmail, grounding, llm, master, pipeline, scoring
from jobhunter.config import load_config
from jobhunter.db import DB
from jobhunter.migrate import migrate
from jobhunter.states import ALLOWED, LEGACY, IllegalTransition

from .fakellm import Script, fake_embed
from .test_grounding import good_letter

CFG = load_config()
import os
LIVE = Path(os.environ.get("JH_LIVE_DB", "/tmp/claude-0/live.db"))  # staged copy of the live database

EXTRACTED = {"requirements_must": ["Microsoft Intune and Autopilot", "Okta SSO and MFA"], "ad_language": "en",
             "work_mode": "hybrid", "office_city": "Barcelona", "role_family": "endpoint",
             "required_languages": ["English"]}


def job(**kw):
    base = dict(source="x", external_id="1", title="Endpoint Engineer", company="Acme", location="Barcelona",
                remote=False, url="https://boards.greenhouse.io/acme/jobs/1",
                description="We need Microsoft Intune and Autopilot, Okta SSO and MFA. English. Hybrid Barcelona.",
                posted_at="")
    base.update(kw)
    return base


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    sc = Script()
    monkeypatch.setattr(llm, "structured", sc.structured)
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr(llm, "missing_models", lambda c: [])
    monkeypatch.setattr(scoring.llm, "ollama_json", lambda c, s, p, **kw: {"score": 85, "reasons": ["Intune match"]})
    monkeypatch.setattr("jobhunter.vectors.DATA", tmp_path, raising=False)
    monkeypatch.setattr(pipeline, "LOCK", tmp_path / "run.lock")
    return sc


def cfg_for_tests(tmp_path):
    c = json.loads(json.dumps(CFG))
    c["enrich"] = {"enabled": False}
    c["recruiters"]["enabled"] = False
    c["grounding"] = {"words": [0, 999], "entailment": False}
    return c


# ---------- state machine ----------

def test_every_state_has_a_legacy_status():
    assert set(ALLOWED) == set(LEGACY)
    assert ALLOWED["submitted"] == set()  # terminal: nothing can move a sent application


def test_transitions_are_guarded_and_logged(db):
    db.upsert_job(job())
    jid = db.jobs()[0]["id"]
    with pytest.raises(IllegalTransition):
        db.move(jid, "approved")  # queued -> approved skips review
    db.move(jid, "extracted")
    db.move(jid, "scored")
    db.move(jid, "generated")
    db.move(jid, "needs_review")
    assert db.try_move(jid, "approved", actor="jean")
    assert not db.try_move(jid, "approved", actor="jean")  # double click: second one is a no-op
    db.move(jid, "submitting")
    db.move(jid, "awaiting_click")
    db.move(jid, "submitted", actor="jean")
    with pytest.raises(IllegalTransition):
        db.move(jid, "approved")
    ev = [(e["from_state"], e["to_state"]) for e in db.events(jid)]
    assert ("needs_review", "approved") in ev and ev.count(("needs_review", "approved")) == 1
    assert db.job(jid)["status"] == "applied"  # Streamlit tracker still works


# ---------- migration on a copy of the live database ----------

@pytest.mark.skipif(not LIVE.exists(), reason="no staged copy of the live database")
def test_migrate_live_copy(tmp_path):
    p = tmp_path / "jobhunter.db"
    shutil.copy(LIVE, p)
    before = sqlite3.connect(p)
    n_jobs = before.execute("SELECT count(*) FROM jobs").fetchone()[0]
    statuses = dict(before.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
    n_msgs = before.execute("SELECT count(*) FROM recruiter_msgs").fetchone()[0]
    before.close()
    out = migrate(p)
    assert Path(out["backup"]).exists()
    c = sqlite3.connect(p)
    assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == n_jobs
    assert c.execute("SELECT count(*) FROM recruiter_msgs").fetchone()[0] == n_msgs
    assert c.execute("SELECT count(*) FROM jobs WHERE state IS NULL").fetchone()[0] == 0
    states = dict(c.execute("SELECT state, count(*) FROM jobs GROUP BY state").fetchall())
    # old v0.2 drafts are sent back for grounded regeneration, not shown as reviewed
    assert states.get("needs_review", 0) == 0
    assert states.get("submitted", 0) == sum(statuses.get(s, 0) for s in ("applied", "interview", "offer", "rejected",
                                                                         "withdrawn"))
    assert c.execute("SELECT count(*) FROM job_events").fetchone()[0] == n_jobs
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    # LinkedIn rows repaired: the title is no longer copied into the company
    assert c.execute("SELECT count(*) FROM jobs WHERE source='linkedin' AND company=title").fetchone()[0] == 0
    c.close()
    again = migrate(p)  # idempotent
    assert again["from"] == 2 and "backup" not in again


# ---------- one pipeline run with fake models ----------

def test_pipeline_run_reaches_review_and_sends_nothing(db, fake, tmp_path, monkeypatch):
    sc = fake
    sc.push("extractor", EXTRACTED)
    sc.push("extractor", {**EXTRACTED, "role_family": "other", "requirements_must": ["cooking"]})
    sc.push("writer", good_letter)
    cfg = cfg_for_tests(tmp_path)
    monkeypatch.setattr(pipeline, "fetch_all",
                        lambda c: iter([job(), job(external_id="2", title="IT Admin", company="Beta",
                                               description="Systems IT admin, cooking skills")]))
    sent = mock.Mock()
    monkeypatch.setattr(gmail, "send", sent)
    out = pipeline.run(cfg, db)
    assert out["new_jobs"] == 2, out
    states = {j["company"]: j["state"] for j in db.jobs()}
    assert states["Acme"] == "needs_review", (out, states)
    assert states["Beta"] == "scored"  # low coverage: no letter
    a = db.application(next(j["id"] for j in db.jobs() if j["company"] == "Acme"))
    v = json.loads(a["validation_json"])
    assert v["hard"] == [] and a["letter_text"].startswith("Dear Hiring Team,")
    sent.assert_not_called()


# ---------- review gateway ----------

@pytest.fixture
def client(db, fake, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from jobhunter.review import app as rv
    monkeypatch.setattr(rv, "_db", lambda: db)
    cfg = cfg_for_tests(tmp_path)
    monkeypatch.setattr(rv, "load_config", lambda: cfg)
    return TestClient(rv.app), rv


def ready(db, fake, tmp_path, **kw):
    fake.push("extractor", EXTRACTED)
    fake.push("writer", good_letter)
    db.upsert_job(job(**kw))
    jid = max(j["id"] for j in db.jobs())
    assert pipeline.draft_job(db, cfg_for_tests(tmp_path), None, jid)
    return jid


def test_review_queue_and_reject(client, db, fake, tmp_path):
    c, _ = client
    jid = ready(db, fake, tmp_path)
    q = c.get("/api/queue").json()
    assert [x["id"] for x in q["review"]] == [jid]
    card = q["review"][0]
    assert card["route"] == "web" and any(s["facts"] for s in card["sentences"])
    assert c.post(f"/api/jobs/{jid}/reject", json={"reason": "location"}).json()["done"].startswith("Rejected")
    assert db.job(jid)["state"] == "rejected"
    assert c.post(f"/api/jobs/{jid}/reject", json={}).status_code == 409


def test_email_approval_needs_second_confirmation(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    jid = ready(db, fake, tmp_path, apply_email="jobs@acme.example")
    monkeypatch.setattr(rv, "attachment_problem", lambda p: None)
    sent = mock.Mock()
    monkeypatch.setattr(gmail, "send", sent)
    r = c.post(f"/api/jobs/{jid}/approve", json={}).json()
    assert "jobs@acme.example" in r["confirm"] and r["confirm_email"] is True
    sent.assert_not_called()
    assert db.job(jid)["state"] == "needs_review"
    r = c.post(f"/api/jobs/{jid}/approve", json={"confirm_email": True}).json()
    assert r["done"] == "Sent by email."
    sent.assert_called_once()
    assert sent.call_args.args[0] == "jobs@acme.example"
    assert db.job(jid)["state"] == "submitted"


def test_email_send_failure_rolls_back(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    jid = ready(db, fake, tmp_path, apply_email="jobs@acme.example")
    monkeypatch.setattr(rv, "attachment_problem", lambda p: None)
    monkeypatch.setattr(gmail, "send", mock.Mock(side_effect=RuntimeError("smtp down")))
    r = c.post(f"/api/jobs/{jid}/approve", json={"confirm_email": True})
    assert r.status_code == 502 and db.job(jid)["state"] == "approved"


def test_web_approval_is_a_handoff(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    jid = ready(db, fake, tmp_path)
    r = c.post(f"/api/jobs/{jid}/approve", json={}).json()
    assert r["open"] and r["copy"].startswith(db.application(jid)["letter_text"][:40])
    assert "own browser" in r["done"] and "I applied" in r["done"]
    assert db.job(jid)["state"] == "approved"
    assert c.get(f"/api/jobs/{jid}/handoff").json()["copy"] == r["copy"]
    assert [x["id"] for x in c.get("/api/queue").json()["waiting"]] == [jid]
    assert c.post(f"/api/jobs/{jid}/submitted").json()["done"].startswith("Marked as applied")
    assert db.job(jid)["state"] == "submitted"


def test_linkedin_is_a_handoff_too(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    jid = ready(db, fake, tmp_path, source="linkedin", url="https://www.linkedin.com/jobs/view/1")
    r = c.post(f"/api/jobs/{jid}/approve", json={}).json()
    assert r["open"].startswith("https://www.linkedin.com") and "own browser" in r["done"]


def test_hand_edit_is_revalidated(client, db, fake, tmp_path):
    c, _ = client
    jid = ready(db, fake, tmp_path)
    bad = db.application(jid)["letter_text"] + "\nI designed the company's ServiceNow platform."
    v = c.post(f"/api/jobs/{jid}/edit", json={"letter": bad}).json()["validation"]
    assert any("ServiceNow" in h for h in v["hard"]), v
    r = c.post(f"/api/jobs/{jid}/approve", json={}).json()
    assert "Approve anyway" in r["confirm"] and db.job(jid)["state"] == "needs_review"


def test_answer_saved_to_bank(client, db, fake, tmp_path, monkeypatch):
    c, _ = client
    jid = ready(db, fake, tmp_path)
    saved = {}
    monkeypatch.setattr(master, "save_qa", lambda k, a, q: saved.update({k: a}))
    r = c.post(f"/api/jobs/{jid}/answer", json={"question": "What is your notice period?", "answer": "1 month",
                                                "save": True}).json()
    assert any(a["answer"] == "1 month" and a["source"] == "jean" for a in r["answers"])
    assert saved == {"what_is_your_notice_period": "1 month"}


# ---------- IMAP parsing ----------

def test_imap_meta_from_raw():
    from jobhunter.mailimap import meta_from_raw
    raw = (b"From: =?utf-8?q?Ana_L=C3=B3pez?= <ana@recruit.example>\r\nSubject: Oportunidad IT\r\n"
           b"Message-ID: <abc@x>\r\nDate: Fri, 20 Nov 2026 10:00:00 +0100\r\nContent-Type: text/plain; charset=utf-8\r\n"
           b"\r\nHola Alex")
    m = meta_from_raw(raw, "123", "456")
    assert m["sender_name"] == "Ana López" and m["sender_email"] == "ana@recruit.example"
    assert m["subject"] == "Oportunidad IT" and "Hola Alex" in m["plain"] and m["thread_id"] == "456"


def test_interrupted_generation_is_requeued(db):
    db.upsert_job(job())
    jid = db.jobs()[0]["id"]
    db.move(jid, "extracted"); db.move(jid, "scored"); db.move(jid, "generated")
    assert pipeline.recover_interrupted(db) == 0  # fresh: may still be running
    db.conn.execute("UPDATE jobs SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (jid,))
    db.conn.commit()
    assert pipeline.recover_interrupted(db) == 1
    assert db.job(jid)["state"] == "scored"


def test_recheck_sends_overclaims_back(db, fake, tmp_path, monkeypatch):
    from jobhunter import __main__ as cli
    jid = ready(db, fake, tmp_path)
    app = db.application(jid)
    bad = json.loads(app["letter_json"])
    bad["p1"][1]["text"] = "I led the Microsoft Intune and Windows Autopilot rollout for ~90 Windows devices."
    text = app["letter_text"].replace("I led delivery of the", "I led the")
    db.update_application(app["id"], letter_json=bad, letter_text=text)
    monkeypatch.setattr("jobhunter.db.DB", lambda *a, **k: db)
    cli.recheck(cfg_for_tests(tmp_path))
    assert db.job(jid)["state"] == "scored"
    assert "overstates" in db.events(jid)[-1]["reason"]


def test_redraft_without_workers_runs_in_background(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    jid = ready(db, fake, tmp_path)
    monkeypatch.setattr(rv, "workers_alive", lambda: False)
    started = []
    monkeypatch.setattr("threading.Thread", lambda target, daemon: type("T", (), {"start": lambda s: started.append(1)})())
    r = c.post(f"/api/jobs/{jid}/redraft").json()
    assert "Redrafting now" in r["done"] and started == [1]
    assert "workers_alive" in c.get("/api/queue").json()
