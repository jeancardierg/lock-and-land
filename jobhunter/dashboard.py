"""jobhunter: daily interface. Start with scripts\\dashboard.cmd (or the desktop shortcut) -> http://localhost:8501

Safety rules kept here:
- Nothing is sent without a ticked "I've reviewed this" box plus a click, and the exact recipient on screen.
- LinkedIn is never automated: the app opens pages in your own browser and you paste and send yourself.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from jobhunter import drafting, gmail, master, recruiters, status  # noqa: E402
# Faster review with keyboard keys: http://127.0.0.1:8502 (python -m jobhunter review)
from jobhunter.config import attachment_problem, cv_attachment, load_config  # noqa: E402
from jobhunter.db import DB  # noqa: E402
from jobhunter.pipeline import draft_job, run_in_progress  # noqa: E402

st.set_page_config(page_title="jobhunter", page_icon="💼", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>
.block-container {padding-top: 3.2rem; max-width: 1500px;}
div[data-testid="stMetricValue"] {font-size: 1.6rem;}
.jh-pill {display:inline-block;padding:2px 10px;border-radius:999px;font-size:.8rem;margin-right:6px;
          background:rgba(127,127,127,.15);}
.jh-ok {color:#1a7f37;} .jh-warn {color:#b35900;} .jh-bad {color:#cf222e;}
</style>""", unsafe_allow_html=True)

cfg = load_config()
db = DB()
TRACK_STAGES = ["approved", "applied", "interview", "offer", "rejected", "withdrawn"]


# ---------------- helpers ----------------
def local(ts: str | datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    if not ts:
        return "—"
    try:
        d = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            return d.strftime(fmt)  # Task Scheduler gives local time already
        return d.astimezone().strftime(fmt)
    except ValueError:
        return str(ts)[:16]


def safe_url(u: str | None) -> str | None:
    return u if u and u.lower().startswith(("https://", "http://")) else None


def jload(s: str | None) -> dict:
    try:
        return json.loads(s or "{}") or {}
    except json.JSONDecodeError:
        return {}


def badge(score: int | None) -> str:
    s = score or 0
    return "🟢" if s >= 80 else "🟡" if s >= 65 else "⚪"


# ---------------- sidebar: status ----------------
with st.sidebar:
    st.markdown("## 💼 jobhunter")
    st.caption("Finds, scores and drafts. Sends nothing without your click.")

    running = run_in_progress()
    if running:
        st.info("⏳ A run is in progress (scheduled or manual). Refresh in a few minutes.")
    if st.button("▶ Run now", width="stretch", disabled=running, type="primary",
                 help="Fetch, score and draft in the background. Never sends anything."):
        status.start_background(["run"])
        st.toast("Run started in the background")
        st.rerun()
    if st.button("🔄 Refresh", width="stretch"):
        st.rerun()

    st.divider()
    st.markdown("**Runs**")
    finished, summ = status.last_summary(db)
    st.caption(f"Last run: {local(finished)}")
    if summ:
        st.caption(" · ".join(f"{k.replace('_', ' ')}: {v}" for k, v in summ.items()
                              if not isinstance(v, dict) and k != "skipped") or summ.get("skipped", ""))
    task = status.scheduled_task()
    if task:
        st.caption(f"Next scheduled: {local(task.get('next'))} · task {task.get('state', '').lower()}")
    elif sys.platform == "win32":
        st.caption("⚠️ No scheduled task. Run `scripts\\register_task.ps1 -Hours 3`.")

    st.markdown("**Gmail**")
    g = status.gmail_status(cfg)
    if g["state"] == "missing":
        st.markdown("<span class='jh-bad'>● Not signed in</span>", unsafe_allow_html=True)
    elif g["expires_at"]:
        cls = {"ok": "jh-ok", "expiring": "jh-warn", "expired": "jh-bad"}[g["state"]]
        st.markdown(f"<span class='{cls}'>● Signed in · renew by {local(g['expires_at'])}</span>", unsafe_allow_html=True)
    else:
        st.markdown("<span class='jh-ok'>● Signed in</span>", unsafe_allow_html=True)
    if st.button("🔑 Sign in to Gmail again", width="stretch",
                 help="Opens Google's sign-in in your browser. Needed about once a week while the app is in Testing."):
        gmail._service = None
        status.start_background(["gmail-auth", "--force"])
        st.toast("Opening Google sign-in in your browser: pick your Gmail and click Continue")
        st.caption("Nothing opened after 10 s? In PowerShell, in this folder: "
                   "`.\\.venv\\Scripts\\python.exe -m jobhunter gmail-auth --force`")
    err = status.DATA / "gmail_auth_error.txt"
    if err.exists():
        st.error("Last Gmail sign-in failed: " + err.read_text(encoding="utf-8")[:300])

    st.markdown("**Sources (last run)**")
    for name, v in (summ.get("sources") or {}).items():
        icon = "🟢" if v.startswith("ok") else "🔴"
        st.caption(f"{icon} {name}: {v}")
    if not summ.get("sources"):
        st.caption("No source data yet. Run once.")
    for k in ("recruiter_error", "draft_error"):
        if summ.get(k):
            st.caption(f"🔴 {k.replace('_', ' ')}: {summ[k][:160]}")


# ---------------- header metrics ----------------
all_jobs = db.jobs()
counts: dict[str, int] = {}
for j in all_jobs:
    counts[j["status"]] = counts.get(j["status"], 0) + 1
week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
applied_week = sum(1 for j in all_jobs if j["status"] in ("applied", "interview", "offer") and (j["updated_at"] or "") >= week_ago)
st.markdown("#### Today")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("To review", counts.get("shortlisted", 0) + counts.get("drafted", 0))
m2.metric("Drafts ready", counts.get("drafted", 0))
m3.metric("Applied (7 days)", applied_week)
m4.metric("Interviews", counts.get("interview", 0))
m5.metric("Recruiter replies to review", len(db.msgs(["drafted"])))

tab_jobs, tab_reloc, tab_rec, tab_track, tab_help = st.tabs(
    ["📋 Jobs", f"🧳 Relocation needed ({counts.get('relocation', 0)})", "✉️ Recruiters", "📈 Tracker", "❓ Help"])


# ---------------- jobs ----------------
def job_actions(j, kp: str = "") -> None:
    n = jload(j["notes"])
    url = safe_url(j["url"])
    st.markdown(f"### {badge(j['score'])} {j['title']}")
    st.markdown(f"<span class='jh-pill'>{j['company'] or '—'}</span><span class='jh-pill'>{j['location'] or '—'}</span>"
                f"{'<span class=jh-pill>🌍 remote</span>' if j['remote'] else ''}"
                f"<span class='jh-pill'>score {j['score'] if j['score'] is not None else '—'}</span>"
                f"<span class='jh-pill'>{j['source']}</span><span class='jh-pill'>{j['status']}</span>",
                unsafe_allow_html=True)
    d = jload(j["score_reasons"])
    if d.get("relocation"):
        st.info(f"🧳 Relocation needed: {d['relocation']}")
    if d.get("excluded"):
        st.error(f"Excluded: {d['excluded']}")
    c1, c2 = st.columns(2)
    with c1:
        if d.get("reasons"):
            st.markdown("**Why it fits**\n\n" + "\n".join(f"- {r}" for r in d["reasons"]))
        if d.get("keyword_hits"):
            st.caption("Keywords: " + ", ".join(d["keyword_hits"]))
    with c2:
        flags = list(d.get("red_flags") or [])
        if d.get("language_required"):
            flags.append(f"Language: {d['language_required']}")
        if flags:
            st.markdown("**Watch out**\n\n" + "\n".join(f"- {r}" for r in flags))
    if url:
        st.link_button("🌐 Open the job ad", url)
    with st.expander("Job description"):
        st.text((j["description"] or "")[:8000])

    st.divider()
    if not j["cover_letter"]:
        st.caption("No draft yet.")
        if st.button("✍️ Draft cover letter", key=f"{kp}d{j['id']}", type="primary"):
            with st.spinner("Drafting locally (about a minute)…"):
                draft_job(db, cfg, None, j["id"])
            st.rerun()
    else:
        variant = n.get("cv_variant", "main")
        st.caption(f"Drafted from your **{variant}** CV. Read every line before sending.")
        app_row = db.application(j["id"])
        v = json.loads(app_row["validation_json"] or "{}") if app_row else {}
        if v.get("hard") or v.get("soft"):
            st.warning("**Fact checks** (fix before sending):\n\n"
                       + "\n".join(f"- {x}" for x in (v.get("hard") or []) + (v.get("soft") or [])))
        elif n.get("unsupported"):
            st.warning("**Check these before sending** (the CV may not support them):\n\n"
                       + "\n".join(f"- {u}" for u in n["unsupported"]))
        cl = st.text_area("Cover letter (edit freely)", j["cover_letter"], height=330, key=f"{kp}cl{j['id']}")
        with st.expander("Tailored CV summary and bullets"):
            st.text_area("Summary", j["cv_summary"] or "", height=180, key=f"{kp}cv{j['id']}", label_visibility="collapsed")
        b1, b2, b3 = st.columns(3)
        if b1.button("💾 Save edits", key=f"{kp}s{j['id']}", width="stretch"):
            db.update_job(j["id"], cover_letter=cl)
            st.toast("Saved")
        if b2.button("📄 Export .docx", key=f"{kp}x{j['id']}", width="stretch"):
            p = drafting.export_docx(dict(j), cl, master.display_name(cfg))
            st.success(f"Saved to {p}")
        if b3.button("🔁 Redraft", key=f"{kp}rd{j['id']}", width="stretch"):
            with st.spinner("Redrafting locally…"):
                draft_job(db, cfg, None, j["id"])
            st.rerun()

        st.markdown("#### Apply")
        if j["apply_email"]:
            att = cv_attachment(cfg, variant)
            try:
                to = gmail.single_recipient(j["apply_email"])
            except gmail.SendBlocked as e:
                to = None
                st.error(f"Can't email this one: {e}")
            if to:
                subj = n.get("email_subject") or f"Application: {j['title']} — {cfg['profile']['name']}"
                att_problem = attachment_problem(att)
                st.markdown(f"**To:** `{to}`  \n**Subject:** {subj}  \n**Attachment:** `{att.name}`")
                if att_problem:
                    st.error(f"Sending blocked: {att_problem}")
                ok = st.checkbox("I've reviewed the letter, recipient and attachment", key=f"{kp}ok{j['id']}")
                if st.button("📧 Approve & send email", key=f"{kp}e{j['id']}", type="primary",
                             disabled=bool(att_problem) or not (ok and cl.strip())):
                    db.update_job(j["id"], cover_letter=cl)
                    if db.try_move(j["id"], "approved", actor="jean", reason="dashboard email approve") and \
                            db.try_move(j["id"], "submitting", actor="jean", reason="sending email"):
                        try:
                            gmail.send(to, subj, cl, cfg["profile"]["email"], attachment=att)
                        except Exception as e:
                            db.try_move(j["id"], "approved", reason=f"send failed: {e}"[:300])
                            st.error(f"Not sent: {e}")
                        else:
                            db.move(j["id"], "submitted", actor="jean", reason=f"emailed to {to}")
                            st.success("Sent. Moved to Tracker.")
                            st.rerun()
                    else:
                        st.warning("Already sent, being sent, or not ready for review.")
        elif url:
            st.caption("1. Copy the letter (copy icon, top right of the box). 2. Open the ad and apply in your browser. "
                       "3. Come back and click **I applied**.")
            st.code(cl, language=None, wrap_lines=True)
            a1, a2 = st.columns(2)
            a1.link_button("🌐 Open ad to apply", url, type="primary", width="stretch")
            if a2.button("✔ I applied", key=f"{kp}ap{j['id']}", width="stretch"):
                db.update_job(j["id"], cover_letter=cl)
                if not db.try_move(j["id"], "submitted", actor="jean", reason="you applied by hand"):
                    db.try_move(j["id"], "approved", actor="jean", reason="applied by hand")
                    db.try_move(j["id"], "submitted", actor="jean", reason="you applied by hand")
                st.rerun()
    st.divider()
    if st.button("🗑 Skip this job", key=f"{kp}k{j['id']}"):
        if not db.try_move(j["id"], "rejected", actor="jean", reason="skipped in dashboard"):
            st.warning("This job can't be skipped from its current stage.")
        st.session_state.pop("job_sel", None)
        st.rerun()


def jobs_view(kp: str, status_options: list[str], default: list[str], default_min: int, empty: str) -> None:
    f1, f2, f3, f4, f5 = st.columns([2, 2, 2, 1, 3])
    statuses = f1.multiselect("Status", status_options, default=default, key=f"{kp}st")
    min_score = f2.slider("Min score", 0, 100, default_min, key=f"{kp}ms")
    sources = sorted({j["source"] for j in all_jobs})
    src_f = f3.multiselect("Source", sources, default=[], key=f"{kp}src")
    only_remote = f4.toggle("Remote", key=f"{kp}rem")
    text_f = f5.text_input("Search title, company, location", placeholder="e.g. Intune, Madrid", key=f"{kp}q")
    queue = [j for j in db.jobs(statuses or status_options)
             if (j["score"] or 0) >= min_score and (not only_remote or j["remote"])
             and (not src_f or j["source"] in src_f)
             and (not text_f or text_f.lower() in f"{j['title']} {j['company']} {j['location']}".lower())]

    left, right = st.columns([6, 5], gap="large")
    with left:
        st.caption(f"{len(queue)} jobs · click a row to open it")
        rows = [{"Score": j["score"], "Title": j["title"], "Company": j["company"],
                 "Where": ("🌍 " if j["remote"] else "") + (j["location"] or ""),
                 "Draft": "✍️" if j["cover_letter"] else ""} for j in queue]
        ev = st.dataframe(rows, hide_index=True, width="stretch", height=min(640, 38 + 35 * max(1, len(rows))),
                          on_select="rerun", selection_mode="single-row", key=f"{kp}table",
                          column_config={"Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100,
                                                                                  format="%d", width="small"),
                                         "Title": st.column_config.TextColumn(width="medium"),
                                         "Draft": st.column_config.TextColumn(width="small")})
        sel = ev.selection.rows if ev and ev.selection else []
    with right:
        if not queue:
            st.info(empty)
        else:
            job = queue[sel[0]] if sel and sel[0] < len(queue) else queue[0]
            job_actions(db.job(job["id"]), kp)


with tab_jobs:
    jobs_view("q_", ["shortlisted", "drafted", "scored", "new", "skipped", "filtered"], ["drafted", "shortlisted"],
              cfg["thresholds"]["shortlist_score"],
              "Nothing here. Lower the score filter, add statuses, or click ▶ Run now.")

with tab_reloc:
    st.caption("On-site or hybrid jobs outside Barcelona and its ~1 h commute area. Kept apart so they don't crowd "
               "your main queue. You can still draft and apply from here.")
    jobs_view("r_", ["relocation"], ["relocation"], 0, "No relocation jobs right now.")


# ---------------- recruiters ----------------
with tab_rec:
    msgs = db.msgs(["drafted", "new"])
    if not msgs:
        st.info("No recruiter messages waiting. They're pulled from Gmail on every run, LinkedIn InMails included.")
    for m in msgs:
        icon = "💼 LinkedIn" if m["channel"] == "linkedin" else "✉️ Email"
        fit = {"good": "🟢", "maybe": "🟡", "poor": "🔴"}.get(m["fit"] or "", "⚪")
        with st.expander(f"{fit} {m['sender_name'] or m['sender_email']} — {m['subject']} · {icon} · {local(m['received_at'])}"):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Their message**")
                st.text((m["body"] or "")[:4000])
            with c2:
                reply = st.text_area("Your reply (edit freely)", m["draft_reply"] or "", height=260, key=f"r{m['id']}")
                if st.button("💾 Save", key=f"rv{m['id']}"):
                    db.update_msg(m["id"], draft_reply=reply)
                    st.toast("Saved")
                if m["channel"] == "gmail":
                    try:
                        to, _ = recruiters.reply_target(m)
                        st.markdown(f"**Reply goes to:** `{to}` (same Gmail thread)")
                    except Exception as e:
                        to = None
                        st.error(f"Can't reply by email: {e}")
                    ok = st.checkbox("I've reviewed the reply and recipient", key=f"rok{m['id']}")
                    if st.button("📨 Approve & send", key=f"rs{m['id']}", type="primary",
                                 disabled=not (to and ok and reply.strip())):
                        try:
                            recruiters.send_email_reply(db, cfg, m["id"], reply, to)
                        except Exception as e:
                            st.error(f"Not sent: {e}")
                        else:
                            st.success("Sent")
                            st.rerun()
                else:
                    st.caption("LinkedIn: copy the reply, open the conversation, paste and press Send yourself.")
                    st.code(reply, language=None, wrap_lines=True)
                    st.link_button("💼 Open on LinkedIn", safe_url(m["link"]) or "https://www.linkedin.com/messaging/",
                                   type="primary")
                    if st.button("✔ I sent it", key=f"rm{m['id']}"):
                        db.update_msg(m["id"], draft_reply=reply, status="sent")
                        st.rerun()
                if st.button("🗑 Ignore", key=f"ri{m['id']}"):
                    db.update_msg(m["id"], status="ignored")
                    st.rerun()


# ---------------- tracker ----------------
with tab_track:
    tracked = db.jobs(TRACK_STAGES + ["sending"], order="updated_at DESC")
    if not tracked:
        st.info("Applications you send or mark as applied show up here.")
    else:
        st.caption("Change the stage as things move. Saved straight away.")
        rows = [{"id": j["id"], "Stage": j["status"] if j["status"] in TRACK_STAGES else "applied",
                 "Title": j["title"], "Company": j["company"], "Location": j["location"],
                 "Updated": local(j["updated_at"], "%d %b"), "Ad": safe_url(j["url"])} for j in tracked]
        edited = st.data_editor(rows, hide_index=True, width="stretch", key="tracker",
                                disabled=["id", "Title", "Company", "Location", "Updated", "Ad"],
                                column_config={"id": None,
                                               "Stage": st.column_config.SelectboxColumn("Stage", options=TRACK_STAGES,
                                                                                         required=True),
                                               "Ad": st.column_config.LinkColumn("Ad", display_text="open")})
        changed = False
        for before, after in zip(rows, edited):
            if after["Stage"] != before["Stage"]:
                if after["Stage"] != "approved":   # anything past 'approved' means it was sent: stop the filler
                    db.try_move(before["id"], "submitted", actor="jean", reason=f"tracker: {after['Stage']}")
                db.update_job(before["id"], status=after["Stage"])
                changed = True
        if changed:
            st.toast("Stage updated")
            st.rerun()


# ---------------- help ----------------
with tab_help:
    st.markdown("""
**Daily routine (5–10 minutes)**
1. **Jobs**: click a job, read the draft, fix anything in the yellow box, then apply: *Approve & send email*, or copy the letter → *Open ad to apply* → *I applied*. *Skip* anything you don't want.
2. **Recruiters**: edit the reply, tick the box, send. For LinkedIn, copy → open → paste and Send yourself.
3. **Tracker**: move applications to interview / offer / rejected.

**Rules:** jobs that require a language other than English or Spanish are excluded. On-site or hybrid jobs
outside Barcelona and its ~1 h commute area go to **Relocation needed**. Remote jobs open to Spain or the EU stay in **Jobs**.

**What runs on its own:** every 3 hours while you're logged in, the app fetches jobs, scores them locally and drafts letters for the best ones. It never sends anything.

**Gmail sign-in** lasts 7 days while the Google app is in Testing. When the sidebar says *renew*, click **Sign in to Gmail again**.

**Files:** your settings are in `config.yaml`, keys in `.env`, CV texts in `cv\\`, and the PDFs in `Work and CV\\CV\\`.
""")
