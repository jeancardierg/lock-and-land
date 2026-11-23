"""Findwork.dev API (free key from findwork.dev, stored in the keychain as FINDWORK_API_KEY). Aggregates tech
jobs from several boards, many remote."""
from __future__ import annotations

from typing import Iterator

from ..config import env
from .base import Job, get_json, is_recent, location_ok, throttled, title_ok

URL = "https://findwork.dev/api/jobs/"


def parse_job(j: dict) -> Job:
    return Job(source="findwork", external_id=str(j.get("id")), title=j.get("role") or "",
               company=j.get("company_name") or "", location=j.get("location") or ("Remote" if j.get("remote") else ""),
               remote=bool(j.get("remote")), url=j.get("url") or "", description=j.get("text") or "",
               posted_at=j.get("date_posted") or "")


def fetch(cfg: dict) -> Iterator[Job]:
    key = env("FINDWORK_API_KEY")
    if not key:
        raise RuntimeError("Findwork key missing: add it on the Configuration page")
    scfg = cfg["sources"]["findwork"]
    if throttled("findwork", scfg.get("min_hours_between", 24)):
        return
    days = cfg["search"].get("max_age_days", 14)
    seen: set[str] = set()
    for q in (scfg.get("queries") or cfg["search"].get("queries") or [])[:10]:
        d = get_json(URL, params={"search": q, "sort_by": "date"}, headers={"Authorization": f"Token {key}"})
        for j in d.get("results", []):
            job = parse_job(j)
            if job["external_id"] in seen:
                continue
            seen.add(job["external_id"])
            if title_ok(job["title"], cfg) and (job["remote"] or location_ok(job["location"])) \
                    and is_recent(job["posted_at"], days):
                yield job
