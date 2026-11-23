"""Jobicy remote jobs API (no key). Fair use: no more than one poll per hour; we do one per day by default."""
from __future__ import annotations

from typing import Iterator

from .base import Job, get_json, html_to_text, is_recent, location_ok, throttled, title_ok

URL = "https://jobicy.com/api/v2/remote-jobs"
DEFAULT_TAGS = ["sysadmin", "system administrator", "intune", "identity", "okta", "endpoint", "it manager",
                "it operations"]


def parse_job(j: dict) -> Job:
    return Job(
        source="jobicy",
        external_id=str(j.get("id")),
        title=j.get("jobTitle") or "",
        company=j.get("companyName") or "",
        location=f"Remote ({j.get('jobGeo') or 'Anywhere'})",
        remote=True,
        url=j.get("url") or "",
        description=html_to_text(j.get("jobDescription")) or j.get("jobExcerpt") or "",
        posted_at=j.get("pubDate") or "",
    )


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["jobicy"]
    if throttled("jobicy", scfg.get("min_hours_between", 24)):
        return
    days = cfg["search"].get("max_age_days", 14)
    seen: set[str] = set()
    tags = scfg.get("tags") or [q for q in cfg["search"].get("queries", []) if 3 <= len(q) <= 50] or DEFAULT_TAGS
    for tag in tags[:12]:  # fair use: keep it to a dozen calls a day
        for j in get_json(URL, params={"count": 50, "tag": tag}).get("jobs", []):
            job = parse_job(j)
            if job["external_id"] in seen:
                continue
            seen.add(job["external_id"])
            if title_ok(job["title"], cfg) and location_ok(j.get("jobGeo") or "") and is_recent(job["posted_at"], days):
                yield job
