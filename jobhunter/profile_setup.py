"""A starting fact profile (facts/master_profile.json) built from your own CV, for anyone setting jobhunter up
fresh. Every bullet or sentence of the CV becomes one fact, in your own words; nothing is added or reworded.
Letters may only claim what these facts say, and the checker holds them to the verbs and numbers in them.

A profile written by hand (like the original one) is never overwritten: only one marked "generated_from_cv"."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import lexicon

BULLET = re.compile(r"^\s*(?:[-*•▪◦·‣–]|\d+[.)])\s+(.*)$")
SKILLS_HEAD = re.compile(r"^\s*#*\s*\**\s*(skills|key skills|technical skills|competencies|tools|software|"
                         r"habilidades|competencias|herramientas|conocimientos)\b", re.I)
CERT_HEAD = re.compile(r"^\s*#*\s*\**\s*(certifications?|licen[cs]es|certificaciones|acreditaciones)\b", re.I)
HEAD = re.compile(r"^\s*(#{1,6}\s+|\*\*[^*]+\*\*\s*$|[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ &/]{3,}$)")


def text_from_file(data: bytes, name: str = "") -> str:
    """Your CV file (PDF, Word .docx, .txt or .md) as plain text with one line per bullet, for the setup page to
    prefill. Scanned PDFs (images only) have no text to read."""
    import io
    n = (name or "").lower()
    if data[:4] == b"%PDF" or n.endswith(".pdf"):
        from pypdf import PdfReader
        try:
            pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        except Exception as e:  # damaged, encrypted or not really a PDF
            raise ValueError(f"Couldn't read that PDF ({type(e).__name__}). Paste the CV text instead") from e
        text = "\n".join(pages)
    elif data[:2] == b"PK" or n.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        lines = []
        for para in d.paragraphs:
            t = para.text.strip()
            if not t:
                continue
            style = (para.style.name if para.style is not None else "").lower()
            if "list" in style and not BULLET.match(t):
                t = "- " + t
            elif style.startswith("heading"):
                t = "## " + t
            lines.append(t)
        for table in d.tables:  # two-column CV layouts often put everything in a table
            for row in table.rows:
                for cell in row.cells:
                    lines += [c.strip() for c in cell.text.splitlines() if c.strip()]
        text = "\n".join(dict.fromkeys(lines))
    elif n.endswith((".txt", ".md")) or not n:
        text = data.decode("utf-8", errors="replace")
    else:
        raise ValueError("Use a PDF, Word (.docx) or text file")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"^\s*[•▪◦·‣●○■□➢➤►]\s*", "- ", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 80:
        raise ValueError("No text found in that file (a scanned PDF?). Paste the CV text instead")
    return text


def path() -> Path:
    from .master import PROFILE_PATH
    return PROFILE_PATH


def existing_kind(p: Path | None = None) -> str:
    """'none', 'generated' (from the wizard, safe to rebuild) or 'manual' (hand-made: never touched)."""
    p = p or path()
    if not p.exists():
        return "none"
    try:
        return "generated" if json.loads(p.read_text(encoding="utf-8")).get("generated_from_cv") else "manual"
    except (OSError, ValueError):
        return "manual"


def _level(text: str) -> str:
    found = [lvl for _, lvl in lexicon.verb_matches(text)]
    lvl = max(found) if found else 2
    return lexicon.LEVEL_NAMES.get(lvl, "executed")


def _items(line: str) -> list[str]:
    line = re.sub(r"^\s*#*\s*\**[^:]*:\s*", "", line) if ":" in line else line
    return [x.strip(" .*") for x in re.split(r"[,;|·•]", line) if 1 < len(x.strip(" .*")) < 50]


def parse_cv(text: str) -> dict:
    """Facts, skills and certifications from CV text (markdown or text copied from a PDF)."""
    facts, skills, certs = [], [], []
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if SKILLS_HEAD.match(line):
            section = "skills"
            skills += _items(line) if ":" in line else []
            continue
        if CERT_HEAD.match(line):
            section = "certs"
            continue
        if HEAD.match(line) and not BULLET.match(line):
            section = ""
            continue
        m = BULLET.match(line)
        body = (m.group(1) if m else line).strip()
        if section == "skills":
            skills += _items(body)
            continue
        if section == "certs":
            certs.append(body.strip(" .*")[:120])
            continue
        if m and len(body) >= 25:
            facts.append(body)
        elif not m and len(body) >= 60:  # a paragraph: one fact per sentence
            facts += [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if len(s.strip()) >= 30]
    return {"facts": list(dict.fromkeys(facts)), "skills": list(dict.fromkeys(skills)),
            "certifications": list(dict.fromkeys(certs))}


def build(cv_text: str, identity: dict, extra_skills: list[str] | None = None) -> dict:
    parsed = parse_cv(cv_text)
    if len(parsed["facts"]) < 3:
        raise ValueError("Couldn't find your experience in that text: paste the CV with one achievement or duty "
                         "per line (bullets are fine)")
    facts = [{"id": f"F-{i:03d}", "employer": None, "area": "", "verb_level": _level(t), "text": t, "tags": []}
             for i, t in enumerate(parsed["facts"], 1)]
    ident = {k: v for k, v in identity.items() if v}
    if parsed["certifications"]:
        ident["certifications"] = parsed["certifications"]
    return {
        "version": datetime.now().strftime("%Y-%m-%d"),
        "status": "APPROVED (from your CV, confirmed in the setup wizard)",
        "generated_from_cv": True,
        "identity": ident,
        "experience": [],
        "facts": facts,
        "skills_verified": list(dict.fromkeys(parsed["skills"] + list(extra_skills or []))),
        "phrasing_rules": {}, "never_mention": {}, "fixed_lines": {}, "qa_bank": [],
    }


def save(profile: dict, p: Path | None = None) -> Path:
    p = p or path()
    if existing_kind(p) == "manual":
        raise ValueError("A hand-made facts/master_profile.json already exists; it was left unchanged")
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.replace(p.with_name(f"master_profile.json.bak-{datetime.now():%Y%m%d-%H%M%S}"))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p
