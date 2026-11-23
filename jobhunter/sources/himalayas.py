"""Himalayas remote jobs API (no key). Terms: credit Himalayas, use its applicationLink, don't republish listings.
Data refreshes daily, so this runs at most once per `min_hours_between` (default 24)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from .base import Job, get_json, html_to_text, is_recent, location_ok, throttled, title_ok

URL = "https://himalayas.app/jobs/api/search"


def _locs(j: dict) -> str:
    """locationRestrictions comes as ["Spain", ...] (seen live) or [{"name": "Spain"}, ...] (documented)."""
    return ", ".join(r if isinstance(r, str) else (r or {}).get("name", "") for r in (j.get("locationRestrictions") or []))


def parse_job(j: dict) -> Job:
    locs = _locs(j)
    pub = j.get("pubDate")
    return Job(
        source="himalayas",
        external_id=str(j.get("guid") or j.get("applicationLink")),
        title=j.get("title") or "",
        company=j.get("companyName") or "",
        location=f"Remote ({locs})" if locs else "Remote (worldwide)",
        remote=True,
        url=j.get("applicationLink") or "",
        description=html_to_text(j.get("description")) or j.get("excerpt") or "",
        posted_at=datetime.fromtimestamp(pub, timezone.utc).isoformat() if isinstance(pub, (int, float)) else "",
    )


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["himalayas"]
    if throttled("himalayas", scfg.get("min_hours_between", 24)):
        return
    days = cfg["search"].get("max_age_days", 14)
    seen: set[str] = set()
    for q in scfg.get("queries") or cfg["search"]["queries"]:
        params = {"q": q, "sort": "recent"}
        if scfg.get("country"):
            params["country"] = scfg["country"]  # jobs open to this country (worldwide ones included)
        for j in get_json(URL, params=params).get("jobs", []):
            job = parse_job(j)
            if job["external_id"] in seen:
                continue
            seen.add(job["external_id"])
            locs = _locs(j)
            if title_ok(job["title"], cfg) and location_ok(locs) and is_recent(job["posted_at"], days):
                yield job
