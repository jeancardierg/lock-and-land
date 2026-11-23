"""Everything the Configuration page shows and does: sign-in state per source, keychain, database encryption,
disk encryption, Ollama models, and importing an older install. No secret value is ever returned by status()."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from . import secrets, sqldb
from .config import DATA, HOME, ROOT, app_config_dir, env, load_config

log = logging.getLogger(__name__)
_gmail_cache: dict = {"t": 0.0, "v": None}
_jobs: dict[str, dict] = {}
WINDOW_HOOK = None  # set by the desktop app: shows a page in the app window
RESTART_HOOK = None  # set by the desktop app: restarts it (used after a data-folder change)  # background actions (gmail sign-in, model pulls): name -> {state, detail}

# What each job source really supports. Shown on the page so nobody expects a LinkedIn login.
SOURCES = {
    "adzuna": {"name": "Adzuna", "auth": "api_key",
               "paid": True,
               "note": "Optional, paid: the free trial is 25 calls, then Adzuna wants a paid plan. Off unless you turn "
                       "it on; everything else here is free."},
    "remotive": {"name": "Remotive", "auth": "public", "note": "Free job site, nothing to sign in to."},
    "arbeitnow": {"name": "Arbeitnow", "auth": "public", "note": "Free job site, nothing to sign in to."},
    "jobtech": {"name": "JobTech (Sweden)", "auth": "public", "note": "Free job site, nothing to sign in to."},
    "jooble": {"name": "Jooble (Spain)", "auth": "api_key", "secret": "JOOBLE_API_KEY",
               "note": "Free key per country (100 calls/day): request the Spain key at es.jooble.org/api/about."},
    "himalayas": {"name": "Himalayas", "auth": "public", "note": "Free remote-jobs site, no key. Checked once a day."},
    "jobicy": {"name": "Jobicy", "auth": "public", "note": "Free remote-jobs site, no key. Checked once a day."},
    "weworkremotely": {"name": "We Work Remotely", "auth": "public", "note": "Free remote-jobs site, no key."},
    "remoteok": {"name": "RemoteOK", "auth": "public", "note": "Free remote-jobs site, no key. Checked twice a day."},
    "hnhiring": {"name": "HN Who's Hiring", "auth": "public",
                 "note": "The monthly Hacker News hiring thread. Free, no key. Mostly tech and startups."},
    "themuse": {"name": "The Muse", "auth": "public", "secret": "MUSE_API_KEY", "optional_key": True,
                "note": "Every kind of job, mostly US and some Europe. No key needed; a free key from "
                        "themuse.com/developers raises the rate limit. (Its API was down in Nov 2026.)"},
    "careerjet": {"name": "Careerjet (Spain)", "auth": "api_key", "secret": "CAREERJET_API_KEY",
                  "note": "Every sector, all Spanish boards. Free publisher key: sign up at careerjet.es/partners "
                          "(Publisher), then paste the API key here."},
    "findwork": {"name": "Findwork", "auth": "api_key", "secret": "FINDWORK_API_KEY",
                 "note": "Mostly tech jobs from several boards. Free key: create an account at findwork.dev, the key is "
                         "on your account page."},
    "workingnomads": {"name": "Working Nomads", "auth": "public", "note": "Free remote-jobs site, no key."},
    "watchlist": {"name": "Company watchlist", "auth": "public",
                  "note": "Greenhouse / Lever / Ashby / Workable / SmartRecruiters boards of the companies listed in "
                          "config.yaml (sources.watchlist)."},
    "linkedin_alerts": {"name": "LinkedIn job alerts", "auth": "via_gmail",
                        "note": "Read from LinkedIn alert emails in Gmail. LinkedIn itself is never logged in to "
                                "or automated (no API for individuals, and automation risks a ban)."},
}


# ---------------- background actions ----------------

def _run_bg(name: str, fn) -> dict:
    cur = _jobs.get(name)
    if cur and cur["state"] == "running":
        return cur

    def wrap():
        try:
            _jobs[name] = {"state": "running", "detail": "", "t": time.time()}
            detail = fn()
            _jobs[name] = {"state": "done", "detail": detail or "", "t": time.time()}
        except Exception as e:
            log.exception("%s failed", name)
            _jobs[name] = {"state": "failed", "detail": f"{type(e).__name__}: {e}", "t": time.time()}
        _gmail_cache["t"] = 0.0

    _jobs[name] = {"state": "running", "detail": "", "t": time.time()}
    threading.Thread(target=wrap, name=f"bg-{name}", daemon=True).start()
    return _jobs[name]


def job(name: str) -> dict:
    return _jobs.get(name) or {"state": "idle", "detail": ""}


_cv_fill: dict = {}


def cv_fill_start(text: str, cfg: dict) -> dict:
    """Read criteria from a CV on a background thread (the 7B model can take a minute on a cold start), so the
    page shows progress instead of one long request. Poll cv_fill_state()."""
    from . import cv_criteria
    if not text or len(text.strip()) < 80:
        raise ValueError("Add your CV first: choose the file or paste its text.")
    _cv_fill.clear()

    def go():
        _cv_fill["result"] = cv_criteria.extract(text, cfg)
        return "read"
    st = _run_bg("cv_fill", go)
    return {**st, "started": st.get("t", time.time())}


def cv_fill_state() -> dict:
    st = dict(job("cv_fill"))
    if st.get("state") == "done":
        st["result"] = _cv_fill.get("result")
    st["elapsed"] = round(time.time() - st["t"]) if st.get("t") else 0
    return st


# ---------------- Gmail ----------------

def gmail_state(cfg: dict, refresh: bool = False) -> dict:
    from . import gmail, status
    if (cfg.get("mail") or {}).get("backend") == "imap":
        return _imap_state(cfg, refresh)
    if refresh or time.time() - _gmail_cache["t"] > 600 or _gmail_cache["v"] is None:
        _gmail_cache.update(t=time.time(), v=gmail.check())
    v = dict(_gmail_cache["v"])
    st = status.gmail_status(cfg)
    v.update(backend="gmail_api", testing_mode=bool((cfg.get("gmail") or {}).get("testing_mode", True)),
             expires_at=st["expires_at"].isoformat() if st.get("expires_at") else None, signin=job("gmail_signin"))
    if v["state"] == "ok" and st["state"] == "expiring":
        v["state"] = "expiring"
    return v


def gmail_signin() -> dict:
    from . import gmail

    def go():
        try:
            gmail.sign_in()
        except Exception as e:
            raise RuntimeError(explain_oauth_error(e)) from e
        return "signed in"
    return _run_bg("gmail_signin", go)


def explain_oauth_error(e: Exception) -> str:
    """Google sign-in failures in plain words, with the fix."""
    t = f"{type(e).__name__}: {e}"
    low = t.lower()
    if "access_denied" in low or "accessdenied" in low:
        return ("Google blocked the sign-in. If the Google app belongs to someone else, they must add your Gmail "
                "address as a test user first. Or use the App password way instead: it doesn't need that.")
    if "timeout" in low or "timed out" in low:
        return "The Google tab wasn't finished within 10 minutes. Press Sign in with Google and try again."
    if "mismatching_state" in low or "csrf" in low:
        return "The sign-in got mixed up with an older Google tab. Close the Google tabs and press Sign in again."
    if "invalid_client" in low or "unauthorized_client" in low:
        return "Google doesn't recognise this app's client file. Load a fresh Desktop app client JSON."
    if "connection" in low or "network" in low:
        return "Couldn't reach Google. Check the internet connection and try again."
    return "Google sign-in didn't finish: " + t


# ---------------- Gmail with an app password (the easy way) ----------------

_imap_cache: dict = {"t": 0.0, "v": None, "k": None}
EMAIL_OK = r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"


def _imap_state(cfg: dict, refresh: bool = False) -> dict:
    from . import mailimap
    email = str((cfg.get("profile") or {}).get("email") or "")
    if email == "you@gmail.com":
        email = ""
    base = {"backend": "imap", "email": email or None, "has_client": _has_client()}
    if not env("GMAIL_APP_PASSWORD"):
        return {**base, "state": "missing", "detail": "Add a Gmail app password"}
    if not email:
        return {**base, "state": "missing", "detail": "Type your Gmail address"}
    key = (email, secrets.get("GMAIL_APP_PASSWORD"))
    if refresh or _imap_cache["k"] != key or time.time() - _imap_cache["t"] > 1800:
        try:
            mailimap.test_login(email, env("GMAIL_APP_PASSWORD"))
            v = {"state": "ok", "detail": ""}
        except Exception as e:
            log.warning("Gmail app password check failed: %s", e)
            auth = "authenticat" in str(e).lower() or "credentials" in str(e).lower() \
                or "password" in str(e).lower()
            v = {"state": "expired" if auth else "error", "detail": mailimap.explain(e)}
        _imap_cache.update(t=time.time(), v=v, k=key)
    return {**base, **_imap_cache["v"]}


def _has_client() -> bool:
    try:
        from . import gmail
        return bool(gmail.client_config())
    except Exception:
        return False


def _edit_config(fn) -> None:
    """Change config.yaml through fn(raw), keeping a dated backup (same as the criteria page)."""
    import yaml
    from . import criteria
    path = criteria.config_path()
    if not path.exists():
        ensure_home()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    shutil.copy2(path, path.with_name(f"config.yaml.bak-{datetime.now():%Y%m%d-%H%M%S}"))
    fn(raw)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")


# ---------------- Telegram alerts ----------------

def telegram_state(tg: dict | None = None) -> dict:
    from . import notify
    tg = tg if tg is not None else notify.tg_settings(load_config())
    return {"enabled": bool(tg.get("enabled")), "has_token": secrets.has("TELEGRAM_BOT_TOKEN"),
            "chat_set": bool(str(tg.get("chat_id") or "").strip()), "who": tg.get("chat_name") or "",
            "bot": tg.get("bot") or "", "last": notify.last_status()}


def _set_telegram(**kv) -> None:
    def fn(raw):
        if not isinstance(raw.get("notify"), dict):
            raw["notify"] = {}
        n = raw["notify"]
        if not isinstance(n.get("telegram"), dict):
            n["telegram"] = {}
        n["telegram"].update(kv)
    _edit_config(fn)


def telegram_token_saved() -> dict:
    """Right after the token is saved: check it and remember the bot's @name for the 'open your bot' link."""
    from . import notify
    r = notify.telegram_bot()
    if r["ok"]:
        _set_telegram(bot=r["username"])
    return r


def telegram_connect() -> dict:
    """Find the chat of whoever pressed Start in the bot, save it, turn alerts on and say hello there."""
    from . import notify
    r = notify.telegram_find_chat()
    if r.get("bot"):
        _set_telegram(bot=r["bot"])
    if not r["ok"]:
        return r
    _set_telegram(chat_id=r["chat_id"], chat_name=r.get("who", ""), enabled=True)
    sent = notify.telegram(load_config(), "Lock & Land is connected. Alerts will arrive in this chat.", force=True)
    who = f" ({r['who']})" if r.get("who") else ""
    return {"ok": True, "sent": sent,
            "detail": f"Connected to your chat{who}. " + ("Check Telegram: a hello message is there." if sent
                                                           else "But the hello message failed: " +
                                                           notify.last_status().get("detail", ""))}


def telegram_set_chat(chat_id: str) -> dict:
    """Manual fallback: a numeric chat id (or @channelname for a channel the bot is admin of)."""
    import re
    c = (chat_id or "").strip()
    if not re.fullmatch(r"-?\d{3,20}|@[A-Za-z0-9_]{5,64}", c):
        raise ValueError("A chat id is a number like 123456789 (or @channelname for a channel).")
    _set_telegram(chat_id=c, chat_name="", enabled=True)
    return {"ok": True}


def telegram_enable(on: bool) -> dict:
    _set_telegram(enabled=bool(on))
    return {"ok": True, "enabled": bool(on)}


def telegram_test() -> dict:
    from . import notify
    cfg = load_config()
    st = telegram_state(notify.tg_settings(cfg))
    if not st["has_token"]:
        return {"ok": False, "detail": "Save the bot token first."}
    if not st["chat_set"]:
        return {"ok": False, "detail": "Press Find my chat first (after pressing Start in the bot)."}
    ok = notify.telegram(cfg, "Lock & Land test message: Telegram alerts work.", force=True)
    return {"ok": ok, "detail": "Sent. Check Telegram." if ok else notify.last_status().get("detail", "Not sent")}


def gmail_app_password(email: str, password: str) -> dict:
    """Test the address + app password against Gmail, and only then save them and switch mail to IMAP."""
    import re
    from . import mailimap
    email = (email or "").strip()
    pw = mailimap.clean_password(password)
    if not re.match(EMAIL_OK, email):
        raise ValueError("Type your full Gmail address, like name@gmail.com.")
    if not pw:
        raise ValueError("Paste the app password first (16 letters from Google).")
    if not re.fullmatch(r"[A-Za-z]{16}", pw):
        raise ValueError("An app password is exactly 16 letters (Google shows it in four groups of four). "
                         f"What you pasted has {len(pw)} characters: copy it again from Google's window. "
                         "Your normal Google password won't work here.")
    try:
        mailimap.test_login(email, pw)
    except Exception as e:
        raise ValueError(mailimap.explain(e)) from e
    secrets.set("GMAIL_APP_PASSWORD", pw)

    def upd(raw):
        raw.setdefault("mail", {})["backend"] = "imap"
        raw.setdefault("profile", {})["email"] = email
    _edit_config(upd)
    _imap_cache.update(t=time.time(), v={"state": "ok", "detail": ""}, k=(email, pw))
    _gmail_cache["t"] = 0.0
    return {"ok": True, "email": email}


def gmail_mode(mode: str) -> dict:
    """Switch between the app password ('imap') and Google sign-in ('gmail_api')."""
    if mode not in ("imap", "gmail_api"):
        raise ValueError("mode must be imap or gmail_api")
    _edit_config(lambda raw: raw.setdefault("mail", {}).__setitem__("backend", mode))
    _gmail_cache["t"] = 0.0
    return {"ok": True, "backend": mode}


def gmail_forget_app_password() -> dict:
    secrets.delete("GMAIL_APP_PASSWORD")
    _imap_cache.update(t=0.0, v=None, k=None)
    return {"ok": True}


# ---------------- free job-board keys: test right after saving ----------------

KEY_HELP = {
    "jooble": "Jooble sends the key by email after you fill in the form; it's a long mix of letters, numbers and "
              "dashes. Copy it again from that email.",
    "careerjet": "Use the API key from your Careerjet publisher account page (not your password).",
    "findwork": "Use the API key from your findwork.dev account page (not your password).",
}


def source_test(key: str, cfg: dict | None = None) -> dict:
    """One small request to check a saved key. {'ok': bool, 'detail': plain words}."""
    from .sources.base import UA
    cfg = cfg or load_config()
    k = {"jooble": "JOOBLE_API_KEY", "careerjet": "CAREERJET_API_KEY", "findwork": "FINDWORK_API_KEY"}.get(key)
    if not k:
        return {"ok": True, "detail": "nothing to test"}
    val = env(k).strip()
    if not val:
        return {"ok": False, "detail": "No key saved yet."}
    try:
        if key == "jooble":
            ep = ((cfg.get("sources") or {}).get("jooble") or {}).get("endpoint", "https://es.jooble.org/api/")
            r = requests.post(ep.rstrip("/") + "/" + val, json={"keywords": "engineer", "page": 1, "ResultOnPage": 1},
                              headers={"User-Agent": UA, "Content-Type": "application/json"}, timeout=20)
        elif key == "careerjet":
            from .sources.careerjet import URL, _ip
            r = requests.get(URL, params={"locale_code": "es_ES", "keywords": "engineer", "page_size": 1,
                                          "user_ip": _ip(), "user_agent": UA},
                             auth=(val, ""), headers={"User-Agent": UA}, timeout=20)
        else:
            from .sources.findwork import URL
            r = requests.get(URL, params={"search": "engineer"},
                             headers={"User-Agent": UA, "Authorization": f"Token {val}"}, timeout=20)
    except requests.RequestException:
        return {"ok": False, "detail": "Couldn't reach the job board. Check the internet connection and press Test."}
    if r.status_code in (401, 403, 404):
        return {"ok": False, "detail": "The job board didn't accept this key. " + KEY_HELP[key]}
    if r.status_code == 429:
        return {"ok": True, "detail": "Key accepted, but today's free limit is used up. It works again tomorrow."}
    if r.status_code >= 400:
        return {"ok": False, "detail": f"The job board answered with an error ({r.status_code}). Try Test again later."}
    return {"ok": True, "detail": "Key works."}


# ---------------- recovery key saved? ----------------

RECOVERY_SAVED = "recovery_key_saved.txt"


def recovery_saved() -> bool:
    return (DATA / RECOVERY_SAVED).exists()


def mark_recovery_saved() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / RECOVERY_SAVED).write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    return {"ok": True}


# ---------------- Adzuna ----------------

def adzuna_test(cfg: dict) -> dict:
    a = (cfg.get("sources") or {}).get("adzuna") or {}
    country = (a.get("countries") or ["es"])[0]
    r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                     params={"app_id": env("ADZUNA_APP_ID"), "app_key": env("ADZUNA_APP_KEY"),
                             "results_per_page": 1, "what": "it"}, timeout=20)
    if r.status_code in (401, 403):
        return {"ok": False, "detail": "Adzuna rejected the keys"}
    r.raise_for_status()
    return {"ok": True, "detail": f"{r.json().get('count', 0)} jobs visible"}


# ---------------- Ollama ----------------

def ollama_state(cfg: dict) -> dict:
    from . import llm
    try:
        missing = llm.missing_models(cfg)
        return {"state": "ok" if not missing else "missing_models", "missing": missing, "host": llm.host(cfg),
                "pull": job("ollama_pull"), "fallbacks": llm.fallbacks()}
    except Exception as e:
        return {"state": "down", "missing": [], "host": llm.host(cfg), "detail": str(e), "pull": job("ollama_pull")}


def ollama_pull(cfg: dict) -> dict:
    from . import llm

    def go():
        todo = llm.missing_models(cfg)
        for i, m in enumerate(todo, 1):
            _jobs["ollama_pull"]["detail"] = f"pulling {m} ({i}/{len(todo)})"
            with requests.post(f"{llm.host(cfg)}/api/pull", json={"name": m, "stream": True}, stream=True,
                               timeout=(10, 3600)) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    try:
                        ev = json.loads(line or b"{}")
                    except ValueError:
                        continue
                    if ev.get("error"):
                        raise RuntimeError(f"{m}: {ev['error']}")
                    if ev.get("total") and ev.get("completed") is not None:
                        pct = int(ev["completed"] * 100 / ev["total"])
                        _jobs["ollama_pull"]["detail"] = f"pulling {m} ({i}/{len(todo)}): {pct}%"
        return "models ready"
    return _run_bg("ollama_pull", go)


def ai_advice(cfg: dict, refresh: bool = False) -> dict:
    """Local AI card: this computer's hardware, Ollama's state and the suggested models per task."""
    from . import ai_advisor, hardware, llm
    hw = hardware.detect(refresh=refresh)
    oi = hardware.ollama_info(llm.host(cfg))
    current = {t: llm.configured_model(cfg, t) for t in ("writer", "extractor", "checker", "embed")}
    plan = ai_advisor.recommend(hw, oi, current)
    gpus = [{k: g.get(k) for k in ("vendor", "name", "vram_gb", "ollama_can_use", "why", "integrated")}
            for g in hw.get("gpus", [])]
    return {"hardware": {**{k: hw.get(k) for k in ("os", "os_version", "arch", "cpu", "cores", "ram_gb", "ram_free_gb",
                                                    "unified_memory", "apple_silicon")}, "gpus": gpus},
            "ollama": {k: oi.get(k) for k in ("host", "local", "installed", "running", "version", "loaded")}
            | {"models": sorted(oi.get("models") or {})},
            "current": current, "current_ctx": {t: llm.task_ctx(cfg, t) for t in ("writer", "extractor", "checker")},
            "tuned_for": (cfg.get("llm") or {}).get("tuned_for"), "plan": plan, "pull": job("ollama_pull"),
            "start": job("ollama_start")}


def ai_apply(use_alternative: bool = False, pull: bool = True) -> dict:
    """Save the suggested models and context sizes to config.yaml, then download whatever is missing."""
    from . import ai_advisor, criteria, hardware, llm
    cfg = load_config()
    plan = ai_advisor.recommend(hardware.detect(), hardware.ollama_info(llm.host(cfg)))
    path = criteria.config_path()
    if not path.exists():
        ensure_home()
    out = ai_advisor.apply(plan, path, use_alternative=use_alternative)
    cfg = load_config()
    if pull and hardware.ollama_info(llm.host(cfg)).get("running") and llm.missing_models(cfg):
        out["pull"] = ollama_pull(cfg)
    return out


def ollama_start() -> dict:
    from . import hardware

    def go():
        if not hardware.start_ollama():
            raise RuntimeError("Ollama isn't installed here (ollama.com/download)")
        from . import llm
        h = llm.host(load_config())
        for _ in range(20):
            time.sleep(1)
            if hardware.ollama_info(h).get("running"):
                return "Ollama is running"
        raise RuntimeError("Ollama didn't answer within 20 seconds; open it from the Start menu / Applications")
    return _run_bg("ollama_start", go)


# ---------------- disk / database encryption ----------------

_disk_cache: dict = {}
DISK_CHECK_EVERY = 6 * 3600  # BitLocker/FileVault rarely change; the check starts PowerShell (~0.5 s CPU)


def disk_encryption(path: Path | None = None, refresh: bool = False) -> dict:
    """Cached disk_encryption_now(): the tray refreshes its status every 2 minutes and must not start a PowerShell
    each time. refresh=True (the Configuration page's Refresh) checks again."""
    key = str(Path(path or HOME))
    hit = _disk_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < DISK_CHECK_EVERY:
        return dict(hit[1])
    v = disk_encryption_now(path)
    _disk_cache[key] = (time.time(), v)
    return dict(v)


def disk_encryption_now(path: Path | None = None) -> dict:
    """Is the disk holding the data folder encrypted? BitLocker (Windows) or FileVault (macOS). No admin needed."""
    path = Path(path or HOME)
    try:
        if sys.platform == "win32":
            drive = (path.drive or "C:").rstrip("\\")
            ps = (f"(New-Object -ComObject Shell.Application).NameSpace('{drive}').Self"
                  f".ExtendedProperty('System.Volume.BitLockerProtection')")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            # 1 = on, 3 = encrypting, 5 = on (locked/unlocked variants); 2 = off; empty = not supported
            on = out in ("1", "3", "5")
            return {"tool": "BitLocker", "drive": drive, "on": on, "raw": out,
                    "detail": "on" if on else ("off" if out == "2" else "unknown")}
        if sys.platform == "darwin":
            out = subprocess.run(["fdesetup", "status"], capture_output=True, text=True, timeout=20).stdout.strip()
            on = "FileVault is On" in out
            return {"tool": "FileVault", "on": on, "raw": out, "detail": "on" if on else "off"}
    except Exception as e:
        return {"tool": "?", "on": False, "detail": f"could not check: {e}"}
    return {"tool": "LUKS?", "on": None, "detail": "not checked on this OS"}


def database_state() -> dict:
    from . import backup
    files = sqldb.status(DATA) if DATA.exists() else []
    return {"cipher": sqldb.HAVE_CIPHER, "enabled": sqldb.enabled(), "files": files,
            "plain_files": [f["name"] for f in files if not f["encrypted"]],
            "has_key": secrets.has("DB_KEY"), "health": backup.health(), "backups": backup.backups()}


def restore_backup(name: str) -> dict:
    """Stage a backup and restart; the swap happens at the next start (backup.run_pending_restore)."""
    from . import backup
    out = backup.request_restore(name)
    if RESTART_HOOK:
        threading.Timer(1.0, RESTART_HOOK).start()
        out["restarting"] = True
    return out


def recovery_key() -> str:
    """The DB key formatted in groups of 8 for a password manager."""
    k = sqldb.db_key(create=False)
    return "-".join(k[i:i + 8] for i in range(0, len(k), 8))


def restore_key(text: str) -> dict:
    k = "".join(c for c in text.lower() if c in "0123456789abcdef")
    if len(k) != 64:
        raise ValueError("A recovery key has 64 hex characters")
    db = DATA / "jobhunter.db"
    if db.exists() and not sqldb.is_plain(db):
        c = sqldb.sqlite.connect(str(db))
        c.execute(f"PRAGMA key = \"x'{k}'\"")
        try:
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqldb.DatabaseError as e:
            raise ValueError("That key does not open data/jobhunter.db") from e
        finally:
            c.close()
    secrets.set("DB_KEY", k)
    return {"ok": True}


def encrypt_all() -> dict:
    """Encrypt every plain .db in data/ (old backups included)."""
    out = []
    for p in sorted(DATA.glob("*.db")):
        if sqldb.is_plain(p):
            out.append(sqldb.encrypt_file(p))
    return {"encrypted": out}


# ---------------- importing an older install ----------------

def legacy_candidates() -> list[str]:
    """Older source installs (folders with config.yaml + data/jobhunter.db), newest first."""
    seen, out = set(), []
    pats = ["*/jobhunter-v3", "*/jobhunter", "jobhunter-v3", "jobhunter", "*/*/jobhunter-v3"]
    for pat in pats:
        for p in Path.home().glob(pat):
            try:
                if (p / "data" / "jobhunter.db").exists() and p.resolve() != HOME.resolve() and p not in seen:
                    seen.add(p)
                    out.append(p)
            except OSError:
                continue
    out.sort(key=lambda p: (p / "data" / "jobhunter.db").stat().st_mtime, reverse=True)
    return [str(p) for p in out]


COPY_DIRS = ["cv", "facts", "data/applications", "data/screenshots"]
COPY_FILES = ["config.yaml", "data/jobhunter.db", "data/vectors.db", "data/adzuna_last_fetch.txt",
              "data/gmail_auth_at.txt"]


def import_install(src: str | Path, wipe_secrets: bool = False) -> dict:
    """Copy an older install into HOME: settings, CVs, facts, database (encrypted on the way in) and
    letters. Secrets from its .env / token.json / credentials.json go to the keychain. The old folder is left
    working unless wipe_secrets=True (then its secret files are overwritten and deleted). Queues are not copied."""
    src = Path(src)
    if not (src / "data" / "jobhunter.db").exists():
        raise FileNotFoundError(f"No data/jobhunter.db in {src}")
    if (DATA / "jobhunter.db").exists():
        raise FileExistsError(f"{DATA / 'jobhunter.db'} already exists; not overwriting it")
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "data").mkdir(exist_ok=True)
    copied = []
    for d in COPY_DIRS:
        if (src / d).is_dir() and not (HOME / d).exists():
            shutil.copytree(src / d, HOME / d, ignore=shutil.ignore_patterns("Singleton*", "*.lock", "lockfile"))
            copied.append(d)
    for f in COPY_FILES:
        if (src / f).exists() and not (HOME / f).exists():
            if f.endswith(".db"):
                _copy_db(src / f, HOME / f)
            else:
                shutil.copy2(src / f, HOME / f)
            copied.append(f)
    sec = secrets.import_legacy(src / ".env", src / "data" / "token.json", src / "data" / "credentials.json",
                                wipe_files=wipe_secrets)
    enc = encrypt_all() if sqldb.enabled() else {"encrypted": []}
    _gmail_cache["t"] = 0.0
    return {"from": str(src), "to": str(HOME), "copied": copied, "secrets": sec, "encrypted": enc["encrypted"]}


def _copy_db(src: Path, dst: Path) -> None:
    """Consistent copy of a live (WAL) plain database, then encrypt the copy."""
    s = sqldb.sqlite.connect(str(src), timeout=30)  # plain source: no key
    out = sqldb.sqlite.connect(str(dst))
    with out:
        s.backup(out)
    out.close()
    s.close()
    if sqldb.enabled():
        sqldb.encrypt_file(dst)


def retire_legacy(src: str | Path) -> dict:
    """Wipe the plain-text secret files of an older install (after it was imported). Its database stays."""
    src = Path(src)
    wiped = []
    for f in (src / ".env", src / "data" / "token.json", src / "data" / "credentials.json"):
        if f.exists():
            secrets.wipe(f)
            if not f.exists():
                wiped.append(str(f))
    return {"wiped": wiped}


# ---------------- data folder ----------------

def set_home(path: str | Path) -> Path:
    """Point the app at another data folder (takes effect on restart)."""
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    app_config_dir().mkdir(parents=True, exist_ok=True)
    (app_config_dir() / "home.txt").write_text(str(p), encoding="utf-8")
    return p


def set_source(key: str, enabled: bool) -> dict:
    """Turn a job source on or off in config.yaml (a dated backup is kept, like the criteria page)."""
    import yaml
    from . import criteria
    if key not in SOURCES:
        raise ValueError(f"Unknown source {key!r}")
    path = criteria.config_path()
    if not path.exists():
        ensure_home()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    shutil.copy2(path, path.with_name(f"config.yaml.bak-{datetime.now():%Y%m%d-%H%M%S}"))
    src = raw.setdefault("sources", {})
    if not isinstance(src.get(key), dict):
        src[key] = {}
    src[key]["enabled"] = bool(enabled)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    return {"ok": True, "key": key, "enabled": bool(enabled)}


def ensure_home() -> None:
    """First start of the app: create the folders and a config.yaml from the example."""
    for d in ("data", "cv", "facts"):
        (HOME / d).mkdir(parents=True, exist_ok=True)
    if not (HOME / "config.yaml").exists() and (ROOT / "config.example.yaml").exists():
        shutil.copy2(ROOT / "config.example.yaml", HOME / "config.yaml")


# ---------------- summary ----------------

def status(refresh: bool = False) -> dict:
    cfg = load_config()
    from .sources import with_defaults
    src_cfg = with_defaults(cfg).get("sources") or {}
    sources = []
    for key, meta in SOURCES.items():
        s = {"key": key, **meta, "enabled": bool((src_cfg.get(key) or {}).get("enabled"))}
        if meta["auth"] == "api_key" and key != "adzuna":
            s["state"] = "ok" if env(meta["secret"]) else "missing"
        elif meta["auth"] == "api_key":
            s["state"] = "ok" if (env("ADZUNA_APP_ID") and env("ADZUNA_APP_KEY")) else "missing"
            from .sources import adzuna
            u = adzuna.usage()
            a = src_cfg.get(key) or {}
            s["usage"] = {"today": u["day_calls"], "month": u["month_calls"], "total": u["total_calls"],
                          "per_day": a.get("max_calls_per_day", 12), "per_month": a.get("max_calls_per_month", 300),
                          "blocked": adzuna.blocked(u)}
            if s["usage"]["blocked"] and s["state"] == "ok":
                s["state"] = "blocked"
        elif meta["auth"] == "via_gmail":
            s["state"] = "depends"
        else:
            s["state"] = "ok"
        sources.append(s)
    tg = (cfg.get("notify") or {}).get("telegram") or {}
    gm = gmail_state(cfg, refresh)
    db = database_state()
    setup_needed = []
    if gm["state"] in ("no_client", "missing", "expired"):
        setup_needed.append("gmail")
    for s in sources:
        if s["enabled"] and s["state"] == "missing" and not s.get("paid"):
            setup_needed.append(s["key"])
    if db["health"].get("ok") is False:
        setup_needed.insert(0, "db")
    if not (DATA / "jobhunter.db").exists() and legacy_candidates():
        setup_needed.append("import")
    from . import master
    if not master.exists():
        setup_needed.insert(0, "facts")
    elif cv_problem(cfg):
        setup_needed.append("cv")
    if not (cfg.get("llm") or {}).get("tuned_for"):
        setup_needed.append("ai")
    from . import criteria
    if not criteria.done(cfg):
        setup_needed.insert(0, "criteria")
    return {
        "home": str(HOME), "keychain": secrets.backend_name(), "gmail": gm, "sources": sources,
        "telegram": telegram_state(tg),
        "database": db, "disk": disk_encryption(refresh=refresh), "ollama": ollama_state(cfg),
        "legacy": legacy_candidates() if not (DATA / "jobhunter.db").exists() else [],
        "legacy_secret_files": [p for p in legacy_candidates_with_secrets()],
        "setup_needed": setup_needed, "can_restart": RESTART_HOOK is not None, "cv_problem": cv_problem(cfg),
        "last_move": _last_move(), "recovery_saved": recovery_saved(), "criteria_done": criteria.done(cfg),
        "facts": master.exists(),
    }


def _last_move() -> dict | None:
    from .desktop import move
    return move.last_result()


def change_home(path: str, mode: str) -> dict:
    """Ask for the data folder to change on the next start (mode 'move' copies your data, 'use' just switches)."""
    from .desktop import move
    job = move.request(HOME, Path(path), mode)
    if RESTART_HOOK:
        threading.Timer(1.0, RESTART_HOOK).start()
        job["restarting"] = True
    return job


def legacy_candidates_with_secrets() -> list[str]:
    out = []
    for c in legacy_candidates():
        p = Path(c)
        if any(f.exists() for f in (p / ".env", p / "data" / "token.json", p / "data" / "credentials.json")):
            out.append(c)
    return out


_cv_cache: dict = {}


def cv_problem(cfg: dict) -> str | None:
    """The CV file that applications attach (profile.cv_variants.main.attachment): missing, unreadable or still
    holding placeholders. Scoring and letters work without it, sending by email doesn't. Cached per file
    and modification time, because reading a PDF on every tray refresh is wasteful."""
    from .config import attachment_problem, cv_attachment
    try:
        p = cv_attachment(cfg, "main")
    except (KeyError, TypeError):
        return None
    try:
        stamp = (str(p), p.stat().st_mtime)
    except OSError:
        return f"CV file missing: {p}"
    if _cv_cache.get("k") != stamp:
        _cv_cache.update(k=stamp, v=attachment_problem(p))
    return _cv_cache["v"]


def needs_attention() -> list[str]:
    """Cheap check for the tray: which connections need you (no network unless the Gmail cache is stale)."""
    try:
        return status()["setup_needed"]
    except Exception as e:
        log.warning("connections check failed: %s", e)
        return []
