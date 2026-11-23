"""Cover-letter export. Generation itself lives in grounding.py (fact-ID letters + validator)."""
from __future__ import annotations

import re
from pathlib import Path

from .config import DATA


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "job"


def export_docx(job: dict, cover_letter: str, name: str) -> Path:
    from docx import Document
    from docx.shared import Pt
    out_dir = DATA / "applications"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job['id']}_{safe_name(job.get('company') or '')}_{safe_name(job['title'])}_cover_letter.docx"
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name, style.font.size = "Calibri", Pt(11)
    for para in cover_letter.split("\n\n"):
        doc.add_paragraph(para.strip())
    doc.save(path)
    return path
