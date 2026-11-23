"""Setup a non-technical tester can follow: Gmail with an app password, key checks, plain errors, recovery key."""
import imaplib
import shutil
from pathlib import Path

import pytest
import yaml

from jobhunter import connections as C
from jobhunter import criteria, mailimap, secrets

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"profile": {"email": "you@gmail.com"}, "mail": {"backend": "gmail_api"}}),
                   encoding="utf-8")
    monkeypatch.setattr(criteria, "config_path", lambda: cfg)
    secrets.delete("GMAIL_APP_PASSWORD")
    yield cfg
    secrets.delete("GMAIL_APP_PASSWORD")


def test_app_password_is_checked_before_it_is_saved(cfgfile, monkeypatch):
    with pytest.raises(ValueError, match="16 letters"):
        C.gmail_app_password("laura@gmail.com", "MyNormalPassword1!")
    with pytest.raises(ValueError, match="Gmail address"):
        C.gmail_app_password("laura", "abcd efgh ijkl mnop")

    def refuse(user, pw):
        raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    monkeypatch.setattr(mailimap, "test_login", refuse)
    with pytest.raises(ValueError, match="didn't accept"):
        C.gmail_app_password("laura@gmail.com", "abcd efgh ijkl mnop")
    assert not secrets.get("GMAIL_APP_PASSWORD")
    assert yaml.safe_load(cfgfile.read_text())["mail"]["backend"] == "gmail_api"


def test_app_password_saves_and_switches_to_imap(cfgfile, monkeypatch):
    seen = []
    monkeypatch.setattr(mailimap, "test_login", lambda u, p: seen.append((u, p)))
    assert C.gmail_app_password(" laura@gmail.com ", "abcd-efgh ijkl mnop")["ok"]
    assert seen == [("laura@gmail.com", "abcdefghijklmnop")]
    assert secrets.get("GMAIL_APP_PASSWORD") == "abcdefghijklmnop"
    raw = yaml.safe_load(cfgfile.read_text())
    assert raw["mail"]["backend"] == "imap" and raw["profile"]["email"] == "laura@gmail.com"
    st = C.gmail_state(raw)
    assert st["backend"] == "imap" and st["state"] == "ok" and st["email"] == "laura@gmail.com"
    C.gmail_mode("gmail_api")
    assert yaml.safe_load(cfgfile.read_text())["mail"]["backend"] == "gmail_api"
    with pytest.raises(ValueError):
        C.gmail_mode("pop3")


def test_imap_errors_in_plain_words():
    assert "normal Google password" in mailimap.explain(
        imaplib.IMAP4.error(b"[ALERT] Application-specific password required: https://support.google.com/"))
    assert "internet" in mailimap.explain(TimeoutError("timed out"))
    assert mailimap.clean_password(" abcd efgh-ijkl\tmnop ") == "abcdefghijklmnop"


def test_oauth_errors_in_plain_words():
    assert "test user" in C.explain_oauth_error(Exception("(access_denied) Access blocked"))
    assert "10 minutes" in C.explain_oauth_error(TimeoutError("timed out"))


class _R:
    def __init__(self, code):
        self.status_code = code


@pytest.mark.parametrize("code,ok", [(200, True), (403, False), (429, True), (500, False)])
def test_job_board_key_test(monkeypatch, code, ok):
    secrets.set("JOOBLE_API_KEY", "11111111-2222-3333")
    calls = []
    monkeypatch.setattr(C.requests, "post", lambda url, **kw: calls.append(url) or _R(code))
    r = C.source_test("jooble", {"sources": {}})
    assert r["ok"] is ok and calls[0].endswith("/11111111-2222-3333")
    if code == 403:
        assert "email" in r["detail"]  # says where to find the key again
    secrets.delete("JOOBLE_API_KEY")
    assert C.source_test("jooble", {"sources": {}})["ok"] is False


def test_saving_a_key_reports_the_test(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jobhunter.review import connections as R
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\n", encoding="utf-8")
    monkeypatch.setattr(criteria, "config_path", lambda: cfg)
    monkeypatch.setattr(C, "source_test", lambda key, cfg=None: {"ok": False, "detail": "nope"})
    app = FastAPI()
    app.include_router(R.router)
    c = TestClient(app, base_url="http://localhost")
    r = c.post("/api/connections/secret", json={"name": "FINDWORK_API_KEY", "value": "abc"}, headers={"X-Jobhunter": "1"})
    assert r.status_code == 200 and r.json()["test"] == {"ok": False, "detail": "nope"}
    r = c.post("/api/connections/gmail/app-password", json={"email": "x@gmail.com", "password": "short"},
               headers={"X-Jobhunter": "1"})
    assert r.status_code == 400 and "16 letters" in r.json()["detail"]
    secrets.delete("FINDWORK_API_KEY")


def test_recovery_key_saved_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "DATA", tmp_path)
    assert not C.recovery_saved()
    C.mark_recovery_saved()
    assert C.recovery_saved()


def test_new_person_never_inherits_the_example_details(tmp_path, monkeypatch):
    from jobhunter import profile_setup
    shutil.copy2(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    monkeypatch.setattr(profile_setup, "path", lambda: tmp_path / "facts" / "master_profile.json")
    monkeypatch.setattr(criteria, "HOME", tmp_path)
    criteria.apply({"roles": "Nurse", "home_city": "Valencia", "remote_regions": "Spain"}, tmp_path / "config.yaml")
    p = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["profile"]
    assert p["first_name"] != "Jean" and p["work_authorization"] == ""
    assert p["current_title"] == "" and "Intune" not in p["headline"]


def test_connections_page_speaks_plainly():
    html = (ROOT / "jobhunter" / "review" / "connections.html").read_text(encoding="utf-8")
    for must in ("Getting started", "myaccount.google.com/apppasswords", "Download Ollama", "I've saved it",
                 "How to get the free key", "Advanced (you can ignore this)"):
        assert must in html
