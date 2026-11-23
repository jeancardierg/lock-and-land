"""App window plumbing and free-by-default sources."""
import json

import yaml

from jobhunter import connections as C
from jobhunter.desktop import window


def test_window_manager_reuses_running_window(tmp_path, monkeypatch):
    started = []

    class P:
        def __init__(self, argv, **kw):
            started.append(argv)

        def poll(self):
            return None

    monkeypatch.setattr(window.subprocess, "Popen", P)
    wm = window.WindowManager(tmp_path)
    assert wm.open("/setup") == "started"
    assert started[0][-2] == "/setup" and "--window" in started[0]  # a path: there is no web address any more
    assert wm.open("connections") == "shown"
    assert json.loads((tmp_path / "window.cmd").read_text())["path"] == "/connections"
    assert len(started) == 1


def test_window_off_opens_nothing(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(window.webbrowser, "open", opened.append)
    assert window.WindowManager(tmp_path, enabled=False).open("/") == "off" and opened == []


def test_adzuna_is_optional_and_toggle_writes_config(monkeypatch, tmp_path):
    from jobhunter import criteria
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"sources": {"adzuna": {"enabled": True, "countries": ["es"]}}}), encoding="utf-8")
    monkeypatch.setattr(criteria, "config_path", lambda: cfg)
    C.set_source("adzuna", False)
    C.set_source("jooble", True)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert raw["sources"]["adzuna"] == {"enabled": False, "countries": ["es"]}
    assert raw["sources"]["jooble"]["enabled"] is True
    assert C.SOURCES["adzuna"].get("paid")


def test_example_config_is_free_by_default():
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).parent.parent / "config.example.yaml").read_text(encoding="utf-8"))
    assert raw["sources"]["adzuna"]["enabled"] is False
