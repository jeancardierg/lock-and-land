"""The user's work-mode and pay preference:

  remote            -> the ideal: small bonus
  hybrid (Barcelona) -> fine: no change
  fully on-site     -> only worth it if the pay is above the floor (default EUR 45k gross/year):
                         stated pay >= floor  -> kept, small penalty
                         stated pay <  floor  -> filtered out
                         pay not stated       -> kept in the list, but capped below the auto-draft score

Jobs outside Barcelona that aren't remote never get here: rules.decide() sends them to 'relocation'.
"""
from __future__ import annotations

import re

DEFAULTS = {"remote_bonus": 5, "onsite_min_salary_eur": 45000, "onsite_penalty": 5,
            "work_mode": "remote_or_hybrid"}  # remote_only | remote_or_hybrid | any (setup wizard)

_NUM = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|mil)?\b", re.I)
_MONEY_NEAR = re.compile(r"(€|eur\b|euros?\b|k\s*€|salar|sueldo|retribuci|brut|gross|compensation|pay)", re.I)


def prefs(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("preferences") or {})}


def _annual(v: float | None, period: str) -> int | None:
    if not v:
        return None
    if period == "month" or (period == "unknown" and 700 <= v < 12000):
        return int(v * 12)
    if period == "hour":
        return int(v * 1760)
    return int(v) if v >= 12000 else None


def salary_from_text(text: str) -> int | None:
    """Best-effort annual figure from an ad ("45.000 - 52.000 € brutos", "€50k", "3.500 €/mes")."""
    best = None
    for m in _NUM.finditer(text or ""):
        ctx = text[max(0, m.start() - 40): m.end() + 40]
        if not _MONEY_NEAR.search(ctx):
            continue
        raw = m.group(1).replace(" ", "")
        if re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
            v = float(re.sub(r"[.,]", "", raw))
        else:
            v = float(raw.replace(",", "."))
        if m.group(2):
            v *= 1000
        tail = text[m.end(): m.end() + 25].lower()
        period = "month" if re.search(r"/\s*(mes|month)|al mes|per month|mensual", tail) else \
            "hour" if re.search(r"/\s*(h|hora|hour)|por hora|per hour", tail) else "unknown"
        a = _annual(v, period)
        if a and 12000 <= a <= 400000:
            best = max(best or 0, a)
    return best


def annual_salary(jf, job: dict) -> int | None:
    s = getattr(jf, "salary", None) if jf is not None else None
    if s and (s.max_eur or s.min_eur):
        a = _annual(s.max_eur or s.min_eur, s.period)
        if a and a >= 12000:
            return a
    return salary_from_text(" ".join(str(job.get(k) or "") for k in ("title", "description")))


_HYBRID = re.compile(r"\bh[ií]brid[oa]?\b|\bhybrid\b", re.I)
_REMOTE = re.compile(r"\bremote\b|\bremot[oa]\b|teletrabajo|work from home|\bwfh\b", re.I)
_ONSITE = re.compile(r"\bon[- ]?site\b|\bpresencial\b|in[- ]office\b", re.I)


def work_mode(jf, job: dict) -> str:
    m = (getattr(jf, "work_mode", None) or "unknown") if jf is not None else "unknown"
    if m != "unknown":
        return m
    head = " ".join(str(job.get(k) or "") for k in ("title", "location"))
    text = head + " " + str(job.get("description") or "")
    if _HYBRID.search(head) or (not _REMOTE.search(head) and _HYBRID.search(text)):
        return "hybrid"
    if job.get("remote") or _REMOTE.search(head):
        return "remote"
    if _ONSITE.search(text) and not _REMOTE.search(text):
        return "onsite"
    return "unknown"


def assess(jf, job: dict, cfg: dict) -> dict:
    """{'mode', 'salary', 'adjust', 'cap', 'filter', 'label'}. Pure: no DB."""
    p = prefs(cfg)
    mode, pay = work_mode(jf, job), annual_salary(jf, job)
    floor = int(p["onsite_min_salary_eur"])
    out = {"mode": mode, "salary": pay, "adjust": 0, "cap": None, "filter": None}
    k = lambda v: f"€{round(v / 1000)}k"  # noqa: E731
    from .config import home_city
    if p.get("work_mode") == "remote_only" and mode in ("hybrid", "onsite"):
        out.update(filter=f"{mode}: you asked for remote only", label=f"{mode} (you want remote only)")
        return out
    if mode == "remote":
        out.update(adjust=int(p["remote_bonus"]), label="remote (your first choice)")
    elif mode == "onsite" and p.get("work_mode") == "any":
        out["label"] = "on-site near you"
    elif mode == "onsite":
        if pay is None:
            draft = (cfg.get("thresholds") or {}).get("draft_score", 70)
            out.update(cap=draft - 1, label=f"fully on-site, pay not stated (needs > {k(floor)}): no automatic letter")
        elif pay >= floor:
            out.update(adjust=-int(p["onsite_penalty"]), label=f"fully on-site, pay up to {k(pay)}")
        else:
            out.update(filter=f"fully on-site and pay up to {k(pay)} is below {k(floor)}",
                       label=f"fully on-site, pay up to {k(pay)} (below {k(floor)})")
    elif mode == "hybrid":
        out["label"] = f"hybrid in {home_city(cfg)}"
    else:
        out["label"] = "work mode not stated"
    return out


def apply(base: int, a: dict) -> int:
    s = max(0, min(100, base + a["adjust"]))
    return min(s, a["cap"]) if a["cap"] is not None else s
