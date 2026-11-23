"""Change the data folder from inside the app. The Configuration page records the request, the app restarts, and
this runs before anything opens the old folder: copy everything, check it, point home.txt at the new folder and
rename the old one to "<name>.moved-<date>" (kept as a backup; delete it yourself once you're happy).
Only stdlib here: it must run before jobhunter.config fixes HOME for the process."""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


def cfg_dir() -> Path:  # same as config.app_config_dir(), duplicated on purpose (see module doc)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "jobhunter"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "jobhunter"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jobhunter"


PENDING, RESULT = "pending_move.json", "last_move.json"


def request(src: Path, dst: Path, mode: str) -> dict:
    src, dst = Path(src).resolve(), Path(dst).expanduser().resolve()
    if mode not in ("move", "use"):
        raise ValueError("mode is move or use")
    if dst == src:
        raise ValueError("That is already the data folder")
    if mode == "move" and (dst == src or src in dst.parents):
        raise ValueError("The new folder can't be inside the current data folder")
    if mode == "move" and (dst / "data" / "jobhunter.db").exists():
        raise ValueError("That folder already has jobhunter data: choose 'Use that folder' instead, or pick an empty one")
    if mode == "use" and not (dst / "config.yaml").exists():
        raise ValueError("No jobhunter data (config.yaml) in that folder: choose 'Move my data there' instead")
    cfg_dir().mkdir(parents=True, exist_ok=True)
    job = {"src": str(src), "dst": str(dst), "mode": mode, "at": datetime.now().isoformat(timespec="seconds")}
    (cfg_dir() / PENDING).write_text(json.dumps(job), encoding="utf-8")
    return job


def _files(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def run_pending() -> dict | None:
    """Called at app start. Returns the result (also saved to last_move.json) or None if nothing was pending."""
    pend = cfg_dir() / PENDING
    if not pend.exists():
        return None
    job = json.loads(pend.read_text(encoding="utf-8"))
    pend.unlink()
    src, dst = Path(job["src"]), Path(job["dst"])
    res = {**job, "ok": False}
    try:
        if job["mode"] == "move":
            dst.mkdir(parents=True, exist_ok=True)
            # Chrome's lock files and the heartbeat are recreated; everything else is copied as is (DBs stay encrypted)
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("Singleton*", "lockfile", "workers.heartbeat", "*.lock"))
            want = {k: v for k, v in _files(src).items()
                    if not any(x in k for x in ("Singleton", "lockfile", "workers.heartbeat", ".lock"))}
            got = _files(dst)
            missing = [k for k, v in want.items() if got.get(k) != v]
            if missing:
                raise RuntimeError(f"{len(missing)} file(s) didn't copy correctly, e.g. {missing[0]}; nothing changed")
        (cfg_dir() / "home.txt").write_text(str(dst), encoding="utf-8")
        if job["mode"] == "move":
            backup = src.with_name(f"{src.name}.moved-{datetime.now():%Y%m%d-%H%M%S}")
            try:
                src.rename(backup)
                res["old_folder_kept_as"] = str(backup)
            except OSError as e:
                res["old_folder_kept_as"] = str(src)
                res["note"] = f"old folder left in place ({e})"
        res["ok"] = True
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    (cfg_dir() / RESULT).write_text(json.dumps(res), encoding="utf-8")
    return res


def last_result() -> dict | None:
    try:
        return json.loads((cfg_dir() / RESULT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
