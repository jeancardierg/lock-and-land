"""Jooble REST API (free key per country, 100 requests/day). Spain key from es.jooble.org/api/about, stored in the
keychain as JOOBLE_API_KEY (Configuration page). Aggregates many Spanish boards; returns snippets, not full ads."""
from __future__ import annotations

import json
import time
from datetime import date
from typing import Iterator

import requests

from ..config import DATA, env
from .base import UA, Job, html_to_text, is_recent, throttled

USAGE = "jooble_usage.json"


def _usage() -> dict:
    try:
        u = json.loads((DATA / USAGE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        u = {}
    if u.get("day") != date.today().isoformat():
        u.update(day=date.today().isoformat(), calls=0)
    return u


def parse_job(j: dict) -> Job:
    text = f"{j.get('title') or ''} {j.get('snippet') or ''} {j.get('location') or ''}".lower()
    return Job(source="jooble", external_id=str(j.get("id")), title=html_to_text(j.get("title")) or "",
               company=j.get("company") or "", location=j.get("location") or "",
               remote=any(w in text for w in ("remote", "remoto", "teletrabajo", "híbrid", "hibrid", "hybrid")),
               url=j.get("link") or "", description=html_to_text(j.get("snippet")),
               posted_at=(j.get("updated") or "")[:19])


def fetch(cfg: dict) -> Iterator[Job]:
    key = env("JOOBLE_API_KEY")
    if not key:
        raise RuntimeError("Jooble key missing: add it on the Configuration page")
    scfg = cfg["sources"]["jooble"]
    if throttled("jooble", scfg.get("min_hours_between", 24)):
        return
    endpoint = scfg.get("endpoint", "https://es.jooble.org/api/").rstrip("/") + "/"
    days = cfg["search"].get("max_age_days", 14)
    budget = scfg.get("max_calls_per_day", 60)
    u = _usage()
    seen: set[str] = set()
    for loc in scfg.get("locations", ["Barcelona", "España"]):
        for q in scfg.get("queries") or cfg["search"]["queries"]:
            if u["calls"] >= budget:
                return
            u["calls"] += 1
            (DATA / USAGE).write_text(json.dumps(u), encoding="utf-8")
            r = requests.post(endpoint + key, json={"keywords": q, "location": loc, "page": 1,
                                                     "ResultOnPage": scfg.get("results_per_page", 50)},
                              headers={"User-Agent": UA, "Content-Type": "application/json"}, timeout=30)
            if r.status_code in (403, 429):
                raise RuntimeError(f"Jooble refused the request (HTTP {r.status_code}: "
                                   f"{'bad key' if r.status_code == 403 else 'daily limit'})")
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                job = parse_job(j)
                if job["external_id"] in seen:  # the same ad comes back for several queries / locations
                    continue
                seen.add(job["external_id"])
                if is_recent(job["posted_at"], days):
                    yield job
            time.sleep(0.5)
