"""Audit fixes: per-task sampling, context budgets, schema-constrained scores, pause between jobs, Ollama going
away mid-batch, jobs that keep failing, the cached disk-encryption check and the profile-based logistics line."""
from unittest import mock

import requests

from jobhunter import connections, grounding, llm, pipeline, scoring
from jobhunter.db import DB

NOENRICH = {"enrich": {"enabled": False}}


def test_task_options_and_override():
    cfg = {"llm": {"options": {"writer": {"temperature": 9, "num_predict": 50}}}}
    assert llm.options({}, "extractor")["repeat_penalty"] == 1.0
    assert llm.options(cfg, "writer")["num_predict"] == 50
    assert llm.options({}, "unknown-task")["num_predict"] == llm.TASK_OPTIONS["writer"]["num_predict"]


def test_chat_sends_task_options_and_split_timeout():
    seen = {}

    class R:
        status_code, text = 200, ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "{\"a\": 1}"}, "done_reason": "stop"}

    def post(url, json=None, timeout=None, **kw):
        seen.update(body=json, timeout=timeout)
        return R()

    with mock.patch.object(llm.requests, "post", post):
        assert llm.structured({}, "s", "p", {"type": "object"}, task="checker") == {"a": 1}
    o = seen["body"]["options"]
    assert o["temperature"] == 0.0 and o["repeat_penalty"] == 1.0 and o["num_predict"] == 800
    assert seen["timeout"][0] == 10.0


def test_char_budget_shrinks_with_context():
    small = {"llm": {"num_ctx": {"extractor": 4096}}}
    big = {"llm": {"num_ctx": {"extractor": 16384}}}
    assert llm.char_budget(small, "extractor", 700) < llm.char_budget(big, "extractor", 700)
    assert llm.char_budget({"llm": {"num_ctx": {"extractor": 512}}}, "extractor") == 2000  # floor


def test_score_prompt_fits_small_context_and_uses_schema():
    cfg = {"profile": {"languages": ["English"]}, "targets": {}, "llm": {"num_ctx": {"extractor": 4096}}}
    job = {"title": "Sysadmin", "company": "X", "description": "d" * 20000}
    prompt = scoring.build_prompt(job, "c" * 20000, cfg)
    assert len(prompt) < (4096 - 1500) * llm.CHARS_PER_TOKEN
    with mock.patch.object(scoring.llm, "ollama_json", return_value={"score": 50}) as m:
        scoring.score_job(job, "cv", cfg)
    assert m.call_args.kwargs["schema"]["properties"]["score"]["type"] == "integer"


def _queued(db, n=1):
    for i in range(n):
        db.upsert_job({"source": "t", "external_id": f"perf-{i}", "title": f"Systems engineer perf {i}",
                       "company": f"PerfCo {i}", "description": "Intune"})
    return [j["id"] for j in db.in_state("queued", order="id")]


def test_pause_stops_batch_between_jobs(tmp_path, monkeypatch):
    db = DB(tmp_path / "p.db")
    _queued(db, 2)
    monkeypatch.setattr(pipeline, "_paused", lambda: True)
    out = pipeline.prepare_batch(db, NOENRICH, limit=5)
    assert out["paused"] and out["prepared"] == 0


def test_ollama_down_stops_batch_and_does_not_count(tmp_path, monkeypatch):
    db = DB(tmp_path / "d.db")
    ids = _queued(db, 3)
    calls = []

    def boom(db_, cfg, jid):
        calls.append(jid)
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(pipeline, "prepare", boom)
    for _ in range(pipeline.MAX_FAILS + 1):
        out = pipeline.prepare_batch(db, NOENRICH, limit=5)
    assert out["ollama"] == "down" and len(calls) == pipeline.MAX_FAILS + 1  # one try per batch, then stop
    assert all(db.job(i)["state"] == "queued" for i in ids)


def test_job_that_keeps_failing_is_parked(tmp_path, monkeypatch):
    db = DB(tmp_path / "f.db")
    ids = _queued(db, 2)

    def bad(db_, cfg, jid):
        if jid == ids[0]:
            raise ValueError("model returned junk")
        return "extracted"
    monkeypatch.setattr(pipeline, "prepare", bad)
    for _ in range(pipeline.MAX_FAILS):
        pipeline.prepare_batch(db, NOENRICH, limit=5)
    assert db.job(ids[0])["state"] == "failed"


def test_disk_check_is_cached(monkeypatch):
    connections._disk_cache.clear()
    n = []
    monkeypatch.setattr(connections, "disk_encryption_now", lambda p=None: n.append(1) or {"on": True})
    connections.disk_encryption()
    connections.disk_encryption()
    assert len(n) == 1
    connections.disk_encryption(refresh=True)
    assert len(n) == 2


def test_logistics_line_uses_profile_not_a_fixed_city():
    P = type("P", (), {})
    p = P()
    p.fixed_lines, p.identity = {}, {"location": "Valencia, Spain"}
    assert "Valencia" in grounding.logistics_line(p, "en") and "Barcelona" not in grounding.logistics_line(p, "es")
    p.identity = {"location": "08001 Barcelona"}
    assert grounding.logistics_line(p, "en") == grounding.LOGISTICS["en"]
    p.fixed_lines = {"logistics": "Custom."}
    assert grounding.logistics_line(p, "en") == "Custom."


def test_batch_writes_all_letters_then_checks_them(tmp_path, monkeypatch):
    """Two-phase drafting: the writer runs for every letter first, then the checker, so the GPU loads each model
    once per batch instead of swapping writer <-> checker for every letter."""
    import json
    from . import test_phase2 as t2
    from .fakellm import Script, fake_embed
    from .test_grounding import good_letter
    sc = Script()
    monkeypatch.setattr(llm, "structured", sc.structured)
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr(llm, "missing_models", lambda c: [])
    monkeypatch.setattr(llm, "reachable", lambda c: True)
    monkeypatch.setattr(scoring.llm, "ollama_json", lambda c, s, p, **kw: {"score": 85, "reasons": ["Intune"]})
    monkeypatch.setattr("jobhunter.vectors.DATA", tmp_path, raising=False)
    cfg = t2.cfg_for_tests(tmp_path)
    cfg["grounding"] = {"words": [0, 999], "entailment": True}
    db = DB(tmp_path / "two.db")
    for i, co in enumerate(("Acme", "Gamma")):
        db.upsert_job(t2.job(external_id=str(i), company=co))
        sc.push("extractor", t2.EXTRACTED)
        sc.push("writer", good_letter)
    pipeline.prepare_batch(db, cfg)
    pipeline.score_batch(db, cfg)
    out = pipeline.generate_batch(db, cfg, limit=4)
    assert out["needs_review"] == 2 and out["checked"] == 2, out
    tasks = [c["task"] for c in sc.calls if c["task"] in ("writer", "checker")]
    assert tasks.index("checker") > max(i for i, t in enumerate(tasks) if t == "writer"), tasks
    for j in db.in_state("needs_review"):
        v = json.loads(db.application(j["id"])["validation_json"])
        assert "checker" not in v and v["hard"] == []
    assert pipeline.check_pending(db, cfg) == 0  # nothing left pending


def test_full_text_fetch_runs_on_main_queue_not_gpu(tmp_path, monkeypatch):
    """enrich_batch fetches the full ad; prepare then uses it without any network call, and waits for it meanwhile."""
    import json
    from jobhunter import enrich, grounding as g
    from jobhunter.schemas import JobFacts
    db = DB(tmp_path / "e.db")
    jid = _queued(db, 1)[0]
    cfg = {"search": {}}
    monkeypatch.setattr(g, "extract", lambda c, j: JobFacts())
    calls = []
    monkeypatch.setattr(enrich, "enrich", lambda j, c=None: calls.append(j["id"]) or
                        {"questions": [{"text": "Notice period?"}], "ats": "greenhouse", "description": "full " * 80})
    assert pipeline.prepare_batch(db, cfg)["prepared"] == 0  # fresh, not enriched yet: left for the main queue
    assert pipeline.enrich_batch(db, cfg)["enriched"] == 1 and calls == [jid]
    assert db.job(jid)["ats"] == "greenhouse"
    pipeline.prepare_batch(db, cfg)
    assert calls == [jid]  # no second fetch inside the GPU job
    facts = json.loads(db.job(jid)["facts_json"])
    assert facts["ats_questions"][0]["text"] == "Notice period?"


def test_get_json_honours_retry_after(monkeypatch):
    from jobhunter.sources import base
    waits = []
    monkeypatch.setattr(base.time, "sleep", waits.append)

    class R:
        def __init__(self, code):
            self.status_code, self.headers = code, {"Retry-After": "7"}

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": 1}
    seq = iter([R(429), R(200)])
    monkeypatch.setattr(base.requests, "get", lambda *a, **k: next(seq))
    assert base.get_json("https://x") == {"ok": 1}
    assert 7 <= waits[0] <= 8


def test_sources_fetch_in_parallel_and_one_failure_is_isolated(monkeypatch):
    import threading
    import time
    from jobhunter import sources
    seen = set()

    def slow(name):
        def fn(cfg):
            seen.add(threading.current_thread().name)
            time.sleep(0.2)
            if name == "bad":
                raise RuntimeError("down")
            yield {"title": name, "source": name, "external_id": "1"}
        return fn
    monkeypatch.setattr(sources, "REGISTRY", {n: slow(n) for n in ("a", "b", "c", "bad")})
    cfg = {"sources": {n: {"enabled": True} for n in ("a", "b", "c", "bad")}}
    monkeypatch.setattr(sources, "DEFAULT_ON", set())
    t = time.time()
    jobs = list(sources.fetch_all(cfg))
    assert time.time() - t < 0.6 and len(seen) > 1
    assert sorted(j["title"] for j in jobs) == ["a", "b", "c"]
    assert sources.LAST_STATS["bad"].startswith("failed")


def test_out_of_memory_falls_back_to_a_smaller_installed_model(monkeypatch):
    llm._fallback.clear()
    sent = []

    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(self.text)

        def json(self):
            return {"message": {"content": "{}"}}

    def post(url, json=None, timeout=None, **kw):
        sent.append(json["model"])
        return R(500, "model requires more system memory (11.2 GiB) than is available") if json["model"] == \
            "qwen2.5:14b" else R(200)
    monkeypatch.setattr(llm.requests, "post", post)
    monkeypatch.setattr(llm, "available", lambda c: {"qwen2.5:14b", "qwen2.5:7b-instruct", "qwen2.5:3b", "bge-m3"})
    cfg = {"llm": {"models": {"extractor": "qwen2.5:14b"}}}
    llm.chat(cfg, "s", "p", task="extractor")
    assert sent == ["qwen2.5:14b", "qwen2.5:7b-instruct"]
    assert llm.model(cfg, "extractor") == "qwen2.5:7b-instruct"
    assert llm.fallbacks()[0]["using"] == "qwen2.5:7b-instruct"
    assert llm.missing_models(cfg, ("extractor",)) == []  # the configured model still counts as installed
    llm._fallback.clear()


def test_score_uses_verified_facts_not_the_raw_cv(monkeypatch):
    from jobhunter import master
    prof = master.get()
    text = scoring.candidate_text(prof, prof.facts[:3])
    assert prof.facts[0].text in text and "Verified tools" in text
    assert "required_languages" not in scoring.system_prompt({})


def test_daily_backup_rotation_check_and_staged_restore(tmp_path, monkeypatch):
    import os
    import time
    from jobhunter import backup
    monkeypatch.setattr(backup, "DATA", tmp_path)
    db = DB(tmp_path / "jobhunter.db")
    _queued(db, 2)
    out = backup.daily()
    assert out["health"]["ok"] is True and len(backup.backups()) == 1
    assert backup.daily()["skipped"]  # once a day
    for i in range(9):  # rotation keeps the newest KEEP
        p = tmp_path / "backups" / f"jobhunter-2026010{i}-0000.db"
        p.write_bytes((tmp_path / "backups" / backup.backups()[0]["name"]).read_bytes())
        os.utime(p, (time.time() - 86400 * (10 - i),) * 2)
    backup.daily(force=True)
    assert len(backup.backups()) == backup.KEEP
    name = backup.backups()[0]["name"]
    db.conn.execute("DELETE FROM jobs")
    db.conn.commit()
    db.conn.close()
    backup.request_restore(name)
    r = backup.run_pending_restore()
    assert r["restored"] == name and (tmp_path / r["kept"]).exists()  # the replaced file is kept, never deleted
    assert len(DB(tmp_path / "jobhunter.db").jobs()) == 2
    assert backup.run_pending_restore() is None


def test_corrupt_database_is_reported_and_not_backed_up(tmp_path, monkeypatch):
    from jobhunter import backup
    monkeypatch.setattr(backup, "DATA", tmp_path)
    (tmp_path / "jobhunter.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 300)
    out = backup.daily(force=True)
    assert out["health"]["ok"] is False and backup.backups() == []


def test_cv_fill_runs_in_background(monkeypatch):
    import time
    from jobhunter import connections as C, cv_criteria
    monkeypatch.setattr(cv_criteria, "extract", lambda t, c=None: {"values": {"roles": ["x"]}, "missing": []})
    C.cv_fill_start("cv text " * 20, {})
    for _ in range(50):
        st = C.cv_fill_state()
        if st["state"] != "running":
            break
        time.sleep(0.02)
    assert st["state"] == "done" and st["result"]["values"]["roles"] == ["x"]


def test_db_shared_reuses_connection_and_migrates_once(tmp_path, monkeypatch):
    import jobhunter.migrate as mig
    p = tmp_path / "s.db"
    a = DB.shared(p)
    assert DB.shared(p) is a
    calls = []
    monkeypatch.setattr(mig, "migrate", lambda *x, **k: calls.append(1))
    DB(p)
    assert calls == []  # already migrated by this process



class _Stream:
    status_code, text = 200, ""

    def __init__(self, chunks, on_chunk=None):
        self.chunks, self.on_chunk, self.closed = chunks, on_chunk, False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        import json
        for i, c in enumerate(self.chunks):
            if self.on_chunk:
                self.on_chunk(i)
            yield json.dumps(c).encode()

    def close(self):
        self.closed = True


def test_writer_streams_and_pause_stops_it_mid_letter(monkeypatch):
    import time
    chunks = [{"message": {"content": "{\"a\""}}, {"message": {"content": ": 1}"}, "done": True, "done_reason": "stop"}]
    r = _Stream(chunks)
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: r)
    assert llm.chat({}, "s", "p", task="writer") == '{"a": 1}' and r.closed
    clock = iter(range(0, 1000))
    monkeypatch.setattr(time, "time", lambda: next(clock))
    monkeypatch.setattr(llm, "_paused", lambda: True)
    r2 = _Stream(chunks * 3)
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: r2)
    import pytest
    with pytest.raises(llm.Paused):
        llm.chat({}, "s", "p", task="writer")
    assert r2.closed  # closing the stream is what stops Ollama generating


def test_paused_letter_goes_back_to_scored(tmp_path, monkeypatch):
    from jobhunter import grounding as g
    db = DB(tmp_path / "pz.db")
    jid = _queued(db, 1)[0]
    for st in ("extracted", "scored"):
        db.move(jid, st)
    db.update_job(jid, facts_json='{"job_facts": {"seniority": "mid"}}')
    monkeypatch.setattr(g, "retrieve", lambda *a, **k: (_ for _ in ()).throw(llm.Paused("p")))
    assert pipeline.generate(db, {}, jid, prof=object(), store=object()) == "scored"
    assert db.job(jid)["state"] == "scored"


def test_idle_tick_releases_the_writer_once(monkeypatch):
    from jobhunter import tasks
    calls = []
    monkeypatch.setattr(llm, "unload", lambda cfg, task: calls.append(task) or True)
    tasks._idle["released"] = False
    tasks._release_when_idle({}, {"generated": 1})
    tasks._release_when_idle({}, {"generated": 0})
    tasks._release_when_idle({}, {"generated": 0})
    assert calls == ["writer"]
    tasks._release_when_idle({}, {"scored": 2})
    tasks._release_when_idle({}, {})
    assert calls == ["writer", "writer"]


def test_count_in_state(tmp_path):
    db = DB(tmp_path / "c.db")
    _queued(db, 3)
    assert db.count_in_state("queued") == 3 and db.count_in_state("needs_review") == 0


def test_unreadable_queue_file_is_set_aside(tmp_path):
    from jobhunter import tasks
    q = tmp_path / "queue_x.db"
    q.write_bytes(b"SQLite format 3\x00" + b"\xff" * 500)
    tasks._usable_queue(q)
    assert not q.exists() and list(tmp_path.glob("queue_x.db.corrupt-*"))
    good = tmp_path / "queue_ok.db"
    DB(good)
    tasks._usable_queue(good)
    assert good.exists()


def test_cv_pdf_is_read_once_per_version_not_once_per_card(tmp_path, monkeypatch):
    """The review queue builds a card per letter and each checks the CV PDF: reading it every time made
    'Review the letter' slow with many letters ready."""
    import pypdf
    from jobhunter import config
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    reads = []

    class Reader:
        def __init__(self, p):
            reads.append(p)
            self.pages = []
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    config._pdf_problem.cache_clear()
    for _ in range(20):
        assert config.attachment_problem(pdf) is None
    assert len(reads) == 1
    import os
    os.utime(pdf, ns=(1, 1))  # a new version of the file is read again
    config.attachment_problem(pdf)
    assert len(reads) == 2
