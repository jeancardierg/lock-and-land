"""The Muse public jobs API (no key needed; an optional free key raises the limit from 500 to 3,600 calls an
hour). Every kind of job, mostly US and some Europe; we ask for your city and for remote ("Flexible / Remote").
In Nov 2026 the API answered 404 to every request; the source stays in and reports that until it is back."""
from __future__ import annotations

from typing import Iterator

import requests

from ..config import env
from .base import UA, Job, html_to_text, is_recent, location_ok, throttled, title_ok

URL = "https://www.themuse.com/api/public/jobs"


def parse_job(j: dict) -> Job:
    locs = ", ".join(l.get("name", "") for l in j.get("locations") or [])
    return Job(source="themuse", external_id=str(j.get("id")), title=j.get("name") or "",
               company=(j.get("company") or {}).get("name") or "", location=locs,
               remote="remote" in locs.lower() or "flexible" in locs.lower(),
               url=(j.get("refs") or {}).get("landing_page") or "", description=html_to_text(j.get("contents")),
               posted_at=j.get("publication_date") or "")


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["themuse"]
    if throttled("themuse", scfg.get("min_hours_between", 24)):
        return
    days = cfg["search"].get("max_age_days", 14)
    from ..config import home_city
    city = home_city(cfg)
    city = "" if city == "your city" else city
    locs = scfg.get("locations") or [x for x in [f"{city}, Spain" if city else "", "Flexible / Remote"] if x]
    key = env("MUSE_API_KEY")
    seen: set[str] = set()
    for page in range(scfg.get("pages", 5)):
        params = {"page": page, "location": locs, "descending": "true"}
        if scfg.get("categories"):
            params["category"] = scfg["categories"]
        if key:
            params["api_key"] = key
        r = requests.get(URL, params=params, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
        if r.status_code == 404:
            raise RuntimeError("The Muse API is not answering (HTTP 404); it is retried on the next fetch")
        r.raise_for_status()
        d = r.json()
        for j in d.get("results", []):
            job = parse_job(j)
            if job["external_id"] in seen:
                continue
            seen.add(job["external_id"])
            if title_ok(job["title"], cfg) and (location_ok(job["location"]) or job["remote"]) \
                    and is_recent(job["posted_at"], days):
                yield job
        if page + 1 >= d.get("page_count", 0):
            break
