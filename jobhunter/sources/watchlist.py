"""Company watchlist: the public job boards of companies you pick (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters). No keys and full ads.

config.yaml:
  sources:
    watchlist:
      enabled: true
      companies:
        - {name: "Typeform", ats: "greenhouse", board: "typeform"}
        - {name: "Example", ats: "lever", board: "example"}
Find a company's board with: jobhunter watchlist-find "Company Name"
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator

import requests

from .base import UA, Job, get_json, html_to_text, is_recent, title_ok

OK_LOC = ["barcelona", "spain", "españa", "remote", "remoto", "europe", "emea", "anywhere", "worldwide", "eu"]


def _loc_ok(loc: str, remote: bool, allowed: list[str]) -> bool:
    l = (loc or "").lower()
    if any(w in l for w in ("us only", "usa only", "united states only", "canada only")):
        return False
    return remote or not l or any(a in l for a in allowed)


def greenhouse(board: str, name: str) -> Iterator[tuple[Job, bool]]:
    import html
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        yield Job(source="watchlist", external_id=f"gh-{board}-{j.get('id')}", title=j.get("title") or "",
                  company=name, location=loc, remote="remote" in loc.lower(), url=j.get("absolute_url") or "",
                  description=html_to_text(html.unescape(j.get("content") or "")),
                  posted_at=j.get("first_published") or j.get("updated_at") or ""), "remote" in loc.lower()


def lever(board: str, name: str) -> Iterator[tuple[Job, bool]]:
    for j in get_json(f"https://api.lever.co/v0/postings/{board}", params={"mode": "json"}):
        cat = j.get("categories") or {}
        loc = cat.get("location") or ""
        remote = (j.get("workplaceType") == "remote") or "remote" in loc.lower()
        ts = j.get("createdAt")
        yield Job(source="watchlist", external_id=f"lv-{board}-{j.get('id')}", title=j.get("text") or "",
                  company=name, location=loc, remote=remote, url=j.get("hostedUrl") or "",
                  description=j.get("descriptionPlain") or html_to_text(j.get("description")),
                  posted_at=datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else ""), remote


def ashby(board: str, name: str) -> Iterator[tuple[Job, bool]]:
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    for j in d.get("jobs", []):
        loc = j.get("location") or ""
        remote = bool(j.get("isRemote")) or j.get("workplaceType") == "Remote"
        yield Job(source="watchlist", external_id=f"ab-{board}-{j.get('id')}", title=j.get("title") or "",
                  company=name, location=loc, remote=remote, url=j.get("jobUrl") or j.get("applyUrl") or "",
                  description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                  posted_at=j.get("publishedAt") or ""), remote


def workable(board: str, name: str) -> Iterator[tuple[Job, bool]]:
    d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{board}", params={"details": "true"})
    for j in d.get("jobs", []):
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        remote = bool(j.get("telecommuting")) or "remote" in loc.lower()
        yield Job(source="watchlist", external_id=f"wk-{board}-{j.get('shortcode')}", title=j.get("title") or "",
                  company=d.get("name") or name, location=loc or ("Remote" if remote else ""), remote=remote,
                  url=j.get("url") or j.get("shortlink") or "", description=html_to_text(j.get("description")),
                  posted_at=j.get("published_on") or j.get("created_at") or ""), remote


def smartrecruiters(board: str, name: str) -> Iterator[tuple[Job, bool]]:
    offset = 0
    while True:
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{board}/postings",
                     params={"limit": 100, "offset": offset})
        items = d.get("content") or []
        for j in items:
            l = j.get("location") or {}
            loc = ", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x)
            remote = bool(l.get("remote")) or "remote" in loc.lower()
            yield Job(source="watchlist", external_id=f"sr-{board}-{j.get('id')}", title=j.get("name") or "",
                      company=(j.get("company") or {}).get("name") or name, location=loc, remote=remote,
                      url=f"https://jobs.smartrecruiters.com/{board}/{j.get('id')}",
                      description=" · ".join(x.get("label", "") for x in (j.get("function"), j.get("industry"),
                                                                          j.get("experienceLevel")) if x),
                      posted_at=j.get("releasedDate") or ""), remote
        offset += len(items)
        if not items or offset >= d.get("totalFound", 0) or offset >= 500:
            break


ATS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
       "smartrecruiters": smartrecruiters}


def fetch(cfg: dict) -> Iterator[Job]:
    scfg = cfg["sources"]["watchlist"]
    days = cfg["search"].get("max_age_days", 30)
    allowed = scfg.get("allowed_locations") or OK_LOC
    errors = []
    for c in scfg.get("companies") or []:
        fn = ATS.get((c.get("ats") or "").lower())
        if not fn or not c.get("board"):
            errors.append(f"{c.get('name')}: needs ats and board")
            continue
        try:
            for job, remote in fn(c["board"], c.get("name") or c["board"]):
                if title_ok(job["title"], cfg) and _loc_ok(job["location"], remote, allowed) \
                        and (not job["posted_at"] or is_recent(job["posted_at"], scfg.get("max_age_days", 60))):
                    yield job
        except Exception as e:  # one company's board being down must not stop the rest
            errors.append(f"{c.get('name')}: {e}")
    if errors:
        raise RuntimeError("; ".join(errors)[:300])


def slug_guesses(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    return list(dict.fromkeys([base.replace(" ", ""), base.replace(" ", "-"), base.split(" ")[0]]))


def find(name: str) -> list[dict]:
    """Try the company's likely board slugs on Greenhouse, Lever and Ashby. Returns the ones that answer."""
    found = []
    probes = {"greenhouse": "https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
              "lever": "https://api.lever.co/v0/postings/{s}?mode=json&limit=1",
              "ashby": "https://api.ashbyhq.com/posting-api/job-board/{s}",
              "workable": "https://apply.workable.com/api/v1/widget/accounts/{s}",
              "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1"}
    for slug in slug_guesses(name):
        for ats, url in probes.items():
            try:
                r = requests.get(url.format(s=slug), headers={"User-Agent": UA}, timeout=15)
            except requests.RequestException:
                continue
            if r.ok:
                d = r.json()
                n = len(d) if isinstance(d, list) else (d.get("totalFound") if ats == "smartrecruiters"
                                                        else len(d.get("jobs", [])))
                if ats in ("lever", "smartrecruiters", "workable") and not n:
                    continue  # these answer an empty list for unknown boards
                found.append({"name": name, "ats": ats, "board": slug, "open_jobs": n})
    return found
