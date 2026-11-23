"""No browser automation: web jobs are a hand-off, and passwords saved by earlier builds stay usable."""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunter import accounts, secrets

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Jobhunter": "1"}
SITE = "acme.wd3.myworkdayjobs.com"


def test_no_form_filling_code_or_playwright_left():
    assert importlib.util.find_spec("jobhunter.browser") is None
    from jobhunter import tasks
    assert set(tasks.QUEUES) == {"main", "gpu"}
    for f in ("requirements.txt", "requirements-app.txt"):
        assert "playwright" not in (ROOT / f).read_text(encoding="utf-8").lower()
    assert "collect_data_files(\"playwright\")" not in (ROOT / "packaging" / "jobhunter.spec").read_text(encoding="utf-8")
    from jobhunter.review.app import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not [p for p in paths if "/account/" in p or p.endswith("/refill") or "/connections/browser" in p]
    page = (ROOT / "jobhunter" / "review" / "index.html").read_text(encoding="utf-8")
    assert "Approve &amp; fire" not in page and "account/create" not in page


@pytest.fixture
def saved(monkeypatch):
    from jobhunter.desktop import confirm
    secrets.set(accounts.PREFIX + SITE, "Old-Pass-123")
    secrets.set(accounts.INDEX, json.dumps({SITE: {"email": "me@example.com", "company": "Acme",
                                                   "status": "created", "added": "2026-11-23"}}))
    ans = {"yes": True}
    monkeypatch.setattr(confirm, "ask", lambda q, d="": ans["yes"])
    yield ans
    accounts.forget(SITE)


def test_saved_passwords_are_listed_copied_after_the_popup_and_forgotten(saved):
    from jobhunter.review.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8502")
    rows = c.get("/api/accounts", headers=H).json()["accounts"]
    assert rows == [{"site": SITE, "email": "me@example.com", "company": "Acme", "status": "created",
                     "added": "2026-11-23"}]
    assert "Old-Pass-123" not in json.dumps(rows) and "Old-Pass-123" not in secrets.get(accounts.INDEX)
    saved["yes"] = False
    assert c.post("/api/accounts/reveal", json={"site": SITE}, headers=H).status_code == 403
    saved["yes"] = True
    assert c.post("/api/accounts/reveal", json={"site": SITE}, headers=H).json()["password"] == "Old-Pass-123"
    assert c.post("/api/accounts/forget", json={"site": SITE}, headers=H).json()["done"] == "Forgotten."
    assert accounts.get(SITE) is None and accounts.password(SITE) is None and secrets.get(accounts.INDEX) is None


def test_handoff_text_adds_only_answered_questions():
    from jobhunter.review.app import handoff_text
    assert handoff_text("Dear team", []) == "Dear team"
    out = handoff_text("Dear team", [{"question": "Notice period?", "answer": "One month"},
                                     {"question": "Salary?", "answer": "", "needs_jean": True}])
    assert out.startswith("Dear team\n\n---\nScreening answers") and "Notice period?\nOne month" in out
    assert "Salary?" not in out
