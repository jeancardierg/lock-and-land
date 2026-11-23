import base64
import json
from unittest import mock

import pytest

from jobhunter import pipeline, recruiters, scoring
from jobhunter.db import DB, dedup_key
from jobhunter.gmail import extract_bodies, reply_subject
from jobhunter.sources import adzuna, arbeitnow, jobtech, linkedin_alerts, remotive
from jobhunter import config, drafting, gmail
from jobhunter.config import load_config

CFG = load_config()


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def job(**kw):
    base = dict(source="x", external_id="1", title="IT Support Engineer", company="Acme", location="Barcelona",
                remote=False, url="https://x", description="Intune, Entra ID, ISO 27001 support", posted_at="")
    base.update(kw)
    return base


def test_dedup_across_sources(db):
    assert db.upsert_job(job())
    assert not db.upsert_job(job(source="y", external_id="9", title="IT  support engineer!", company="ACME"))
    assert db.upsert_job(job(external_id="2", title="Sysadmin"))


def test_prefilter():
    assert scoring.prefilter(job(), CFG)[0]
    assert not scoring.prefilter(job(title="Senior Software Engineer Backend"), CFG)[0]
    # 'IT' must match as a word, not inside 'Kitchen'
    assert not scoring.prefilter(job(title="Kitchen porter", description="kitchen work"), CFG)[0]


def test_jobtech_parse():
    h = {"id": "123", "headline": "IT-tekniker", "employer": {"name": "Volvo"},
         "workplace_address": {"municipality": "Göteborg", "region": "Västra Götaland", "country": "Sverige"},
         "application_details": {"url": None, "email": "jobb@volvo.se"}, "webpage_url": "https://af/123",
         "description": {"text": "Support for users"}, "publication_date": "2026-11-18T10:00:00"}
    j = jobtech.parse_hit(h)
    assert j["company"] == "Volvo" and j["apply_email"] == "jobb@volvo.se" and j["url"] == "https://af/123"
    assert "Göteborg" in j["location"]


def test_adzuna_remotive_arbeitnow_parse():
    a = adzuna.parse_result({"id": 5, "title": "IT Admin (remote)", "company": {"display_name": "Co"},
                             "location": {"display_name": "Amsterdam"}, "redirect_url": "u", "created": "2026-11-19T00:00:00Z"}, "nl")
    assert a["external_id"] == "nl-5" and a["remote"]
    r = remotive.parse_job({"id": 7, "title": "SysOps", "company_name": "R", "candidate_required_location": "Europe",
                            "url": "u", "description": "<p>Hi <b>there</b></p>", "publication_date": "2026-11-19T00:00:00"})
    assert r["description"] == "Hi\nthere" and remotive.location_ok("Europe", ["europe"])
    assert not remotive.location_ok("USA only", ["europe", "worldwide"])
    w = arbeitnow.parse_job({"slug": "s", "title": "IT Support", "company_name": "B", "remote": True, "url": "u",
                             "location": "Berlin", "created_at": 1763500000, "description": "<p>x</p>"})
    assert w["remote"] and w["external_id"] == "s"


LINKEDIN_ALERT = """
<table><tr><td>
<a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trk=eml">IT Operations Engineer</a>
<p>Spotify · Stockholm, Sweden (Hybrid)</p>
<a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trk=eml2">View job</a>
</td></tr><tr><td>
<a href="https://www.linkedin.com/comm/jobs/view/4099999999/">Service Desk Lead</a>
<p>Booking.com · Amsterdam (Remote)</p>
</td></tr></table>"""


def test_linkedin_alert_parse():
    jobs = linkedin_alerts.parse_alert_html(LINKEDIN_ALERT)
    assert [j["external_id"] for j in jobs] == ["4012345678", "4099999999"]
    assert jobs[0]["company"] == "Spotify" and "Stockholm" in jobs[0]["location"]
    assert jobs[1]["remote"] and jobs[0]["url"].endswith("/jobs/view/4012345678/")


def test_gmail_bodies_and_subject():
    enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": enc("hola")}},
        {"mimeType": "text/html", "body": {"data": enc("<b>hola</b>")}}]}
    assert extract_bodies(payload) == ("hola", "<b>hola</b>")
    assert reply_subject("Role at X") == "Re: Role at X" and reply_subject("RE: y") == "RE: y"


def test_recruiter_linkedin_parse():
    meta = {"gmail_id": "g1", "thread_id": "t1", "sender_name": "Anna Berg via LinkedIn",
            "sender_email": "inmail-hit-reply@linkedin.com", "subject": "IT role in Oslo", "received_at": "2026-11-19",
            "plain": "", "html": '<a href="https://www.linkedin.com/comm/messaging/thread/2-ABCdef123==?trk=x">Reply</a> Hi Alex'}
    m = recruiters.parse_message(meta)
    assert m["channel"] == "linkedin" and m["sender_name"] == "Anna Berg"
    assert m["link"] == "https://www.linkedin.com/messaging/thread/2-ABCdef123=="




# ---------- v0.2: safety + CV integration ----------

def test_cv_variant_selection():
    assert config.cv_variant({"title": "IT Manager", "description": ""}) == "lead"
    assert config.cv_variant({"title": "Head of IT", "description": ""}) == "lead"
    assert config.cv_variant({"title": "Endpoint Engineer (Intune)", "description": "We need Intune"}) == "main"
    es = "Buscamos administrador de sistemas. Requisitos: experiencia con Intune. Ofrecemos jornada híbrida."
    assert config.cv_variant({"title": "Administrador de sistemas", "description": es}) == "es"


def test_cv_files_present_and_clean():
    for v in ("main", "lead", "es"):
        text = config.read_cv(CFG, v)
        assert "Northwind Media" in text and "IT Support Specialist" in text
        assert "SCIM" in text and "[phone]" not in text and "confirm]" not in text
        for name in ("Sam", "Robin", "Kai"):
            assert name not in text


def test_drafting_rules_in_prompts():
    rp = recruiters.REPLY_SYSTEM.format(rules=config.drafting_rules(CFG))
    assert "Never state a salary number" in rp


def test_keyword_bonus():
    j = job(description="Intune, Autopilot, Okta SSO, Entra ID, ISO 27001, Kandji, PowerShell")
    hits = scoring.keyword_hits(j, CFG)
    assert {"Intune", "Autopilot", "Okta", "Entra ID"} <= set(hits)
    assert "Iru" not in hits  # must not match inside other words
    with mock.patch.object(scoring.llm, "ollama_json", return_value={"score": 70}):
        s, d = scoring.score_job(j, "cv", CFG)
    assert s == 80 and d["llm_score"] == 70 and d["keyword_bonus"] == 10


def test_send_guards(tmp_path):
    with pytest.raises(gmail.SendBlocked):
        gmail.single_recipient("a@x.com, b@y.com")
    with pytest.raises(gmail.SendBlocked):
        gmail.single_recipient("not-an-email")
    assert gmail.single_recipient("Jane <Jane@Corp.com>") == "Jane@Corp.com"
    with pytest.raises(gmail.SendBlocked):  # never send an application without the CV
        gmail._build("hr@corp.com", "S", "body", "me@gmail.com", attachment=tmp_path / "missing.pdf")
    m = gmail._build("hr@corp.com", "Line1\r\nBcc: evil@x.com", "body", "me@gmail.com")
    assert "Bcc" not in m and "\n" not in m["Subject"]


def test_double_send_claim(db):
    db.upsert_job(job())
    jid = db.jobs()[0]["id"]
    db.update_job(jid, status="drafted")
    assert db.claim("jobs", jid, ["drafted"])
    assert not db.claim("jobs", jid, ["drafted"])


def test_gmail_never_interactive_in_background(tmp_path, monkeypatch):
    pytest.importorskip("google.oauth2.credentials")
    from jobhunter import secrets
    secrets.delete("GMAIL_TOKEN")
    monkeypatch.setattr(gmail, "_service", None)
    with pytest.raises(RuntimeError, match="gmail-auth"):
        gmail.service()




def test_placeholder_cv_blocks_send(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    def pdf(name, line):
        p = tmp_path / name
        c = canvas.Canvas(str(p)); c.drawString(72, 720, line); c.save()
        return p
    bad = pdf("bad.pdf", "Barcelona, Spain · [phone] · [email] · [LinkedIn URL]")
    good = pdf("good.pdf", "Barcelona, Spain · +34 600 000 000 · me@example.com")
    assert "[phone]" in config.attachment_problem(bad)
    assert config.attachment_problem(good) is None
    with pytest.raises(gmail.SendBlocked, match="placeholder"):
        gmail._build("hr@corp.com", "S", "body", "me@gmail.com", attachment=bad)
    assert gmail._build("hr@corp.com", "S", "body", "me@gmail.com", attachment=good)["To"] == "hr@corp.com"






def test_forwarded_linkedin_inmail():
    own = ("me@gmail.com", "me@hotmail.com")
    meta = {"gmail_id": "g2", "thread_id": "t2", "sender_name": "Me", "sender_email": "me@hotmail.com",
            "subject": "FW: Anna sent you a message", "received_at": "2026-11-19", "plain": "",
            "html": '<a href="https://www.linkedin.com/comm/messaging/thread/2-XYZ==?trk=x">Reply</a>'}
    m = recruiters.parse_message(meta, own)
    assert m["channel"] == "linkedin" and m["link"] == "https://www.linkedin.com/messaging/thread/2-XYZ=="
    assert recruiters.channel_for("me@hotmail.com", "no linkedin here", own) == "gmail"
    assert "jobalerts-noreply@linkedin.com" in linkedin_alerts.SENDERS




def test_rules_language_and_location():
    from jobhunter import rules
    bcn = job(location="Barcelona, Spain")
    assert rules.decide(bcn, {"required_languages": ["English", "Spanish"], "work_mode": "hybrid"}, CFG) == (None, "")
    st, why = rules.decide(bcn, {"required_languages": ["German"], "work_mode": "remote"}, CFG)
    assert st == "filtered" and "German" in why
    # optional language does not exclude
    opt = job(description="Fluent English required. German is a plus. Intune.")
    assert rules.decide(opt, {"required_languages": [], "work_mode": "remote"}, CFG)[0] is None
    req = job(description="You speak fluent Dutch and English. Intune.")
    assert rules.decide(req, {"required_languages": [], "work_mode": "remote"}, CFG)[0] == "filtered"
    # ad written in German
    de = job(description=("Wir suchen für unser Team eine engagierte Person und bieten Ihnen mit der Rolle eine "
                          "spannende Aufgabe. Sie sind für die Betreuung der Systeme bei uns zuständig und arbeiten "
                          "mit unseren Kollegen auf Augenhöhe, die Sie unterstützen, und wir freuen uns auf Sie. ") * 2)
    assert rules.quick_language_block(de) == "ad written in German"
    # location
    mun = job(location="Munich, Germany", remote=False)
    assert rules.decide(mun, {"required_languages": ["English"], "work_mode": "onsite"}, CFG)[0] == "relocation"
    assert rules.decide(mun, {"required_languages": [], "work_mode": "remote", "remote_scope": "EU"}, CFG)[0] is None
    us = rules.decide(mun, {"required_languages": [], "work_mode": "remote", "remote_scope": "US only"}, CFG)
    assert us[0] == "filtered"




def test_local_area():
    from jobhunter import rules
    for loc in ["Sant Cugat del Vallès", "Terrassa, Barcelona", "L'Hospitalet de Llobregat", "Mataró", "Barcelona"]:
        assert rules.is_local(loc, CFG), loc
    for loc in ["Madrid", "Valencia", "Girona", "Rubik Street, Berlin"]:
        assert not rules.is_local(loc, CFG), loc
    assert rules.decide(job(location="Madrid", remote=False), {"work_mode": "hybrid"}, CFG)[0] == "relocation"
    assert rules.decide(job(location="Madrid"), {"work_mode": "remote", "remote_scope": "Spain"}, CFG)[0] is None


def test_dashboard_compiles():
    import ast, pathlib
    ast.parse(pathlib.Path(config.ROOT, "jobhunter", "dashboard.py").read_text(encoding="utf-8"))


def test_rules_v3_office_abroad_province_and_us():
    from jobhunter import rules
    d = lambda loc, info, **kw: rules.decide(job(location=loc, **kw), info, CFG)[0]
    # office outside Spain beats a listing that says Barcelona
    assert d("Barcelona", {"work_mode": "onsite", "office_city": "Lisbon"}, title="Onsite Systems Administrator - Lisbon") == "relocation"
    # Adzuna appends the province: only the town counts
    assert d("Bellprat, Anoia, Barcelona", {"work_mode": "onsite", "office_city": "Barcelona"}) == "relocation"
    assert d("Sant Cugat del Vallès, Vallès Occidental, Barcelona", {"work_mode": "hybrid"}) is None
    assert d("Barcelona, Cataluña", {"work_mode": "hybrid", "office_city": "null"}) is None
    assert d("España", {"work_mode": "hybrid", "office_city": "Barcelona"}) is None
    # US-only remote and clearance jobs are excluded
    assert d("Remote", {"work_mode": "remote"}, title="Remote System Administrator (Palm Beach, FL Area)", remote=1) == "filtered"
    assert d("Remote", {"work_mode": "remote", "remote_scope": "EU"}, title="Remote System Administrator (Palm Beach, Fl Area)", remote=1) == "filtered"
    assert d("Remote", {"work_mode": "remote"}, description="Active Top Secret clearance required.", remote=1) == "filtered"
    assert d("Remote", {"work_mode": "remote", "remote_scope": "EU"}, description="Manage Intune for our EU teams.", remote=1) is None
