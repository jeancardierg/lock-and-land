"""The desktop app opens no network port: the window talks to the app over a private pipe/socket, and the pages'
fetch() calls go through the window's bridge into the same FastAPI routes, called in-process."""
import base64
import json
import socket
import sys

import pytest

from jobhunter.desktop import ipc, window
from jobhunter.review.app import app


def test_dispatcher_calls_routes_without_a_server():
    d = ipc.AsgiDispatcher(app)
    r = d.request("GET", "/setup")
    assert r["status"] == 200 and b"Lock &amp; Land" in r["body"]
    r = d.request("GET", "/api/setup")
    assert r["status"] == 200 and "roles" in json.loads(r["body"])
    r = d.request("POST", "/api/setup/cv-fill", {"Content-Type": "application/json"}, b'{"text":"short"}')
    assert r["status"] == 400  # the X-Jobhunter header is added: the window is this app
    assert d.request("GET", "/static/leaflet/leaflet.js")["status"] == 200


@pytest.mark.skipif(sys.platform == "win32", reason="the named-pipe variant runs on Windows builds")
def test_pipe_round_trip_and_no_tcp_listener(tmp_path):
    before = _tcp_listeners()
    srv = ipc.Server(tmp_path, app, on_show=lambda p: f"showed {p}")
    try:
        assert ipc.running(tmp_path)
        assert ipc.call(tmp_path, {"op": "show", "path": "/connections"})["how"] == "showed /connections"
        r = ipc.call(tmp_path, {"op": "http", "method": "GET", "path": "/api/setup"})
        assert r["ok"] and r["status"] == 200 and "roles" in json.loads(base64.b64decode(r["body"]))
        assert _tcp_listeners() == before  # nothing new listens on TCP
        (tmp_path / "window" / "ipc.key").write_bytes(b"wrong key")
        with pytest.raises(Exception):
            ipc.call(tmp_path, {"op": "ping"})
    finally:
        srv.close()
    assert not ipc.running(tmp_path)


def _tcp_listeners():
    try:
        import psutil
    except ImportError:
        return None
    import os
    try:
        return sorted((c.laddr.ip, c.laddr.port) for c in psutil.Process(os.getpid()).net_connections("tcp")
                      if c.status == psutil.CONN_LISTEN)
    except Exception:
        return None


def test_inject_puts_the_bridge_first_and_keeps_the_page():
    html = "<!doctype html><html><head><title>x</title></head><body><script>fetch('/api/setup')</script></body></html>"
    out = window.inject(html, "/#waiting", {"jh_filters": '{"min":3}'})
    assert out.index("window.fetch=async") < out.index("<title>x</title>")
    assert 'window.JH_PATH="/"' in out and 'window.JH_HASH="#waiting"' in out and "jh_filters" in out
    assert "<\\/script" not in json.dumps({"a": 1})  # sanity
    assert window.inject("<p>no head</p>", "/").endswith("<p>no head</p>")


def test_bridge_rejects_non_app_paths_and_saves_prefs(tmp_path, monkeypatch):
    b = window.Bridge(tmp_path)
    assert b.http("GET", "https://example.com/", {}, "")["ok"] is False
    b.pref("jh_filters", '{"min":3}')
    assert window.Bridge(tmp_path)._prefs() == {"jh_filters": '{"min":3}'}
    opened = []
    monkeypatch.setattr(window.webbrowser, "open", opened.append)
    b.external("javascript:alert(1)")
    b.external("https://jobs.example/1")
    assert opened == ["https://jobs.example/1"]


def test_pages_have_no_server_only_navigation():
    from pathlib import Path
    root = Path(window.__file__).resolve().parent.parent / "review"
    for f in ("index.html", "setup.html", "connections.html"):
        s = (root / f).read_text(encoding="utf-8")
        assert "location.href=" not in s.replace("location.href=p", ""), f
        assert "src='/static" not in s and 'src="/static' not in s, f


def test_one_card_route():
    d = ipc.AsgiDispatcher(app)
    assert d.request("GET", "/api/jobs/987654321/card")["status"] == 404


def test_copy_buttons_go_through_the_bridge():
    """The window's pages have no secure origin, so navigator.clipboard is undefined there: the shim must provide
    a copy function and the pages must use it (a user hit "Cannot read properties of undefined (reading 'writeText')")."""
    from pathlib import Path
    from jobhunter.desktop import window
    assert "window.jhCopy=" in window.SHIM and "defineProperty(navigator,'clipboard'" in window.SHIM
    root = Path(window.__file__).resolve().parents[1] / "review"
    for f in ("index.html", "connections.html", "setup.html"):
        page = (root / f).read_text(encoding="utf-8")
        assert "navigator.clipboard.writeText(" not in page.replace("navigator.clipboard?navigator.clipboard.writeText(t)", "")
    assert window.Bridge.__dict__["copy"] and hasattr(window, "_win_copy")


def test_docs_and_pages_describe_no_local_server():
    """The app has no web server, so docs, example config and pages must not send anyone to a local port."""
    from pathlib import Path
    root = Path(window.__file__).resolve().parents[2]
    files = ["README.md", "DESKTOP.md", "SECURITY.md", "config.example.yaml",
             *(f"jobhunter/review/{f}" for f in ("index.html", "setup.html", "connections.html"))]
    for f in files:
        s = (root / f).read_text(encoding="utf-8").lower()
        for bad in (":8501", ":8502", "tailscale", "127.0.0.1", "playwright", "local server"):
            assert bad not in s, (f, bad)
