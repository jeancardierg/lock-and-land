"""Arbetsförmedlingen JobTech JobSearch API (Sweden). Public, no key."""
from __future__ import annotations

from typing import Iterator

from .base import Job, get_json, is_recent

URL = "https://jobsearch.api.jobtechdev.se/search"


def parse_hit(h: dict) -> Job:
    addr = h.get("workplace_address") or {}
    loc = ", ".join(x for x in [addr.get("municipality"), addr.get("region"), addr.get("country")] if x)
    app = h.get("application_details") or {}
    desc = (h.get("description") or {}).get("text") or ""
    return Job(
        source="jobtech",
        external_id=str(h.get("id")),
        title=h.get("headline") or "",
        company=(h.get("employer") or {}).get("name") or "",
        location=loc or "Sweden",
        remote=bool(h.get("remote_work")) or "distans" in desc.lower() or "remote" in desc.lower(),
        url=app.get("url") or h.get("webpage_url") or "",
        apply_email=app.get("email") or "",
        description=desc,
        posted_at=h.get("publication_date") or "",
    )


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["jobtech"]
    days = cfg["search"].get("max_age_days", 14)
    seen: set[str] = set()
    for q in cfg["search"]["queries"]:
        data = get_json(URL, params={"q": q, "limit": scfg.get("limit_per_query", 50), "offset": 0})
        for h in data.get("hits", []):
            job = parse_hit(h)
            if job["external_id"] in seen or not is_recent(job["posted_at"], days):
                continue
            seen.add(job["external_id"])
            yield job
