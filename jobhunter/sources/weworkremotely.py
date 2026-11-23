"""We Work Remotely: public RSS of all remote jobs plus the DevOps & Sysadmin category (no key)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Iterator

import requests

from .base import UA, Job, html_to_text, is_recent, location_ok, throttled, title_ok

FEEDS = ["https://weworkremotely.com/remote-jobs.rss",  # all categories; the title gate picks your roles
         "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"]


def parse_item(it: ET.Element) -> Job:
    g = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
    raw = g("title")
    company, _, title = raw.partition(": ")
    if not title:
        company, title = "", raw
    try:
        posted = parsedate_to_datetime(g("pubDate")).isoformat()
    except (TypeError, ValueError):
        posted = ""
    region = g("region")
    return Job(source="weworkremotely", external_id=g("guid") or g("link"), title=title, company=company,
               location=f"Remote ({region})" if region else "Remote", remote=True, url=g("link"),
               description=html_to_text(g("description")), posted_at=posted)


def parse_feed(xml: bytes) -> list[tuple[Job, str]]:
    root = ET.fromstring(xml)
    return [(parse_item(it), (it.findtext("region") or "")) for it in root.iter("item")]


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["weworkremotely"]
    if throttled("weworkremotely", scfg.get("min_hours_between", 12)):
        return
    days = cfg["search"].get("max_age_days", 14)
    seen: set[str] = set()
    for url in scfg.get("feeds") or FEEDS:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            items = parse_feed(r.content)
        except Exception as e:  # one feed down must not stop the other
            import logging
            logging.getLogger(__name__).warning("wwr feed %s: %s", url, e)
            continue
        for job, region in items:
            if job["external_id"] in seen:
                continue
            seen.add(job["external_id"])
            if title_ok(job["title"], cfg) and location_ok(region) and is_recent(job["posted_at"], days):
                yield job
