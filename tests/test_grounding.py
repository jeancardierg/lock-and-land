"""Phase 1: validator golden cases (taken from real v0.2 drafts), retrieval, writer loop, screening."""
import pytest

from jobhunter import grounding, lexicon, llm, master, validate
from jobhunter.config import load_config
from jobhunter.schemas import JobFacts, Sentence
from jobhunter.screening import Screener
from jobhunter.vectors import Store

from .fakellm import Script, fake_embed

CFG = load_config()
PROF = master.load()


def S(kind, text, *ids):
    return Sentence(kind=kind, text=text, fact_ids=list(ids))


def hard(*sents, lang="en"):
    return validate.validate_letter(list(sents), PROF, lang, words=(0, 999)).hard


# ---------- golden validator cases ----------

def test_profile_is_consistent():
    probs = [p for p in master.problems(PROF) if "APPROVED" not in p]
    assert probs == []


def test_true_cv_claims_pass():
    # v0.2's checker wrongly flagged this one; the CV says "Led delivery"
    assert hard(S("claim", "I led delivery of the Microsoft Intune and Windows Autopilot rollout for ~90 Windows devices.",
                  "F-INTUNE-01")) == []
    assert hard(S("claim", "I led the Bitdefender GravityZone EDR rollout single-handed to ~150 endpoints.", "F-EDR-01")) == []
    assert hard(S("claim", "Lideré en solitario el despliegue de Bitdefender GravityZone EDR en ~150 endpoints.", "F-EDR-01"),
                lang="es") == []
    assert hard(S("claim", "I resolved ~900 tickets with a 3.9-hour median.", "F-SVC-01")) == []
    assert hard(S("claim", "Resolví ~900 tickets con una mediana de 3,9 horas.", "F-SVC-01"), lang="es") == []
    assert hard(S("claim", "I wrote IT policies mapped to ISO 27001:2022 controls.", "F-GOV-01")) == []
    assert hard(S("claim", "I executed the Kandji MDM rollout for a ~70-Mac fleet.", "F-MAC-01")) == []


def test_upgraded_verbs_blocked():
    assert any("designed" in h for h in hard(S("claim", "I designed the Intune and Autopilot rollout.", "F-INTUNE-01")))
    assert any("led" in h for h in hard(S("claim", "I led the Okta SSO and MFA rollout company-wide.", "F-OKTA-01")))
    assert any("ownership" in h for h in hard(S("claim", "I owned the Iru MDM rollout.", "F-MAC-01")))


def test_invented_numbers_and_tools_blocked():
    assert any("90" in h for h in hard(S("claim", "I reached 90% of EU devices managed and compliant.", "F-INTUNE-02")))
    h = hard(S("claim", "I administer Jira Service Management and Confluence and work with agile methodologies.",
               "F-SKILL-SAAS"))
    assert any("Agile" in x for x in h)
    assert any("ServiceNow" in x for x in hard(S("claim", "I ran ServiceNow queues for ~900 tickets.", "F-SVC-01")))
    # a true tool, but cited from the wrong fact
    assert any("Okta" in x for x in hard(S("claim", "I rolled out Okta to ~150 endpoints.", "F-EDR-01")))


def test_motivation_cannot_carry_claims():
    assert hard(S("motivation", "Your focus on endpoint management matches what I enjoy most.")) == [
        "no claim sentences: the letter must show at least one CV result"]
    h = hard(S("motivation", "I would bring my Intune experience to your team."))
    assert any("names" in x for x in h)
    h = hard(S("motivation", "I delivered 3 migrations last year."))
    assert any("numbers" in x for x in h) and any("result" in x for x in h)


def test_profile_rules():
    claim = S("claim", "I ran 40 onboardings end to end.", "F-SVC-02")
    assert any("certification" in x for x in hard(claim, S("motivation", "I am ITIL 4 certified.")))
    assert any("salary" in x for x in hard(claim, S("logistics", "My salary of 45k EUR is flexible.")))
    assert any("title" in x for x in hard(claim, S("motivation", "As an IT Manager at Northwind Media I care about users.")))
    assert any("career break" in x for x in hard(claim, S("logistics", "After my career break I am ready.")))
    assert any("names" in x for x in hard(claim, S("motivation", "Sam and I worked well together.")))
    assert hard(claim, S("logistics", "Career break for personal reasons, now resolved, followed by a job search.")) == []


def test_jamf_only_as_comparable():
    assert hard(S("claim", "I run Jamf-comparable macOS management with Iru.", "F-SKILL-MAC")) == []
    assert any("Jamf" in x for x in hard(S("claim", "I administer Jamf Pro for the Mac fleet.", "F-MAC-01")))


def test_banned_phrase_is_soft():
    rep = validate.validate_letter([S("claim", "I have extensive experience and ran 40 onboardings end to end.",
                                      "F-SVC-02")], PROF, "en", words=(0, 999))
    assert rep.ok and rep.score < 100 and any("extensive" in s for s in rep.soft)


# ---------- retrieval + writer loop with a fake model ----------

@pytest.fixture
def fake(monkeypatch, tmp_path):
    sc = Script()
    monkeypatch.setattr(llm, "structured", sc.structured)
    monkeypatch.setattr(llm, "embed", fake_embed)
    return sc, Store(CFG, tmp_path / "v.db")


JOB = {"title": "Endpoint Engineer (Intune)", "company": "Acme", "location": "Barcelona",
       "description": "We need Microsoft Intune and Autopilot experience, Okta SSO, and ServiceNow. English."}
JF = JobFacts(requirements_must=["Microsoft Intune and Autopilot", "Okta SSO and MFA", "ServiceNow administration"],
              ad_language="en", work_mode="hybrid", office_city="Barcelona")


def good_letter(prompt, schema):
    return {"subject": "Application: Endpoint Engineer", "p1": [
        {"kind": "motivation", "text": "Your endpoint team's focus on modern management is the work I want to keep doing.", "fact_ids": []},
        {"kind": "claim", "text": "I led delivery of the Microsoft Intune and Windows Autopilot rollout for ~90 Windows devices.", "fact_ids": ["F-INTUNE-01"]}],
        "p2": [{"kind": "claim", "text": "I rolled out Okta SSO and MFA company-wide, reaching 100% adoption against an 80% target.", "fact_ids": ["F-OKTA-01"]},
               {"kind": "claim", "text": "We met the goal of 80%+ of EU devices managed and compliant.", "fact_ids": ["F-INTUNE-02"]}],
        "p3": [{"kind": "logistics", "text": "I am in Barcelona, open to hybrid work, and free for a call this week or next.", "fact_ids": []}]}


def bad_letter(prompt, schema):
    d = good_letter(prompt, schema)
    d["p2"][0] = {"kind": "claim", "text": "I designed the Intune rollout and administer ServiceNow.", "fact_ids": ["F-INTUNE-01"]}
    return d


def test_retrieval_finds_facts_and_gaps(fake):
    sc, store = fake
    ret = grounding.retrieve(CFG, PROF, JOB, JF, store)
    ids = [f.id for f in ret.facts]
    assert "F-INTUNE-01" in ids and any(i.startswith("F-OKTA") for i in ids)
    assert ret.gaps == ["ServiceNow administration"] and 0.6 < ret.score < 0.7


def test_writer_regenerates_until_clean(fake):
    sc, store = fake
    sc.push("writer", bad_letter)
    sc.push("writer", good_letter)
    ret = grounding.retrieve(CFG, PROF, JOB, JF, store)
    out = grounding.write_letter({**CFG, "grounding": {"words": [0, 999]}}, PROF, JOB, JF, ret)
    assert out["ok"] and out["attempts"] == 2
    writer_calls = [c for c in sc.calls if c["task"] == "writer"]
    assert "REWRITE" in writer_calls[1]["prompt"] and "designed" in writer_calls[1]["prompt"]
    assert writer_calls[0]["schema"]["properties"]["p1"]["items"]["properties"]["fact_ids"]["items"]["enum"] == \
        [f.id for f in ret.facts]
    assert out["text"].startswith("Dear Hiring Team,") and out["text"].endswith("Kind regards,\n" + PROF.identity["name"])


def test_writer_gives_up_after_three(fake):
    sc, store = fake
    for _ in range(3):
        sc.push("writer", bad_letter)
    ret = grounding.retrieve(CFG, PROF, JOB, JF, store)
    out = grounding.write_letter(CFG, PROF, JOB, JF, ret)
    assert not out["ok"] and out["attempts"] == 3 and out["validation"]["hard"]


def test_entailment_doubt_lowers_score(fake):
    sc, store = fake
    sc.push("writer", good_letter)
    sc.push("checker", {"results": [{"i": 1, "supported": False, "why": "count differs"}]})
    ret = grounding.retrieve(CFG, PROF, JOB, JF, store)
    out = grounding.write_letter({**CFG, "grounding": {"words": [0, 999]}}, PROF, JOB, JF, ret)
    assert out["ok"] and out["validation"]["score"] == 85


def test_hand_edit_revalidation(fake):
    sc, store = fake
    sc.push("writer", good_letter)
    ret = grounding.retrieve(CFG, PROF, JOB, JF, store)
    out = grounding.write_letter({**CFG, "grounding": {"words": [0, 999], "entailment": False}}, PROF, JOB, JF, ret)
    edited = out["text"].replace("this week or next.", "this week or next. I also cut costs by 35%.")
    rep = grounding.revalidate_text(CFG, PROF, out["letter_json"], edited)
    assert not rep["ok"] and any("35" in h for h in rep["hard"])
    assert grounding.revalidate_text(CFG, PROF, out["letter_json"], out["text"])["ok"]


# ---------- screening ----------

def test_screening_sources(fake):
    sc, store = fake
    scr = Screener(CFG, PROF, store)
    a = scr.answer("How many years of experience do you have with Microsoft Intune?")
    assert a.source == "derived" and a.answer == "1" and not a.needs_jean
    a = scr.answer("How many years of experience do you have with ServiceNow?")
    assert a.needs_jean
    assert scr.answer("What are your salary expectations?").needs_jean
    assert scr.answer("Gender").needs_jean
    sc.push("checker", {"key": "needs_sponsorship"})
    a = scr.answer("Will you now or in the future require visa sponsorship?")
    assert (a.source, a.answer, a.needs_jean) == ("bank", "No", False)
    sc.push("checker", {"key": "work_authorization_eu"})
    a = scr.answer("¿Tienes permiso de trabajo en la UE?", lang="es")
    assert a.answer == "Sí"
    assert scr.answer("First name").answer == "Alex"


def test_lexicon_numbers_ignore_product_versions():
    assert lexicon.numbers("Microsoft 365 and ISO 27001 with 114 EMS E3 seats") == {"114"}


def test_inline_fact_ids_are_moved_out_of_the_text():
    from jobhunter.schemas import Letter
    lt = Letter(p1=[S("claim", "I rolled out Intune for about 90 devices (F-INTUNE-01).")],
                p2=[S("claim", "I owned the Okta tenant [F-OKTA-04, F-AI-01] and renewals.", "F-OKTA-04")],
                p3=[S("logistics", "Available next week.")])
    grounding._strip_inline_ids(lt, PROF)
    a, b, _ = lt.sentences()
    assert a.text == "I rolled out Intune for about 90 devices." and a.fact_ids == ["F-INTUNE-01"]
    assert b.text == "I owned the Okta tenant and renewals." and b.fact_ids == ["F-OKTA-04", "F-AI-01"]
    assert "01" not in lexicon.numbers(a.text)


# ---------- round 2: fewer needless failures, same safety ----------

def test_motivation_may_name_the_employers_tools_but_not_claim_them():
    ad = "We run Microsoft Intune and Entra ID for 500 staff."
    ok = validate.validate_letter([S("motivation", "Your move to Microsoft Intune and Entra ID is the kind of work I enjoy."),
                                   S("claim", "I led delivery of the Microsoft Intune and Windows Autopilot rollout for ~90 Windows devices.", "F-INTUNE-01")],
                                  PROF, "en", (0, 999), ad)
    assert ok.hard == []
    bad = validate.validate_letter([S("motivation", "My experience with Entra ID makes me a strong fit."),
                                    S("claim", "I led delivery of the Microsoft Intune and Windows Autopilot rollout for ~90 Windows devices.", "F-INTUNE-01")],
                                   PROF, "en", (0, 999), ad)
    assert any("Entra" in h for h in bad.hard)
    not_in_ad = validate.validate_letter([S("motivation", "Your ServiceNow platform sounds great.")], PROF, "en", (0, 999), ad)
    assert any("ServiceNow" in h for h in not_in_ad.hard)


def test_repair_drops_only_blocked_sentences(fake):
    sc, store = fake

    def overreach(prompt, schema):
        d = good_letter(prompt, schema)
        d["p3"] = [{"kind": "logistics", "text": "I can be in the office 2 days a week.", "fact_ids": []}]
        d["p2"].append({"kind": "claim", "text": "I designed the Intune rollout.", "fact_ids": ["F-INTUNE-01"]})
        return d
    sc.push("writer", overreach)
    ret = grounding.retrieve(CFG, PROF, JOB, JF, store)
    out = grounding.write_letter({**CFG, "grounding": {"words": [0, 999], "entailment": False}}, PROF, JOB, JF, ret)
    assert out["ok"] and out["attempts"] == 1
    assert "designed" not in out["text"] and "2 days" not in out["text"]
    assert grounding.logistics_line(PROF, "en") in out["text"]
    assert any("removed 2 sentence" in x for x in out["validation"]["soft"])


def test_bare_led_overclaim_blocked():
    # the approved fact says "Led delivery ... (planned by the former IT manager)": "led the rollout" overclaims
    h = hard(S("claim", "I led the Microsoft Intune and Windows Autopilot rollout for approximately 90 Windows devices.",
               "F-INTUNE-01"))
    assert any("overstates" in x for x in h)
    assert hard(S("claim", "I led delivery of the Microsoft Intune and Windows Autopilot rollout for ~90 Windows devices.",
                  "F-INTUNE-01")) == []
    assert any("overstates" in x for x in hard(
        S("claim", "Lideré el despliegue de Microsoft Intune y Windows Autopilot en ~90 equipos Windows.", "F-INTUNE-01"),
        lang="es"))
    assert hard(S("claim", "Lideré la ejecución del despliegue de Microsoft Intune y Windows Autopilot en ~90 equipos Windows.",
                  "F-INTUNE-01"), lang="es") == []
