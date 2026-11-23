"""LinkedIn jobs via the job-alert emails LinkedIn sends you (Gmail). No scraping of linkedin.com.

Create alerts on LinkedIn (Jobs -> search -> 'Set alert') for each market: Sweden, Norway, Denmark,
Finland, Netherlands, Germany, Barcelona, 'European Union' + Remote. They land in Gmail daily.
"""
from __future__ import annotations

import re
from typing import Iterator

from bs4 import BeautifulSoup

from .base import Job

JOB_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
FLAGS = re.compile(r"\s*\b(Actively recruiting|Easy Apply|Promoted|Be an early applicant|\d+ (?:connections?|alumni)"
                   r"[^·]*|Fast growing|Responses managed off LinkedIn)\b\s*", re.I)


def clean(t: str) -> str:
    return " ".join(FLAGS.sub(" ", t or "").split())
# Direct alerts, plus alerts forwarded from another mailbox (the original sender then only appears in the body)
SENDERS = ('{from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com) '
           '"jobalerts-noreply@linkedin.com" "jobs-noreply@linkedin.com"}')


def parse_alert_html(html: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: dict[str, Job] = {}
    for a in soup.find_all("a", href=True):
        m = JOB_RE.search(a["href"])
        if not m:
            continue
        jid = m.group(1)
        text = a.get_text(" ", strip=True)
        if jid not in jobs:
            jobs[jid] = Job(source="linkedin", external_id=jid, title="", company="", location="",
                            url=f"https://www.linkedin.com/jobs/view/{jid}/", remote=False, description="")
        j = jobs[jid]
        # LinkedIn renders title in one link and "Company · Location" in nearby text/links
        if not j["title"] and text and len(text) < 200 and not text.lower().startswith(("view", "apply", "see")):
            j["title"] = clean(text)
            # look at the next text lines after the title anchor for company · location
            tail = []
            for s in a.find_all_next(string=True, limit=12):
                t = s.strip()
                if t and t != text:
                    tail.append(t)
                if len(tail) >= 2:
                    break
            # Newer alerts put "Title Company · Location" in one card link, followed by the title and
            # "Company · Location" as separate text nodes. Then the first tail line is the real title.
            if len(tail) >= 2 and "·" in tail[1] and j["title"].startswith(clean(tail[0])) and "·" not in tail[0]:
                j["title"] = clean(tail[0])
                tail = tail[1:]
            if tail:
                first = clean(tail[0])
                if "·" in first:
                    comp, _, loc = first.partition("·")
                    j["company"], j["location"] = comp.strip(), loc.strip()
                else:
                    j["company"] = first
                    if len(tail) > 1:
                        j["location"] = clean(tail[1])
    out = []
    for j in jobs.values():
        if not j["title"]:
            continue
        j["remote"] = "remote" in (j["location"] + j["title"]).lower()
        j["description"] = f"(LinkedIn job alert — full description on LinkedIn)\n{j['title']} at {j['company']}, {j['location']}"
        out.append(j)
    return out


def fetch(cfg: dict) -> Iterator[Job]:
    from .. import gmail
    days = cfg["sources"]["linkedin_alerts"].get("lookback_days", 7)
    for msg in gmail.search(f"{SENDERS} newer_than:{days}d", max_results=60):
        meta = gmail.message_meta(msg)
        for job in parse_alert_html(meta["html"] or ""):
            job["posted_at"] = meta["received_at"]
            yield job
