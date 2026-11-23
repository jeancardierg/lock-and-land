"""A new person (not the original user): a materials engineer sets up from the example config with her own CV."""
import json
import shutil
from pathlib import Path

import pytest
import yaml

from jobhunter import criteria, master, profile_setup, validate
from jobhunter.lexicon import find_tools

ROOT = Path(__file__).resolve().parent.parent
CV = """# Laura Example
Materials Engineer

## Experience
### Materials Engineer, Example Composites SL (2021-2025)
- Characterised carbon-fibre composites with SEM, XRD and DSC for aerospace clients
- Led a 3-person team that reduced porosity defects by 40% in autoclave curing
- Responsible for supplier qualification of epoxy resins under ISO 9001
### R&D Intern, Instituto de Materiales (2020)
- Assisted with tensile testing and metallography of aluminium alloys

## Skills
MATLAB, SolidWorks, Minitab, FMEA, design of experiments

## Certifications
- Six Sigma Green Belt
"""


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    shutil.copy2(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    monkeypatch.setattr(profile_setup, "path", lambda: tmp_path / "facts" / "master_profile.json")
    monkeypatch.setattr(criteria, "HOME", tmp_path)
    return tmp_path


def test_parse_cv_keeps_her_words():
    p = profile_setup.parse_cv(CV)
    assert len(p["facts"]) == 4 and "MATLAB" in p["skills"] and p["certifications"] == ["Six Sigma Green Belt"]
    prof = profile_setup.build(CV, {"name": "Laura Example"}, ["XRD"])
    levels = {f["text"][:10]: f["verb_level"] for f in prof["facts"]}
    assert levels["Led a 3-pe"] == "led_delivery" and levels["Assisted w"] == "supported"
    assert levels["Responsibl"] == "owned" and levels["Characteri"] == "executed"
    with pytest.raises(ValueError):
        profile_setup.build("just a name", {})


def test_fresh_wizard_has_empty_criteria_and_asks_for_cv(fresh):
    v = criteria.from_config(yaml.safe_load((fresh / "config.yaml").read_text(encoding="utf-8")))
    assert v["roles"] == [] and v["skills"] == [] and v["avoid"] == []
    assert v["about"] is not None and v["about"]["name"] == ""


def test_fresh_wizard_saves_cv_profile_and_criteria(fresh):
    body = {"roles": "Materials engineer\nIngeniera de materiales\nR&D engineer\nProcess engineer",
            "title_words": "materials, materiales, process, R&D, composites, quality", "skills": "SEM, XRD",
            "home_city": "Barcelona", "remote_regions": "Spain, EU", "work_mode": "any", "languages": "Spanish, English",
            "about": {"name": "Laura Example", "email": "laura@example.com", "current_title": "Materials Engineer",
                      "cv_text": CV, "confirm": True, "cv_pdf_b64": "data:application/pdf;base64,JVBERi0xLjQK"}}
    res = criteria.apply(body, fresh / "config.yaml")
    assert res["facts"] == 4
    raw = yaml.safe_load((fresh / "config.yaml").read_text(encoding="utf-8"))
    assert raw["profile"]["name"] == "Laura Example" and raw["profile"]["first_name"] == "Laura"
    assert raw["profile"]["cv_variants"] == {"main": {"markdown": "cv/cv_main.md", "attachment": "cv/Laura_Example_CV.pdf"}}
    assert raw["targets"]["skip_role_families"] == [] and "template" not in raw["wizard"]
    assert (fresh / "cv" / "Laura_Example_CV.pdf").read_bytes().startswith(b"%PDF")
    prof = master.load(fresh / "facts" / "master_profile.json")
    assert prof.approved and not master.problems(prof)
    assert {"SEM", "XRD", "DSC", "MATLAB"} <= prof.verified_tools()
    # the checker still stops claims her CV doesn't support
    rep = validate.Report() if hasattr(validate, "Report") else None
    if rep is not None:
        validate.check_text("I used ANSYS and COMSOL daily.", prof, rep)
        assert any("ANSYS" in h for h in rep.hard)


def test_cv_needs_confirmation_and_never_overwrites_manual(fresh):
    body = {"roles": "Materials engineer", "about": {"name": "L", "cv_text": CV, "confirm": False}}
    with pytest.raises(ValueError):
        criteria.apply(body, fresh / "config.yaml")
    (fresh / "facts").mkdir(exist_ok=True)
    (fresh / "facts" / "master_profile.json").write_text(json.dumps({"version": "1"}), encoding="utf-8")
    assert profile_setup.existing_kind() == "manual"
    with pytest.raises(ValueError):
        profile_setup.save({"x": 1})


def test_engineering_jobs_are_not_skipped_for_new_users():
    from jobhunter.pipeline import _skip_families
    assert _skip_families({"targets": {"skip_role_families": []}}) == []
    assert "software_dev" in _skip_families({"targets": {}})
    assert set(find_tools("FTIR, Abaqus and ISO 13485")) == {"FTIR", "Abaqus", "ISO 13485"}


def _pdf(lines):
    """A minimal one-page PDF with plain text lines (no extra libraries)."""
    body = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({l}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(body)} >>\nstream\n{body}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF"
    return out.encode("latin-1")


def test_cv_text_from_pdf_docx_and_txt():
    lines = ["Materials Engineer", "- Characterised composites with SEM and XRD for aerospace clients",
             "- Led a 3-person team that reduced porosity defects by 40 percent"]
    t = profile_setup.text_from_file(_pdf(lines), "cv.pdf")
    assert "Characterised composites with SEM" in t and "Led a 3-person team" in t
    import io
    import docx
    d = docx.Document()
    d.add_heading("Experience", 2)
    d.add_paragraph("Characterised composites with SEM and XRD for aerospace clients", style="List Bullet")
    d.add_paragraph("Responsible for supplier qualification of epoxy resins under ISO 9001", style="List Bullet")
    buf = io.BytesIO()
    d.save(buf)
    t = profile_setup.text_from_file(buf.getvalue(), "cv.docx")
    assert "## Experience" in t and "- Responsible for supplier qualification" in t
    assert len(profile_setup.parse_cv(t)["facts"]) == 2
    assert "SEM" in profile_setup.text_from_file(CV.encode(), "cv.md")
    with pytest.raises(ValueError):
        profile_setup.text_from_file(b"%PDF-1.4 nothing", "scan.pdf")


def test_cv_text_route():
    import base64
    from fastapi.testclient import TestClient
    from jobhunter.review.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8502")
    b64 = "data:application/pdf;base64," + base64.b64encode(_pdf(["- Ran DSC and TGA on polymer blends every week for two years"] * 3)).decode()
    r = c.post("/api/setup/cv-text", json={"name": "cv.pdf", "b64": b64}, headers={"X-Jobhunter": "1"})
    assert r.status_code == 200 and "DSC and TGA" in r.json()["text"]
    assert c.post("/api/setup/cv-text", json={"name": "x.exe", "b64": "AAAA"}, headers={"X-Jobhunter": "1"}).status_code == 400
