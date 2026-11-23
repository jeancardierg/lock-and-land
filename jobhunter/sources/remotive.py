"""Remotive public API (remote jobs). Their terms ask for low request volume and a link back."""
from __future__ import annotations

from typing import Iterator

from .base import Job, get_json, html_to_text, is_recent

URL = "https://remotive.com/api/remote-jobs"


def location_ok(loc: str, allowed: list[str]) -> bool:
    l = (loc or "").lower()
    return not l or any(a in l for a in allowed)


def parse_job(j: dict) -> Job:
    return Job(
        source="remotive",
        external_id=str(j.get("id")),
        title=j.get("title") or "",
        company=j.get("company_name") or "",
        location=j.get("candidate_required_location") or "Remote",
        remote=True,
        url=j.get("url") or "",
        description=html_to_text(j.get("description")),
        posted_at=j.get("publication_date") or "",
    )


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["remotive"]
    allowed = [a.lower() for a in scfg.get("allowed_locations", [])]
    days = cfg["search"].get("max_age_days", 14)
    jobs: list[dict] = []
    if scfg.get("categories"):
        for cat in scfg["categories"]:  # one request per category; their terms ask for low volume
            jobs += get_json(URL, params={"category": cat}).get("jobs", [])
    else:  # no categories set: search by your roles instead (any kind of job)
        for q in cfg["search"].get("queries", [])[:6]:
            jobs += get_json(URL, params={"search": q, "limit": 50}).get("jobs", [])
        jobs = list({j.get("id"): j for j in jobs}.values())
    for j in jobs:
        job = parse_job(j)
        if location_ok(job["location"], allowed) and is_recent(job["posted_at"], days):
            yield job
