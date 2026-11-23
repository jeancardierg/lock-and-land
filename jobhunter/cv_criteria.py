"""Job criteria read from a CV, for the setup page's "fill from my CV".

Two passes, both local and free:
1. Code reads what it can be sure of: email, phone, LinkedIn URL, languages, skills (the CV's skills section and
   known tools), and years of experience from the CV's date ranges.
2. The local Ollama model (if it's running) reads the rest into a fixed JSON schema: name, current title, roles,
   city, country, seniority. Code then checks every answer against the CV text: a value the CV doesn't contain is
   dropped, never guessed. So a field is either filled from the CV or left empty for you to fill in.

Nothing is saved here: the page shows the values for you to review and save."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from . import lexicon, profile_setup

log = logging.getLogger("jobhunter.cv_criteria")

# Fields the page shows; FROM_CV are the ones a CV can answer, the rest are always yours to set.
FROM_CV = ["a_name", "a_email", "a_phone", "a_linkedin", "a_title", "roles", "primary", "secondary", "title_words",
           "seniority", "skills", "home_city", "location", "remote_regions", "languages"]
NEVER_IN_CV = ["work_mode", "min_salary", "onsite_min_salary", "commute_towns"]

LANGS = {"English": ["english", "ingles"], "Spanish": ["spanish", "espanol", "castellano"],
         "Catalan": ["catalan", "catala"], "French": ["french", "frances", "francais"],
         "German": ["german", "aleman", "deutsch"], "Italian": ["italian", "italiano"],
         "Portuguese": ["portuguese", "portugues"], "Dutch": ["dutch", "neerlandes", "nederlands"],
         "Basque": ["basque", "euskera"], "Galician": ["galician", "gallego"], "Polish": ["polish", "polaco"],
         "Romanian": ["romanian", "rumano"], "Russian": ["russian", "ruso"], "Arabic": ["arabic", "arabe"],
         "Chinese": ["chinese", "mandarin", "chino"], "Japanese": ["japanese", "japones"],
         "Swedish": ["swedish", "sueco"], "Danish": ["danish", "danes"], "Norwegian": ["norwegian", "noruego"],
         "Greek": ["greek", "griego"], "Turkish": ["turkish", "turco"], "Hindi": ["hindi"],
         "Ukrainian": ["ukrainian", "ucraniano"]}
EU = {"spain", "españa", "portugal", "france", "germany", "italy", "netherlands", "belgium", "ireland", "austria",
      "poland", "sweden", "denmark", "finland", "greece", "czechia", "czech republic", "romania", "hungary",
      "luxembourg", "slovakia", "slovenia", "croatia", "bulgaria", "estonia", "latvia", "lithuania", "malta",
      "cyprus"}
LEAD_WORDS = re.compile(r"\b(lead|head|manager|principal|director|chief|jefe|jefa|responsable|coordinador[a]?)\b", re.I)
GENERIC = {"engineer", "ingeniero", "ingeniera", "manager", "specialist", "senior", "junior", "lead", "assistant",
           "intern", "technician", "tecnico", "tecnica", "and", "the", "for", "with", "de", "del", "la", "el", "y",
           "en", "of", "in", "at", "officer", "analyst", "associate", "consultant", "head", "jefe", "mid", "level"}
SKIP_RANGE = re.compile(r"\b(universit|degree|bachelor|master|msc|bsc|phd|grado|licenciatura|school|colegio|"
                        r"instituto de educ|education|formaci[oó]n|estudios|course|curso)", re.I)
YEAR_RANGE = re.compile(r"((?:19|20)\d{2})\s*(?:[-–—]|to|a|hasta)\s*((?:19|20)\d{2}|present|current|now|today|"
                        r"actualidad|actual|presente|hoy)", re.I)

SECTIONS = {"experience", "work experience", "summary", "profile", "about me", "contact", "skills", "education",
            "experiencia", "perfil", "resumen", "contacto", "formacion", "curriculum vitae", "cv", "resume"}
PLACE = re.compile(r"(?:based in|located in|living in|location|ubicaci[oó]n|residencia|resido en|vivo en)\s*[:-]?\s*"
                   r"([A-ZÀ-Ý][\wÀ-ÿ'’ -]{1,30}?)(?:,\s*([A-ZÀ-Ý][\wÀ-ÿ'’ -]{1,30}?))?\s*(?:[.|;()\n]|$)", re.I | re.M)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w])(\+?\d[\d ()./-]{7,}\d)(?![\w])")
LINKEDIN = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)

SCHEMA = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string"},
        "current_title": {"type": "string", "description": "the most recent job title, copied exactly"},
        "job_titles": {"type": "array", "items": {"type": "string"},
                       "description": "job titles this person has held or names as their goal, copied from the CV"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "city": {"type": "string"}, "country": {"type": "string"},
        "languages": {"type": "array", "items": {"type": "string"}},
        "years_experience": {"type": "number"},
    },
    "required": ["full_name", "current_title", "job_titles", "skills", "city", "country", "languages",
                 "years_experience"],
}
SYSTEM = ("You read a CV and copy facts out of it into JSON. Copy words exactly as the CV writes them. "
          "If the CV doesn't say something, use an empty string, an empty list or 0. Never guess or add anything.")


def _f(s: str) -> str:
    return re.sub(r"\s+", " ", lexicon.fold(s or "")).strip()


def _in(value: str, cv_folded: str) -> bool:
    v = _f(value)
    return len(v) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", cv_folded) is not None


def _words_in(value: str, cv_folded: str) -> bool:
    """Every real word of a job title appears in the CV (so 'Process engineer' passes if the CV has both)."""
    ws = [w for w in re.findall(r"[a-z0-9&+#]+", _f(value)) if len(w) > 1]
    return bool(ws) and all(re.search(rf"(?<![a-z0-9]){re.escape(w)}", cv_folded) for w in ws)


def years_experience(text: str, now: int | None = None) -> float | None:
    """Years covered by the CV's job date ranges (overlaps counted once; study dates left out)."""
    now = now or datetime.now().year
    spans = []
    for line in (text or "").splitlines():
        if SKIP_RANGE.search(line) or re.search(r"\b(intern|becari[oa]|pr[aá]cticas|trainee)\b", line, re.I):
            continue
        for a, b in YEAR_RANGE.findall(line):
            start, end = int(a), (int(b) if b.isdigit() else now)
            if 1960 <= start <= end <= now + 1:
                spans.append((start, end))
    if not spans:
        return None
    spans.sort()
    total, (cs, ce) = 0, spans[0]
    for s, e in spans[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            total, cs, ce = total + ce - cs, s, e
    return float(total + ce - cs)


def seniority(years: float | None, title: str) -> list[str]:
    if years is None and not title:
        return []
    out = []
    if years is not None:
        out.append("junior" if years < 2 else "mid" if years < 5 else "senior")
    if title and LEAD_WORDS.search(title):
        out.append("lead")
    return out


def _title_words(roles: list[str]) -> list[str]:
    words = []
    for r in roles:
        words.append(r)
        words += [w for w in re.findall(r"[A-Za-zÀ-ÿ&+#]+", r) if len(w) > 2 and _f(w) not in GENERIC]
    seen, out = set(), []
    for w in words:
        if _f(w) not in seen:
            seen.add(_f(w))
            out.append(w if " " in w else w.lower())
    return out[:20]


def _languages(cv_folded: str, ai: list[str]) -> list[str]:
    out = [canon for canon, names in LANGS.items()
           if any(re.search(rf"(?<![a-z]){n}(?![a-z])", cv_folded) for n in names)]
    for a in ai or []:  # the model may name one in another form; keep it only if the CV says it
        for canon, names in LANGS.items():
            if _f(a) in names + [canon.lower()] and canon not in out and _in(a, cv_folded):
                out.append(canon)
    return out


def _ai(cfg: dict, text: str) -> dict:
    from . import llm
    if not llm.ollama_ok(cfg):
        return {}
    try:
        cap = min(12000, llm.char_budget(cfg, "extractor", reserve_tokens=800))  # fit a small context
        return llm.structured(cfg, SYSTEM, "CV:\n" + text[:cap], SCHEMA, task="extractor") or {}
    except Exception as e:
        log.warning("CV reading with the local model failed: %s", e)
        return {}


def extract(text: str, cfg: dict | None = None, use_ai: bool = True) -> dict:
    """{"values": {field: value}, "missing": [fields], "ai": bool, "note": str}. Values are checked against the CV."""
    text = (text or "").strip()
    if len(text) < 80:
        raise ValueError("Add your CV first: choose the file or paste its text")
    if cfg is None:
        from .config import load_config
        cfg = load_config()
    cvf = _f(text)
    lines = [ln.strip(" #*\t") for ln in text.splitlines() if ln.strip(" #*\t")]
    ai = _ai(cfg, text) if use_ai else {}
    v: dict = {}

    m = EMAIL.search(text)
    v["a_email"] = m.group(0) if m else ""
    m = LINKEDIN.search(text)
    v["a_linkedin"] = ("https://" + re.sub(r"^https?://", "", m.group(0))) if m else ""
    phone = next((p.strip() for p in PHONE.findall(text) if 9 <= len(re.sub(r"\D", "", p)) <= 15
                  and not YEAR_RANGE.search(p)), "")
    v["a_phone"] = phone

    name = str(ai.get("full_name") or "").strip()
    if not (name and _in(name, cvf)):  # no model: a CV usually starts with the name
        first = lines[0] if lines else ""
        name = first if re.fullmatch(r"[A-Za-zÀ-ÿ'’.-]+(?: [A-Za-zÀ-ÿ'’.-]+){1,4}", first) else ""
    v["a_name"] = name

    title = str(ai.get("current_title") or "").strip()
    if not (title and _in(title, cvf)):  # no model: the line under the name is usually the title
        nxt = lines[1] if len(lines) > 1 and name and lines[0] == name else ""
        title = nxt if re.fullmatch(r"[A-Za-zÀ-ÿ&/,'’. -]{3,60}", nxt) and len(nxt.split()) <= 6 \
            and _f(nxt) not in SECTIONS else ""
    v["a_title"] = title

    roles = []
    for r in [v["a_title"], *(ai.get("job_titles") or [])]:
        r = str(r).strip(" .,;")
        if r and len(r) < 70 and _words_in(r, cvf) and _f(r) not in {_f(x) for x in roles} \
                and not re.search(r"\b(intern|becari[oa]|trainee|pr[aá]cticas)\b", r, re.I):
            roles.append(r)
    roles = roles[:8]
    v["roles"] = roles
    v["primary"] = roles[0] if roles else ""
    v["secondary"] = roles[1] if len(roles) > 1 else ""
    v["title_words"] = _title_words(roles) if roles else []

    skills = profile_setup.parse_cv(text)["skills"] + lexicon.find_tools(text)
    skills += [s for s in (ai.get("skills") or []) if isinstance(s, str) and len(s) < 50 and _in(s, cvf)]
    seen, sk = set(), []
    for s in skills:
        s = s.strip()
        if s and _f(s) not in seen:
            seen.add(_f(s))
            sk.append(s)
    v["skills"] = sk[:40]

    v["languages"] = _languages(cvf, ai.get("languages") or [])

    city = str(ai.get("city") or "").strip()
    country = str(ai.get("country") or "").strip()
    city = city if city and _in(city, cvf) else ""
    country = country if country and _in(country, cvf) else ""
    m = PLACE.search(text)
    if m and not city:
        city = m.group(1).strip()
        country = country or (m.group(2) or "").strip()
    v["home_city"] = city
    v["location"] = ", ".join(x for x in (city, country) if x)
    v["remote_regions"] = ([country] + (["EU"] if _f(country) in {_f(x) for x in EU} else [])) if country else []

    years = years_experience(text)
    if years is None:
        y = ai.get("years_experience")
        years = float(y) if isinstance(y, (int, float)) and 0 < y <= 50 else None
    v["seniority"] = seniority(years, v["a_title"])

    missing = [k for k in FROM_CV if not v.get(k)] + NEVER_IN_CV
    used_ai = bool(ai)
    note = ("Read with the local AI and checked against your CV." if used_ai else
            "The local AI isn't running, so only the basics were read (contact details, skills, languages, "
            "experience). Set up Local AI on Configuration to also read the job titles you've held, your city and more.")
    return {"values": v, "missing": missing, "ai": used_ai, "note": note,
            "years": years}
