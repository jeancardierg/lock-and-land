"""Deterministic claim validator. Runs on every generated letter or answer before review.

Hard failures block the text (it is regenerated with the failures as feedback, then marked failed).
Soft issues lower the score and are shown on the review card.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import lexicon
from .master import Profile
from .schemas import Sentence

STOP = set("""a an the and or of to in on for with at by from as is are was were be been this that these those it its
our your their my me i we you they he she them his her into over under than then so such very more most also
y o de del la el los las en con por para que un una unos unas al lo se su sus mi mis es son fue como más""".split())
JAMF_OK = re.compile(r"jamf[- ]comparable|comparable to jamf|equivalente a jamf", re.I)
# "Led delivery of X" (planned by someone else) must not shrink to "led X": a bare "led" reads as owning the project
BARE_LED = re.compile(r"(?<![a-z])(led|lead|leads|leading|lider[eé]|lidero|liderando|liderado)(?![a-z])"
                      r"(?!\W+(?:[\w-]+\W+){0,2}(?:delivery|execution|ejecuci[oó]n|entrega))", re.I)
# a motivation sentence may name the employer's own tools ("your Intune and Entra ID estate"), but not claim them
FIRST_PERSON_CLAIM = re.compile(r"\b(my|mine|i have|i've|i bring|i am experienced|i'm experienced|experience (with|in)|"
                                r"expertise|hands-on|background in|proficien|skilled|familiar with|"
                                r"mi experiencia|tengo experiencia|he trabajado|domino|conozco)\b", re.I)


@dataclass
class Report:
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    penalty: int = 0
    sentences: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard

    @property
    def score(self) -> int:
        return 0 if self.hard else max(0, 100 - self.penalty)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "score": self.score, "hard": self.hard, "soft": self.soft, "sentences": self.sentences}


def _content_words(t: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]+", lexicon.fold(t)) if len(w) > 3 and w not in STOP}


def _tools(text: str) -> set[str]:
    found = set(lexicon.find_tools(text))
    if "Jamf" in found and JAMF_OK.search(text):
        found.discard("Jamf")
    return found


def check_sentence(s: Sentence, prof: Profile, i: int, rep: Report, ad_tools: set[str] | None = None) -> None:
    facts = prof.by_id
    cited = [facts[f] for f in s.fact_ids if f in facts]
    unknown = [f for f in s.fact_ids if f not in facts]
    info: dict = {"i": i, "kind": s.kind, "text": s.text, "fact_ids": s.fact_ids, "issues": []}

    def hard(msg: str) -> None:
        info["issues"].append(msg)
        rep.hard.append(f"sentence {i + 1}: {msg}")

    if unknown:
        hard(f"cites unknown fact ids {unknown}")
    nums = lexicon.numbers(s.text)
    tools = _tools(s.text)
    if s.kind == "claim":
        if not cited:
            hard("claim without a cited fact")
        else:
            allowed_nums = set().union(*(f.metrics for f in cited))
            for n in sorted(nums - allowed_nums):
                hard(f"number {n} is not in the cited facts")
            allowed_tools = set().union(*(set(f.tools) for f in cited))
            for t in sorted(tools - allowed_tools):
                hard(f"{t} is not in the cited facts")
            max_level = max(f.level for f in cited)
            fact_text = lexicon.fold(" ".join(f"{f.text} {f.text_es or ''}" for f in cited))
            for word, lvl in lexicon.verb_matches(s.text):
                if lvl > max_level and not re.search(rf"(?<![a-z]){re.escape(lexicon.fold(word))}(?![a-z])", fact_text):
                    hard(f"\"{word}\" claims more ownership than the facts ({cited[0].verb_level})")
            if max_level == lexicon.LEVELS["led_delivery"]:
                m = BARE_LED.search(s.text)
                if m:
                    hard(f"\"{m.group(0)}\" on its own overstates {', '.join(f.id for f in cited)}: "
                         f"say \"led delivery of\" / \"lideré la ejecución de\" as the fact does")
            for f in cited:
                for w in f.never_say:
                    if re.search(rf"(?<![a-z]){re.escape(lexicon.fold(w))}", lexicon.fold(s.text)):
                        hard(f"\"{w}\" is never allowed for {f.id}")
            sw = _content_words(s.text)
            fw = set().union(*(_content_words(f"{f.text} {f.text_es or ''} {' '.join(f.tags)}") for f in cited))
            if sw:
                overlap = len(sw & fw) / len(sw)
                info["overlap"] = round(overlap, 2)
                if overlap < 0.25:
                    rep.soft.append(f"sentence {i + 1}: wording is far from the cited facts ({overlap:.0%} overlap)")
                    rep.penalty += 10
    else:
        rest = s.text
        for line in prof.fixed_lines.values():
            rest = rest.replace(line, " ")
        nums, tools = lexicon.numbers(rest), _tools(rest)
        if nums:
            hard(f"{s.kind} sentence contains numbers {sorted(nums)}: results must cite a fact")
        if tools and s.kind == "motivation" and ad_tools is not None and tools <= ad_tools \
                and not FIRST_PERSON_CLAIM.search(rest):
            info["employer_tools"] = sorted(tools)   # talks about their stack, claims nothing
        elif tools:
            hard(f"{s.kind} sentence names {sorted(tools)}: tools must be in a cited claim")
        m = lexicon.PAST_RESULT.search(rest)
        if m:
            hard(f"{s.kind} sentence claims a result (\"{m.group(0)}\") without citing a fact")
    rep.sentences.append(info)


def check_text(text: str, prof: Profile, rep: Report, lang: str = "en", ignore_tools: set[str] | None = None) -> None:
    """Checks on the whole text, independent of citations."""
    low = lexicon.fold(text)
    verified = prof.verified_tools() | (ignore_tools or set())
    for t in sorted(_tools(text) - verified):
        rep.hard.append(f"{t} is not in the profile")
    for name in prof.never_mention.get("names", []):
        if re.search(rf"\b{re.escape(name.lower())}\b", low):
            rep.hard.append(f"names a person ({name})")
    for ph in prof.never_mention.get("phrases", []):
        if re.search(rf"(?<![a-z]){re.escape(lexicon.fold(ph))}(?![a-z])", low):
            rep.hard.append(f"mentions \"{ph}\"")
    m = lexicon.CERT.search(text)
    if m and not prof.identity.get("certifications"):
        rep.hard.append(f"mentions a certification (\"{m.group(0)}\"); the profile has none")
    m = lexicon.SALARY.search(text)
    if m:
        rep.hard.append(f"states a salary figure (\"{m.group(0)}\")")
    at = lexicon.false_title_at(e.employer for e in prof.experience if e.it)
    m = lexicon.FALSE_TITLE.search(text) or (at.search(lexicon.fold(text)) if at else None)
    if m:
        rep.hard.append(f"presents a title that is not the official one (\"{m.group(0)}\")")
    if lexicon.CAREER_BREAK.search(text):
        line = prof.fixed_lines.get("career_break_es" if lang == "es" else "career_break", "")
        if not line or line.lower() not in text.lower():
            rep.hard.append("mentions the career break without the fixed wording")
    for pat in lexicon.BANNED:
        m = re.search(pat, text, re.I)
        if m:
            rep.soft.append(f"style: avoid \"{m.group(0)}\"")
            rep.penalty += 10


def validate_letter(sentences: list[Sentence], prof: Profile, lang: str = "en",
                    words: tuple[int, int] = (150, 240), ad_text: str | None = None) -> Report:
    rep = Report()
    ad_tools = _tools(ad_text) if ad_text else None
    for i, s in enumerate(sentences):
        check_sentence(s, prof, i, rep, ad_tools)
    text = " ".join(s.text for s in sentences)
    employer = set().union(*(set(x.get("employer_tools", [])) for x in rep.sentences)) if rep.sentences else set()
    check_text(text, prof, rep, lang, employer)
    n = len(text.split())
    if not words[0] <= n <= words[1]:
        rep.soft.append(f"length {n} words (aim {words[0]}-{words[1]})")
        rep.penalty += 5
    if not any(s.kind == "claim" for s in sentences):
        rep.hard.append("no claim sentences: the letter must show at least one CV result")
    return rep


def validate_free(text: str, fact_ids: list[str], prof: Profile, lang: str = "en") -> Report:
    """For short answers written outside the letter schema (one 'claim' unit)."""
    rep = Report()
    check_sentence(Sentence(kind="claim" if fact_ids else "motivation", text=text, fact_ids=fact_ids), prof, 0, rep)
    check_text(text, prof, rep, lang)
    return rep
