"""Careerjet search API v4 (free publisher key from careerjet.com/partners, keychain CAREERJET_API_KEY). Every
sector, Spain via locale es_ES. The API wants the searcher's IP and browser; we send your public IP (looked up
once a day) and the app's name. Results are excerpts; the link goes to the ad via Careerjet."""
from __future__ import annotations

import json
import time
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Iterator

import requests

from ..config import DATA, env
from .base import UA, Job, html_to_text, is_recent, throttled

URL = "https://search.api.careerjet.net/v4/query"
USAGE = "careerjet_usage.json"


def _ip() -> str:
    f = DATA / "careerjet_ip.json"
    try:
        c = json.loads(f.read_text(encoding="utf-8"))
        if c.get("day") == date.today().isoformat():
            return c["ip"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
    except requests.RequestException:
        ip = "127.0.0.1"
    f.write_text(json.dumps({"day": date.today().isoformat(), "ip": ip}), encoding="utf-8")
    return ip


def _date(v: str) -> str:
    try:
        return parsedate_to_datetime(v).isoformat()
    except (TypeError, ValueError):
        return ""


def parse_job(j: dict) -> Job:
    text = f"{j.get('title') or ''} {j.get('description') or ''} {j.get('locations') or ''}".lower()
    return Job(source="careerjet", external_id=(j.get("url") or "")[-120:], title=html_to_text(j.get("title")),
               company=j.get("company") or "", location=j.get("locations") or "",
               remote=any(w in text for w in ("remote", "remoto", "teletrabajo", "híbrid", "hibrid", "hybrid")),
               url=j.get("url") or "", description=html_to_text(j.get("description")),
               posted_at=_date(j.get("date") or ""))


def fetch(cfg: dict) -> Iterator[Job]:
    key = env("CAREERJET_API_KEY")
    if not key:
        raise RuntimeError("Careerjet key missing: add it on the Configuration page")
    scfg = cfg["sources"]["careerjet"]
    if throttled("careerjet", scfg.get("min_hours_between", 24)):
        return
    from ..config import home_city
    days = cfg["search"].get("max_age_days", 14)
    locs = scfg.get("locations") or (cfg.get("sources", {}).get("jooble") or {}).get("locations") \
        or [x for x in [home_city(cfg)] if x != "your city"] or [""]
    ip, seen = _ip(), set()
    for loc in locs:
        for q in (scfg.get("queries") or cfg["search"].get("queries") or [])[:scfg.get("max_queries", 12)]:
            params = {"locale_code": scfg.get("locale_code", "es_ES"), "keywords": q, "sort": "date",
                      "page_size": scfg.get("page_size", 50), "user_ip": ip, "user_agent": UA}
            if loc:
                params["location"] = loc
            r = requests.get(URL, params=params, auth=(key, ""), headers={"User-Agent": UA}, timeout=30)
            if r.status_code in (401, 403):
                raise RuntimeError(f"Careerjet refused the request (HTTP {r.status_code}): check the key")
            r.raise_for_status()
            d = r.json()
            for j in d.get("jobs", []) if d.get("type") == "JOBS" else []:
                job = parse_job(j)
                if job["url"] in seen:
                    continue
                seen.add(job["url"])
                if is_recent(job["posted_at"], days):
                    yield job
            time.sleep(0.5)
