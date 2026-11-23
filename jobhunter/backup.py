"""Daily database health check and rolling backups of jobhunter.db (encrypted like the database itself).

- daily(): once a day (the main queue checks every hour), PRAGMA quick_check, then an encrypted copy in
  data/backups/jobhunter-YYYYMMDD.db; the newest KEEP copies are kept. A database that fails the check is never
  copied over a good backup: the result is recorded and shown on the Configuration page instead.
- Restore is staged: request_restore() writes data/restore.pending and the app restarts; at the next start, before
  anything opens the database, run_pending_restore() renames the current file to jobhunter.db.replaced-<time>
  (never deleted) and puts the backup in its place.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import sqldb
from .config import DATA

log = logging.getLogger(__name__)
KEEP = 7
EVERY_H = 24


def _dir() -> Path:
    return DATA / "backups"


def _health_file() -> Path:
    return DATA / "db_health.json"


def health() -> dict:
    try:
        return json.loads(_health_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": None, "detail": "not checked yet"}


def _write_health(ok: bool, detail: str, backup: str | None = None) -> dict:
    h = {"ok": ok, "detail": detail, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if backup:
        h["backup"] = backup
    elif health().get("backup"):
        h["backup"] = health()["backup"]
    _health_file().write_text(json.dumps(h), encoding="utf-8")
    return h


def backups() -> list[dict]:
    d = _dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("jobhunter-*.db"), reverse=True):
        out.append({"name": p.name, "bytes": p.stat().st_size,
                    "date": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


def due(now: float | None = None) -> bool:
    b = backups()
    if not b:
        return True
    newest = (_dir() / b[0]["name"]).stat().st_mtime
    return (now or time.time()) - newest >= EVERY_H * 3600 - 300


def daily(force: bool = False, path: Path | None = None) -> dict:
    path = Path(path or DATA / "jobhunter.db")
    if not path.exists():
        return {"skipped": "no database yet"}
    if not force and not due():
        return {"skipped": "backed up within the last day"}
    try:
        conn = sqldb.connect(path, timeout=30)
        res = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    except Exception as e:
        return {"health": _write_health(False, f"could not open the database: {e}")}
    if res != ["ok"]:
        conn.close()
        log.error("database check failed: %s", res[:5])
        return {"health": _write_health(False, "; ".join(res[:5]))}
    _dir().mkdir(parents=True, exist_ok=True)
    dst = _dir() / f"jobhunter-{datetime.now().strftime('%Y%m%d-%H%M')}.db"
    try:
        sqldb.copy(conn, dst)
    finally:
        conn.close()
    for old in backups()[KEEP:]:
        (_dir() / old["name"]).unlink(missing_ok=True)
    log.info("database ok, backed up to %s", dst.name)
    return {"health": _write_health(True, "ok", dst.name), "backup": dst.name}


def _pending() -> Path:
    return DATA / "restore.pending"


def request_restore(name: str) -> dict:
    if name not in {b["name"] for b in backups()}:
        raise ValueError("No such backup")
    src = _dir() / name
    conn = sqldb.connect(src)  # must open with this computer's key, or restoring it would lock you out
    ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()
    if not ok:
        raise ValueError(f"{name} fails the database check; pick another backup")
    _pending().write_text(name, encoding="utf-8")
    return {"staged": name}


def run_pending_restore() -> dict | None:
    """At start, before the database is opened: swap in the staged backup. The current file is kept."""
    p = _pending()
    if not p.exists():
        return None
    name = p.read_text(encoding="utf-8").strip()
    src = _dir() / name
    dst = DATA / "jobhunter.db"
    p.unlink(missing_ok=True)
    if not src.exists():
        log.error("restore: %s is gone", name)
        return {"error": f"{name} not found"}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for side in ("", "-wal", "-shm"):
        f = dst.with_name(dst.name + side)
        if f.exists():
            os.replace(f, f.with_name(f"{dst.name}.replaced-{stamp}{side}"))
    shutil.copy2(src, dst)
    log.warning("restored the database from %s (previous file kept as jobhunter.db.replaced-%s)", name, stamp)
    _write_health(True, f"restored from {name}", name)
    return {"restored": name, "kept": f"jobhunter.db.replaced-{stamp}"}
