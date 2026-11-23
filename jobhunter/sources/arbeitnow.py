"""Arbeitnow job board API (Germany + remote Europe). Public, no key."""
from __future__ import annotations

from typing import Iterator

from .base import Job, get_json, html_to_text, is_recent, title_ok

URL = "https://www.arbeitnow.com/api/job-board-api"


def parse_job(j: dict) -> Job:
    return Job(
        source="arbeitnow",
        external_id=j.get("slug") or j.get("url"),
        title=j.get("title") or "",
        company=j.get("company_name") or "",
        location=j.get("location") or "Germany",
        remote=bool(j.get("remote")),
        url=j.get("url") or "",
        description=html_to_text(j.get("description")),
        posted_at=j.get("created_at") or "",
    )


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["arbeitnow"]
    pages, remote_only = scfg.get("pages", 3), scfg.get("remote_only", True)
    days = cfg["search"].get("max_age_days", 14)
    for page in range(1, pages + 1):
        data = get_json(URL, params={"page": page})
        items = data.get("data", [])
        if not items:
            break
        for j in items:
            job = parse_job(j)
            if (job["remote"] or not remote_only) and title_ok(job["title"], cfg) and is_recent(job["posted_at"], days):
                yield job
