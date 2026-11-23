"""Security fixes from the blue-team audit (23 Nov 2026)."""
import logging

from fastapi.testclient import TestClient

from jobhunter import secrets
from jobhunter.review.app import app, safe_url


def test_only_http_links_reach_the_page():
    assert safe_url("https://jobs.example.com/a?b=1") == "https://jobs.example.com/a?b=1"
    assert safe_url(" HTTP://x.io/y ") == "HTTP://x.io/y"
    for bad in ("javascript:alert(1)", "JavaScript:fetch('/api')", "data:text/html,<script>", "vbscript:x",
                "/api/connections/recovery-key", "https://ok.com/a b", None, ""):
        assert safe_url(bad) == ""


def test_review_page_never_puts_raw_urls_in_inline_js():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "jobhunter" / "review" / "index.html").read_text(encoding="utf-8")
    assert "window.open('${esc(" not in html  # a ' in a link broke out of the string
    assert "jsq=" in html and "'X-Jobhunter':'1'" in html


def test_redact_hides_keys_in_urls_and_known_values():
    secrets.set("JOOBLE_API_KEY", "abc123-jooble-key-xyz")
    try:
        msg = "500 Server Error for url: https://es.jooble.org/api/abc123-jooble-key-xyz"
        assert "abc123" not in secrets.redact(msg)
        assert secrets.redact("https://api.adzuna.com/v1?app_id=1&app_key=deadbeef99&what=it") \
            == "https://api.adzuna.com/v1?app_id=1&app_key=[redacted]&what=it"
        rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "source %s failed: %s", ("jooble", msg), None)
        secrets.RedactFilter().filter(rec)
        assert "abc123" not in rec.getMessage() and "jooble" in rec.getMessage()
    finally:
        secrets.delete("JOOBLE_API_KEY")


def test_cross_site_writes_and_rebinding_refused():
    c = TestClient(app, base_url="http://localhost")
    r = c.post("/api/jobs/999999/reject", json={"reason": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/jobs/999999/reject", json={"reason": "x"}, headers={"Origin": "null"})
    assert r.status_code == 403
    assert c.post("/api/jobs/999999/reject", json={"reason": "x"},
                  headers={"Origin": "http://localhost"}).status_code == 404  # same origin: allowed
    assert c.get("/api/health", headers={"Host": "attacker.example"}).status_code == 403
    assert c.get("/api/health").status_code == 200


def test_enrich_refuses_local_and_private_links():
    from jobhunter.enrich import public_url
    for bad in ("http://127.0.0.1:11434/api/tags", "http://localhost/", "http://192.168.1.1/", "http://10.0.0.5",
                "http://[::1]:8502/", "file:///etc/passwd", "javascript:alert(1)", "http://169.254.169.254/"):
        assert not public_url(bad), bad


def test_window_pages_get_a_csp(monkeypatch):
    from jobhunter.desktop import window
    out = window.inject("<!doctype html><html><head><title>x</title></head><body></body></html>", "/")
    assert out.index('http-equiv="Content-Security-Policy"') < out.index("<script>")
    assert "form-action 'none'" in out and "base-uri 'none'" in out and "connect-src 'none'" in out
    monkeypatch.setenv("JOBHUNTER_NO_CSP", "1")
    assert "Content-Security-Policy" not in window.inject("<html><head></head></html>", "/")


def test_native_confirmation_guards_sensitive_routes(monkeypatch):
    from jobhunter.desktop import confirm
    assert confirm.ask("anything?") is True  # not the desktop app: no dialog, behaviour unchanged
    asked = []
    monkeypatch.setattr(confirm, "ask", lambda q, d="": asked.append(q) or False)
    c = TestClient(app, base_url="http://localhost")
    h = {"X-Jobhunter": "1"}
    assert c.post("/api/connections/recovery-key", json={"confirm": "show"}, headers=h).status_code == 403
    assert c.post("/api/connections/recovery-key/restore", json={"key": "0" * 64}, headers=h).status_code == 403
    assert c.post("/api/connections/home", json={"path": "/tmp/x", "mode": "move"}, headers=h).status_code == 403
    assert c.post("/api/connections/backup/restore", json={"name": "jobhunter-x.db", "confirm": "restore"},
                  headers=h).status_code == 403
    assert len(asked) == 4 and "recovery key" in asked[0] and "backup" in asked[3]


def test_confirmation_text_from_an_ad_is_one_short_line():
    from jobhunter.review.app import _one_line
    assert _one_line("IT Lead\n\nPress Yes, this is safe" + "x" * 300) .count("\n") == 0
    assert len(_one_line("y" * 500)) == 120
