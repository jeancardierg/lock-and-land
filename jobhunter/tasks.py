"""Local queues on SQLite (Huey). Two queues so GPU work never blocks fetching:

  main     ingest every 3 h, recruiter mail every 30 min, notifications, housekeeping
  gpu      one worker: extraction, scoring and letters (one Ollama model loaded at a time)

The app never fills application forms (bot protection blocks it): you apply in your own browser.
The desktop app starts both in its own process; `python -m jobhunter workers` runs them without the window.
"""
from __future__ import annotations

import logging

from huey import Huey, crontab
from huey.exceptions import TaskLockedException
from huey.storage import SqliteStorage

from . import sqldb
from .config import DATA, load_config

log = logging.getLogger(__name__)
DATA.mkdir(parents=True, exist_ok=True)


class EncryptedSqliteStorage(SqliteStorage):
    """Huey's SQLite storage, opened through sqldb so the queue files are encrypted like the main database."""
    integrity_error = sqldb.IntegrityError
    operational_error = sqldb.OperationalError

    def _create_connection(self):
        conn = sqldb.connect(self.filename, timeout=self._timeout, check_same_thread=False, **self._conn_kwargs)
        conn.isolation_level = None  # autocommit, as in SqliteStorage
        self._set_journal_mode(conn)
        if self._cache_mb:
            conn.execute("pragma cache_size=%s" % (-1000 * self._cache_mb))
        if self._fsync is not None:
            conn.execute("pragma synchronous=%s" % (2 if self._fsync else 0))
        return conn


def _usable_queue(path) -> None:
    """A queue file that won't open (crash mid-write, disk error) would stop the workers from starting. It only
    holds pending work, which the periodic ticks recreate, so set it aside (renamed, not deleted) and start fresh."""
    from datetime import datetime
    if not path.exists() or path.stat().st_size == 0:
        return
    try:
        c = sqldb.connect(path, timeout=10)
        ok = c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        c.close()
    except Exception as e:
        if "keychain" in str(e).lower() or "DB_KEY" in str(e):
            return  # a missing key is not corruption: leave it for the recovery-key flow
        ok = False
    if not ok:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for side in ("", "-wal", "-shm"):
            f = path.with_name(path.name + side)
            if f.exists():
                f.rename(f.with_name(f"{path.name}.corrupt-{stamp}{side}"))
        log.warning("queue file %s was unreadable; set aside as %s.corrupt-%s", path.name, path.name, stamp)


def _queue(name: str, file: str) -> Huey:
    try:
        _usable_queue(DATA / file)
    except Exception as e:  # never block start-up on the check itself
        log.warning("queue check %s: %s", file, e)
    return Huey(name, storage_class=EncryptedSqliteStorage, filename=str(DATA / file), utc=False)


main = _queue("jobhunter-main", "queue_main.db")
gpu = _queue("jobhunter-gpu", "queue_gpu.db")


PAUSE = DATA / "paused"


def paused() -> bool:
    """The tray's "Pause background work" writes data/paused. Paused = no fetching or letters."""
    return PAUSE.exists()


def _ctx():
    from .db import DB
    return load_config(), DB.shared()


# ---------------- main queue ----------------

@main.periodic_task(crontab(minute="7", hour="*/3"), retries=1, retry_delay=600)
@main.lock_task("ingest")
def ingest_all():
    if paused():
        return {"skipped": "paused"}
    from . import pipeline
    from .db import now
    cfg, db = _ctx()
    started = now()
    out = pipeline.ingest(db, cfg)
    out.update(pipeline.enrich_batch(db, cfg, limit=cfg.get("limits", {}).get("enrich_per_run", 200)))
    db.log_run(started, out)
    gpu_tick()  # start processing right away
    return out


@main.periodic_task(crontab(minute="*/30"))
@main.lock_task("recruiters")
def recruiters_poll():
    if paused():
        return {"skipped": "paused"}
    cfg, db = _ctx()
    if not cfg.get("recruiters", {}).get("enabled"):
        return {}
    from . import recruiters
    try:
        return {"new": recruiters.pull(db, cfg), "drafted": recruiters.classify_and_draft(db, cfg, None)}
    except Exception as e:
        log.warning("recruiters: %s", e)
        return {"error": str(e)}


_notified: dict = {}


def _since(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _summary(db) -> str:
    """Daily summary text: counts only, no letter text."""
    one = lambda q, *a: db.conn.execute(q, a).fetchone()[0]
    new = one("SELECT count(*) FROM jobs WHERE fetched_at >= ?", _since(1))
    rev = db.count_in_state("needs_review")
    hum = db.count_in_state("needs_human", "awaiting_click")
    try:
        rec = one("SELECT count(*) FROM recruiter_msgs WHERE is_recruiter=1 AND status IN ('new','drafted')")
    except Exception:
        rec = 0
    return (f"{new} new jobs in the last 24 h. {rev} letters to review, {hum} applications waiting for you, "
            f"{rec} recruiter messages to answer.")


@main.periodic_task(crontab(minute="*/5"))
def notify_tick():
    """Every 5 min: find what is new, hand it to notify.send (which applies the Notifications settings), then
    send whatever is due (hourly batch, daily digest, end of quiet hours). What was already announced is kept in
    data/notify_state.json, so a restart does not repeat old alerts."""
    from . import notify
    cfg, db = _ctx()
    p = notify.prefs(cfg)
    st = notify.state()
    first = "seeded" not in st
    n_review = db.count_in_state("needs_review")
    n_human = db.count_in_state("needs_human", "awaiting_click")
    every = max(1, int((cfg.get("notify") or {}).get("review_batch", 5) or 5))
    if not first and n_review >= every and n_review // every > st.get("review", 0) // every:
        notify.send(cfg, "Lock & Land: letters to review", f"{n_review} letters are ready (about 10 s each).",
                    event="letters_ready")
    if not first and n_human > st.get("human", 0):
        notify.send(cfg, "Lock & Land: your turn", f"{n_human} application(s) are waiting for you.", "/#waiting",
                    event="waiting")
    st.update(review=n_review, human=n_human)

    # strong matches: scored jobs from the last 3 days at or above the score, each announced once
    seen = set(st.get("jobs", []))
    rows = db.conn.execute("SELECT id, title, company, score FROM jobs WHERE score >= ? AND fetched_at >= ? "
                           "ORDER BY score DESC", (p["strong_score"], _since(3))).fetchall()
    fresh = [r for r in rows if r["id"] not in seen]
    if fresh and not first:
        lines = [f"{r['score']} · {r['title']}" + (f", {r['company']}" if r["company"] else "") for r in fresh[:5]]
        more = f"\n…and {len(fresh) - 5} more" if len(fresh) > 5 else ""
        notify.send(cfg, f"Lock & Land: {len(fresh)} strong match{'es' if len(fresh) > 1 else ''}",
                    "\n".join(lines) + more, event="strong_jobs")
    st["jobs"] = (list(seen) + [r["id"] for r in fresh])[-3000:]

    # recruiter messages, once each, after the AI has decided they are from a recruiter
    try:
        last = int(st.get("rec_id", 0))
        new_rec = []
        for r in db.conn.execute("SELECT id, sender_name, subject, is_recruiter FROM recruiter_msgs WHERE id > ? "
                                 "ORDER BY id", (last,)).fetchall():
            if r["is_recruiter"] is None:
                break
            last = r["id"]
            if r["is_recruiter"]:
                new_rec.append(r)
        if new_rec and not first:
            notify.send(cfg, f"Lock & Land: {len(new_rec)} recruiter message{'s' if len(new_rec) > 1 else ''}",
                        "\n".join(f"{r['sender_name'] or 'Recruiter'}: {r['subject'] or ''}"[:120]
                                  for r in new_rec[:5]), event="recruiter")
        st["rec_id"] = last
    except Exception as e:
        log.info("notify: recruiter check skipped: %s", e)

    st["seeded"] = True
    notify.save_state({**notify.state(), **st})
    return notify.flush(cfg, summary=lambda: _summary(db))


# ---------------- gpu queue ----------------

@gpu.periodic_task(crontab(minute="*/2"))
def gpu_tick():
    try:
        return _gpu_tick()
    except TaskLockedException:
        return {"skipped": "busy"}


@gpu.lock_task("gpu")
def _gpu_tick():
    if paused():
        return {"skipped": "paused"}
    from . import llm, pipeline
    cfg = load_config()
    if not llm.reachable(cfg):
        return {"skipped": "ollama down", "started": _autostart_ollama(cfg)}
    missing = llm.missing_models(cfg)
    if missing:
        return {"skipped": f"pull {', '.join(missing)}"}
    from . import master
    from .db import DB
    db = DB.shared()
    out = {"recovered": pipeline.recover_interrupted(db)}
    if not master.exists():  # scoring and letters need the facts; say so instead of crashing every tick
        out.update(pipeline.prepare_batch(db, cfg, limit=cfg.get("limits", {}).get("prepare_per_tick", 25)))
        log.warning("not scoring: %s", master.MISSING_HINT)
        out["skipped"] = "no facts profile"
        return out
    # batch by model: 7B extraction for everything new, then 14B letters
    out.update(pipeline.prepare_batch(db, cfg, limit=cfg.get("limits", {}).get("prepare_per_tick", 25)))
    out.update(pipeline.score_batch(db, cfg, limit=cfg.get("limits", {}).get("score_per_tick", 25)))
    out.update(pipeline.generate_batch(db, cfg, limit=cfg.get("limits", {}).get("drafts_per_tick", 4)))
    _release_when_idle(cfg, out)
    return out


_idle = {"released": False}


def _release_when_idle(cfg: dict, out: dict) -> None:
    """The first tick with nothing to do frees the writer's memory right away, instead of holding the VRAM for
    keep_alive (10 min) while you game or video-call. The extractor stays: it runs on every new job."""
    from . import llm
    busy = any(out.get(k) for k in ("prepared", "scored", "generated", "checked"))
    if busy:
        _idle["released"] = False
    elif not _idle["released"] and (cfg.get("llm") or {}).get("release_when_idle", True) is not False:
        _idle["released"] = llm.unload(cfg, "writer")
        out["released"] = _idle["released"]


_last_start = {"t": 0.0}


def _autostart_ollama(cfg: dict) -> bool:
    """Ollama isn't answering: start it (the Ollama app, or `ollama serve`), at most once every 15 minutes.
    Off with llm.ollama.autostart: false. Only for the default local host, never a remote one."""
    import time
    from . import llm
    o = (cfg.get("llm") or {}).get("ollama") or {}
    if o.get("autostart", True) is False or not llm.host(cfg).startswith(("http://localhost", "http://127.0.0.1")):
        return False
    if time.time() - _last_start["t"] < 900:
        return False
    _last_start["t"] = time.time()
    from .hardware import start_ollama
    ok = start_ollama()
    log.info("Ollama was not answering; started it: %s", ok)
    return ok


@gpu.task(retries=1, retry_delay=60)
def generate_one(job_id: int, actor: str = "jean"):
    from . import pipeline
    cfg, db = _ctx()
    return pipeline.draft_job(db, cfg, None, job_id)


@main.periodic_task(crontab(minute="41"))
@main.lock_task("backup")
def db_backup():
    """Hourly look, daily work: quick_check + a rolling encrypted backup of jobhunter.db (see backup.py)."""
    from . import backup, notify
    out = backup.daily()
    h = out.get("health") or {}
    if h.get("ok") is False and _notified.get("db") != h.get("detail"):
        notify.send(load_config(), "Lock & Land: database problem",
                    "The daily check found a problem. Open Configuration to restore a backup.", "/connections",
                    event="problems")
        _notified["db"] = h.get("detail")
    return out


@main.periodic_task(crontab(minute="13", hour="*/6"))
def auth_check():
    """Every 6 h: if a sign-in has expired (Gmail) or keys are missing, tell the user once per problem."""
    from . import connections, notify
    cfg = load_config()
    need = connections.needs_attention()
    new = [n for n in need if n not in _notified.get("auth", [])]
    if new:
        what = {"gmail": "Gmail sign-in expired or missing", "adzuna": "Adzuna keys missing",
                "jooble": "Jooble key missing",
                "import": "import your old data"}
        notify.send(cfg, "Lock & Land: sign-in needed", "; ".join(what.get(n, n) for n in new) + ". Open Configuration.",
                    "/connections", event="problems")
    _notified["auth"] = need
    return {"needs": need}


QUEUES = {"main": main, "gpu": gpu}
