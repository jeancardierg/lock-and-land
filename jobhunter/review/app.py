"""Review gateway: one card per job, three keys. A = approve and apply, E = edit, R = reject.

No server and no port: the app window calls these routes in-process over a private pipe (desktop/ipc.py).
Web applications are always done by hand: "Approve & apply" copies the letter and screening answers and opens the
ad in your normal browser (the app never fills forms: bot protection blocks automation). You then press 'I applied'.
Email applications keep v0.2's safe path: exact recipient shown, second confirmation, CV PDF checked.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .. import accounts, fit, gmail, grounding, master, sqldb
from ..config import attachment_problem, cv_attachment, load_config
from ..db import DB
from ..routing import route
from ..states import IllegalTransition

log = logging.getLogger(__name__)
app = FastAPI(title="Lock & Land", docs_url=None, redoc_url=None)
from .connections import router as _connections  # noqa: E402

app.include_router(_connections)


@app.middleware("http")
async def _same_origin_only(request, call_next):
    """Defence in depth: the routes are never on a network port, but if they were ever served, a web page in your
    browser still couldn't approve, reject or read jobs (cross-site writes and DNS-rebinding reads are refused).
    The app window calls the routes in-process with no Origin header and a local Host, so it is unaffected."""
    from fastapi.responses import JSONResponse
    from urllib.parse import urlsplit
    host = (request.headers.get("host") or "").lower()
    name = host.rsplit(":", 1)[0].strip("[]") if not host.startswith("[") or "]:" in host else host.strip("[]")
    allowed = {"127.0.0.1", "::1", "localhost", "testserver"}
    if name not in allowed:
        return JSONResponse({"detail": "Bad Host header"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
        o = urlsplit(origin)
        if origin == "null" or (o.netloc or "").lower() != host:
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    return await call_next(request)


@app.exception_handler(sqldb.OperationalError)
async def _db_busy(request, exc):
    """A write that waited the full busy timeout. Say so plainly (nothing was saved) instead of a bare error."""
    from fastapi.responses import JSONResponse
    log.error("%s %s: %s", request.method, request.url.path, exc)
    if "locked" in str(exc) or "busy" in str(exc):
        return JSONResponse({"detail": "The database was busy with background work, so this was not saved. "
                                       "Try again in a few seconds."}, status_code=503)
    return JSONResponse({"detail": f"Database error: {exc}"}, status_code=500)


HERE = Path(__file__).parent
REJECT_REASONS = ["wrong role", "location", "salary or level", "company", "other"]


def _db() -> DB:
    return DB.shared()


def workers_alive() -> bool:
    """The workers write data/workers.heartbeat every 5 s."""
    import time
    from ..config import DATA
    hb = DATA / "workers.heartbeat"
    try:
        return time.time() - float(hb.read_text().strip() or 0) < 30
    except (OSError, ValueError):
        return False


def safe_url(u) -> str:
    """Job links come from outside (boards, feeds, ads, HN comments, the extractor). Only http(s) links reach the
    page, so a `javascript:` or `data:` link in an ad can't run code inside the app window."""
    u = str(u or "").strip()
    return u if re.match(r"(?i)^https?://[^\s]+$", u) else ""


def _one_line(s, n: int = 120) -> str:
    """Text from an ad shown in a native dialog: one line, short, so an ad can't write its own instructions there."""
    return re.sub(r"\s+", " ", str(s or ""))[:n]


def _j(s) -> dict | list:
    try:
        return json.loads(s or "null") or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def handoff_text(letter: str, answers) -> str:
    """What 'Approve & apply' puts on the clipboard: the letter, then any screening answers to paste."""
    rows = [a for a in (answers or []) if isinstance(a, dict) and (a.get("answer") or "").strip()]
    if not rows:
        return letter
    qa = "\n\n".join(f"{a.get('question', '').strip()}\n{a['answer'].strip()}" for a in rows)
    return f"{letter}\n\n---\nScreening answers\n\n{qa}"


def card(db: DB, job, cfg: dict) -> dict:
    app_row = db.application(job["id"])
    notes = _j(job["notes"]) or {}
    detail = _j(job["score_reasons"]) or {}
    facts = master.get().by_id if master.exists() else {}
    letter_json = _j(app_row["letter_json"]) if app_row else {}
    sentences = []
    for k in ("p1", "p2", "p3"):
        for s in (letter_json or {}).get(k, []):
            sentences.append({**s, "facts": [facts[f].text for f in s.get("fact_ids", []) if f in facts]})
    validation = _j(app_row["validation_json"]) if app_row else {}
    retrieval = _j(app_row["retrieval_json"]) if app_row else {}
    variant = notes.get("cv_variant", "main")
    tcv = tailored(notes)
    use_tailored = bool(tcv) and notes.get("cv_choice", "tailored" if _attach_tailored(cfg) else "standard") == "tailored"
    att = Path(tcv["pdf"]) if use_tailored else cv_attachment(cfg, variant)
    r = route(job)
    out = {
        "id": job["id"], "title": job["title"], "company": job["company"], "location": job["location"],
        "remote": bool(job["remote"]), "score": job["score"], "coverage": job["embed_score"], "state": job["state"],
        "source": job["source"], "ats": job["ats"], "route": r, "url": safe_url(job["apply_url"] or job["url"]),
        "letter": (app_row["letter_text"] if app_row else None) or job["cover_letter"] or "",
        "subject": (app_row["subject"] if app_row else None) or notes.get("email_subject") or "",
        "sentences": sentences, "validation": validation, "gaps": (retrieval or {}).get("gaps", []),
        "reasons": detail.get("reasons", []), "red_flags": detail.get("red_flags", []),
        "answers": _j(app_row["answers_json"]) if app_row else [], "edited": bool(app_row and app_row["edited_by_jean"]),
        "attachment": att.name, "attachment_problem": attachment_problem(att), "cv_variant": variant,
        "application_id": app_row["id"] if app_row else None,
        "attachment_path": str(att), "attachment_kind": "tailored" if use_tailored else "standard",
        "tailored_cv": _cv_card(tcv), "tailored_cv_error": notes.get("tailored_cv_error"),
    }
    if r == "email":
        try:
            out["recipient"] = gmail.single_recipient(job["apply_email"])
        except gmail.SendBlocked as e:
            out["recipient_problem"] = str(e)
    return out


def tailored(notes: dict) -> dict | None:
    """The job's tailored CV from its notes, if its PDF is still on disk."""
    t = (notes or {}).get("tailored_cv")
    if isinstance(t, dict) and t.get("pdf") and Path(t["pdf"]).exists():
        return t
    return None


def _attach_tailored(cfg: dict) -> bool:
    from ..tailored_cv import _ccfg
    return bool(_ccfg(cfg)["attach"])


def _cv_card(t: dict | None) -> dict | None:
    if not t:
        return None
    cv = t.get("cv") or {}
    return {"pdf_name": Path(t["pdf"]).name, "lang": t.get("lang"), "summary_source": t.get("summary_source"),
            "keywords": t.get("keywords") or {}, "checks": t.get("checks") or {}, "notes": t.get("notes") or [],
            "headline": cv.get("headline", ""), "summary": cv.get("summary", []), "skills": cv.get("skills", []),
            "roles": cv.get("roles", [])}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    import os
    return {"ok": True, "desktop": os.environ.get("JOBHUNTER_DESKTOP") == "1"}


@app.get("/api/queue")
def queue() -> dict:
    cfg, db = load_config(), _db()
    review = [card(db, j, cfg) for j in db.in_state("needs_review")]
    waiting = [card(db, j, cfg) for j in db.in_state("awaiting_click", "needs_human", "approved", "submitting",
                                                       order="updated_at")]
    counts = dict(db.conn.execute("SELECT state, count(*) FROM jobs GROUP BY state").fetchall())
    prof = master.get() if master.exists() else None
    return {"review": review, "waiting": waiting, "counts": counts, "submitted_today": db.submitted_today(),
            "daily_cap": cfg.get("limits", {}).get("submissions_per_day", 15), "reasons": REJECT_REASONS,
            "profile_approved": bool(prof and prof.approved),
            "profile_missing": prof is None, "workers_alive": workers_alive()}


# friendly names for the All jobs list
STAGE = {"queued": "new", "extracted": "new", "scored": "scored", "generated": "writing letter",
         "needs_review": "letter ready", "approved": "apply now", "submitting": "apply now",
         "awaiting_click": "apply now", "needs_human": "apply now", "submitted": "applied",
         "filtered": "filtered out", "relocation": "relocation", "rejected": "skipped", "failed": "error"}


def list_row(j, cfg: dict) -> dict:
    job = dict(j)
    detail = _j(job.get("score_reasons")) or {}
    facts = (_j(job.get("facts_json")) or {}).get("job_facts")
    jf = None
    if facts:
        try:
            from ..schemas import JobFacts
            jf = JobFacts.model_validate(facts)
        except Exception:
            jf = None
    a = fit.assess(jf, job, cfg)
    why = detail.get("excluded") or detail.get("relocation") or ""
    if not why and job["state"] in ("filtered", "rejected", "failed"):
        why = (job.get("notes") or "")[:200] if not str(job.get("notes") or "").startswith("{") else ""
    return {
        "id": job["id"], "title": job["title"], "company": job["company"] or "", "location": job["location"] or "",
        "mode": a["mode"], "salary": a["salary"], "fit": a["label"], "score": job["score"],
        "coverage": job.get("embed_score"), "state": job["state"], "stage": STAGE.get(job["state"], job["state"]),
        "source": job["source"], "url": safe_url(job.get("apply_url") or job["url"]), "posted": (job["posted_at"] or "")[:10],
        "fetched": (job["fetched_at"] or "")[:10], "why": why, "reasons": detail.get("reasons", [])[:4],
        "red_flags": detail.get("red_flags", [])[:4], "gaps": detail.get("gaps", [])[:6],
        "snippet": re.sub(r"\s+", " ", job.get("description") or "")[:600],
    }


@app.get("/api/jobs/{job_id}/card")
def one_card(job_id: int) -> dict:
    """One letter card, so 'Review the letter' can show it without rebuilding the whole queue."""
    db = _db()
    j = db.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not j:
        raise HTTPException(404, "No such job")
    return card(db, j, load_config())


@app.get("/api/jobs")
def all_jobs() -> dict:
    """Every job, compact, for the All jobs list. Filtering and sorting happen in the page."""
    cfg, db = load_config(), _db()
    rows = db.conn.execute("SELECT * FROM jobs ORDER BY COALESCE(score, -1) DESC, posted_at DESC").fetchall()
    p = fit.prefs(cfg)
    return {"jobs": [list_row(j, cfg) for j in rows],
            "prefs": {"onsite_min_salary_eur": p["onsite_min_salary_eur"],
                      "draft_score": (cfg.get("thresholds") or {}).get("draft_score", 70)}}


@app.post("/api/jobs/{job_id}/restore")
def restore(job_id: int) -> dict:
    db = _db()
    _job(db, job_id)
    try:
        db.move(job_id, "scored", actor="jean", reason="you brought it back")
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    return {"done": "Back in the list as scored. Press Write letter to draft one."}


def _job(db: DB, job_id: int):
    j = db.job(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j


@app.post("/api/jobs/{job_id}/approve")
def approve(job_id: int, body: dict = Body(default={})) -> dict:
    cfg, db = load_config(), _db()
    job = _job(db, job_id)
    c = card(db, job, cfg)
    if c["validation"].get("hard") and not body.get("override"):
        return {"confirm": "This letter has unresolved problems (red). Approve anyway?", "override": True}
    r = c["route"]
    try:
        if r == "email":
            if c.get("recipient_problem") or c["attachment_problem"]:
                raise HTTPException(400, c.get("recipient_problem") or c["attachment_problem"])
            if not body.get("confirm_email"):
                return {"confirm": f"Send to {c['recipient']} with {c['attachment']} attached?", "confirm_email": True}
            from ..desktop.confirm import require
            require(f"Send this application by email to {c['recipient']}?",
                    f"Job: {_one_line(job['title'])} at {_one_line(job['company'] or '?')}\n"
                    f"Attachment: {c['attachment']}")
            db.move(job_id, "approved", actor="jean", reason="approved email application")
            db.move(job_id, "submitting", actor="jean", reason="sending email")
            try:
                gmail.send(c["recipient"], c["subject"] or f"Application: {job['title']}", c["letter"],
                           cfg["profile"]["email"], attachment=Path(c["attachment_path"]))
            except Exception as e:
                db.move(job_id, "approved", reason=f"send failed: {e}"[:300])
                raise HTTPException(502, f"Not sent: {e}")
            db.move(job_id, "submitted", actor="jean", reason=f"emailed to {c['recipient']}")
            return {"done": "Sent by email."}
        db.move(job_id, "approved", actor="jean", reason="approved, apply by hand")
        text = handoff_text(c["letter"], c["answers"])
        what = "Letter and screening answers copied" if text != c["letter"] else "Letter copied"
        where = "Apply in your own browser" if c["url"] else "Apply as the ad says"
        cv = f" Upload {c['attachment']} as your CV (Open CV on the Waiting tab)." if c["tailored_cv"] else ""
        return {"open": c["url"] or None, "copy": text,
                "done": f"{what}. {where}, then press 'I applied' on the Waiting tab.{cv}"}
    except IllegalTransition as e:
        raise HTTPException(409, str(e))


@app.post("/api/jobs/{job_id}/edit")
def edit(job_id: int, body: dict = Body(...)) -> dict:
    cfg, db = load_config(), _db()
    job = _job(db, job_id)
    app_row = db.application(job_id)
    letter = (body.get("letter") or "").strip()
    if not letter:
        raise HTTPException(400, "empty letter")
    lang = (app_row["lang"] if app_row else None) or "en"
    v = grounding.revalidate_text(cfg, master.get(), _j(app_row["letter_json"]) if app_row else None, letter, lang)
    fields = {"letter_text": letter, "validation_json": v, "validation_score": v["score"], "edited_by_jean": 1}
    if "answers" in body:
        fields["answers_json"] = body["answers"]
    if app_row:
        db.update_application(app_row["id"], **fields)
    db.update_job(job_id, cover_letter=letter)
    if job["state"] != "needs_review":
        try:
            db.move(job_id, "needs_review", actor="jean", reason="edited")
        except IllegalTransition:
            pass
    return {"validation": v}


@app.post("/api/jobs/{job_id}/answer")
def answer(job_id: int, body: dict = Body(...)) -> dict:
    """the user answers a screening question. With save=true it goes to the Q&A bank for next time."""
    db = _db()
    _job(db, job_id)
    app_row = db.application(job_id)
    if not app_row:
        raise HTTPException(404, "no application")
    answers = _j(app_row["answers_json"]) or []
    q, a = body.get("question", ""), (body.get("answer") or "").strip()
    for x in answers:
        if x["question"] == q:
            x.update(answer=a, source="jean", needs_jean=not a)
            break
    else:
        answers.append({"question": q, "answer": a, "source": "jean", "needs_jean": not a, "key": None,
                        "fact_ids": [], "note": ""})
    db.update_application(app_row["id"], answers_json=answers)
    if body.get("save") and a:
        key = next((x.get("key") for x in answers if x["question"] == q and x.get("key")), None) or \
            re.sub(r"[^a-z0-9]+", "_", q.lower()).strip("_")[:50]
        master.save_qa(key, a, q)
    return {"answers": answers}


@app.post("/api/jobs/{job_id}/reject")
def reject(job_id: int, body: dict = Body(default={})) -> dict:
    db = _db()
    _job(db, job_id)
    reason = body.get("reason") or "rejected"
    try:
        db.move(job_id, "rejected", actor="jean", reason=reason)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    return {"done": f"Rejected ({reason})."}


@app.post("/api/jobs/{job_id}/redraft")
def redraft(job_id: int) -> dict:
    db = _db()
    job = _job(db, job_id)
    if job["state"] in ("filtered", "rejected"):   # "Write letter" from the All jobs list
        db.move(job_id, "scored", actor="jean", reason="you asked for a letter")
        job = _job(db, job_id)
    if job["state"] not in ("needs_review", "scored", "failed", "relocation", "queued", "extracted"):
        raise HTTPException(409, f"can't write a letter for a job that is {STAGE.get(job['state'], job['state'])}")
    if not master.exists():
        raise HTTPException(409, master.MISSING_HINT)
    if workers_alive():
        try:
            from ..tasks import generate_one
            generate_one(job_id)
            return {"done": "Redraft queued: the new letter appears here in about a minute."}
        except Exception as e:
            log.warning("could not enqueue redraft: %s", e)
    # no workers: write it here, in the background, so the page stays responsive
    import threading

    def run():
        try:
            from ..pipeline import draft_job
            draft_job(DB(), load_config(), None, job_id)
        except Exception as e:
            log.warning("redraft %s failed: %s", job_id, e)
    threading.Thread(target=run, daemon=True).start()
    return {"done": "Redrafting now (about a minute). The card disappears while it's written and comes back."}


@app.post("/api/jobs/{job_id}/submitted")
def submitted(job_id: int) -> dict:
    db = _db()
    _job(db, job_id)
    try:
        db.move(job_id, "submitted", actor="jean", reason="you confirmed it was submitted")
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    return {"done": "Marked as applied. It's in the Tracker now."}


@app.get("/api/jobs/{job_id}/handoff")
def handoff(job_id: int) -> dict:
    """The letter and answers again, for 'Copy letter' on the Waiting tab."""
    cfg, db = load_config(), _db()
    c = card(db, _job(db, job_id), cfg)
    return {"copy": handoff_text(c["letter"], c["answers"]), "open": c["url"] or None}


# ---------------- tailored CV ----------------

def _notes(job) -> dict:
    n = _j(job["notes"])
    return n if isinstance(n, dict) else {}


@app.post("/api/jobs/{job_id}/cv/open")
def cv_open(job_id: int, body: dict = Body(default={})) -> dict:
    """Open the tailored CV (pdf or docx) in your own app, or its folder. Only the job's own files can be opened."""
    from .. import tailored_cv
    db = _db()
    t = tailored(_notes(_job(db, job_id)))
    if not t:
        raise HTTPException(404, "This job has no tailored CV yet. Press Build CV.")
    what = body.get("what") or "pdf"
    p = Path(t["pdf"]).parent if what == "folder" else Path(t["docx"] if what == "docx" else t["pdf"])
    if not p.exists():
        raise HTTPException(404, f"{p.name} is no longer on disk. Press Build CV again.")
    tailored_cv.open_file(p)
    return {"done": f"Opened {p.name}." if what != "folder" else "Opened the folder."}


@app.post("/api/jobs/{job_id}/cv/choice")
def cv_choice(job_id: int, body: dict = Body(...)) -> dict:
    """Which CV an email application attaches: the tailored one or your standard one."""
    db = _db()
    job = _job(db, job_id)
    use = body.get("use")
    if use not in ("tailored", "standard"):
        raise HTTPException(400, "use must be tailored or standard")
    notes = _notes(job)
    if use == "tailored" and not tailored(notes):
        raise HTTPException(409, "This job has no tailored CV yet. Press Build CV.")
    notes["cv_choice"] = use
    db.update_job(job_id, notes=json.dumps(notes, ensure_ascii=False))
    return {"done": "Email applications attach the tailored CV." if use == "tailored"
            else "Email applications attach your standard CV."}


@app.post("/api/jobs/{job_id}/cv/build")
def cv_build(job_id: int) -> dict:
    """Tailor CV: build (or rebuild) the job's tailored CV in the background. Works with or without a letter."""
    db = _db()
    _job(db, job_id)
    if not master.exists():
        raise HTTPException(409, master.MISSING_HINT)
    import threading

    def run():
        try:
            from ..pipeline import rebuild_cv
            rebuild_cv(DB(), load_config(), job_id)
        except Exception as e:
            log.warning("CV build %s failed: %s", job_id, e)
    threading.Thread(target=run, daemon=True).start()
    return {"done": "Tailoring your CV to this ad (under a minute). It shows on the card when ready."}


# ---------------- a job ad you paste in ----------------

@app.post("/api/jobs/add")
def add_job(body: dict = Body(...)) -> dict:
    """A job from an ad you paste (sites behind a sign-up wall, a recruiter's message, a PDF). It goes through the
    same steps as found jobs (scored, then a letter is written whatever its score) and, unless tailor_cv is false,
    a tailored CV is built with the letter."""
    import hashlib
    db = _db()
    title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:200]
    company = re.sub(r"\s+", " ", str(body.get("company") or "")).strip()[:200]
    text = str(body.get("text") or "").strip()[:30000]
    url = safe_url(body.get("url"))
    if not title:
        raise HTTPException(400, "Give the job title.")
    if len(text) < 150:
        raise HTTPException(400, "Paste the whole ad text (it looks too short to tailor a CV to).")
    if body.get("url") and not url:
        raise HTTPException(400, "The link must start with http:// or https://")
    email = str(body.get("apply_email") or "").strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email, re.I):
        raise HTTPException(400, "That email address doesn't look right.")
    loc = re.sub(r"\s+", " ", str(body.get("location") or "")).strip()[:120]
    remote = bool(re.search(r"(?i)\b(remote|fully remote|teletrabajo|remoto|100% remot)", f"{title} {loc} {text}"))
    ext = hashlib.sha1(f"{title}|{company}|{text}".encode("utf-8")).hexdigest()[:16]
    if not db.upsert_job({"source": "pasted", "external_id": ext, "title": title, "company": company or None,
                          "location": loc or None, "remote": remote, "url": url or None,
                          "apply_email": email or None, "description": text, "posted_at": ""}):
        raise HTTPException(409, "This job is already in your list (same title and company).")
    job_id = db.conn.execute("SELECT id FROM jobs WHERE source='pasted' AND external_id=?", (ext,)).fetchone()[0]
    want_cv = body.get("tailor_cv", True) is not False
    if want_cv:
        db.update_job(job_id, notes=json.dumps({"want_cv": True}))
    what = "letter and tailored CV" if want_cv else "letter"
    if not master.exists():
        return {"id": job_id, "done": "Added. " + master.MISSING_HINT}
    if workers_alive():
        try:
            from ..tasks import generate_one
            generate_one(job_id)
            return {"id": job_id, "done": f"Added. Its {what} appear under To review in a minute or two."}
        except Exception as e:
            log.warning("could not enqueue pasted job: %s", e)
    import threading

    def run():
        try:
            from ..pipeline import draft_job
            draft_job(DB(), load_config(), None, job_id)
        except Exception as e:
            log.warning("pasted job %s failed: %s", job_id, e)
    threading.Thread(target=run, daemon=True).start()
    return {"id": job_id, "done": f"Added. Writing its {what} now (a minute or two)."}


# ---------------- passwords saved for job sites by earlier versions ----------------

@app.get("/api/accounts")
def accounts_list() -> dict:
    try:
        return {"accounts": [{k: a.get(k) for k in ("site", "email", "company", "status", "added")}
                             for a in accounts.listing()]}
    except Exception:
        return {"accounts": [], "problem": "The keychain isn't available."}


@app.post("/api/accounts/reveal")
def accounts_reveal(body: dict = Body(...)) -> dict:
    from ..desktop.confirm import require
    site = str(body.get("site") or "")
    if not accounts.get(site) or not accounts.password(site):
        raise HTTPException(404, "No saved account for that site.")
    require(f"Copy your saved password for {_one_line(site, 80)} to the clipboard?")
    return {"password": accounts.password(site), "done": "Password copied."}


@app.post("/api/accounts/forget")
def accounts_forget(body: dict = Body(...)) -> dict:
    from ..desktop.confirm import require
    site = str(body.get("site") or "")
    if not accounts.get(site):
        raise HTTPException(404, "No saved account for that site.")
    require(f"Forget the saved account for {_one_line(site, 80)}?",
            "The password is deleted from your keychain. The account on the site itself stays: close it there "
            "if you want it gone.")
    accounts.forget(site)
    return {"done": "Forgotten."}
