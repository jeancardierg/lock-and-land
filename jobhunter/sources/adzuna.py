"""Adzuna API (ES, NL, DE, AT, BE, CH, PL, ...). Free key: developer.adzuna.com"""
from __future__ import annotations

from typing import Iterator

import time

from ..config import DATA, env
from .base import Job, get_json, is_recent

URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"


def parse_result(r: dict, country: str) -> Job:
    desc = r.get("description") or ""
    title = r.get("title") or ""
    return Job(
        source="adzuna",
        external_id=f"{country}-{r.get('id')}",
        title=title,
        company=(r.get("company") or {}).get("display_name") or "",
        location=(r.get("location") or {}).get("display_name") or country.upper(),
        remote="remote" in (title + desc).lower(),
        url=r.get("redirect_url") or "",
        description=desc,  # Adzuna returns a snippet only
        posted_at=r.get("created") or "",
    )


USAGE = "adzuna_usage.json"


def usage() -> dict:
    """Calls made today / this month and any block (quota or key refused). Shown on the Configuration page."""
    import json
    from datetime import date
    p = DATA / USAGE
    try:
        u = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        u = {}
    today, month = date.today().isoformat(), date.today().isoformat()[:7]
    if u.get("day") != today:
        u.update(day=today, day_calls=0)
    if u.get("month") != month:
        u.update(month=month, month_calls=0)
    u.setdefault("total_calls", 0)
    return u


def _save(u: dict) -> None:
    import json
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / USAGE).write_text(json.dumps(u), encoding="utf-8")


def blocked(u: dict | None = None) -> str | None:
    u = u or usage()
    if u.get("blocked_until", 0) > time.time():
        return u.get("blocked_reason") or "blocked"
    return None


def _call(url: str, params: dict, u: dict, scfg: dict) -> dict | None:
    """One Adzuna request with no automatic retries (every call counts against the quota).
    401/403 = key refused (trial calls used up, plan pending): stop for 24 h. 429 = over the rate: stop for today."""
    import requests
    if u["day_calls"] >= scfg.get("max_calls_per_day", 12) or u["month_calls"] >= scfg.get("max_calls_per_month", 300):
        return None
    u["day_calls"] += 1
    u["month_calls"] += 1
    u["total_calls"] += 1
    _save(u)
    r = requests.get(url, params=params, headers={"Accept": "application/json"}, timeout=30)
    if r.status_code in (401, 403, 429):
        hours = 24 if r.status_code != 429 else 12
        u["blocked_until"] = time.time() + hours * 3600
        u["blocked_reason"] = ("Adzuna refused the key: trial calls used up or the plan isn't active"
                               if r.status_code != 429 else "Adzuna rate limit reached") + f" (HTTP {r.status_code})"
        _save(u)
        raise RuntimeError(u["blocked_reason"] + f"; pausing Adzuna for {hours} h")
    r.raise_for_status()
    u.pop("blocked_until", None)
    u.pop("blocked_reason", None)
    _save(u)
    return r.json()


def fetch(cfg: dict) -> Iterator[Job]:
    """Budgeted: at most `min_hours_between` apart, `max_calls_per_day` and `max_calls_per_month` calls.
    `what_or` (space-separated words, any may match) replaces the per-query loop with one call per page,
    which keeps a small plan (or the 25-call trial) going for weeks."""
    app_id, app_key = env("ADZUNA_APP_ID"), env("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError("Adzuna keys missing: add them on the Configuration page")
    scfg = cfg["sources"]["adzuna"]
    u = usage()
    if blocked(u):
        return
    stamp = DATA / "adzuna_last_fetch.txt"
    gap_h = scfg.get("min_hours_between", 24)
    if stamp.exists() and time.time() - float(stamp.read_text() or 0) < gap_h * 3600:
        return
    stamp.write_text(str(time.time()))
    days = cfg["search"].get("max_age_days", 14)
    base = {"app_id": app_id, "app_key": app_key, "results_per_page": scfg.get("results_per_page", 50),
            "max_days_old": days, "content-type": "application/json"}
    if scfg.get("what_or"):
        searches = [{"what_or": scfg["what_or"]}]
        pages = int(scfg.get("pages", 1))
    else:
        searches = [{"what": q} for q in cfg["search"]["queries"]]
        pages = 1
    for country in scfg.get("countries", []):
        where = (scfg.get("where") or {}).get(country)
        for srch in searches:
            for page in range(1, pages + 1):
                params = {**base, **srch, **({"where": where} if where else {})}
                data = _call(URL.format(country=country).replace("/search/1", f"/search/{page}"), params, u, scfg)
                if data is None:
                    return  # budget used up for today / this month
                results = data.get("results", [])
                for r in results:
                    job = parse_result(r, country)
                    if is_recent(job["posted_at"], days):
                        yield job
                if len(results) < base["results_per_page"]:
                    break
