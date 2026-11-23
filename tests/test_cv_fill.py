"""Fill job criteria from a CV: code reads the sure things, the local model the rest, and every value is checked
against the CV text (anything the CV doesn't say is dropped and left for the person to fill in)."""
from jobhunter import cv_criteria, llm

from .test_fresh_user import CV

FULL = CV + """
Contact: laura.example@mail.com | +34 612 345 678 | linkedin.com/in/laura-example
Based in Barcelona, Spain
Languages: Spanish (native), English (C1), Catalan
"""


def test_without_ai_reads_the_basics(monkeypatch):
    monkeypatch.setattr(llm, "ollama_ok", lambda cfg: False)
    r = cv_criteria.extract(FULL, {})
    v = r["values"]
    assert not r["ai"] and "Local AI" in r["note"]
    assert v["a_name"] == "Laura Example" and v["a_title"] == "Materials Engineer"
    assert v["a_email"] == "laura.example@mail.com" and v["a_phone"] == "+34 612 345 678"
    assert v["a_linkedin"] == "https://linkedin.com/in/laura-example"
    assert v["roles"] == ["Materials Engineer"] and v["primary"] == "Materials Engineer"
    assert {"MATLAB", "SEM", "XRD", "FMEA"} <= set(v["skills"])
    assert v["languages"] == ["English", "Spanish", "Catalan"]
    assert v["home_city"] == "Barcelona" and v["location"] == "Barcelona, Spain" and v["remote_regions"] == ["Spain", "EU"]
    assert v["seniority"] == ["mid"] and r["years"] == 4.0  # 2021-2025; the internship isn't counted
    assert "min_salary" in r["missing"] and "work_mode" in r["missing"] and "a_name" not in r["missing"]


def test_ai_answers_are_checked_against_the_cv(monkeypatch):
    monkeypatch.setattr(llm, "ollama_ok", lambda cfg: True)
    seen = {}

    def fake(cfg, system, prompt, schema, **kw):
        seen["schema"] = schema
        return {"full_name": "Laura Example", "current_title": "Senior Materials Scientist",  # not in the CV
                "job_titles": ["Materials Engineer", "Composites Engineer", "Process Engineer", "R&D Intern"],
                "skills": ["SolidWorks", "Python"], "city": "Madrid", "country": "Spain",
                "languages": ["German"], "years_experience": 12}
    monkeypatch.setattr(llm, "structured", fake)
    r = cv_criteria.extract(CV, {})
    v = r["values"]
    assert r["ai"] and "full_name" in seen["schema"]["properties"]
    assert v["a_title"] == "Materials Engineer"  # the made-up title is dropped; the CV's own line is used
    assert v["roles"] == ["Materials Engineer", "Composites Engineer"]  # every word is in the CV; "process" isn't
    assert "Python" not in v["skills"] and "SolidWorks" in v["skills"]
    assert v["home_city"] == "" and v["languages"] == []  # Madrid / German aren't in the CV
    assert "home_city" in r["missing"] and v["seniority"] == ["mid"]  # years come from the CV's dates, not the model


def test_seniority_and_years():
    assert cv_criteria.years_experience("Engineer 2010 - 2015\nLead 2014 – present", now=2026) == 16.0
    assert cv_criteria.years_experience("BSc, Universidad de Sevilla 2008-2012") is None
    assert cv_criteria.seniority(1, "Engineer") == ["junior"]
    assert cv_criteria.seniority(8, "Quality Manager") == ["senior", "lead"]


def test_cv_fill_route(monkeypatch):
    from fastapi.testclient import TestClient
    from jobhunter.review.app import app
    monkeypatch.setattr(llm, "ollama_ok", lambda cfg: False)
    c = TestClient(app, base_url="http://127.0.0.1:8502")
    r = c.post("/api/setup/cv-fill", json={"text": FULL}, headers={"X-Jobhunter": "1"})
    assert r.status_code == 200 and r.json()["values"]["a_name"] == "Laura Example"
    assert c.post("/api/setup/cv-fill", json={"text": "short"}, headers={"X-Jobhunter": "1"}).status_code == 400
