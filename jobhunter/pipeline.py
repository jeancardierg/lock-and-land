"""Pipeline stages. Each stage takes one job in one state, does one thing and moves it to the next state.

Stages are called by the Huey workers (jobhunter.tasks) and, for a manual or fallback run, by run().
Nothing here sends or submits anything.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import enrich, fit, grounding, llm, master, rules, scoring
from .config import DATA, cv_variant
from .db import DB, now
from .schemas import JobFacts, Question
from .screening import Screener
from .sources import LAST_STATS, fetch_all
from .vectors import Store

log = logging.getLogger(__name__)


def _th(cfg: dict) -> dict:
    t = cfg.get("thresholds", {})
    return {"shortlist": t.get("shortlist_score", 60), "draft": t.get("draft_score", 70),
            "min_coverage": t.get("min_coverage", 0.34)}


# ---------------- batch control: pause, Ollama down, jobs that keep failing ----------------

MAX_FAILS = 3  # a job that errors this many times in a row is parked in 'failed' so it can't block the queue
_fails: dict[tuple[str, int], int] = {}


def _paused() -> bool:
    """Same flag as tasks.paused(): checked between jobs so Pause takes effect within one job, not one batch."""
    return (DATA / "paused").exists()


def _fresh(ts: str | None, minutes: float = 30) -> bool:
    from datetime import datetime, timedelta, timezone
    try:
        t = datetime.fromisoformat(ts or "")
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - t < timedelta(minutes=minutes)


def _ollama_down(e: BaseException) -> bool:
    import requests
    return isinstance(e, requests.ConnectionError)  # refused / reset / connect timeout; a slow reply is not this


def _failed(db: DB, stage: str, job_id: int, e: BaseException) -> None:
    """Count a stage error for this job; after MAX_FAILS move it to 'failed' (Retry/Redraft brings it back)."""
    k = (stage, job_id)
    _fails[k] = _fails.get(k, 0) + 1
    if _fails[k] >= MAX_FAILS:
        _fails.pop(k, None)
        if db.try_move(job_id, "failed", reason=f"{stage} failed {MAX_FAILS} times: {e}"[:300]):
            log.warning("job %s parked in 'failed' after %s %s errors", job_id, MAX_FAILS, stage)


# ---------------- ingest ----------------

def ingest(db: DB, cfg: dict) -> dict:
    n = sum(1 for job in fetch_all(cfg) if job.get("title") and db.upsert_job(job))
    return {"new_jobs": n, "sources": dict(LAST_STATS)}


fetch_step = lambda db, cfg: ingest(db, cfg)["new_jobs"]  # noqa: E731  (v0.2 CLI name)


# ---------------- enrich: full ad text and ATS questions, on the main queue (network only, no GPU) ----------------

def enrich_batch(db: DB, cfg: dict, limit: int = 60) -> dict:
    """Fetch the full ad for new jobs that pass the cheap gates, before the GPU tick needs them. Network waits used
    to happen inside the GPU job, with the GPU idle. Jobs the gates will drop are not fetched at all."""
    out = {"enriched": 0, "skipped": 0}
    if not cfg.get("enrich", {}).get("enabled", True):
        return out
    rows = db.conn.execute("SELECT * FROM jobs WHERE state='queued' AND enrich_json IS NULL ORDER BY id LIMIT ?",
                           (limit,)).fetchall()
    for j in rows:
        if _paused():
            break
        job = dict(j)
        if not scoring.prefilter(job, cfg)[0] or rules.quick_language_block(job, cfg):
            out["skipped"] += 1  # prepare() filters it without needing the full text
            continue
        try:
            e = enrich.enrich(job, cfg)
        except Exception as ex:  # best effort, like before: the GPU tick works from the snippet
            log.info("enrich failed job %s: %s", job["id"], ex)
            e = {"questions": []}
        fields = {k: e[k] for k in ("ats", "apply_url", "description", "full_text") if e.get(k) is not None}
        db.update_job(job["id"], enrich_json=json.dumps({"questions": e.get("questions") or []}, ensure_ascii=False),
                      **fields)
        out["enriched"] += 1
    return out


# ---------------- prepare: gates, full text, extraction, location/language rules ----------------

def _facts(job) -> dict:
    try:
        return json.loads(job["facts_json"] or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def prepare(db: DB, cfg: dict, job_id: int) -> str:
    job = dict(db.job(job_id))
    if job["state"] != "queued":
        return job["state"]
    ok, why = scoring.prefilter(job, cfg)
    if not ok:
        db.move(job_id, "filtered", reason=why, notes=why)
        return "filtered"
    lang = rules.quick_language_block(job, cfg)
    if lang:
        db.move(job_id, "filtered", reason=f"language: {lang}", notes=f"language: {lang}")
        return "filtered"
    extra: dict[str, Any] = {}
    if job.get("enrich_json"):  # already fetched on the main queue (enrich_batch)
        ats_questions = (json.loads(job["enrich_json"]) or {}).get("questions") or []
    elif cfg.get("enrich", {}).get("enabled", True):  # not yet (manual Draft, or the main queue is behind)
        e = enrich.enrich(job, cfg)
        for k in ("ats", "apply_url", "description", "full_text"):
            if e.get(k) is not None:
                extra[k] = e[k]
        job.update(extra)
        ats_questions = e.get("questions") or []
    else:
        ats_questions = []
    jf = grounding.extract(cfg, job)
    facts = {"job_facts": jf.model_dump(), "ats_questions": ats_questions}
    info = {"required_languages": jf.required_languages, "work_mode": jf.work_mode,
            "remote_scope": jf.remote_scope, "office_city": jf.office_city}
    override, reason = rules.decide(job, info, cfg)
    to = override or "extracted"
    db.move(job_id, to, reason=reason or "facts extracted", facts_json=json.dumps(facts, ensure_ascii=False),
            score_reasons=json.dumps({**info, "rules_v": rules.RULES_V,
                                      **({"excluded" if override == "filtered" else "relocation": reason}
                                         if override else {})}, ensure_ascii=False), **extra)
    return to


# ---------------- score: embedding coverage first, LLM fit score only when it is worth it ----------------

def _skip_families(cfg: dict) -> list[str]:
    """Job families scored down hard: your criteria's list, else (older configs) the original IT-only one."""
    t = cfg.get("targets") or {}
    if t.get("skip_role_families") is not None:
        return list(t["skip_role_families"])
    return ["software_dev", "data", "sales"]


def score(db: DB, cfg: dict, job_id: int, prof=None, store: Store | None = None) -> str:
    job = dict(db.job(job_id))
    if job["state"] != "extracted":
        return job["state"]
    prof = prof or master.get()
    store = store or Store(cfg)
    jf = JobFacts.model_validate(_facts(job).get("job_facts") or {})
    ret = grounding.retrieve(cfg, prof, job, jf, store)
    th = _th(cfg)
    detail = json.loads(job["score_reasons"] or "{}") or {}
    detail.update(coverage=ret.score, gaps=ret.gaps, matched_facts=[f.id for f in ret.facts])
    if jf.role_family in _skip_families(cfg):
        final = min(40, int(ret.score * 100))
        detail["reasons"] = [f"role family: {jf.role_family}"]
    elif ret.score < th["min_coverage"]:
        final = int(ret.score * 100 * 0.8)
        detail["reasons"] = [f"only {ret.score:.0%} of must-have requirements match the profile"]
    else:
        # verified facts, not the raw CV: fewer tokens, and the score rests on what the letter may claim
        s, d = scoring.score_job(job, scoring.candidate_text(prof, ret.facts), cfg)
        detail.update({k: v for k, v in d.items() if k in ("reasons", "red_flags", "keyword_hits", "keyword_bonus",
                                                          "llm_score", "seniority")})
        final = s
    a = fit.assess(jf, job, cfg)
    detail.update(base_score=final, work_mode=a["mode"], salary_eur=a["salary"], fit=a["label"])
    if a["filter"]:
        detail["excluded"] = a["filter"]
        db.move(job_id, "filtered", reason=a["filter"], notes=a["filter"], score=final, embed_score=ret.score,
                score_reasons=json.dumps(detail, ensure_ascii=False))
        return "filtered"
    final = fit.apply(final, a)
    legacy = "shortlisted" if final >= th["shortlist"] else "scored"
    db.move(job_id, "scored", reason=f"score {final}, coverage {ret.score:.0%}, {a['label']}", legacy=legacy,
            score=final, embed_score=ret.score, score_reasons=json.dumps(detail, ensure_ascii=False))
    return "scored"


# ---------------- generate: grounded letter + screening answers + validation ----------------

def generate(db: DB, cfg: dict, job_id: int, prof=None, store: Store | None = None, actor: str = "system",
             defer_check: bool = False) -> str:
    job = dict(db.job(job_id))
    if job["state"] not in ("scored", "relocation", "failed", "needs_review"):
        return job["state"]
    prof = prof or master.get()
    store = store or Store(cfg)
    f = _facts(job)
    jf = JobFacts.model_validate(f["job_facts"]) if f.get("job_facts") else grounding.extract(cfg, job)
    db.move(job_id, "generated", actor=actor, reason="generating")
    try:
        ret = grounding.retrieve(cfg, prof, job, jf, store)
        out = grounding.write_letter(cfg, prof, job, jf, ret, entail=not defer_check)
        out.update(profile_version=prof.sha, model=llm.model(cfg, "writer"),
                   retrieval={"facts": [x.id for x in ret.facts], "matched": ret.matched, "gaps": ret.gaps,
                              "score": ret.score})
        questions = [Question.model_validate(q) for q in (f.get("ats_questions") or [])] or jf.screening_questions
        answers = Screener(cfg, prof, store).answer_all(questions, out.get("lang", "en"), job) if questions else []
    except Exception as e:
        if not isinstance(e, llm.Paused):
            log.warning("generate failed job %s: %s", job_id, e)
        db.move(job_id, "failed", reason=f"generation error: {e}"[:300])
        if isinstance(e, llm.Paused):
            db.try_move(job_id, "scored", reason="requeued: paused while writing")
            return "scored"
        if _ollama_down(e):  # Ollama stopped or crashed mid-letter: not the job's fault, draft it again later
            db.try_move(job_id, "scored", reason="requeued: the local AI stopped during the letter")
            return "scored"
        return "failed"
    app_id = db.add_application(job_id, out, answers)
    variant = cv_variant(job)
    key_points = "\n".join(f"- {x.for_lang(out.get('lang', 'en'))}" for x in ret.facts[:6])
    notes = {"email_subject": out.get("subject"), "cv_variant": variant, "application_id": app_id,
             "unsupported": out["validation"]["hard"] + out["validation"]["soft"]}
    if _cv_wanted(job):
        notes["want_cv"] = True
    try:   # a redraft writes a new letter; a CV you already tailored (and your attach choice) stays
        old = json.loads(job.get("notes") or "{}")
    except (json.JSONDecodeError, TypeError):
        old = {}
    if isinstance(old, dict):
        notes.update({k: old[k] for k in ("tailored_cv", "tailored_cv_error", "cv_choice") if k in old})
    if out["ok"] and (_cv_wanted(job) or _cv_auto(cfg)):   # on demand: Tailor CV, or the pasted-ad tick box
        notes.update(tailor_cv(cfg, prof, job, jf, ret.gaps, store, out.get("lang")))
    if out["ok"]:
        db.move(job_id, "needs_review", reason=f"validation score {out['validation']['score']}",
                cover_letter=out["text"], cv_summary="Most relevant CV points for this job:\n" + key_points,
                notes=json.dumps(notes, ensure_ascii=False))
        return "needs_review"
    db.move(job_id, "failed", reason="validator blocked all attempts: " + "; ".join(out["validation"]["hard"][:3]),
            notes=json.dumps(notes, ensure_ascii=False))
    return "failed"


def _cv_auto(cfg: dict) -> bool:
    from .tailored_cv import _ccfg
    return bool(_ccfg(cfg)["auto"])


def _cv_wanted(job: dict) -> bool:
    """The job asked for a CV with its letter (Add a job with "Tailor my CV too" ticked)."""
    try:
        n = json.loads(job.get("notes") or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(n, dict) and bool(n.get("want_cv"))


def tailor_cv(cfg: dict, prof, job: dict, jf: JobFacts, gaps: list, store=None, lang: str | None = None) -> dict:
    """The job's tailored CV (tailored_cv.py), as the notes keys it lives under. Never fails the letter: a problem
    is kept as tailored_cv_error and the card offers to build it again."""
    from . import tailored_cv
    if not tailored_cv._ccfg(cfg)["enabled"]:
        return {}
    try:
        return {"tailored_cv": tailored_cv.make(cfg, prof, job, jf, gaps, store, lang), "tailored_cv_error": None}
    except Exception as e:
        log.warning("tailored CV failed job %s: %s", job.get("id"), e)
        return {"tailored_cv": None, "tailored_cv_error": str(e)[:300]}


def rebuild_cv(db: DB, cfg: dict, job_id: int) -> dict:
    """Tailor CV on a card: a fresh tailored CV for one job (letter or not). Language follows the letter, else
    the ad."""
    job = dict(db.job(job_id))
    prof, store = master.get(), Store(cfg)
    f = _facts(job)
    jf = JobFacts.model_validate(f["job_facts"]) if f.get("job_facts") else grounding.extract(cfg, job)
    app_row = db.application(job_id)
    gaps = (json.loads(app_row["retrieval_json"] or "{}") or {}).get("gaps", []) if app_row else []
    lang = (app_row["lang"] if app_row else None) or None
    got = tailor_cv(cfg, prof, job, jf, gaps, store, lang)
    if got:
        cur = db.job(job_id)  # read again: the card may have changed notes while the CV was built
        try:
            notes = json.loads(cur["notes"] or "{}")
        except (json.JSONDecodeError, TypeError):
            notes = {}
        notes = notes if isinstance(notes, dict) else {}
        notes.update(got)
        db.update_job(job_id, notes=json.dumps(notes, ensure_ascii=False))
    return got


# ---------------- batch helpers (used by workers and run()) ----------------

def prepare_batch(db: DB, cfg: dict, limit: int = 40) -> dict:
    out = {"prepared": 0, "filtered": 0, "relocation": 0, "errors": 0}
    wait_enrich = cfg.get("enrich", {}).get("enabled", True)
    picked = 0
    for j in db.in_state("queued", order="id"):
        if picked >= limit:
            break
        if wait_enrich and not j["enrich_json"] and _fresh(j["fetched_at"]) and \
                scoring.prefilter(dict(j), cfg)[0] and not rules.quick_language_block(dict(j), cfg):
            continue  # the main queue fetches its full text first (fallback: inline once it's 30 min old)
        picked += 1
        if _paused():
            out["paused"] = True
            break
        try:
            st = prepare(db, cfg, j["id"])
            _fails.pop(("prepare", j["id"]), None)
            out["prepared"] += 1
            out[st] = out.get(st, 0) + 1
        except Exception as e:
            log.warning("prepare failed job %s: %s", j["id"], e)
            out["errors"] += 1
            if _ollama_down(e):
                out["ollama"] = "down"
                break
            _failed(db, "prepare", j["id"], e)
    return out


def score_batch(db: DB, cfg: dict, limit: int = 40) -> dict:
    out = {"scored": 0, "errors": 0}
    prof, store = master.get(), Store(cfg)
    for j in db.in_state("extracted", order="id")[:limit]:
        if _paused():
            out["paused"] = True
            break
        try:
            score(db, cfg, j["id"], prof, store)
            _fails.pop(("score", j["id"]), None)
            out["scored"] += 1
        except Exception as e:
            log.warning("score failed job %s: %s", j["id"], e)
            out["errors"] += 1
            if _ollama_down(e):
                out["ollama"] = "down"
                break
            _failed(db, "score", j["id"], e)
    return out


def refit(db: DB, cfg: dict) -> dict:
    """Apply the current work-mode/pay preference (fit.py) to jobs scored before it existed, or after the
    floor changed. Idempotent: the score before the adjustment is kept as score_reasons.base_score."""
    out = {"adjusted": 0, "filtered": 0, "flagged": 0}
    for j in db.in_state("scored", "needs_review"):
        job = dict(j)
        detail = json.loads(job["score_reasons"] or "{}") or {}
        f = _facts(job).get("job_facts")
        jf = JobFacts.model_validate(f) if f else None
        a = fit.assess(jf, job, cfg)
        base = detail.get("base_score", job["score"] or 0)
        detail.update(base_score=base, work_mode=a["mode"], salary_eur=a["salary"], fit=a["label"])
        if a["filter"]:
            app = db.application(job["id"]) if job["state"] == "needs_review" else None
            if app and app["edited_by_jean"]:
                flags = [x for x in detail.get("red_flags", []) if not str(x).startswith("fully on-site")]
                detail["red_flags"] = flags + [a["filter"]]
                db.update_job(job["id"], score_reasons=detail)
                out["flagged"] += 1
                continue
            detail["excluded"] = a["filter"]
            if job["state"] == "needs_review":
                db.move(job["id"], "scored", reason="preference: " + a["filter"])
            db.move(job["id"], "filtered", reason=a["filter"], notes=a["filter"], score_reasons=json.dumps(
                detail, ensure_ascii=False))
            out["filtered"] += 1
            continue
        new = fit.apply(int(base), a)
        if new != job["score"] or "fit" not in json.loads(job["score_reasons"] or "{}"):
            db.update_job(job["id"], score=new, score_reasons=detail)
            out["adjusted"] += 1
    return out


def recover_interrupted(db: DB, max_age_min: float = 30) -> int:
    """A job left in 'generated' (worker killed or PC rebooted mid-letter) is otherwise never picked up again.
    Send it back to 'scored' so the next batch redrafts it. Only jobs untouched for max_age_min are moved."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_min)
    n = 0
    for j in db.in_state("generated", order="id"):
        try:
            ts = datetime.fromisoformat(j["updated_at"] or "")
        except ValueError:
            ts = None
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts and ts > cutoff:
            continue
        if db.try_move(j["id"], "failed", reason="generation interrupted") and \
                db.try_move(j["id"], "scored", reason="requeued after interrupted generation"):
            n += 1
    if n:
        log.info("requeued %s job(s) left in 'generated' by an interrupted run", n)
    return n


def generate_batch(db: DB, cfg: dict, limit: int | None = None) -> dict:
    cap = limit or cfg.get("limits", {}).get("max_drafts_per_run", 8)
    th = _th(cfg)["draft"]
    out = {"generated": 0, "needs_review": 0, "failed": 0}
    prof, store = master.get(), Store(cfg)
    for j in db.in_state("scored"):
        if out["generated"] >= cap:
            break
        if _paused():
            out["paused"] = True
            break
        if (j["score"] or 0) < th:
            continue
        # phase 1: letters and screening answers only, so the writer model stays loaded for the whole batch
        st = generate(db, cfg, j["id"], prof, store, defer_check=True)
        out["generated"] += 1
        out[st] = out.get(st, 0) + 1
        if st == "scored":  # requeued (paused, or Ollama stopped): don't try the next letters now
            if _paused():
                out["paused"] = True
            else:
                out["ollama"] = "down"
            break
    # phase 2: the second-opinion checks for every letter written above (and any left from a paused batch),
    # with the checker model loaded once, instead of writer -> checker -> writer for every letter
    if out.get("ollama") != "down":
        out["checked"] = check_pending(db, cfg, prof)
    return out


def check_pending(db: DB, cfg: dict, prof=None) -> int:
    """Letters waiting for review whose second-opinion check was deferred (validation.checker == "pending")."""
    prof = prof or master.get()
    n = 0
    if not llm.reachable(cfg):  # the check would only record "not run"; keep it pending for the next tick
        return 0
    for j in db.in_state("needs_review", order="id"):
        if _paused():
            break
        app = db.application(j["id"])
        if not app or app["edited_by_jean"]:
            continue
        try:
            v = json.loads(app["validation_json"] or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            continue
        if v.get("checker") != "pending" or not app["letter_json"]:
            continue
        try:
            nv = grounding.check_later(cfg, prof, json.loads(app["letter_json"]), v)
        except Exception as e:  # the letter already passed the deterministic checks; try again next tick
            log.warning("deferred check failed job %s: %s", j["id"], e)
            continue
        # the user may have edited and re-checked the letter while the check ran: never overwrite that result
        cur = db._write("UPDATE applications SET validation_json=?, validation_score=? "
                        "WHERE id=? AND COALESCE(edited_by_jean, 0)=0 AND letter_json=?",
                        (json.dumps(nv, ensure_ascii=False), nv["score"], app["id"], app["letter_json"]))
        n += cur.rowcount
    return n


LOCK = DATA / "run.lock"


def run_in_progress(max_age_h: float = 2.0) -> bool:
    import time
    return LOCK.exists() and time.time() - LOCK.stat().st_mtime < max_age_h * 3600


def run(cfg: dict, db: DB | None = None) -> dict:
    """One synchronous cycle (CLI `run`, or the scheduled task when workers are not installed)."""
    import os
    if run_in_progress():
        return {"skipped": "another run is in progress"}
    LOCK.write_text(str(os.getpid()))
    try:
        db = db or DB()
        started = now()
        summary: dict = ingest(db, cfg)
        summary.update(enrich_batch(db, cfg, limit=400))
        missing = llm.missing_models(cfg)
        if missing:
            summary["llm"] = f"skipped: pull {', '.join(missing)}"
        else:
            summary.update(prepare_batch(db, cfg, limit=400))
            summary.update(score_batch(db, cfg, limit=400))
            summary.update(generate_batch(db, cfg))
        if cfg.get("recruiters", {}).get("enabled"):
            try:
                from . import recruiters
                summary["recruiter_new"] = recruiters.pull(db, cfg)
                summary["recruiter_drafted"] = recruiters.classify_and_draft(db, cfg, None)
            except Exception as e:
                summary["recruiter_error"] = str(e)
        db.log_run(started, summary)
        return summary
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


def draft_job(db: DB, cfg: dict, cv: str | None, job_id: int) -> bool:
    """Dashboard 'Draft' / 'Redraft' button: run grounded generation now for one job."""
    job = db.job(job_id)
    if job["state"] in ("queued",):
        prepare(db, cfg, job_id)
    if db.job(job_id)["state"] == "extracted":
        score(db, cfg, job_id)
    return generate(db, cfg, job_id, actor="jean") == "needs_review"
