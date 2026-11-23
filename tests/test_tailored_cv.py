"""Tailored CV per job: built only from the facts profile, ordered by the ad, rendered to ATS-friendly DOCX + PDF."""
import json
from pathlib import Path
from unittest import mock

import pytest

from jobhunter import gmail, llm, master, tailored_cv
from jobhunter.schemas import JobFacts

from .fakellm import Script, fake_embed
from .test_grounding import good_letter
from .test_phase2 import EXTRACTED, cfg_for_tests, client, db, fake, job, ready  # noqa: F401 (fixtures)

PROFILE = Path(__file__).parent / "fixtures" / "facts" / "master_profile.json"
AD = ("Hybrid IT Specialist - Okta & Endpoint Support. You will run Okta SSO, MFA and SCIM, manage devices in "
      "Microsoft Intune and Jamf, handle onboarding and work in Jira Service Management. ServiceNow is a plus.")


@pytest.fixture
def prof():
    return master.load(PROFILE)


@pytest.fixture
def no_ai(monkeypatch, tmp_path):
    sc = Script()
    monkeypatch.setattr(llm, "structured", sc.structured)
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr("jobhunter.vectors.DATA", tmp_path, raising=False)
    return sc


def ad_job(**kw):
    return {"id": 7, "title": "Hybrid IT Specialist", "company": "Acme", "description": AD, **kw}


JF = JobFacts(requirements_must=["Okta SSO and MFA", "Microsoft Intune", "Jamf"], requirements_nice=["ServiceNow"])


def cfg(tmp_path):
    c = cfg_for_tests(tmp_path)
    c["profile"].update(email="alex@example.com", phone="+34 600 000 000")
    return c


def test_cv_uses_only_profile_facts_in_ad_order(no_ai, prof, tmp_path):
    cv = tailored_cv.build(cfg(tmp_path), prof, ad_job(), JF, gaps=["ServiceNow"])
    h = cv["headings"]
    assert [h[k] for k in ("summary", "skills", "experience", "education", "languages")] == \
        ["Professional Summary", "Skills", "Work Experience", "Education", "Languages"]
    # skills: verified only, the ad's first (in the ad's order), never a bare tool the candidate only compares to
    assert cv["skills"][:3] == ["Okta", "SSO", "MFA"]
    assert "Jamf" not in cv["skills"] and any(s.startswith("Jamf-comparable") for s in cv["skills"])
    assert "ServiceNow" not in cv["skills"]
    assert set(cv["skills"]) - {s for s in cv["skills"] if s.startswith("Jamf-comparable")} <= \
        set(tailored_cv.verified_skills(prof))
    # roles: real titles, employers, MM/YYYY dates, newest first; bullets are facts word for word
    titles = [(r["title"], r["employer"], r["dates"]) for r in cv["roles"]]
    assert titles == [("IT Support Specialist", "Northwind Media", "05/2025 - Present"),
                      ("Career break", "", "06/2023 - 05/2025"),
                      ("IT Support Analyst", "Contoso Services", "03/2019 - 06/2023")]
    facts = {f.text for f in prof.facts}
    assert all(b in facts for r in cv["roles"] if r["employer"] for b in r["bullets"])
    assert "Okta" in cv["roles"][0]["bullets"][0]          # the most relevant fact leads
    assert cv["roles"][1]["bullets"] == [prof.fixed_lines["career_break"]]
    assert len(cv["roles"][0]["bullets"]) <= 6
    # no AI reply here: the summary is the profile's own facts
    assert cv["summary_source"] == "facts" and all(s in facts for s in cv["summary"])
    assert cv["checks"]["hard"] == []
    kw = cv["keywords"]
    assert {"Okta", "SSO", "MFA", "SCIM", "Microsoft Intune", "onboarding"} <= set(kw["covered"])
    assert {"ServiceNow", "Jamf"} <= set(kw["missing"]) and kw["gaps"] == ["ServiceNow"]
    assert cv["contact"] == ["Barcelona, Spain", "alex@example.com", "+34 600 000 000"]


def test_ai_summary_sentences_are_checked(no_ai, prof, tmp_path):
    no_ai.push("cv_summary", {"sentences": [
        {"text": "IT Support Specialist who rolled out Okta SSO and MFA company-wide, reaching 100% adoption.",
         "fact_ids": ["F-OKTA-01"]},
        {"text": "Designed and owned a ServiceNow platform for 500 users.", "fact_ids": ["F-OKTA-01"]}]})
    cv = tailored_cv.build(cfg(tmp_path), prof, ad_job(), JF)
    assert cv["summary_source"] == "ai"
    assert cv["summary"] == ["IT Support Specialist who rolled out Okta SSO and MFA company-wide, reaching 100% adoption."]
    assert any("dropped a summary sentence" in n for n in cv["notes"])
    call = next(c for c in no_ai.calls if "sentences" in c["schema"]["properties"])
    assert "never claim" in call["prompt"] and "ServiceNow" not in call["schema"]["properties"]["sentences"]["items"][
        "properties"]["fact_ids"]["items"]["enum"]


def test_all_bad_summary_falls_back_to_facts(no_ai, prof, tmp_path):
    no_ai.push("cv_summary", {"sentences": [{"text": "Certified CISSP with 15 years in security.", "fact_ids": []},
                                            {"text": "Led a team of 12.", "fact_ids": ["F-OKTA-01"]}]})
    cv = tailored_cv.build(cfg(tmp_path), prof, ad_job(), JF)
    assert cv["summary_source"] == "facts" and cv["summary"]
    assert "CISSP" not in tailored_cv.plain_text(cv) and cv["checks"]["hard"] == []


def test_spanish_ad_gets_a_spanish_cv(no_ai, prof, tmp_path):
    es = ad_job(title="Técnico de sistemas", description="Buscamos técnico con experiencia en Okta, SSO y MFA, "
                                                         "Microsoft Intune y gestión de altas de empleados.")
    cv = tailored_cv.build(cfg(tmp_path), prof, es, JobFacts(requirements_must=["Okta"], ad_language="es"))
    assert cv["lang"] == "es" and cv["headings"]["experience"] == "Experiencia profesional"
    assert cv["roles"][0]["dates"].endswith("Actualidad")
    es_facts = {f.text_es for f in prof.facts if f.text_es}
    assert any(b in es_facts for b in cv["roles"][0]["bullets"])
    assert cv["roles"][1]["bullets"] == [prof.fixed_lines["career_break_es"]]


def test_files_are_ats_friendly(no_ai, prof, tmp_path, monkeypatch):
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    out = tailored_cv.make(cfg(tmp_path), prof, ad_job(), JF)
    pdf, docx = Path(out["pdf"]), Path(out["docx"])
    assert pdf.parent == tmp_path / "applications" / "7" and pdf.name == "Alex_Example_CV_Acme.pdf"
    from pypdf import PdfReader
    text = " ".join(p.extract_text() for p in PdfReader(pdf).pages)
    for word in ("Alex Example", "Professional Summary", "Work Experience", "Okta", "05/2025", "Northwind Media"):
        assert word in text, word
    from docx import Document
    d = Document(docx)
    assert not d.tables and not d.inline_shapes
    assert all(not s.header.paragraphs[0].text for s in d.sections)
    heads = [p.text for p in d.paragraphs if p.style.name.startswith("Heading")]
    assert heads == ["Professional Summary", "Skills", "Work Experience", "Education", "Languages"]
    assert out["keywords"]["covered"] and out["profile_version"] == prof.sha


def ready_cv(db, fake, tmp_path, **kw):
    """A drafted job, then Tailor CV pressed on its card (run inline instead of in a thread)."""
    from jobhunter import pipeline
    jid = ready(db, fake, tmp_path, **kw)
    pipeline.rebuild_cv(db, cfg_for_tests(tmp_path), jid)
    return jid


def test_cv_is_on_demand_only(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    jid = ready(db, fake, tmp_path)
    assert "tailored_cv" not in json.loads(db.job(jid)["notes"])
    card = c.get(f"/api/jobs/{jid}/card").json()
    assert card["tailored_cv"] is None and card["attachment_kind"] == "standard"
    built = []
    monkeypatch.setattr("threading.Thread", lambda target, daemon: mock.Mock(start=lambda: built.append(1)))
    assert "Tailoring" in c.post(f"/api/jobs/{jid}/cv/build").json()["done"] and built == [1]


def test_auto_mode_builds_with_every_letter(db, fake, tmp_path, monkeypatch):
    from jobhunter import pipeline
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    c = cfg_for_tests(tmp_path)
    c["tailored_cv"] = {"auto": True}
    fake.push("extractor", EXTRACTED)
    fake.push("writer", good_letter)
    db.upsert_job(job())
    jid = max(j["id"] for j in db.jobs())
    assert pipeline.draft_job(db, c, None, jid)
    assert json.loads(db.job(jid)["notes"])["tailored_cv"]["pdf"].endswith(".pdf")


def test_redraft_keeps_the_tailored_cv(db, fake, tmp_path, monkeypatch):
    from jobhunter import pipeline
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    jid = ready_cv(db, fake, tmp_path)
    pdf = json.loads(db.job(jid)["notes"])["tailored_cv"]["pdf"]
    fake.push("writer", good_letter)
    assert pipeline.generate(db, cfg_for_tests(tmp_path), jid, actor="jean") == "needs_review"
    assert json.loads(db.job(jid)["notes"])["tailored_cv"]["pdf"] == pdf


def test_tailored_cv_and_email_attaches_it(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    jid = ready_cv(db, fake, tmp_path, apply_email="jobs@acme.example")
    notes = json.loads(db.job(jid)["notes"])
    assert notes["tailored_cv"]["pdf"].endswith(".pdf") and notes["tailored_cv_error"] is None
    card = c.get("/api/queue").json()["review"][0]
    assert card["attachment_kind"] == "tailored" and card["attachment"].endswith("_CV_Acme.pdf")
    assert card["tailored_cv"]["keywords"]["covered"] and card["tailored_cv"]["summary"]
    sent = mock.Mock()
    monkeypatch.setattr(gmail, "send", sent)
    r = c.post(f"/api/jobs/{jid}/approve", json={"confirm_email": True}).json()
    assert r["done"] == "Sent by email."
    assert Path(sent.call_args.kwargs["attachment"]) == Path(notes["tailored_cv"]["pdf"])


def test_card_can_switch_back_to_the_standard_cv(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    jid = ready_cv(db, fake, tmp_path, apply_email="jobs@acme.example")
    assert c.post(f"/api/jobs/{jid}/cv/choice", json={"use": "standard"}).json()["done"].endswith("standard CV.")
    card = c.get(f"/api/jobs/{jid}/card").json()
    assert card["attachment_kind"] == "standard" and not card["attachment"].endswith("_CV_Acme.pdf")
    assert c.post(f"/api/jobs/{jid}/cv/choice", json={"use": "other"}).status_code == 400


def test_open_cv_opens_only_the_jobs_own_files(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    opened = []
    monkeypatch.setattr(tailored_cv, "open_file", opened.append)
    jid = ready_cv(db, fake, tmp_path)
    for what in ("pdf", "docx", "folder", "../../etc/passwd"):
        assert c.post(f"/api/jobs/{jid}/cv/open", json={"what": what}).status_code == 200
    t = json.loads(db.job(jid)["notes"])["tailored_cv"]
    assert opened == [Path(t["pdf"]), Path(t["docx"]), Path(t["pdf"]).parent, Path(t["pdf"])]
    db.upsert_job(job(external_id="9", title="Other", company="Zed"))
    other = max(j["id"] for j in db.jobs())
    assert c.post(f"/api/jobs/{other}/cv/open", json={}).status_code == 404


def test_web_handoff_mentions_the_cv(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    jid = ready_cv(db, fake, tmp_path)
    r = c.post(f"/api/jobs/{jid}/approve", json={}).json()
    assert "_CV_Acme.pdf" in r["done"] and "Open CV" in r["done"]


def test_cv_failure_is_shown_on_the_card(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    monkeypatch.setattr(tailored_cv, "render_pdf", mock.Mock(side_effect=OSError("disk full")))
    jid = ready_cv(db, fake, tmp_path)
    assert db.job(jid)["state"] == "needs_review"
    card = c.get(f"/api/jobs/{jid}/card").json()
    assert card["tailored_cv"] is None and "disk full" in card["tailored_cv_error"]
    assert card["attachment_kind"] == "standard"


def test_cv_without_a_letter(db, fake, tmp_path, monkeypatch):
    """Tailor CV on a scored job that has no letter: the ad is read, the CV is built, the job stays where it was."""
    from jobhunter import pipeline
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    db.upsert_job(job())
    jid = max(j["id"] for j in db.jobs())
    fake.push("extractor", EXTRACTED)
    got = pipeline.rebuild_cv(db, cfg_for_tests(tmp_path), jid)
    assert Path(got["tailored_cv"]["pdf"]).exists() and db.job(jid)["state"] == "queued"


def test_disabled_in_config(db, fake, tmp_path):
    from jobhunter import pipeline
    c = cfg_for_tests(tmp_path)
    c["tailored_cv"] = {"enabled": False, "auto": True}
    jid = ready(db, fake, tmp_path)
    assert pipeline.rebuild_cv(db, c, jid) == {}
    assert "tailored_cv" not in json.loads(db.job(jid)["notes"])


# ---------- a job ad pasted by hand ----------

def test_add_pasted_job(client, db, fake, tmp_path, monkeypatch):
    c, rv = client
    queued = []
    monkeypatch.setattr(rv, "workers_alive", lambda: True)
    monkeypatch.setattr("jobhunter.tasks.generate_one", queued.append)
    body = {"title": "Hybrid IT Specialist", "company": "Acme", "location": "Barcelona, hybrid",
            "url": "https://www.jobleads.com/job/123", "text": AD * 2}
    r = c.post("/api/jobs/add", json=body).json()
    assert "letter and tailored CV" in r["done"] and queued == [r["id"]]
    assert json.loads(db.job(r["id"])["notes"]) == {"want_cv": True}
    j = db.job(r["id"])
    assert (j["source"], j["title"], j["company"], j["state"]) == ("pasted", "Hybrid IT Specialist", "Acme", "queued")
    assert j["description"] == AD * 2 and j["url"] == "https://www.jobleads.com/job/123"
    assert c.post("/api/jobs/add", json=body).status_code == 409          # same title + company again
    r = c.post("/api/jobs/add", json={**body, "company": "Beta", "tailor_cv": False}).json()
    assert "tailored" not in r["done"] and not db.job(r["id"])["notes"]


@pytest.mark.parametrize("bad,msg", [({"title": ""}, "title"), ({"text": "too short"}, "whole ad"),
                                     ({"url": "javascript:alert(1)"}, "http"), ({"apply_email": "nope"}, "email")])
def test_add_pasted_job_validation(client, db, fake, tmp_path, monkeypatch, bad, msg):
    c, rv = client
    body = {"title": "IT Specialist", "company": "Acme", "text": AD * 2, **bad}
    r = c.post("/api/jobs/add", json=body)
    assert r.status_code == 400 and msg in r.json()["detail"]
    assert not db.jobs()


def test_pasted_job_drafts_letter_and_cv_without_workers(client, db, fake, tmp_path, monkeypatch):
    """No background workers: the job is drafted in a thread (the Write letter path) and, as asked, gets a CV."""
    c, rv = client
    from jobhunter import scoring
    monkeypatch.setattr(tailored_cv, "DATA", tmp_path)
    monkeypatch.setattr(rv, "workers_alive", lambda: False)
    monkeypatch.setattr(scoring, "prefilter", lambda j, cf: (True, ""))
    fake.push("extractor", EXTRACTED)
    fake.push("writer", good_letter)
    monkeypatch.setattr("threading.Thread", lambda target, daemon: mock.Mock(start=target))
    monkeypatch.setattr(rv, "DB", lambda: db)
    r = c.post("/api/jobs/add", json={"title": "Endpoint Engineer", "company": "Acme", "text": AD * 2}).json()
    j = db.job(r["id"])
    assert j["state"] == "needs_review", (j["state"], j["notes"])
    notes = json.loads(j["notes"])
    assert notes["tailored_cv"]["pdf"].endswith("_CV_Acme.pdf") and notes["want_cv"]
