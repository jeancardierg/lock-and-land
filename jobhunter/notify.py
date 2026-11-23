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
from datetime import datetime, timedelta

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


# ---------------- what to send, where, and how often ----------------

# key: (label shown on Configuration, desktop default, Telegram default)
EVENTS = {
    "strong_jobs": ("Strong new job matches", "instant", "instant"),
    "letters_ready": ("Letters ready to review", "instant", "instant"),
    "waiting": ("Applications waiting for you", "instant", "instant"),
    "recruiter": ("New recruiter messages", "instant", "instant"),
    "problems": ("Problems: sign-in expired, database check", "instant", "instant"),
    "daily_summary": ("Daily summary (counts of everything)", "off", "daily"),
}
MODES = ("instant", "hourly", "daily", "off")
CHANNELS = ("desktop", "telegram")
QUEUE = "notify_queue.json"
STATE = "notify_state.json"


def _hhmm(v, default: str) -> str:
    v = str(v or default).strip()
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", v)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else default


def prefs(cfg: dict) -> dict:
    """notify.events / quiet_hours / digest_time / strong_score from config.yaml, with defaults filled in."""
    n = cfg.get("notify") or {}
    ev = n.get("events") or {}
    events = {}
    for k, (label, d_desk, d_tg) in EVENTS.items():
        e = ev.get(k) if isinstance(ev.get(k), dict) else {}
        events[k] = {"label": label}
        for ch, d in (("desktop", d_desk), ("telegram", d_tg)):
            m = e.get(ch, d)
            m = "instant" if m is True else "off" if m is False else m
            if m not in MODES or (k == "daily_summary" and m not in ("daily", "off")):
                m = d
            events[k][ch] = m
    q = n.get("quiet_hours") if isinstance(n.get("quiet_hours"), dict) else {}
    th = (cfg.get("thresholds") or {}).get("draft_score", 70)
    try:
        strong = max(0, min(100, int(n.get("strong_score", th))))
    except (TypeError, ValueError):
        strong = th
    return {"events": events, "strong_score": strong, "digest_time": _hhmm(n.get("digest_time"), "09:00"),
            "quiet_hours": {"enabled": bool(q.get("enabled")), "start": _hhmm(q.get("start"), "22:00"),
                            "end": _hhmm(q.get("end"), "08:00")}}


def in_quiet(p: dict, at: datetime | None = None) -> bool:
    q = p["quiet_hours"]
    if not q["enabled"] or q["start"] == q["end"]:
        return False
    t = (at or datetime.now()).strftime("%H:%M")
    return q["start"] <= t < q["end"] if q["start"] < q["end"] else (t >= q["start"] or t < q["end"])


def _load(name: str, default):
    try:
        from .config import DATA
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(name: str, data) -> None:
    from .config import DATA
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DATA / name)


def state() -> dict:
    return _load(STATE, {})


def save_state(st: dict) -> None:
    _save(STATE, st)


def review_url(cfg: dict, path: str = "/") -> str:
    r = cfg.get("review") or {}
    return f"http://{r.get('public_host') or r.get('host', '127.0.0.1')}:{r.get('port', 8502)}{path}"


def _deliver(cfg: dict, channel: str, title: str, body: str, path: str = "/") -> bool:
    if channel == "desktop":
        return toast(title, body)
    url = review_url(cfg, path) if (cfg.get("review") or {}).get("public_host") else ""
    return telegram(cfg, f"{title}\n{body}" + (f"\n{url}" if url else ""))


def send(cfg: dict, title: str, body: str, path: str = "/", event: str | None = None) -> None:
    """Send now, or queue for the hourly batch / daily digest / end of quiet hours, per channel, following the
    Notifications settings. event=None (old callers) means instant on both channels."""
    p = prefs(cfg)
    modes = p["events"].get(event) if event else {"desktop": "instant", "telegram": "instant"}
    quiet = in_quiet(p)
    queued = []
    for ch in CHANNELS:
        m = (modes or {}).get(ch, "instant")
        if m == "off":
            continue
        if m == "instant" and not quiet:
            try:
                _deliver(cfg, ch, title, body, path)
            except Exception as e:
                log.warning("notify %s failed: %s", ch, _redact(e))
            continue
        queued.append({"event": event or "", "channel": ch, "mode": m, "title": title, "body": body,
                       "at": datetime.now().isoformat(timespec="seconds")})
    if queued:
        q = _load(QUEUE, [])
        q.extend(queued)
        _save(QUEUE, q[-500:])


def flush(cfg: dict, now: datetime | None = None, summary=None) -> dict:
    """Called every 5 minutes: outside quiet hours, send what is due as one message per channel.
    instant (held by quiet hours): now; hourly: once an hour; daily: at digest_time. summary() builds the daily
    summary text when it is due."""
    now = now or datetime.now()
    p = prefs(cfg)
    if in_quiet(p, now):
        return {"quiet": True}
    st = state()
    today = now.strftime("%Y-%m-%d")
    hourly_due = now.isoformat(timespec="seconds") >= st.get("next_hourly", "")
    daily_due = now.strftime("%H:%M") >= p["digest_time"] and st.get("digest_day") != today
    q = _load(QUEUE, [])
    if daily_due and summary is not None:
        text = None
        for ch in CHANNELS:
            if p["events"]["daily_summary"][ch] == "daily":
                text = text if text is not None else summary()
                if text:
                    q.append({"event": "daily_summary", "channel": ch, "mode": "daily",
                              "title": "Daily summary", "body": text, "at": now.isoformat(timespec="seconds")})
    due = [i for i in q if i["mode"] == "instant" or (i["mode"] == "hourly" and hourly_due)
           or (i["mode"] == "daily" and daily_due)]
    sent = {}
    for ch in CHANNELS:
        items = [i for i in due if i["channel"] == ch]
        if not items:
            continue
        if len(items) == 1:
            title, body = items[0]["title"], items[0]["body"]
        else:
            title = f"Lock & Land: {len(items)} updates"
            body = "\n".join(f"- {i['title'].replace('Lock & Land: ', '')}: {i['body']}" for i in items)
        try:
            _deliver(cfg, ch, title, body[:3500] if ch == "telegram" else body[:250])
        except Exception as e:
            log.warning("notify %s failed: %s", ch, _redact(e))
        sent[ch] = len(items)
    rest = [i for i in q if i not in due]
    if q or rest:
        _save(QUEUE, rest)
    if hourly_due:
        st["next_hourly"] = (now.replace(minute=0, second=0, microsecond=0)
                             + timedelta(hours=1)).isoformat(timespec="seconds")
    if daily_due:
        st["digest_day"] = today
    save_state(st)
    return {"sent": sent, "waiting": len(rest)}
