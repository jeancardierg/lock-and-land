"""Hard eligibility rules applied after scoring:
1. A job that REQUIRES a language other than English or Spanish is excluded (status 'filtered').
   A language marked nice-to-have / a plus / preferred does not exclude.
2. A job that is not remote and is based outside Barcelona and its ~1 h commute area goes to 'relocation' (its own tab), not the main queue.
   Remote jobs limited to regions outside Europe (e.g. US-only) are excluded.
"""
from __future__ import annotations

import re
import unicodedata

from . import llm

RULES_V = 3
OK_LANGS = {"english", "spanish", "castellano", "español", "espanol", "inglés", "ingles", "castilian"}
OTHER_LANGS = ["german", "dutch", "french", "swedish", "norwegian", "danish", "finnish", "italian", "portuguese",
               "polish", "catalan", "czech", "hungarian", "romanian", "greek", "japanese", "chinese", "mandarin",
               "arabic", "turkish", "russian", "hebrew", "korean", "flemish"]
OPTIONAL = re.compile(r"\b(plus|nice[- ]to[- ]have|advantage|preferred|desirable|bonus|beneficial|valorable|deseable|"
                      r"would be great|is a benefit|not required|optional)\b", re.I)
REQUIRED_NEAR = re.compile(r"\b(fluent|fluency|native|business[- ]level|business fluent|proficien\w*|excellent|very good|"
                           r"strong|advanced|c1|c2|b2|required|must|mandatory|imprescindible|nivel alto)\b", re.I)
STOP = {"german": {"und", "der", "die", "das", "wir", "mit", "für", "sie", "ist", "nicht", "auf", "eine", "unsere", "bei"},
        "dutch": {"en", "het", "een", "voor", "wij", "met", "van", "zijn", "jouw", "naar", "ons", "bij", "werk"},
        "french": {"et", "les", "des", "pour", "nous", "avec", "une", "vous", "dans", "est", "sur", "votre"},
        "swedish": {"och", "att", "för", "med", "som", "det", "vi", "du", "är", "på", "av", "till"}}
NON_EU_REMOTE = re.compile(r"\b(us|usa|u\.s\.|united states|north america|canada|latam|latin america|apac|asia|"
                           r"australia|india|americas)\b", re.I)
EU_REMOTE = re.compile(r"\b(europe|eu|emea|spain|españa|worldwide|anywhere|global|cet|portugal)\b", re.I)

US_STATE = (r"(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
            r"NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)")
US_PLACE = re.compile(rf",\s*{US_STATE}\b|,\s*(?i:{US_STATE}\s+area)\b|\b{US_STATE}\s+[Aa]rea\b|\b(USA|United States)\b")
US_ONLY = re.compile(r"\b(top secret|ts/sci|security clearance|secret clearance|clearance required|"
                     r"u\.?s\.? citizens?(hip)?|green card|authori[sz]ed to work in the (us|u\.s\.|united states)|"
                     r"must (reside|live|be based) in the (us|u\.s\.|united states))\b", re.I)
GENERIC_PLACE = {"", "spain", "espana", "catalonia", "cataluna", "catalunya", "remote", "remoto", "europe", "emea", "hybrid"}

CLASSIFY_SYSTEM = """Extract facts from a job ad. Return JSON only:
{"required_languages": [spoken languages the candidate MUST know; leave out ones marked nice-to-have / plus / preferred],
 "optional_languages": [languages only mentioned as a plus],
 "work_mode": "remote" | "hybrid" | "onsite" | "unknown",
 "remote_scope": string or null (where remote workers may live, e.g. "EU", "Spain", "worldwide", "US only"),
 "office_city": string or null}
English is always fine to list. Do not guess: use "unknown" / null when the ad does not say."""


def ad_language(text: str) -> str | None:
    """Detect an ad written in German/Dutch/French/Swedish (which implies that language is required)."""
    words = re.findall(r"[a-zäöüßåéèàç]+", (text or "").lower())
    if len(words) < 40:
        return None
    best, hits = None, 0
    for lang, stop in STOP.items():
        n = sum(w in stop for w in words)
        if n > hits:
            best, hits = lang, n
    eng = sum(w in {"the", "and", "with", "for", "you", "our", "we", "your", "will", "are"} for w in words)
    return best if hits >= 12 and hits > 1.5 * eng else None


def regex_required_languages(text: str) -> list[str]:
    found = []
    for sent in re.split(r"(?<=[.!?\n])\s+|\n|•|·|;", text or ""):
        low = sent.lower()
        for lang in OTHER_LANGS:
            if re.search(rf"\b{lang}\b", low) and REQUIRED_NEAR.search(sent) and not OPTIONAL.search(sent):
                found.append(lang)
    return sorted(set(found))


def quick_language_block(job: dict, cfg: dict | None = None) -> str | None:
    """Cheap pre-LLM check. Returns a reason to exclude, or None. Languages you speak (profile.languages) never block."""
    from .config import spoken_langs
    ok = spoken_langs(cfg) if cfg and (cfg.get("profile") or {}).get("languages") else OK_LANGS
    text = f"{job.get('title') or ''}\n{job.get('description') or ''}"
    lang = ad_language(text)
    if lang and lang not in ok:
        return f"ad written in {lang.title()}"
    req = [l for l in regex_required_languages(text) if l.split()[0] not in ok]
    return f"requires {', '.join(l.title() for l in req)}" if req else None


def classify(cfg: dict, job: dict) -> dict:
    prompt = f"Title: {job.get('title')}\nLocation: {job.get('location')}\n\n{(job.get('description') or '')[:6000]}"
    return llm.ollama_json(cfg, CLASSIFY_SYSTEM, prompt)


def _langs(v) -> list[str]:
    if isinstance(v, str):
        v = [v] if v and v.lower() not in ("none", "null", "n/a") else []
    return [str(x).strip().lower() for x in (v or []) if str(x).strip()]


def _fold(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", t or "") if not unicodedata.combining(c)).lower()


def is_local(text: str, cfg: dict) -> bool:
    """Is this place in your commute area? Accepts the short and other-language forms ads use for your towns
    ("Hospitalet", "Sant Cugat", "San Cugat del Vallés", "Gerona"), see places.py."""
    from . import places
    towns = tuple(str(c) for c in cfg.get("rules", {}).get("home_cities") or ["barcelona"])
    return bool(_local_re(towns).search(places.fold(text)))


_LOCAL: dict = {}


def _local_re(towns: tuple[str, ...]):
    from . import geo, places
    try:
        al = geo.aliases()
    except Exception:
        al = {}
    key = (towns, tuple(sorted((k, tuple(v)) for k, v in al.items() if k in {places.fold(t) for t in towns})))
    if key not in _LOCAL:
        if len(_LOCAL) > 32:
            _LOCAL.clear()
        _LOCAL[key] = places.local_re(list(towns), al)
    return _LOCAL[key]


def _clean(v) -> str:
    v = str(v or "").strip()
    return "" if v.lower() in ("null", "none", "unknown", "n/a") else v


def place_is_local(job: dict, info: dict, cfg: dict) -> bool:
    """Local only if the office (when the ad names one) AND the most specific part of the listing's
    location are in the commute area. Adzuna appends the province ("Bellprat, Anoia, Barcelona"),
    so only the first part of the location counts."""
    office = _clean(info.get("office_city"))
    if office and not is_local(office, cfg):
        return False
    loc = job.get("location") or ""
    first = loc.split(",")[0].strip()
    if _fold(first) in GENERIC_PLACE:
        return bool(office) or is_local(loc, cfg)
    return is_local(first, cfg)


def us_only(job: dict, info: dict) -> str | None:
    text = f"{job.get('title') or ''}\n{job.get('description') or ''}"
    m = US_ONLY.search(text)
    if m:
        return f"US-only ({m.group(0)})"
    head = f"{job.get('title') or ''} | {job.get('location') or ''} | {_clean(info.get('office_city'))}"
    if US_PLACE.search(head):
        return "based in the US"
    return None


def decide(job: dict, info: dict, cfg: dict) -> tuple[str | None, str]:
    """Return (status_override, reason). status_override is 'filtered', 'relocation' or None (main queue)."""
    from .config import spoken_langs
    ok = spoken_langs(cfg) if (cfg.get("profile") or {}).get("languages") else OK_LANGS
    blocked = [l for l in _langs(info.get("required_languages")) if l.split()[0] not in ok]
    if not blocked:
        lr = info.get("language_required")  # older scoring output
        blocked = [l for l in _langs(lr) if any(o in l for o in OTHER_LANGS) and l.split()[0] not in ok]
    rx = quick_language_block(job, cfg)
    if blocked or rx:
        return "filtered", "language: " + (", ".join(b.title() for b in blocked) if blocked else rx)

    us = us_only(job, info)
    mode = str(info.get("work_mode") or "unknown").lower()
    remote = mode == "remote" or (bool(job.get("remote")) and mode in ("unknown", "remote"))
    if us and (remote or us.startswith("US-only")):
        return "filtered", us
    if remote:
        scope = str(info.get("remote_scope") or "")
        if scope and NON_EU_REMOTE.search(scope) and not EU_REMOTE.search(scope):
            return "filtered", f"remote only for {scope}"
        return None, ""
    if place_is_local(job, info, cfg):
        return None, ""
    from .config import home_city
    city = _clean(info.get("office_city")) or job.get("location") or f"outside {home_city(cfg)}"
    return "relocation", f"{mode if mode != 'unknown' else 'on-site/hybrid'} in {city}"
