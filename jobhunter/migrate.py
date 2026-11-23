"""Versioned SQLite migrations (PRAGMA user_version). Always backs the database up first.

v1 = v0.2 schema. v2 = state machine, job_events, applications, form_runs, enrichment columns,
LinkedIn title/company repair, WAL mode. v3 = jobs.enrich_json.
"""
from __future__ import annotations

import logging
from . import sqldb
from . import sqldb as sqlite3  # type names only
from datetime import datetime
from pathlib import Path

from .states import FROM_LEGACY

log = logging.getLogger(__name__)
TARGET = 3


def backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(f"{path.stem}.backup-{stamp}{path.suffix}")
    src = sqldb.connect(path)
    sqldb.copy(src, dst)  # encrypted with the same key when encryption is on
    src.close()
    return dst


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _v2(conn: sqlite3.Connection) -> None:
    cols = _cols(conn, "jobs")
    for name, decl in [("state", "TEXT"), ("ats", "TEXT"), ("apply_url", "TEXT"), ("facts_json", "TEXT"),
                       ("embed_score", "REAL"), ("full_text", "INTEGER DEFAULT 0")]:
        if name not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS job_events (
      id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
      from_state TEXT, to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT,
      at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id);
    CREATE TABLE IF NOT EXISTS applications (
      id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
      profile_version TEXT NOT NULL, model TEXT NOT NULL, lang TEXT,
      letter_json TEXT, letter_text TEXT, subject TEXT, answers_json TEXT, retrieval_json TEXT,
      validation_json TEXT, validation_score INTEGER, attempt INTEGER DEFAULT 1,
      edited_by_jean INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
    CREATE INDEX IF NOT EXISTS idx_apps_job ON applications(job_id);
    CREATE TABLE IF NOT EXISTS form_runs (
      id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), application_id INTEGER,
      adapter TEXT, filled INTEGER, unresolved_json TEXT, blocker TEXT, screenshot_path TEXT,
      started_at TEXT DEFAULT (datetime('now')), finished_at TEXT, outcome TEXT);
    CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
    """)
    if "issues" not in _cols(conn, "recruiter_msgs"):
        conn.execute("ALTER TABLE recruiter_msgs ADD COLUMN issues TEXT")
    # states from v0.2 statuses (tracker stages stay in `status`)
    for old, new in FROM_LEGACY.items():
        conn.execute("UPDATE jobs SET state=? WHERE state IS NULL AND status=?", (new, old))
    conn.execute("UPDATE jobs SET state='queued' WHERE state IS NULL")
    # v0.2 drafts were never validated: send them back through generation instead of straight to review
    conn.execute("UPDATE jobs SET state='scored' WHERE state='needs_review'")
    conn.execute("INSERT INTO job_events(job_id, from_state, to_state, actor, reason) "
                 "SELECT id, NULL, state, 'system', 'migrated from v0.2 status ' || status FROM jobs")
    # LinkedIn alert parser bug (v0.2): title held "Title Company · Location", company held the title
    rows = conn.execute("SELECT id, title, company, location FROM jobs WHERE source='linkedin' "
                        "AND location LIKE '%·%' AND company <> '' AND title LIKE company || '%'").fetchall()
    for jid, title, company, location in rows:
        comp, _, loc = location.partition("·")
        conn.execute("UPDATE jobs SET title=?, company=?, location=? WHERE id=?",
                     (company.strip(), comp.strip(), loc.strip(), jid))
    log.info("v2: repaired %d LinkedIn rows", len(rows))


def _v3(conn: sqlite3.Connection) -> None:
    """enrich_json: the full-text fetch's result (ATS questions), so it can run on the main queue, not the GPU one."""
    if "enrich_json" not in _cols(conn, "jobs"):
        conn.execute("ALTER TABLE jobs ADD COLUMN enrich_json TEXT")


STEPS = {2: _v2, 3: _v3}


def migrate(path: Path, make_backup: bool = True) -> dict:
    path = Path(path)
    out: dict = {"path": str(path)}
    conn = sqldb.connect(path, timeout=30)
    v = conn.execute("PRAGMA user_version").fetchone()[0] or 1
    out["from"] = v
    if v >= TARGET:
        conn.close()
        out["to"] = v
        return out
    conn.close()
    if make_backup and path.exists() and path.stat().st_size:
        out["backup"] = str(backup(path))
    conn = sqldb.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for step in range(v + 1, TARGET + 1):
        with conn:
            STEPS[step](conn)
            conn.execute(f"PRAGMA user_version={step}")
    out["to"] = TARGET
    out["states"] = dict(conn.execute("SELECT state, count(*) FROM jobs GROUP BY state").fetchall())
    conn.close()
    return out
