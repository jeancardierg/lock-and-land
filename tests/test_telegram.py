"""Telegram alerts: setup from the Configuration page, test message, and the token never leaking into logs."""
import logging

import pytest
import requests

from jobhunter import connections as C
from jobhunter import notify, secrets

TOKEN = "123456789:AAHsecretsecretsecretsecretsecret12"


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.ok, self.reason = status, body, 200 <= status < 300, "x"

    def json(self):
        return self._b


@pytest.fixture
def tg(monkeypatch):
    calls = []
    replies = {}

    def post(url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        calls.append((method, json))
        r = replies.get(method)
        if isinstance(r, Exception):
            raise r
        return r or Resp(200, {"ok": True, "result": True})
    monkeypatch.setattr(notify.requests, "post", post)
    replies["getMe"] = Resp(200, {"ok": True, "result": {"username": "my_alerts_bot", "first_name": "Alerts"}})
    secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
    yield calls, replies
    secrets.delete("TELEGRAM_BOT_TOKEN")
    C._set_telegram(enabled=False, chat_id="", chat_name="", bot="")


def test_clean_token():
    assert notify.clean_token(f" bot{TOKEN} ") == TOKEN
    assert notify.clean_token(f"Use this token to access the HTTP API:\n{TOKEN}\nKeep it secure") == TOKEN


def test_off_until_chat_connected(tg):
    calls, _ = tg
    assert notify.telegram({"notify": {"telegram": {"enabled": True}}}, "hi") is False
    assert not [c for c in calls if c[0] == "sendMessage"]


def test_connect_finds_chat_turns_on_and_says_hello(tg):
    calls, replies = tg
    replies["getUpdates"] = Resp(200, {"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Alex"}, "text": "/start"}}]})
    r = C.telegram_connect()
    assert r["ok"] and r["sent"]
    st = C.telegram_state()
    assert st["enabled"] and st["chat_set"] and st["bot"] == "my_alerts_bot" and st["last"]["ok"]
    assert ("sendMessage", {"chat_id": "42", "text": calls[-1][1]["text"], "disable_web_page_preview": True}) == calls[-1]
    assert C.telegram_test()["ok"]
    # alerts from tasks go out now
    from jobhunter.config import load_config
    notify.send(load_config(), "Lock & Land: your turn", "1 application(s) are waiting for you.")
    assert calls[-1][0] == "sendMessage" and "your turn" in calls[-1][1]["text"]


def test_connect_without_start_explains(tg):
    _, replies = tg
    replies["getUpdates"] = Resp(200, {"ok": True, "result": []})
    r = C.telegram_connect()
    assert not r["ok"] and "@my_alerts_bot" in r["detail"] and "Start" in r["detail"]


def test_bad_token_and_network_errors_never_log_token(tg, caplog):
    _, replies = tg
    replies["getMe"] = Resp(401, {"ok": False, "description": "Unauthorized"})
    assert "BotFather" in notify.telegram_bot()["detail"]
    C._set_telegram(enabled=True, chat_id="42")
    replies["sendMessage"] = requests.exceptions.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    caplog.set_level(logging.INFO)
    from jobhunter.config import load_config
    assert notify.telegram(load_config(), "x") is False
    replies["sendMessage"] = requests.exceptions.ProxyError(f"Unable to connect to proxy /bot{TOKEN}/sendMessage")
    assert notify.telegram(load_config(), "x") is False
    assert "proxy" in notify.last_status()["detail"].lower()
    assert TOKEN not in caplog.text and TOKEN.split(":")[1] not in caplog.text
    assert TOKEN not in str(notify.last_status())


def test_api_routes(tg):
    from fastapi.testclient import TestClient
    from jobhunter.review.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8502")
    h = {"X-Jobhunter": "1"}
    r = c.post("/api/connections/secret", json={"name": "TELEGRAM_BOT_TOKEN", "value": "bot" + TOKEN}, headers=h)
    assert r.status_code == 200 and r.json()["test"]["ok"] and secrets.get("TELEGRAM_BOT_TOKEN") == TOKEN
    assert TOKEN not in r.text
    assert c.post("/api/connections/telegram/test", json={}, headers=h).json()["ok"] is False  # no chat yet
    assert c.post("/api/connections/telegram/chat", json={"chat_id": "nope"}, headers=h).status_code == 400
    assert c.post("/api/connections/telegram/chat", json={"chat_id": "123456789"}, headers=h).json()["ok"]
    assert c.post("/api/connections/telegram/test", json={}, headers=h).json()["ok"] is True
    assert c.post("/api/connections/telegram/enabled", json={"enabled": False}, headers=h).json()["enabled"] is False
    assert c.post("/api/connections/telegram/test", json={}).status_code == 403  # header required
    assert TOKEN not in c.get("/api/connections").text
