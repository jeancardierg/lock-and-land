from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

import requests
from bs4 import BeautifulSoup

UA = "LockAndLand/0.4 (personal desktop job-search app; low volume)"  # no name or account in it


class Job(TypedDict, total=False):
    source: str
    external_id: str
    title: str
    company: str
    location: str
    remote: bool
    url: str
    apply_email: str
    description: str
    posted_at: str


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def get_json(url: str, params: dict | None = None, headers: dict | None = None, retries: int = 2) -> Any:
    h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=h, timeout=30)
            if r.status_code in (429, 503) and i < retries:
                time.sleep(retry_after(r.headers.get("Retry-After"), 5 * (i + 1)))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            if i < retries:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def retry_after(value: str | None, default: float, cap: float = 60) -> float:
    """Seconds to wait from a Retry-After header (seconds or an HTTP date), capped, plus a little jitter."""
    import random
    from email.utils import parsedate_to_datetime
    wait = default
    if value:
        try:
            wait = float(value)
        except ValueError:
            try:
                wait = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                wait = default
    return max(0.5, min(cap, wait)) + random.uniform(0, 1)


def cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def parse_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def is_recent(v: Any, days: int) -> bool:
    d = parse_dt(v)
    return d is None or d >= cutoff(days)


# Title gate for broad boards (they list every kind of job): config `search.title_any` (the setup wizard's
# "Job title words"), else your search queries. DEFAULT_TITLE_ANY is only the wizard's suggestion for IT roles.
DEFAULT_TITLE_ANY = [
    "sysadmin", "system administrator", "systems administrator", "system engineer", "systems engineer",
    "administrador de sistemas", "técnico de sistemas", "ingeniero de sistemas", "endpoint", "intune", "identity",
    "iam", "okta", "entra", "azure ad", "workplace", "it operations", "it ops", "it lead", "it manager",
    "head of it", "it engineer", "it support engineer", "desktop engineer", "mdm", "jamf", "infrastructure",
    "it administrator", "microsoft 365", "m365", "end user computing", "euc",
]
EU_OK = ["anywhere", "worldwide", "europe", "european union", "eu ", "(eu", "emea", "spain", "españa", "barcelona",
         "cet", "cest", "utc+1", "utc+01"]


def title_ok(title: str, cfg: dict) -> bool:
    import re
    t = f" {(title or '').lower()} "
    s = cfg.get("search", {})
    words = s.get("title_any") or s.get("queries") or DEFAULT_TITLE_ANY
    return any(re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", t) for w in words)


def location_ok(loc: str, allowed: list[str] | None = None) -> bool:
    """Empty location = unrestricted. Otherwise one of the allowed words must appear."""
    l = f" {(loc or '').lower()} "
    return not l.strip() or any(a in l for a in (allowed or EU_OK))


def throttled(name: str, hours: float) -> bool:
    """True if this source already ran within `hours` (most boards refresh daily and ask for low volume)."""
    from ..config import DATA
    stamp = DATA / f"{name}_last_fetch.txt"
    try:
        if time.time() - float(stamp.read_text() or 0) < hours * 3600:
            return True
    except (OSError, ValueError):
        pass
    DATA.mkdir(parents=True, exist_ok=True)
    stamp.write_text(str(time.time()))
    return False
