"""master_profile.json: the only source of facts. Loaded, checked and hashed here."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import lexicon
from .config import HOME

PROFILE_PATH = HOME / "facts" / "master_profile.json"
VerbLevel = Literal["supported", "executed", "co_delivered", "led_delivery", "led_solo", "owned", "designed"]


class Fact(BaseModel):
    id: str
    employer: str | None = None
    area: str = ""
    verb_level: VerbLevel = "executed"
    text: str
    text_es: str | None = None
    tags: list[str] = Field(default_factory=list)
    never_say: list[str] = Field(default_factory=list)

    @property
    def level(self) -> int:
        return lexicon.LEVELS[self.verb_level]

    @property
    def tools(self) -> list[str]:
        return lexicon.find_tools(f"{self.text} {self.text_es or ''}")

    @property
    def metrics(self) -> set[str]:
        return lexicon.numbers(self.text) | lexicon.numbers(self.text_es or "")

    def for_lang(self, lang: str) -> str:
        return self.text_es if lang == "es" and self.text_es else self.text


class Experience(BaseModel):
    id: str
    employer: str
    title: str
    start: str
    end: str | None = None
    it: bool = True


class QA(BaseModel):
    key: str
    kind: Literal["yes_no", "number", "choice", "free_text", "url", "jean_only"]
    answer: str = ""
    answer_es: str = ""
    examples: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    version: str
    status: str = ""
    identity: dict
    experience: list[Experience]
    facts: list[Fact]
    skills_verified: list[str] = Field(default_factory=list)
    phrasing_rules: dict[str, str] = Field(default_factory=dict)
    never_mention: dict[str, list[str]] = Field(default_factory=dict)
    fixed_lines: dict[str, str] = Field(default_factory=dict)
    qa_bank: list[QA] = Field(default_factory=list)
    sha: str = ""

    def fact(self, fid: str) -> Fact:
        for f in self.facts:
            if f.id == fid:
                return f
        raise KeyError(fid)

    @property
    def by_id(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts}

    @property
    def approved(self) -> bool:
        return self.status.strip().upper().startswith("APPROVED")

    def verified_tools(self) -> set[str]:
        out = set(self.skills_verified)
        for f in self.facts:
            out.update(f.tools)
        return out

    def qa(self, key: str) -> QA | None:
        return next((q for q in self.qa_bank if q.key == key), None)

    def years_with(self, tool: str, today: date | None = None) -> int:
        """Whole years since the first employer whose facts mention the tool (min 1 if used at all)."""
        today = today or date.today()
        emp = {e.id: e for e in self.experience}
        starts = [emp[f.employer].start for f in self.facts if f.employer in emp and tool in f.tools]
        if not starts:
            return 0
        y, m = map(int, min(starts).split("-"))
        months = (today.year - y) * 12 + today.month - m
        return max(1, months // 12)


def problems(p: Profile) -> list[str]:
    """Consistency checks shown by `doctor` and `profile-check`."""
    out = []
    ids = [f.id for f in p.facts]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        out.append(f"duplicate fact ids: {sorted(dup)}")
    emp = {e.id for e in p.experience}
    for f in p.facts:
        if f.employer and f.employer not in emp:
            out.append(f"{f.id}: unknown employer {f.employer!r}")
        # the fact's own words must not claim more than its level
        for word, lvl in lexicon.verb_matches(f.text):
            if lvl > f.level and lvl < 7:
                out.append(f"{f.id}: text says {word!r} (level {lvl}) but verb_level is {f.verb_level}")
    for q in p.qa_bank:
        if q.kind != "jean_only" and not q.answer:
            out.append(f"qa_bank {q.key}: empty answer")
    if not p.approved:
        out.append("status is not APPROVED: you have not signed off the facts yet")
    return out


def load(path: Path | str | None = None) -> Profile:
    p = Path(path) if path else PROFILE_PATH
    raw = p.read_bytes()
    prof = Profile.model_validate(json.loads(raw))
    prof.sha = hashlib.sha256(raw).hexdigest()[:16]
    return prof


@lru_cache(maxsize=4)
def _cached(path: str, mtime: float) -> Profile:
    return load(path)


def exists() -> bool:
    return PROFILE_PATH.exists()


MISSING_HINT = ("Your facts profile is missing (facts/master_profile.json in the data folder). Nothing can be scored "
                "or written without it: fill in 'You and your CV' on the Job criteria page, or copy your existing "
                "facts/ and cv/ folders into the data folder.")


def get(path: Path | str | None = None) -> Profile:
    p = Path(path) if path else PROFILE_PATH
    return _cached(str(p), p.stat().st_mtime)


def save_qa(key: str, answer: str, example: str, kind: str = "free_text", path: Path | None = None) -> None:
    """Add or update a pre-approved screening answer (called after the user approves it)."""
    p = path or PROFILE_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    bank = data.setdefault("qa_bank", [])
    for q in bank:
        if q["key"] == key:
            q["answer"] = answer
            if example and example not in q.setdefault("examples", []):
                q["examples"].append(example)
            if q.get("kind") == "jean_only" and answer:
                q["kind"] = kind if kind != "jean_only" else "free_text"
            break
    else:
        bank.append({"key": key, "kind": kind, "answer": answer, "answer_es": "", "examples": [example] if example else []})
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def display_name(cfg: dict | None = None) -> str:
    """The name letters are signed with: the approved master profile's, falling back to config.yaml."""
    try:
        n = (get().identity or {}).get("name")
        if n:
            return n
    except Exception:  # profile missing or invalid: config still has a name
        pass
    return ((cfg or {}).get("profile") or {}).get("name") or ""
