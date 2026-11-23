from __future__ import annotations

import json
import re
from . import sqldb
from . import sqldb as sqlite3  # Row / IntegrityError from the same driver as the connection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    remote INTEGER DEFAULT 0,
    url TEXT,
    apply_email TEXT,
    description TEXT,
    posted_at TEXT,
    fetched_at TEXT NOT NULL,
    score INTEGER,
    score_reasons TEXT,
    cover_letter TEXT,
    cv_summary TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT,
    updated_at TEXT,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS recruiter_msgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,            -- 'gmail' | 'linkedin'
    gmail_id TEXT UNIQUE,
    thread_id TEXT,
    sender_name TEXT,
    sender_email TEXT,
    subject TEXT,
    body TEXT,
    link TEXT,                        -- LinkedIn conversation URL for InMail
    received_at TEXT,
    is_recruiter INTEGER,
    fit TEXT,
    draft_reply TEXT,
    status TEXT NOT NULL DEFAULT 'new', -- new|drafted|sending|sent|ignored
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, summary TEXT
);
"""

JOB_STATUSES_V02 = ["new", "filtered", "scored", "shortlisted", "drafted", "relocation", "sending", "approved", "applied", "skipped", "error"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dedup_key(title: str, company: str | None) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(title)}|{norm(company)}"


import threading

_ready: set[str] = set()      # database files already created/migrated by this process
_local = threading.local()    # DB.shared(): one connection per thread and file


class DB:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DATA / "jobhunter.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqldb.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        key = str(self.path.resolve())
        if key not in _ready or not self.path.exists():  # schema + migrations once per process, not per request
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            from .migrate import migrate
            migrate(self.path, make_backup=self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] > 0)
            self.conn.execute("PRAGMA journal_mode=WAL")
            _ready.add(key)
        self.conn.execute("PRAGMA foreign_keys=ON")

    @classmethod
    def shared(cls, path: str | Path | None = None) -> "DB":
        """This thread's open DB for the file (workers and the review page reuse it instead of reopening the
        encrypted database for every tick and request)."""
        p = str(Path(path) if path else DATA / "jobhunter.db")
        cache = getattr(_local, "dbs", None)
        if cache is None:
            cache = _local.dbs = {}
        db = cache.get(p)
        if db is None or not Path(p).exists():
            db = cache[p] = cls(p)
        elif db.conn.in_transaction:
            # A transaction left open on this thread's connection keeps the write lock, and every other write
            # (the user saving a letter, approving) then waits 30 s and fails with "database is locked". Close it.
            import logging
            logging.getLogger(__name__).warning("closing a transaction left open on %s", Path(p).name)
            db.conn.commit()
        return db

    def _write(self, sql: str, params: Iterable[Any] = ()):
        """One write statement, committed, or rolled back if it fails. Never leaves a transaction open: an open
        transaction keeps the database's write lock and every other write then fails with "database is locked"."""
        with self.conn:
            return self.conn.execute(sql, tuple(params))

    # ---------- jobs ----------
    def upsert_job(self, job: dict[str, Any]) -> bool:
        """Insert job if new (by source id and cross-source dedup key). Returns True if inserted."""
        key = dedup_key(job["title"], job.get("company"))
        if self.conn.execute("SELECT 1 FROM jobs WHERE dedup_key=?", (key,)).fetchone():
            return False
        try:
            self._write(
                """INSERT INTO jobs (source, external_id, dedup_key, title, company, location, remote, url,
                   apply_email, description, posted_at, fetched_at, status, state, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'new', 'queued', ?)""",
                (job["source"], str(job["external_id"]), key, job["title"], job.get("company"),
                 job.get("location"), int(bool(job.get("remote"))), job.get("url"), job.get("apply_email"),
                 job.get("description"), job.get("posted_at"), now(), now()),
            )
            return True
        except sqlite3.IntegrityError:  # already stored (_write rolled the failed INSERT back)
            return False

    def jobs(self, statuses: Iterable[str] | None = None, order: str = "score DESC, posted_at DESC") -> list[sqlite3.Row]:
        if statuses:
            st = list(statuses)
            q = f"SELECT * FROM jobs WHERE status IN ({','.join('?'*len(st))}) ORDER BY {order}"
            return self.conn.execute(q, st).fetchall()
        return self.conn.execute(f"SELECT * FROM jobs ORDER BY {order}").fetchall()

    def job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def update_job(self, job_id: int, **fields: Any) -> None:
        fields["updated_at"] = now()
        if "score_reasons" in fields and not isinstance(fields["score_reasons"], str):
            fields["score_reasons"] = json.dumps(fields["score_reasons"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._write(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def claim(self, table: str, row_id: int, from_statuses: Iterable[str]) -> bool:
        """Atomically move a row to 'sending' so a double click can never send twice."""
        assert table in ("jobs", "recruiter_msgs")
        st = list(from_statuses)
        cur = self._write(
            f"UPDATE {table} SET status='sending', updated_at=? WHERE id=? AND status IN ({','.join('?'*len(st))})",
            (now(), row_id, *st))
        return cur.rowcount == 1

    # ---------- v0.3: states, applications, form runs ----------
    def count_in_state(self, *states: str) -> int:
        q = f"SELECT count(*) FROM jobs WHERE state IN ({','.join('?' * len(states))})"
        return self.conn.execute(q, states).fetchone()[0]

    def in_state(self, *states: str, order: str = "COALESCE(score, 0) DESC, id") -> list[sqlite3.Row]:
        q = f"SELECT * FROM jobs WHERE state IN ({','.join('?' * len(states))}) ORDER BY {order}"
        return self.conn.execute(q, states).fetchall()

    def move(self, job_id: int, to: str, actor: str = "system", reason: str = "", **fields: Any) -> str:
        from .states import transition
        return transition(self.conn, job_id, to, actor, reason, **fields)

    def try_move(self, job_id: int, to: str, actor: str = "system", reason: str = "", **fields: Any) -> bool:
        from .states import IllegalTransition
        try:
            self.move(job_id, to, actor, reason, **fields)
            return True
        except IllegalTransition:
            return False

    def events(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

    def add_application(self, job_id: int, out: dict, answers: list | None = None, attempt: int = 1) -> int:
        cur = self._write(
            """INSERT INTO applications (job_id, profile_version, model, lang, letter_json, letter_text, subject,
               answers_json, retrieval_json, validation_json, validation_score, attempt)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, out.get("profile_version", ""), out.get("model", ""), out.get("lang"),
             json.dumps(out.get("letter_json"), ensure_ascii=False), out.get("text", ""), out.get("subject", ""),
             json.dumps(answers or [], ensure_ascii=False), json.dumps(out.get("retrieval"), ensure_ascii=False),
             json.dumps(out.get("validation"), ensure_ascii=False), (out.get("validation") or {}).get("score"),
             out.get("attempts", attempt)))
        return cur.lastrowid

    def application(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                                 (job_id,)).fetchone()

    def update_application(self, app_id: int, **fields: Any) -> None:
        for k, v in list(fields.items()):
            if not isinstance(v, (str, int, float, type(None))):
                fields[k] = json.dumps(v, ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._write(f"UPDATE applications SET {cols} WHERE id=?", (*fields.values(), app_id))

    def add_form_run(self, job_id: int, application_id: int | None, adapter: str) -> int:
        cur = self._write("INSERT INTO form_runs (job_id, application_id, adapter) VALUES (?,?,?)",
                          (job_id, application_id, adapter))
        return cur.lastrowid

    def finish_form_run(self, run_id: int, **fields: Any) -> None:
        fields["finished_at"] = now()
        for k, v in list(fields.items()):
            if isinstance(v, (list, dict)):
                fields[k] = json.dumps(v, ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._write(f"UPDATE form_runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def note_form_run(self, run_id: int, blocker: str) -> None:
        """What a running fill is waiting on (e.g. a security check), shown on the Waiting tab meanwhile."""
        self._write("UPDATE form_runs SET blocker=? WHERE id=?", (blocker[:300], run_id))

    def form_runs(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM form_runs WHERE job_id=? ORDER BY id DESC", (job_id,)).fetchall()

    def submitted_today(self) -> int:
        return self.conn.execute("SELECT count(*) FROM job_events WHERE to_state='submitted' "
                                 "AND date(at)=date('now')").fetchone()[0]

    # ---------- recruiters ----------
    def upsert_msg(self, m: dict[str, Any]) -> bool:
        try:
            self._write(
                """INSERT INTO recruiter_msgs (channel, gmail_id, thread_id, sender_name, sender_email, subject,
                   body, link, received_at, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?, 'new', ?)""",
                (m["channel"], m["gmail_id"], m.get("thread_id"), m.get("sender_name"), m.get("sender_email"),
                 m.get("subject"), m.get("body"), m.get("link"), m.get("received_at"), now()),
            )
            return True
        except sqlite3.IntegrityError:  # already stored (_write rolled the failed INSERT back)
            return False

    def msgs(self, statuses: Iterable[str] | None = None) -> list[sqlite3.Row]:
        if statuses:
            st = list(statuses)
            return self.conn.execute(
                f"SELECT * FROM recruiter_msgs WHERE status IN ({','.join('?'*len(st))}) ORDER BY received_at DESC", st
            ).fetchall()
        return self.conn.execute("SELECT * FROM recruiter_msgs ORDER BY received_at DESC").fetchall()

    def msg(self, msg_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM recruiter_msgs WHERE id=?", (msg_id,)).fetchone()

    def update_msg(self, msg_id: int, **fields: Any) -> None:
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._write(f"UPDATE recruiter_msgs SET {cols} WHERE id=?", (*fields.values(), msg_id))

    # ---------- runs ----------
    def log_run(self, started: str, summary: dict) -> None:
        self._write("INSERT INTO runs (started_at, finished_at, summary) VALUES (?,?,?)",
                    (started, now(), json.dumps(summary)))

    def last_runs(self, n: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()
