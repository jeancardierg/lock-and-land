"""The user's work-mode/pay preference, the All jobs list, and the letter signature."""
from __future__ import annotations

import json

from jobhunter import fit, master, pipeline
from jobhunter.schemas import JobFacts, Salary

from .test_phase2 import EXTRACTED, cfg_for_tests, client, db, fake, job, ready  # noqa: F401

CFG = {"thresholds": {"draft_score": 70}}


def jf(mode, lo=None, hi=None, period="year"):
    return JobFacts(work_mode=mode, salary=Salary(min_eur=lo, max_eur=hi, period=period) if lo or hi else None)


def test_remote_gets_a_bonus_hybrid_is_neutral():
    a = fit.assess(jf("remote"), {}, CFG)
    assert a["adjust"] == 5 and fit.apply(80, a) == 85 and not a["filter"]
    h = fit.assess(jf("hybrid"), {}, CFG)
    assert h["adjust"] == 0 and fit.apply(80, h) == 80


def test_onsite_needs_pay_over_the_floor():
    ok = fit.assess(jf("onsite", 42000, 50000), {}, CFG)
    assert not ok["filter"] and fit.apply(80, ok) == 75
    low = fit.assess(jf("onsite", 30000, 38000), {}, CFG)
    assert low["filter"] and "below €45k" in low["filter"]
    unknown = fit.assess(jf("onsite"), {"description": "On-site role in Barcelona."}, CFG)
    assert not unknown["filter"] and fit.apply(95, unknown) == 69  # listed, but no automatic letter
    monthly = fit.assess(jf("onsite", 4000, None, "month"), {}, CFG)
    assert monthly["salary"] == 48000 and not monthly["filter"]


def test_floor_is_configurable_and_salary_read_from_text():
    cfg = {**CFG, "preferences": {"onsite_min_salary_eur": 55000}}
    assert fit.assess(jf("onsite", 50000, 50000), {}, cfg)["filter"]
    assert fit.salary_from_text("Salario: 45.000 - 52.000 € brutos anuales") == 52000
    assert fit.salary_from_text("Pay €60k plus bonus") == 60000
    assert fit.salary_from_text("3.500 €/mes") == 42000
    assert fit.salary_from_text("25 days holiday and a great salary") is None


def test_unknown_mode_with_remote_flag_counts_as_remote():
    assert fit.assess(None, {"remote": 1}, CFG)["mode"] == "remote"


def test_low_paid_onsite_job_is_filtered_at_scoring(db, fake, tmp_path):  # noqa: F811
    fake.push("extractor", {**EXTRACTED, "work_mode": "onsite", "salary": {"min_eur": 28000, "max_eur": 32000,
                                                                          "period": "year"}})
    db.upsert_job(job())
    jid = db.jobs()[0]["id"]
    cfg = cfg_for_tests(tmp_path)
    pipeline.prepare(db, cfg, jid)
    assert pipeline.score(db, cfg, jid) == "filtered"
    assert "below €40k" in db.job(jid)["notes"]


def test_refit_applies_rule_to_old_scores_once(db, fake, tmp_path):  # noqa: F811
    fake.push("extractor", {**EXTRACTED, "work_mode": "remote"})
    db.upsert_job(job())
    jid = db.jobs()[0]["id"]
    cfg = cfg_for_tests(tmp_path)
    pipeline.prepare(db, cfg, jid)
    pipeline.score(db, cfg, jid)
    s = db.job(jid)["score"]
    base = json.loads(db.job(jid)["score_reasons"])["base_score"]
    assert s == min(100, base + 5)
    pipeline.refit(db, cfg)
    pipeline.refit(db, cfg)
    assert db.job(jid)["score"] == s  # idempotent


def test_all_jobs_list_and_actions(client, db, fake, tmp_path):  # noqa: F811
    c, _ = client
    jid = ready(db, fake, tmp_path)
    db.upsert_job(job(external_id="2", title="Onsite helpdesk"))
    other = max(j["id"] for j in db.jobs())
    db.move(other, "filtered", reason="test")
    rows = {r["id"]: r for r in c.get("/api/jobs").json()["jobs"]}
    assert rows[jid]["mode"] == "hybrid" and rows[jid]["stage"] == "letter ready"
    assert rows[other]["stage"] == "filtered out"
    assert "Back in the list" in c.post(f"/api/jobs/{other}/restore").json()["done"]
    assert db.job(other)["state"] == "scored"
    c.post(f"/api/jobs/{other}/submitted")
    assert db.job(other)["state"] == "submitted"


def test_signature_uses_profile_name(db, fake, tmp_path, monkeypatch):  # noqa: F811
    from jobhunter import __main__ as cli
    jid = ready(db, fake, tmp_path)
    app = db.application(jid)
    name = master.get().identity["name"]
    db.update_application(app["id"], letter_text=app["letter_text"] + "\n\nOld Name Example")
    cfg = cfg_for_tests(tmp_path)
    cfg["profile"]["name"] = "Old Name Example"
    assert cli.fix_signatures(db, master.get(), cfg) == 1
    assert db.application(jid)["letter_text"].endswith(name)
    assert master.display_name(cfg) == name
