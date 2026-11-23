"""Status data for the dashboard: runs, schedule, Gmail sign-in, source health. Read-only helpers."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from .config import DATA, ROOT

GMAIL_AUTH_AT = DATA / "gmail_auth_at.txt"
TESTING_DAYS = 7  # Google expires refresh tokens after 7 days while the OAuth app is in "Testing"


def mark_gmail_auth() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    GMAIL_AUTH_AT.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")


def gmail_status(cfg: dict) -> dict:
    """{'state': 'missing'|'ok'|'expiring'|'expired', 'authorised_at': dt|None, 'expires_at': dt|None}"""
    if (cfg.get("mail") or {}).get("backend") == "imap":
        from .config import env
        st = "ok" if env("GMAIL_APP_PASSWORD") else "missing"
        return {"state": st, "authorised_at": None, "expires_at": None, "backend": "imap"}
    from . import secrets
    if not secrets.get("GMAIL_TOKEN"):
        return {"state": "missing", "authorised_at": None, "expires_at": None}
    if GMAIL_AUTH_AT.exists():
        at = datetime.fromisoformat(GMAIL_AUTH_AT.read_text(encoding="utf-8").strip())
    else:  # imported token, sign-in date unknown: assume now and let the live check correct it
        at = datetime.now(timezone.utc)
    if not cfg.get("gmail", {}).get("testing_mode", True):
        return {"state": "ok", "authorised_at": at, "expires_at": None}
    exp = at + timedelta(days=TESTING_DAYS)
    left = exp - datetime.now(timezone.utc)
    state = "expired" if left.total_seconds() <= 0 else "expiring" if left < timedelta(days=2) else "ok"
    return {"state": state, "authorised_at": at, "expires_at": exp}


_sched_cache: dict = {"t": 0.0, "v": None}


def scheduled_task() -> dict | None:
    """Windows Task Scheduler info for the 'jobhunter' task, cached for 60 s. None if not on Windows / not found."""
    if sys.platform != "win32":
        return None
    if time.time() - _sched_cache["t"] < 60:
        return _sched_cache["v"]
    ps = ("$t=Get-ScheduledTask -TaskName jobhunter -ErrorAction SilentlyContinue; if($t){$i=Get-ScheduledTaskInfo jobhunter;"
          "[pscustomobject]@{state=\"$($t.State)\";next=$i.NextRunTime.ToString('s');last=$i.LastRunTime.ToString('s');"
          "result=$i.LastTaskResult}|ConvertTo-Json}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        v = json.loads(out) if out else None
    except Exception:
        v = None
    _sched_cache.update(t=time.time(), v=v)
    return v


def last_summary(db) -> tuple[str | None, dict]:
    runs = db.last_runs(1)
    if not runs:
        return None, {}
    try:
        return runs[0]["finished_at"], json.loads(runs[0]["summary"] or "{}")
    except json.JSONDecodeError:
        return runs[0]["finished_at"], {}


def start_background(args: list[str]) -> None:
    """Start `python -m jobhunter <args>` (or the packaged app with <args>) detached, without a console window."""
    from .config import FROZEN
    exe = sys.executable
    if not FROZEN and sys.platform == "win32" and exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"
    cmd = [exe, *args] if FROZEN else [exe, "-m", "jobhunter", *args]  # the packaged exe takes CLI args directly
    # no DETACHED_PROCESS: a detached child can lose the desktop session, and the browser never opens
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    DATA.mkdir(exist_ok=True)
    log = open(DATA / f"bg_{args[0]}.log", "ab")
    subprocess.Popen(cmd, cwd=str(ROOT), creationflags=flags,
                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
