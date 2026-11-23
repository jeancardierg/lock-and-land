"""'database is locked' when saving a hand-edited letter (23 Nov 2026).

Cause: the recruiter mail poll (every 30 min) re-inserts messages it has already stored. The INSERT fails on the
UNIQUE gmail_id, and sqlite3 leaves that failed statement's transaction open, so the poll's thread kept the write
lock. Every other write (Save and recheck, Approve) then waited the 30 s busy timeout and failed."""
import json
from unittest import mock

from jobhunter import llm, pipeline, sqldb
from jobhunter.db import DB

from . import test_phase2 as t2


def _other_writer(path):
    c = sqldb.connect(path, timeout=0.2)
    c.execute("UPDATE jobs SET notes='other writer' WHERE 1=0")
    c.commit()
    c.close()


def _msg(gid="g1"):
    return {"channel": "gmail", "gmail_id": gid, "thread_id": "t", "sender_name": "R", "sender_email": "r@x.com",
            "subject": "Role", "body": "Hi", "link": None, "received_at": "2026-11-23T10:00:00+00:00"}


def test_duplicate_recruiter_mail_does_not_keep_the_write_lock(tmp_path):
    db = DB(tmp_path / "j.db")
    assert db.upsert_msg(_msg())
    assert not db.upsert_msg(_msg())      # the second poll sees the same message
    assert not db.conn.in_transaction
    _other_writer(tmp_path / "j.db")        # was: sqlite3.OperationalError: database is locked


def test_duplicate_job_id_does_not_keep_the_write_lock(tmp_path):
    db = DB(tmp_path / "j.db")
    assert db.upsert_job(t2.job(external_id="7"))
    assert not db.upsert_job(t2.job(external_id="7", title="Renamed role"))  # new dedup key, same source id
    assert not db.conn.in_transaction
    _other_writer(tmp_path / "j.db")


def test_failed_write_is_rolled_back(tmp_path):
    db = DB(tmp_path / "j.db")
    try:
        db.update_job(1, no_such_column=1)
    except sqldb.OperationalError:
        pass
    assert not db.conn.in_transaction


def test_shared_connection_closes_a_leaked_transaction(tmp_path):
    p = tmp_path / "j.db"
    db = DB.shared(p)
    db.conn.execute("UPDATE jobs SET notes='x' WHERE 1=0")  # some future code path forgets to commit
    assert db.conn.in_transaction
    assert DB.shared(p) is db and not db.conn.in_transaction
    _other_writer(p)


def test_embeddings_do_not_hold_the_vector_lock_between_chunks(tmp_path, monkeypatch):
    from jobhunter.vectors import Store
    from .fakellm import fake_embed
    path = tmp_path / "vectors.db"
    store = Store({"llm": {}}, path)
    calls = []

    def embed(cfg, texts):
        if calls:  # second chunk: the first one must already be committed
            c = sqldb.connect(path, timeout=0.2)
            c.execute("DELETE FROM vec WHERE 1=0")
            c.commit()
            c.close()
        calls.append(len(texts))
        return fake_embed(cfg, texts)

    monkeypatch.setattr(llm, "embed", embed)
    v = store.vectors([f"text {i}" for i in range(40)])
    assert calls == [32, 8] and v.shape[0] == 40
    assert not store.conn.in_transaction


def test_locked_save_says_nothing_was_saved(client, db, fake, tmp_path, monkeypatch):
    c, _ = client
    jid = t2.ready(db, fake, tmp_path)
    monkeypatch.setattr(db, "update_application", mock.Mock(side_effect=sqldb.OperationalError("database is locked")))
    r = c.post(f"/api/jobs/{jid}/edit", json={"letter": "Hello"})
    assert r.status_code == 503 and "not saved" in r.json()["detail"] and "Try again" in r.json()["detail"]


def test_deferred_check_never_overwrites_a_hand_edit(db, fake, tmp_path, monkeypatch):
    cfg = t2.cfg_for_tests(tmp_path)
    jid = t2.ready(db, fake, tmp_path)
    app = db.application(jid)
    v = json.loads(app["validation_json"])
    v["checker"] = "pending"
    db.update_application(app["id"], validation_json=v)
    mine = {"score": 42, "hard": [], "soft": ["the user's recheck"]}

    def check_later(cfg, prof, letter_json, v):  # the user saves an edit while the checker model is running
        db.update_application(app["id"], letter_text="edited", validation_json=mine, edited_by_jean=1)
        return dict(v, checker="done", score=99)

    monkeypatch.setattr(llm, "reachable", lambda c: True)
    monkeypatch.setattr(pipeline.grounding, "check_later", check_later)
    assert pipeline.check_pending(db, cfg) == 0
    assert json.loads(db.application(jid)["validation_json"]) == mine


# fixtures from test_phase2
client, fake = t2.client, t2.fake
db = t2.db
