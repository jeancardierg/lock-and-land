"""RemoteOK public JSON feed (no key). Their terms: link back to the ad on RemoteOK, which every job URL does.
One request returns the latest few hundred remote jobs of every kind; we keep the ones matching your titles."""
from __future__ import annotations

from typing import Iterator

from .base import Job, get_json, html_to_text, is_recent, location_ok, throttled, title_ok

URL = "https://remoteok.com/api"


def parse_job(j: dict) -> Job:
    return Job(source="remoteok", external_id=str(j.get("id") or j.get("slug")), title=j.get("position") or "",
               company=j.get("company") or "", location=f"Remote ({j.get('location') or 'Anywhere'})", remote=True,
               url=j.get("url") or f"https://remoteok.com/remote-jobs/{j.get('slug')}",
               description=html_to_text(j.get("description")), posted_at=j.get("date") or "")


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["remoteok"]
    if throttled("remoteok", scfg.get("min_hours_between", 12)):
        return
    days = cfg["search"].get("max_age_days", 14)
    for j in get_json(URL):
        if not isinstance(j, dict) or "position" not in j:  # first item is their legal notice
            continue
        job = parse_job(j)
        if title_ok(job["title"], cfg) and location_ok(j.get("location") or "") and is_recent(job["posted_at"], days):
            yield job
