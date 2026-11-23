"""Hacker News "Ask HN: Who is hiring?" (monthly thread, free Algolia API, no key). Posts are free text; the
first line is usually "Company | Role | Location | REMOTE/ONSITE | ...". Mostly tech and startup jobs."""
from __future__ import annotations

import re
from typing import Iterator

from .base import Job, get_json, html_to_text, is_recent, location_ok, throttled, title_ok

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"


def latest_thread() -> dict | None:
    d = get_json(SEARCH, params={"tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5})
    for h in d.get("hits", []):
        if "who is hiring" in (h.get("title") or "").lower():
            return h
    return None


def parse_post(c: dict) -> Job | None:
    text = html_to_text(c.get("text"))
    if not text:
        return None
    first = text.splitlines()[0]
    parts = [p.strip() for p in re.split(r"\s+[|•·]\s+|\s+-\s+(?=[A-Z])", first) if p.strip()]
    if len(parts) < 2:
        return None
    low = first.lower()
    remote = "remote" in low
    role = next((p for p in parts[1:] if not re.search(r"remote|onsite|on-site|hybrid|full[- ]time|\$|€|visa", p, re.I)
                 and len(p) < 120), parts[1])
    loc = next((p for p in parts[1:] if re.search(r"remote|onsite|on-site|hybrid|,", p, re.I) and p != role), "")
    return Job(source="hnhiring", external_id=f"hn-{c.get('id')}", title=role[:160], company=parts[0][:80],
               location=loc[:160], remote=remote, url=f"https://news.ycombinator.com/item?id={c.get('id')}",
               description=text, posted_at=c.get("created_at") or "")


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["hnhiring"]
    if throttled("hnhiring", scfg.get("min_hours_between", 24)):
        return
    t = latest_thread()
    if not t:
        return
    days = max(cfg["search"].get("max_age_days", 14), 35)  # the thread is monthly
    for c in get_json(ITEM.format(id=t["objectID"])).get("children", []):
        job = parse_post(c)
        if not job:
            continue
        head = job["description"].splitlines()[0]
        if title_ok(head, cfg) and (location_ok(job["location"]) or job["remote"]) and is_recent(job["posted_at"], days):
            yield job
