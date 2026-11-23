"""Working Nomads public JSON feed (no key). One request returns the latest remote jobs of every category."""
from __future__ import annotations

from typing import Iterator

from .base import Job, get_json, html_to_text, is_recent, location_ok, throttled, title_ok

URL = "https://www.workingnomads.com/api/exposed_jobs/"


def parse_job(j: dict) -> Job:
    return Job(source="workingnomads", external_id=j.get("url") or j.get("title", ""), title=j.get("title") or "",
               company=j.get("company_name") or "", location=f"Remote ({j.get('location') or 'Anywhere'})",
               remote=True, url=j.get("url") or "", description=html_to_text(j.get("description")),
               posted_at=j.get("pub_date") or "")


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["workingnomads"]
    if throttled("workingnomads", scfg.get("min_hours_between", 12)):
        return
    days = cfg["search"].get("max_age_days", 14)
    data = get_json(URL)
    for j in data if isinstance(data, list) else data.get("jobs", []):
        job = parse_job(j)
        if title_ok(job["title"], cfg) and location_ok(j.get("location") or "") and is_recent(job["posted_at"], days):
            yield job
