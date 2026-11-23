"""Full ad text and application questions from public ATS job-board endpoints.

Adzuna gives only a snippet and LinkedIn alerts give nothing, so scores and letters were built from a few lines.
Many ads live on public ATS boards with free, documented JSON endpoints; those give the full description and
often the questions. LinkedIn, Indeed and InfoJobs are never fetched (their terms ban automated access).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from .sources.base import UA, html_to_text

log = logging.getLogger(__name__)
NEVER = re.compile(r"(^|\.)(linkedin\.com|indeed\.[a-z.]+|infojobs\.net|glassdoor\.[a-z.]+)$", re.I)
TIMEOUT = 20

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?(?P<board>[\w-]+)/jobs/(?P<id>\d+)")),
    ("lever", re.compile(r"^jobs(?:\.eu)?\.lever\.co/(?P<board>[\w.-]+)/(?P<id>[0-9a-f-]{36})")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com/(?P<board>[\w.%-]+)/(?P<id>[0-9a-f-]{36})")),
    ("workable", re.compile(r"^apply\.workable\.com/(?P<board>[\w-]+)/j/(?P<id>[0-9A-Z]+)")),
    ("smartrecruiters", re.compile(r"^(?:jobs|careers)\.smartrecruiters\.com/(?P<board>[\w-]+)/(?P<id>\d+)")),
    ("personio", re.compile(r"^(?P<board>[\w-]+)\.jobs\.personio\.(?:de|com)/job/(?P<id>\d+)")),
    ("teamtailor", re.compile(r"^(?P<board>[\w-]+)\.teamtailor\.com/jobs/(?P<id>\d+)")),
    ("workday", re.compile(r"^(?P<board>[\w-]+)\.(?:wd\d+\.)?myworkdayjobs\.com/(?P<id>.+)")),
    ("successfactors", re.compile(r"^(?P<board>[\w.-]*(?:successfactors|sapsf)\.[\w.]+)/(?P<id>.*)")),
    ("recruitee", re.compile(r"^(?P<board>[\w-]+)\.recruitee\.com/o/(?P<id>[\w-]+)")),
    ("breezy", re.compile(r"^(?P<board>[\w-]+)\.breezy\.hr/p/(?P<id>[\w-]+)")),
]


def detect(url: str | None) -> tuple[str | None, dict]:
    if not url:
        return None, {}
    u = urlparse(url)
    host = (u.netloc or "").lower()
    if NEVER.search(host):
        return ("linkedin" if "linkedin" in host else "blocked"), {}
    hp = f"{host}{u.path}"
    for name, rx in PATTERNS:
        m = rx.match(hp)
        if m:
            return name, m.groupdict()
    q = parse_qs(u.query)
    if "gh_jid" in q:  # company careers page embedding Greenhouse
        return "greenhouse_embed", {"id": q["gh_jid"][0]}
    return None, {}


def public_url(url: str) -> bool:
    """Ad links come from outside. Only fetch http(s) on public addresses, never this computer or the home network
    (router pages, Ollama on :11434, a NAS): a planted link must not make the app read local services."""
    import ipaddress
    import socket
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


def _get(url: str, **kw) -> requests.Response:
    if not public_url(url):
        raise ValueError(f"not fetching a non-public link: {url[:120]}")
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"}, timeout=TIMEOUT,
                     allow_redirects=True, **kw)
    r.raise_for_status()
    return r


def resolve(url: str) -> str:
    """Follow aggregator redirects (Adzuna 'land' pages) to the employer URL when the page says where it goes."""
    host = urlparse(url).netloc.lower()
    if NEVER.search(host):
        return url
    try:
        r = _get(url)
    except Exception as e:
        log.info("resolve %s failed: %s", url, e)
        return url
    final = r.url
    if "adzuna" in urlparse(final).netloc and "text/html" in r.headers.get("content-type", ""):
        soup = BeautifulSoup(r.text, "html.parser")
        meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
        if meta and "url=" in (meta.get("content") or "").lower():
            final = meta["content"].split("=", 1)[1].strip("'\" ")
        else:
            m = re.search(r"""(?:window\.)?location(?:\.href)?\s*=\s*["'](https?://[^"']+)""", r.text)
            if m:
                final = m.group(1)
    return final


def jsonld_posting(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and it.get("@type") in ("JobPosting", ["JobPosting"]):
                return it
    return None


def _questions_greenhouse(qs: list[dict]) -> list[dict]:
    out = []
    for q in qs or []:
        fields = q.get("fields") or [{}]
        f = fields[0]
        t = f.get("type", "")
        kind = {"input_file": "file", "multi_value_single_select": "choice", "multi_value_multi_select": "choice",
                "textarea": "free_text", "input_text": "free_text"}.get(t, "free_text")
        out.append({"text": q.get("label", ""), "required": bool(q.get("required")), "kind": kind,
                    "options": [v.get("label") for v in f.get("values") or []], "name": f.get("name")})
    return out


def fetch_full(ats: str, ids: dict, url: str) -> dict[str, Any]:
    """Return {'description': str, 'questions': [...], 'title': str?}; empty dict when nothing better is available."""
    b, i = ids.get("board"), ids.get("id")
    if ats == "greenhouse":
        d = _get(f"https://boards-api.greenhouse.io/v1/boards/{b}/jobs/{i}", params={"questions": "true"}).json()
        return {"description": html_to_text(BeautifulSoup(d.get("content", ""), "html.parser").get_text()),
                "questions": _questions_greenhouse(d.get("questions")), "title": d.get("title")}
    if ats == "lever":
        d = _get(f"https://api.lever.co/v0/postings/{b}/{i}").json()
        parts = [d.get("descriptionPlain", "")]
        for lst in d.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}")
        parts.append(d.get("additionalPlain", ""))
        return {"description": "\n\n".join(p for p in parts if p), "questions": [], "title": d.get("text")}
    if ats == "ashby":
        d = _get(f"https://api.ashbyhq.com/posting-api/job-board/{b}").json()
        for j in d.get("jobs") or []:
            if j.get("id") == i or str(j.get("jobUrl", "")).rstrip("/").endswith(i):
                return {"description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                        "questions": [], "title": j.get("title")}
        return {}
    if ats == "workable":
        d = _get(f"https://apply.workable.com/api/v2/accounts/{b}/jobs/{i}").json()
        desc = "\n\n".join(html_to_text(d.get(k, "")) for k in ("description", "requirements", "benefits"))
        return {"description": desc, "questions": [], "title": d.get("title")}
    if ats == "smartrecruiters":
        d = _get(f"https://api.smartrecruiters.com/v1/companies/{b}/postings/{i}").json()
        secs = (d.get("jobAd") or {}).get("sections") or {}
        desc = "\n\n".join(html_to_text((secs.get(k) or {}).get("text", "")) for k in
                           ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))
        return {"description": desc, "questions": [], "title": d.get("name")}
    if ats == "personio":
        xml = _get(f"https://{b}.jobs.personio.de/xml").text
        soup = BeautifulSoup(xml, "html.parser")
        for pos in soup.find_all("position"):
            if (pos.find("id").get_text(strip=True) if pos.find("id") else "") == i:
                desc = "\n\n".join(html_to_text(v.get_text()) for v in pos.find_all("value"))
                return {"description": desc, "questions": [], "title": pos.find("name").get_text(strip=True)}
        return {}
    # Teamtailor, Workday, Recruitee, Breezy, company sites: schema.org JobPosting in the page
    r = _get(url)
    jp = jsonld_posting(r.text)
    if jp:
        return {"description": html_to_text(jp.get("description", "")), "questions": [], "title": jp.get("title")}
    return {}


def enrich(job: dict, cfg: dict | None = None) -> dict[str, Any]:
    """Best effort. Returns fields to update: ats, apply_url, description (only if longer), questions, full_text."""
    url = job.get("url") or ""
    out: dict[str, Any] = {"questions": []}
    if job.get("apply_email"):
        out["ats"] = "email"
    if not url:
        return out
    ats, ids = detect(url)
    if ats is None and "adzuna" in urlparse(url).netloc:
        url = resolve(url)
        ats, ids = detect(url)
    if ats in ("linkedin", "blocked"):
        out["ats"] = out.get("ats") or ats
        return out
    out["apply_url"] = url
    if ats and "ats" not in out:
        out["ats"] = ats
    try:
        full = fetch_full(ats or "generic", ids, url) if ats != "greenhouse_embed" else {}
    except Exception as e:
        log.info("enrich %s (%s) failed: %s", url, ats, e)
        full = {}
    desc = (full.get("description") or "").strip()
    if len(desc) > len(job.get("description") or "") + 200:
        out["description"] = desc[:20000]
        out["full_text"] = 1
    out["questions"] = full.get("questions") or []
    if not out.get("ats"):
        out["ats"] = "other"
    return out
