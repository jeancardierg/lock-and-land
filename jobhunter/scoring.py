"""Cheap keyword prefilter, then local Ollama fit score (0-100) plus a small keyword bonus."""
from __future__ import annotations

import logging
import re

from . import llm

log = logging.getLogger(__name__)

SYSTEM_HEAD = """You are a strict recruiter screening jobs for one candidate.
Score how well the job fits the candidate's CV and preferences, 0-100:
90+ near-perfect, 70-89 strong, 50-69 possible stretch, <50 poor.
Penalize: {penalize}.
Judge only from the CANDIDATE information below. It is verified; don't assume skills it doesn't mention.
Return JSON: {"score": int, "reasons": [short strings], "red_flags": [short strings],
"seniority": "junior"|"mid"|"senior"|"lead"|"unknown"}"""
SYSTEM_TAIL = SYSTEM_HEAD[SYSTEM_HEAD.index("Return JSON:"):]


def system_prompt(cfg: dict) -> str:
    """The penalties come from your criteria (setup wizard): languages, things to avoid, home city."""
    from .config import home_city
    p, t = cfg.get("profile") or {}, cfg.get("targets") or {}
    langs = ", ".join(str(l).split("(")[0].strip() for l in p.get("languages") or []) or "English"
    avoid = t.get("avoid")
    if avoid is None:  # configs from before the wizard: the original default penalties
        avoid = ["pure software development", "first-line-only helpdesk"]
    pen = [f"required languages the candidate lacks (they speak only {langs})", "seniority far above or below",
           *avoid, f"onsite or hybrid outside {home_city(cfg)}", "stated pay clearly below the candidate's minimum"]
    return SYSTEM_HEAD.split("Penalize:")[0] + "Penalize: " + ", ".join(pen) + ".\n" + SYSTEM_TAIL


SYSTEM = system_prompt({"profile": {"languages": ["English", "Spanish"]}, "rules": {"home_cities": ["barcelona"]}})


def _term_hit(t: str, text: str) -> bool:
    # short all-caps terms like "IT"/"ABM" must match as a whole word, case-sensitive
    if len(t) <= 3:
        return bool(re.search(rf"\b{re.escape(t)}\b", text))
    return re.search(rf"(?<![A-Za-z]){re.escape(t.lower())}(?![A-Za-z])", text.lower()) is not None


def prefilter(job: dict, cfg: dict) -> tuple[bool, str]:
    s = cfg.get("search") or {}
    title = (job["title"] or "").lower()
    text = f"{job['title']} {job.get('description') or ''}"
    for bad in s.get("exclude_title", []):
        if bad.lower() in title:
            return False, f"excluded title: {bad}"
    terms = s.get("must_have_any", [])
    if terms:
        if any(_term_hit(t, text) for t in terms):
            return True, ""
        return False, "no target keywords"
    return True, ""


def keyword_hits(job: dict, cfg: dict) -> list[str]:
    text = f"{job.get('title') or ''} {job.get('description') or ''}"
    return [k for k in (cfg.get("targets", {}).get("keywords") or []) if _term_hit(k, text)]


def candidate_text(prof, facts: list | None = None, max_facts: int = 12) -> str:
    """What the score sees about the candidate: identity, verified skills and the facts retrieved for this job.
    Smaller than the raw CV (about 40% fewer prompt tokens) and the same verified facts the letter may use."""
    i = prof.identity or {}
    lines = [f"Current title (official): {i.get('current_title_official') or '?'}"
             + (f"; scope: {i['current_scope']}" if i.get("current_scope") else ""),
             f"Years in the field: {i.get('years_corporate_it') or i.get('years_experience') or '?'}",
             f"Location / work mode: {i.get('location') or '?'}; {i.get('work_mode') or ''}".rstrip("; "),
             f"Languages: {', '.join(i.get('languages') or []) or '?'}",
             f"Certifications: {', '.join(i.get('certifications') or []) or 'none'}"]
    tools = sorted(prof.verified_tools())
    if tools:
        lines.append("Verified tools and skills: " + ", ".join(tools[:60]))
    chosen = list(facts or [])[:max_facts] or list(prof.facts)[:max_facts]
    lines.append("Facts most relevant to this job:")
    lines += [f"- {f.text}" for f in chosen]
    return "\n".join(lines)


def build_prompt(job: dict, cv: str, cfg: dict) -> str:
    p, t = cfg["profile"], cfg.get("targets", {})
    # CV + ad must fit the extractor's context (a 4k context on smaller PCs): split what's left between them,
    # at most 6000 characters each as before. The system prompt and the header take ~700 tokens.
    room = llm.char_budget(cfg, "extractor", reserve_tokens=700)
    each = max(1500, min(6000, room // 2))
    desc = (job.get("description") or "")[:each]
    cv = cv[:each]
    return f"""CANDIDATE PREFERENCES
Primary target: {t.get('primary')}
Secondary target: {t.get('secondary')}
Markets: {t.get('markets') or p.get('work_mode')}
Minimum salary: EUR {p.get('min_salary_eur') or 'n/a'} gross/year
Languages: {', '.join(p.get('languages', []))}

CANDIDATE
{cv}

JOB
Title: {job['title']}
Company: {job.get('company')}
Location: {job.get('location')} | Remote: {bool(job.get('remote'))}
Description:
{desc}"""


def score_job(job: dict, cv: str, cfg: dict) -> tuple[int, dict]:
    from .schemas import SCORE_SCHEMA
    out = llm.ollama_json(cfg, system_prompt(cfg), build_prompt(job, cv, cfg), schema=SCORE_SCHEMA, temperature=0.0)
    try:
        base = int(out.get("score", 0))
    except (TypeError, ValueError):
        base = 0
    hits = keyword_hits(job, cfg)
    bonus = min(10, 2 * len(hits))
    out.update(llm_score=base, keyword_hits=hits, keyword_bonus=bonus)
    return max(0, min(100, base + bonus)), out
