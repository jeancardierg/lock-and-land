"""Job criteria: what the setup wizard (/setup) shows and saves. Everything lives in config.yaml, so the pipeline,
sources, scorer and filters all read the same values. Saving keeps a dated backup of config.yaml."""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import HOME, ROOT, load_config
from .cv_criteria import LANGS

# sha256 prefixes of the old example config's first name, title and headline starts (kept as hashes, not text)
_OLD_EXAMPLE = ("56ec4fee9f20de34", "eb73b9f10dc0da32", "24dd3fadebb36743")


def _old_example(value: Any, i: int) -> bool:
    return bool(value) and hashlib.sha256(str(value).encode()).hexdigest()[:16] == _OLD_EXAMPLE[i]


WORK_MODES = {"remote_only": "Remote only",
              "remote_or_hybrid": "Remote, or hybrid / on-site near home (on-site only if the pay is high enough)",
              "any": "Remote, hybrid or on-site near home"}
SENIORITY = ["junior", "mid", "senior", "lead"]
# remote jobs "open to": regions boards use, then countries (a country first works best for Himalayas/Jooble)
REGIONS = ["EU", "Europe", "EMEA", "Worldwide", "Spain", "Portugal", "France", "Germany", "Italy", "Netherlands",
           "Belgium", "Ireland", "United Kingdom", "Switzerland", "Austria", "Poland", "Sweden", "Denmark", "Norway",
           "Finland", "Greece", "Czechia", "Romania", "Hungary", "Luxembourg", "Slovakia", "Slovenia", "Croatia",
           "Bulgaria", "Estonia", "Latvia", "Lithuania", "Malta", "Cyprus", "United States", "Canada", "Mexico",
           "Argentina", "Chile", "Colombia", "Brazil", "Latin America", "Americas", "Asia", "APAC", "Australia",
           "India", "Morocco", "United Arab Emirates"]


def config_path() -> Path:
    return HOME / "config.yaml"


def done(cfg: dict | None = None) -> bool:
    return bool(((cfg or load_config()).get("wizard") or {}).get("completed_at"))


def _list(v) -> list[str]:
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def from_config(cfg: dict | None = None) -> dict[str, Any]:
    """Current criteria, for prefilling the wizard (your existing config.yaml on first run)."""
    from .fit import prefs
    from .sources.base import DEFAULT_TITLE_ANY
    cfg = cfg or load_config()
    p, t, s = cfg.get("profile") or {}, cfg.get("targets") or {}, cfg.get("search") or {}
    template = bool((cfg.get("wizard") or {}).get("template"))
    src, hc = cfg.get("sources") or {}, (cfg.get("rules") or {}).get("home_cities") or []
    pr = prefs(cfg)
    from . import profile_setup
    kind = profile_setup.existing_kind()
    cvp = HOME / "cv" / "cv_main.md"
    about = None
    if kind != "manual":  # fresh install (or a profile this page made): ask for the person and their CV
        about = {"name": "" if template or p.get("name") == "Your Name" else (p.get("name") or ""),
                 "email": "" if template or p.get("email") == "you@gmail.com" else (p.get("email") or ""),
                 "phone": "" if template else (p.get("phone") or ""),
                 "linkedin": "" if template else (p.get("linkedin_url") or ""),
                 "current_title": "" if template else (p.get("current_title") or ""),
                 "cv_text": cvp.read_text(encoding="utf-8") if cvp.exists() else "",
                 "has_profile": kind == "generated", "cv_pdf": _attachment(cfg).name if _attachment(cfg) else ""}
    out = {
        "roles": s.get("queries") or [],
        "primary": t.get("primary") or "", "secondary": t.get("secondary") or "",
        "title_words": s.get("title_any") or sorted(set(s.get("queries") or []) | set(DEFAULT_TITLE_ANY), key=str.lower),
        "skills": t.get("keywords") or [], "must_have": s.get("must_have_any") or [],
        "exclude_titles": s.get("exclude_title") or [],
        "avoid": t.get("avoid") if t.get("avoid") is not None else ["pure software development", "first-line-only helpdesk"],
        "seniority": t.get("seniority") or [],
        "home_city": (hc[0] if hc else (p.get("location") or "").split(",")[0]).strip().title(),
        "commute_towns": hc[1:], "location": p.get("location") or "",
        "work_mode": pr.get("work_mode", "remote_or_hybrid"),
        "remote_regions": t.get("remote_regions") or ["Spain", "EU"],
        "markets": t.get("markets") or "",
        "min_salary": p.get("min_salary_eur") or 0, "onsite_min_salary": pr.get("onsite_min_salary_eur") or 0,
        "languages": p.get("languages") or ["English"],
        "max_age_days": s.get("max_age_days") or 14,
        "jooble_locations": (src.get("jooble") or {}).get("locations") or [],
        "work_modes": WORK_MODES, "seniority_options": SENIORITY, "completed": done(cfg), "about": about,
        "home_geo": (cfg.get("rules") or {}).get("home_geo") or None,
        "language_options": list(LANGS), "region_options": REGIONS,
    }
    if template and not done(cfg):  # a new install: start from empty criteria, not the example's
        out.update(roles=[], primary="", secondary="", title_words=[], skills=[], must_have=[], exclude_titles=[],
                   avoid=[], seniority=[], min_salary=0, onsite_min_salary=0, home_city="", commute_towns=[],
                   location="", remote_regions=[], languages=[], jooble_locations=[], home_geo=None)
    return out


def _attachment(cfg: dict):
    from .config import resolve
    v = ((cfg.get("profile") or {}).get("cv_variants") or {}).get("main") or {}
    a = v.get("attachment")
    return resolve(a) if a and resolve(a).exists() else None


def apply(v: dict, path: Path | None = None) -> dict:
    """Write the wizard's answers into config.yaml (backup first). Unknown keys are ignored."""
    path = path or config_path()
    if not path.exists():
        shutil.copy2(ROOT / "config.example.yaml", path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    bak = path.with_name(f"config.yaml.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, bak)
    roles = _list(v.get("roles"))
    if not roles:
        raise ValueError("Add at least one role or job title to search for")
    mode = v.get("work_mode") if v.get("work_mode") in WORK_MODES else "remote_or_hybrid"
    home = str(v.get("home_city") or "").strip()
    towns = _list(v.get("commute_towns"))
    regions = _list(v.get("remote_regions"))
    p = raw.setdefault("profile", {})
    t = raw.setdefault("targets", {})
    s = raw.setdefault("search", {})
    r = raw.setdefault("rules", {})
    pr = raw.setdefault("preferences", {})
    src = raw.setdefault("sources", {})
    s["queries"] = roles
    s["title_any"] = _list(v.get("title_words")) or roles
    s["must_have_any"] = _list(v.get("must_have"))
    s["exclude_title"] = _list(v.get("exclude_titles"))
    s["max_age_days"] = int(v.get("max_age_days") or 14)
    t["primary"] = str(v.get("primary") or ", ".join(roles[:3]))
    t["secondary"] = str(v.get("secondary") or "")
    t["keywords"] = _list(v.get("skills"))
    t["avoid"] = _list(v.get("avoid"))
    t["seniority"] = [x for x in _list(v.get("seniority")) if x in SENIORITY]
    t["remote_regions"] = regions
    where = {"remote_only": "Fully remote", "remote_or_hybrid": f"Remote, or hybrid in {home}",
             "any": f"Remote, hybrid or on-site in {home}"}[mode]
    t["markets"] = f"{where}" + (f"; remote open to {', '.join(regions)}" if regions else "")
    g = v.get("home_geo") or {}
    try:
        r["home_geo"] = {"lat": round(float(g["lat"]), 4), "lon": round(float(g["lon"]), 4),
                         "km": max(1, min(int(float(g.get("km") or 20)), 150))}
    except (KeyError, TypeError, ValueError):
        pass  # no map pick: keep whatever was there
    if home:
        r["home_cities"] = [home.lower()] + [x.lower() for x in towns if x.lower() != home.lower()]
        p["location"] = str(v.get("location") or "").strip() or (f"{home}, {regions[0]}" if regions else home)
    p["work_mode"] = t["markets"]
    p["languages"] = _list(v.get("languages")) or ["English"]
    p["min_salary_eur"] = int(v.get("min_salary") or 0)
    pr["work_mode"] = mode
    pr["onsite_min_salary_eur"] = int(v.get("onsite_min_salary") or p["min_salary_eur"] or 0)
    country = next((x for x in regions if x.upper() not in ("EU", "EUROPE", "EMEA", "WORLDWIDE", "ANYWHERE")), "")
    if country:
        src.setdefault("himalayas", {"enabled": True})["country"] = country
    jl = _list(v.get("jooble_locations")) or [x for x in [home, country] if x]
    src.setdefault("jooble", {"enabled": False})["locations"] = jl
    about = v.get("about") or {}
    built = None
    if (raw.get("wizard") or {}).get("template"):
        # never let the example's details stand in for a new person's (letters and recruiter replies read them)
        if p.get("work_authorization") == "EU work authorization":
            p["work_authorization"] = ""
        # older example configs carried the original author's first name, title and headline: blank them
        if _old_example(p.get("first_name"), 0) and not str(about.get("name") or "").strip():
            nm = str(p.get("name") or "")
            p["first_name"] = "" if nm in ("", "Your Name") else nm.split()[0]
        if _old_example(str(p.get("current_title") or "")[:29], 1) and not str(about.get("current_title") or "").strip():
            p["current_title"] = ""
        hl = str(p.get("headline") or "")
        if (_old_example(hl[:26], 2) or _old_example(hl[:29], 1)) and not str(about.get("current_title") or "").strip():
            p["headline"] = ""
    if about:
        built = _apply_about(about, raw, path.parent, _list(v.get("skills")))
    if (raw.get("wizard") or {}).get("template"):
        t["skip_role_families"] = []  # the example's IT-only filters don't apply to a new person
    raw["wizard"] = {"completed_at": datetime.now().isoformat(timespec="seconds")}
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    return {"ok": True, "backup": str(bak), "roles": len(roles), "facts": built}


def _apply_about(a: dict, raw: dict, home: Path, skills: list[str]) -> int | None:
    """Your details and CV: config.yaml profile, cv/cv_main.md, the attached PDF and a fact profile from the CV."""
    import base64
    import re
    from . import profile_setup
    p = raw.setdefault("profile", {})
    name = str(a.get("name") or "").strip()
    if name:
        p["name"], p["first_name"] = name, name.split()[0]
    for k, key in (("email", "email"), ("phone", "phone"), ("linkedin", "linkedin_url"),
                   ("current_title", "current_title")):
        if str(a.get(k) or "").strip():
            p[key] = str(a[k]).strip()
    if p.get("current_title"):
        p["headline"] = p["current_title"]
    cv = str(a.get("cv_text") or "").strip()
    if not cv:
        return None
    if not a.get("confirm"):
        raise ValueError("Tick the box confirming the CV text is accurate: letters will only use what it says")
    ident = {"name": p.get("name"), "email": p.get("email"), "phone": p.get("phone"),
             "linkedin": p.get("linkedin_url"), "location": p.get("location"), "current_title": p.get("current_title"),
             "languages": p.get("languages")}
    prof = profile_setup.build(cv, ident, skills)  # checks the CV before anything is written
    (home / "cv").mkdir(parents=True, exist_ok=True)
    (home / "cv" / "cv_main.md").write_text(cv + "\n", encoding="utf-8")
    att = "cv/CV.pdf"
    if a.get("cv_pdf_b64"):
        data = base64.b64decode(str(a["cv_pdf_b64"]).split(",")[-1])
        if not data.startswith(b"%PDF") or len(data) > 15_000_000:
            raise ValueError("The CV file must be a PDF under 15 MB")
        fn = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "My") + "_CV.pdf"
        (home / "cv" / fn).write_bytes(data)
        att = f"cv/{fn}"
    else:
        old = ((p.get("cv_variants") or {}).get("main") or {}).get("attachment")
        att = old if old and old.startswith("cv/") else att
    p["cv_variants"] = {"main": {"markdown": "cv/cv_main.md", "attachment": att}}
    profile_setup.save(prof, home / "facts" / "master_profile.json")
    return len(prof["facts"])
