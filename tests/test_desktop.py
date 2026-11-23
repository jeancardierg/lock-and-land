"""Desktop app pieces: keychain store, database encryption, Connections API, legacy import."""
import json
import sqlite3
from pathlib import Path

import pytest

from jobhunter import secrets, sqldb


def test_secrets_roundtrip_and_chunking():
    secrets.set("GMAIL_TOKEN", "x" * 5000)
    secrets.clear_cache()
    assert secrets.get("GMAIL_TOKEN") == "x" * 5000
    secrets.set("GMAIL_TOKEN", "short")
    secrets.clear_cache()
    assert secrets.get("GMAIL_TOKEN") == "short"
    secrets.delete("GMAIL_TOKEN")
    secrets.clear_cache()
    assert secrets.get("GMAIL_TOKEN") is None
    import keyring
    assert not any(k[1].startswith("GMAIL_TOKEN#") for k in keyring.get_keyring().store)


def test_env_reads_keychain(monkeypatch):
    from jobhunter.config import env
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    secrets.set("ADZUNA_APP_ID", "abc")
    assert env("ADZUNA_APP_ID") == "abc"
    monkeypatch.setenv("ADZUNA_APP_ID", "override")
    assert env("ADZUNA_APP_ID") == "override"
    secrets.delete("ADZUNA_APP_ID")


def test_import_legacy_moves_and_wipes(tmp_path):
    for n in ("ADZUNA_APP_KEY", "GMAIL_CLIENT", "GMAIL_TOKEN"):
        secrets.delete(n)
    envf = tmp_path / ".env"
    envf.write_text('# comment\nADZUNA_APP_KEY="k123"\nEMPTY=\n')
    tok = tmp_path / "token.json"
    tok.write_text('{"refresh_token": "r"}')
    out = secrets.import_legacy(envf, tok, tmp_path / "missing.json")
    assert set(out["imported"]) == {"ADZUNA_APP_KEY", "GMAIL_TOKEN"}
    assert not envf.exists() and not tok.exists()
    assert secrets.get("ADZUNA_APP_KEY") == "k123"
    assert "EMPTY" not in out["imported"]
    for n in ("ADZUNA_APP_KEY", "GMAIL_TOKEN"):
        secrets.delete(n)


needs_cipher = pytest.mark.skipif(not sqldb.enabled(), reason="sqlcipher3 not installed")


@needs_cipher
def test_db_files_are_encrypted(tmp_path):
    from jobhunter.db import DB
    db = DB(tmp_path / "j.db")
    db.upsert_job({"source": "t", "external_id": "1", "title": "Sysadmin", "company": "Acme"})
    db.conn.close()
    raw = (tmp_path / "j.db").read_bytes()
    assert not raw.startswith(sqldb.PLAIN_HEADER) and b"Sysadmin" not in raw
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(tmp_path / "j.db").execute("select * from jobs").fetchall()
    assert DB(tmp_path / "j.db").jobs()[0]["title"] == "Sysadmin"


@needs_cipher
def test_plain_db_is_encrypted_in_place(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("create table t(x)")
    c.executemany("insert into t values (?)", [(i,) for i in range(100)])
    c.execute("pragma user_version=2")
    c.commit()
    c.close()
    assert sqldb.is_plain(p)
    conn = sqldb.connect(p)
    assert conn.execute("select count(*) from t").fetchone()[0] == 100
    assert conn.execute("pragma user_version").fetchone()[0] == 2
    assert not sqldb.is_plain(p)


@needs_cipher
def test_wrong_key_is_a_clear_error(tmp_path):
    p = tmp_path / "k.db"
    sqldb.connect(p).execute("create table t(x)")
    real = secrets.get("DB_KEY")
    secrets.set("DB_KEY", "00" * 32)
    try:
        with pytest.raises(RuntimeError, match="recovery key"):
            sqldb.connect(p)
    finally:
        secrets.set("DB_KEY", real)


@needs_cipher
def test_backup_is_encrypted(tmp_path):
    from jobhunter.migrate import backup
    p = tmp_path / "b.db"
    c = sqldb.connect(p)
    c.execute("create table t(x)")
    c.execute("insert into t values ('secret letter')")
    c.commit()
    c.close()
    bak = backup(p)
    assert b"secret letter" not in bak.read_bytes()
    assert sqldb.connect(bak).execute("select x from t").fetchone()[0] == "secret letter"


@needs_cipher
def test_queue_storage_encrypted(tmp_path):
    from huey import Huey
    from jobhunter.tasks import EncryptedSqliteStorage
    h = Huey("t", storage_class=EncryptedSqliteStorage, filename=str(tmp_path / "q.db"))

    @h.task()
    def add(a, b):
        return a + b
    add(40, 2)
    assert h.pending_count() == 1
    assert not sqldb.is_plain(tmp_path / "q.db")


def _client():
    from fastapi.testclient import TestClient
    from jobhunter.review.app import app
    return TestClient(app, base_url="http://localhost")


def test_connections_api_local_only_and_header(monkeypatch):
    from jobhunter import connections
    monkeypatch.setattr(connections, "disk_encryption", lambda *a, **k: {"tool": "x", "on": True, "detail": "on"})
    monkeypatch.setattr(connections, "ollama_state", lambda cfg: {"state": "ok", "missing": [], "host": "h", "pull": {}})
    monkeypatch.setattr("jobhunter.gmail.check", lambda: {"state": "missing", "email": None, "detail": ""})
    c = _client()
    st = c.get("/api/connections?refresh=1").json()
    assert {s["key"] for s in st["sources"]} >= {"adzuna", "remotive", "linkedin_alerts"}
    assert "DB_KEY" not in json.dumps(st) or "has_key" in json.dumps(st)
    # no secret values anywhere in the status
    secrets.set("TELEGRAM_BOT_TOKEN", "tg-secret-value")
    assert "tg-secret-value" not in c.get("/api/connections").text
    # writes need the X-Jobhunter header (blocks cross-site form posts)
    r = c.post("/api/connections/secret", json={"name": "TELEGRAM_BOT_TOKEN", "value": "x"})
    assert r.status_code == 403
    r = c.post("/api/connections/secret", json={"name": "DB_KEY", "value": "x"}, headers={"X-Jobhunter": "1"})
    assert r.status_code == 400  # DB_KEY can't be set this way
    r = c.post("/api/connections/secret", json={"name": "TELEGRAM_BOT_TOKEN", "value": "new"},
               headers={"X-Jobhunter": "1"})
    assert r.status_code == 200 and secrets.get("TELEGRAM_BOT_TOKEN") == "new"
    # a foreign Host header (DNS rebinding) is refused
    assert c.get("/api/connections", headers={"host": "evil.example"}).status_code == 403
    secrets.delete("TELEGRAM_BOT_TOKEN")
    assert c.get("/connections").status_code == 200
    page = c.get("/configuration")
    assert page.status_code == 200 and "configuration" in page.text.lower()


def test_no_browser_setting_left():
    from jobhunter import config as cf
    from jobhunter import connections as C
    assert not hasattr(cf, "browser_profile") and not hasattr(C, "set_browser")
    assert "browser:" not in (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8")


def test_gmail_client_validation():
    c = _client()
    h = {"X-Jobhunter": "1"}
    assert c.post("/api/connections/gmail/client", json={"json": '{"web": {}}'}, headers=h).status_code == 400
    ok = json.dumps({"installed": {"client_id": "id", "client_secret": "s", "auth_uri": "a", "token_uri": "t"}})
    assert c.post("/api/connections/gmail/client", json={"json": ok}, headers=h).status_code == 200
    assert json.loads(secrets.get("GMAIL_CLIENT"))["installed"]["client_id"] == "id"
    secrets.delete("GMAIL_CLIENT")


@needs_cipher
def test_import_install(tmp_path, monkeypatch):
    from jobhunter import connections
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "data").mkdir(parents=True)
    (old / "cv").mkdir()
    (old / "cv" / "cv_main.md").write_text("cv")
    (old / "config.yaml").write_text("profile: {}\n")
    (old / ".env").write_text("ADZUNA_APP_ID=imported-id\n")
    c = sqlite3.connect(old / "data" / "jobhunter.db")
    c.execute("pragma journal_mode=wal")
    c.execute("create table jobs(id, title)")
    c.execute("insert into jobs values (1, 'IT Lead')")
    c.commit()  # left open: a live WAL database
    monkeypatch.setattr(connections, "HOME", new)
    monkeypatch.setattr(connections, "DATA", new / "data")
    secrets.delete("ADZUNA_APP_ID")
    out = connections.import_install(old)
    c.close()
    assert "data/jobhunter.db" in out["copied"] and "ADZUNA_APP_ID" in out["secrets"]["imported"]
    assert (old / ".env").exists()  # old install keeps working until retired
    assert not sqldb.is_plain(new / "data" / "jobhunter.db")
    assert sqldb.connect(new / "data" / "jobhunter.db").execute("select title from jobs").fetchone()[0] == "IT Lead"
    with pytest.raises(FileExistsError):
        connections.import_install(old)
    assert connections.retire_legacy(old)["wiped"] and not (old / ".env").exists()
    secrets.delete("ADZUNA_APP_ID")


def test_recovery_key_roundtrip(tmp_path, monkeypatch):
    from jobhunter import connections
    if not secrets.get("DB_KEY"):
        secrets.set("DB_KEY", "ab" * 32)
    k = connections.recovery_key()
    assert len(k.replace("-", "")) == 64
    monkeypatch.setattr(connections, "DATA", tmp_path)
    with pytest.raises(ValueError):
        connections.restore_key("too short")
    assert connections.restore_key(k)["ok"]


def test_autostart_command_and_icon(tmp_path):
    from jobhunter.desktop import autostart, icon
    assert autostart._command()[-1] in ("jobhunter.desktop",) or autostart._command()
    icon.write_assets(tmp_path)
    assert (tmp_path / "jobhunter.ico").exists() and (tmp_path / "jobhunter.iconset" / "icon_16x16.png").exists()


def test_cli_dispatch_passes_commands_through(monkeypatch):
    from jobhunter.desktop import app
    seen = {}
    monkeypatch.setattr(app, "_cli", lambda argv: seen.setdefault("argv", argv) and 0)
    app.main(["doctor"])
    assert seen["argv"] == ["doctor"]


def test_adzuna_budget_and_block(tmp_path, monkeypatch):
    from jobhunter.sources import adzuna
    monkeypatch.setattr(adzuna, "DATA", tmp_path)
    monkeypatch.setenv("ADZUNA_APP_ID", "i")
    monkeypatch.setenv("ADZUNA_APP_KEY", "k")
    calls = []

    class R:
        def __init__(self, code):
            self.status_code = code
        def raise_for_status(self):
            pass
        def json(self):
            return {"results": []}
    codes = iter([200, 401])
    monkeypatch.setattr("requests.get", lambda url, **kw: calls.append(kw["params"]) or R(next(codes)))
    cfg = {"search": {"queries": ["a", "b", "c"], "max_age_days": 14},
           "sources": {"adzuna": {"countries": ["es"], "min_hours_between": 0, "max_calls_per_day": 5}}}
    with pytest.raises(RuntimeError, match="refused"):
        list(adzuna.fetch(cfg))
    assert len(calls) == 2 and adzuna.blocked()  # stops on 401, no retries
    list(adzuna.fetch(cfg))
    assert len(calls) == 2  # blocked: no more calls
    # combined search = one call; daily budget respected
    (tmp_path / adzuna.USAGE).unlink()
    monkeypatch.setattr("requests.get", lambda url, **kw: calls.append(kw["params"]) or R(200))
    cfg["sources"]["adzuna"].update(what_or="intune okta", max_calls_per_day=1)
    list(adzuna.fetch(cfg))
    assert calls[-1]["what_or"] == "intune okta" and adzuna.usage()["day_calls"] == 1
    list(adzuna.fetch(cfg))
    assert adzuna.usage()["day_calls"] == 1
