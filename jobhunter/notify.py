"""Local notifications: a Windows toast (win11toast) when available, otherwise just the log.

Optional Telegram (plain HTTPS to the Bot API). It only ever sends short alerts (how many letters are ready,
something waits for you, a sign-in or database problem), never letter text. Set up on the Configuration page:
save the bot token (OS keychain), press Start in the bot, press "Find my chat" (getUpdates fills
notify.telegram.chat_id and turns alerts on), then "Send test message". The token is never logged.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime

import requests

from .config import env

log = logging.getLogger(__name__)


def toast(title: str, body: str, url: str | None = None) -> bool:
    if sys.platform == "darwin":
        import subprocess
        q = lambda s: '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'
        try:
            subprocess.run(["osascript", "-e", f"display notification {q(body)} with title {q(title)}"],
                           timeout=10, check=False)
            return True
        except Exception as e:
            log.info("notification failed (%s): %s | %s", e, title, body)
            return False
    if sys.platform != "win32":
        log.info("notify: %s | %s", title, body)
        return False
    try:
        from win11toast import notify as _n
        kw = {"on_click": url} if url else {}
        _n(title, body, app_id="Lock & Land", **kw)
        return True
    except Exception as e:
        log.info("toast failed (%s): %s | %s", e, title, body)
        return False


TG_API = "https://api.telegram.org"
TG_STATUS = "telegram_status.json"
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")


def clean_token(value: str) -> str:
    """What people paste: 'bot123:ABC', a whole BotFather line, quotes. Keep just the 123:ABC part."""
    v = (value or "").strip().strip('"\'')
    m = _TOKEN_RE.search(v)
    return m.group(0) if m else v


def _redact(text: str, token: str = "") -> str:
    """Never let the bot token reach the log or the page: requests puts the whole URL (/bot<token>/...) in its
    error messages."""
    s = str(text)
    if token:
        s = s.replace(token, "<token>")
    return _TOKEN_RE.sub("<token>", s)


def _token() -> str:
    return clean_token(env("TELEGRAM_BOT_TOKEN"))


def tg_settings(cfg: dict) -> dict:
    return (cfg.get("notify") or {}).get("telegram") or {}


def _call(token: str, method: str, **params) -> tuple[bool, object]:
    """One Bot API call. Returns (True, result) or (False, a plain-English reason). Never raises, never logs the
    token. Uses the system/HTTPS_PROXY proxy like every other request the app makes."""
    if not token:
        return False, "No bot token saved yet."
    try:
        r = requests.post(f"{TG_API}/bot{token}/{method}", json=params or None, timeout=15)
    except requests.exceptions.ProxyError as e:
        return False, "The proxy refused the connection to api.telegram.org: " + _redact(e, token)[:200]
    except requests.exceptions.SSLError as e:
        return False, ("Secure connection to Telegram failed (antivirus or company network inspecting HTTPS?): "
                       + _redact(e, token)[:200])
    except requests.exceptions.RequestException as e:
        return False, ("Could not reach api.telegram.org. Check the internet connection, or whether a firewall, "
                       "VPN or company network blocks Telegram. (" + type(e).__name__ + ")")
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.ok and d.get("ok"):
        return True, d.get("result")
    desc = _redact(d.get("description") or r.reason or "", token)
    if r.status_code in (401, 404):
        return False, "Telegram does not recognise this bot token. Copy it again from @BotFather and save it."
    if r.status_code == 409:
        return False, ("This bot is set up with a webhook, so Lock & Land can't see your messages. Use a new bot "
                       "from @BotFather, or type your chat id by hand.")
    if r.status_code == 403:
        return False, "The bot can't message you: press Start in the bot's chat (or unblock it) and try again."
    if r.status_code == 400 and "chat not found" in desc.lower():
        return False, "Chat not found: open the bot in Telegram, press Start, then press Find my chat again."
    if r.status_code == 429:
        return False, "Telegram says too many messages. Try again in a minute."
    return False, f"Telegram said: {desc or r.status_code}"


def _record(ok: bool, detail: str) -> None:
    try:
        from .config import DATA
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / TG_STATUS).write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "ok": ok,
                                                  "detail": detail}), encoding="utf-8")
    except Exception:
        pass


def last_status() -> dict:
    try:
        from .config import DATA
        return json.loads((DATA / TG_STATUS).read_text(encoding="utf-8"))
    except Exception:
        return {}


def telegram(cfg: dict, text: str, force: bool = False) -> bool:
    """Send one alert. Off unless a token is saved, a chat is connected and alerts are on (force = test button)."""
    t = tg_settings(cfg)
    token, chat = _token(), str(t.get("chat_id") or "").strip()
    if not (token and chat):
        if t.get("enabled"):
            log.info("telegram: on, but %s", "no bot token saved" if not token else "no chat connected yet")
        return False
    if not (t.get("enabled") or force):
        return False
    ok, res = _call(token, "sendMessage", chat_id=chat, text=text[:1000], disable_web_page_preview=True)
    if ok:
        log.info("telegram: message sent")
    else:
        log.warning("telegram: not sent: %s", res)
    _record(ok, "Sent" if ok else str(res))
    return ok


def telegram_bot(token: str | None = None) -> dict:
    """Check the token (getMe). {ok, username, name} or {ok: False, detail}."""
    ok, res = _call(clean_token(token) if token else _token(), "getMe")
    if not ok:
        return {"ok": False, "detail": res}
    return {"ok": True, "username": res.get("username", ""), "name": res.get("first_name", ""),
            "detail": f"Token works: your bot is @{res.get('username', '')}"}


def telegram_find_chat() -> dict:
    """The chat of whoever last wrote to the bot (they press Start). The Bot API keeps messages for 24 h."""
    token = _token()
    bot = telegram_bot(token)
    if not bot["ok"]:
        return bot
    ok, res = _call(token, "getUpdates", timeout=0, allowed_updates=["message", "my_chat_member"])
    if not ok:
        return {"ok": False, "detail": res, "bot": bot["username"]}
    for u in reversed(res or []):
        m = u.get("message") or u.get("my_chat_member") or {}
        chat = m.get("chat") or {}
        if chat.get("id") is not None and chat.get("type") == "private":
            who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
            return {"ok": True, "chat_id": str(chat["id"]), "who": who, "bot": bot["username"]}
    return {"ok": False, "bot": bot["username"],
            "detail": f"No message found yet. In Telegram open @{bot['username']}, press Start (or send hi), "
                      "then press Find my chat again within a few minutes."}


def review_url(cfg: dict, path: str = "/") -> str:
    r = cfg.get("review") or {}
    return f"http://{r.get('public_host') or r.get('host', '127.0.0.1')}:{r.get('port', 8502)}{path}"


def send(cfg: dict, title: str, body: str, path: str = "/") -> None:
    """The desktop app has no web address (no server), so the toast just tells you; open the app from the tray.
    A link goes into Telegram only if you run the review page yourself and set review.public_host."""
    toast(title, body)
    url = review_url(cfg, path) if (cfg.get("review") or {}).get("public_host") else ""
    telegram(cfg, f"{title}\n{body}" + (f"\n{url}" if url else ""))
