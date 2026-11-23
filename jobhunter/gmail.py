"""Gmail access via OAuth (desktop app). Read mail and send approved messages.

Setup: Google Cloud console -> create project -> enable Gmail API -> OAuth consent (External) -> Credentials ->
OAuth client ID -> Desktop app -> download the JSON. In the app: Configuration -> Gmail -> load that JSON, then
Sign in. Both the client JSON and the token are kept in the OS keychain (GMAIL_CLIENT, GMAIL_TOKEN), never in files.

Scopes are read + send only (no modify/delete). Background runs never open a browser for consent:
if the token is missing or revoked they fail with a clear message instead of hanging.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import threading
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

from . import secrets
from .config import attachment_problem

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]
_service = None
_lock = threading.Lock()
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NOT_SIGNED_IN = "Gmail not signed in. Open Configuration and press Sign in (or run: jobhunter gmail-auth)"
EXPIRED = "Gmail sign-in expired or revoked. Open Configuration and press Sign in again (or run: jobhunter gmail-auth)"


class SendBlocked(ValueError):
    """Raised when a send would be unsafe (bad recipient, missing CV, ...)."""


def client_config() -> dict | None:
    raw = secrets.get("GMAIL_CLIENT")
    if not raw:
        return None
    cfg = json.loads(raw)
    if not ({"installed", "web"} & set(cfg)):
        raise ValueError("Not a Google OAuth client file (expected an 'installed' Desktop app client)")
    return cfg


def save_client(raw: str) -> None:
    cfg = json.loads(raw)
    if "installed" not in cfg:
        raise ValueError("Use a Desktop app OAuth client (the JSON must have an 'installed' section)")
    secrets.set("GMAIL_CLIENT", json.dumps(cfg))


def _load_creds():
    from google.oauth2.credentials import Credentials
    raw = secrets.get("GMAIL_TOKEN")
    if not raw:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    return creds if creds.has_scopes(SCOPES) else None  # old token with other scopes -> re-consent


def _save_creds(creds) -> None:
    secrets.set("GMAIL_TOKEN", creds.to_json())
    from .status import mark_gmail_auth
    mark_gmail_auth()


def sign_out() -> None:
    global _service
    secrets.delete("GMAIL_TOKEN")
    _service = None


def sign_in(open_browser: bool = True, timeout_s: int = 600):
    """Google's consent page in the default browser (loopback redirect on a random local port)."""
    global _service
    cc = client_config()
    if not cc:
        raise FileNotFoundError("Gmail OAuth client missing: load the Desktop app client JSON on the Configuration page")
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = InstalledAppFlow.from_client_config(cc, SCOPES).run_local_server(
        port=0, open_browser=open_browser, timeout_seconds=timeout_s,
        authorization_prompt_message="Opening Google sign-in in your browser. If it doesn't open, visit: {url}",
        success_message="jobhunter is signed in to Gmail. You can close this tab.")
    _save_creds(creds)
    _service = None
    return creds


def service(interactive: bool = False, force: bool = False):
    """force=True ignores the saved token and always shows Google's consent page (the Sign in again button)."""
    global _service
    with _lock:
        if _service and not force:
            return _service
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = None if force else _load_creds()
        if creds and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    secrets.set("GMAIL_TOKEN", creds.to_json())  # refreshed access token; sign-in date unchanged
                except RefreshError as e:
                    if not interactive:
                        raise RuntimeError(EXPIRED) from e
                    creds = None
            else:
                creds = None
        if not creds or not creds.valid:
            if not interactive:
                raise RuntimeError(NOT_SIGNED_IN)
            creds = sign_in()
        _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return _service


def check() -> dict:
    """{'state': 'no_client'|'missing'|'expired'|'ok'|'error', 'email': str|None, 'detail': str}.
    Uses the network (one token refresh + getProfile); never opens a browser."""
    global _service
    try:
        if not client_config():
            return {"state": "no_client", "email": None, "detail": "Load the Desktop app OAuth client JSON"}
    except Exception as e:
        return {"state": "no_client", "email": None, "detail": str(e)}
    if not secrets.get("GMAIL_TOKEN"):
        return {"state": "missing", "email": None, "detail": "Not signed in"}
    try:
        _service = None
        prof = service().users().getProfile(userId="me").execute()
        return {"state": "ok", "email": prof.get("emailAddress"), "detail": ""}
    except RuntimeError as e:
        return {"state": "expired" if "expired" in str(e) else "missing", "email": None, "detail": str(e)}
    except Exception as e:  # network down etc.: don't call it expired
        return {"state": "error", "email": None, "detail": f"{type(e).__name__}: {e}"}


def _b64d(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", errors="replace")


def extract_bodies(payload: dict) -> tuple[str, str]:
    """Return (plain, html) from a Gmail message payload."""
    plain, html = "", ""
    stack = [payload]
    while stack:
        p = stack.pop(0)
        mt, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
        if data and mt == "text/plain" and not plain:
            plain = _b64d(data)
        elif data and mt == "text/html" and not html:
            html = _b64d(data)
        stack.extend(p.get("parts", []) or [])
    return plain, html


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def backend() -> tuple[str, str]:
    """('gmail_api' | 'imap', mailbox address) from config.yaml `mail.backend` (default gmail_api)."""
    from .config import load_config
    cfg = load_config()
    return (cfg.get("mail") or {}).get("backend", "gmail_api"), cfg["profile"].get("email") or ""


def search(query: str, max_results: int = 100) -> Iterator[dict]:
    kind, user = backend()
    if kind == "imap":
        from . import mailimap
        yield from mailimap.search(user, query, max_results)
        return
    svc = service()
    req = svc.users().messages().list(userId="me", q=query, maxResults=min(max_results, 100))
    n = 0
    while req is not None and n < max_results:
        resp = req.execute()
        for m in resp.get("messages", []):
            yield svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
            n += 1
            if n >= max_results:
                return
        req = svc.users().messages().list_next(req, resp)


def message_meta(msg: dict) -> dict[str, Any]:
    if "_meta" in msg:  # IMAP backend already parsed it
        return msg["_meta"]
    name, addr = parseaddr(header(msg, "From"))
    try:
        received = parsedate_to_datetime(header(msg, "Date")).isoformat()
    except Exception:
        received = ""
    plain, html = extract_bodies(msg.get("payload", {}))
    return {
        "gmail_id": msg["id"], "thread_id": msg.get("threadId"), "sender_name": name, "sender_email": addr.lower(),
        "subject": header(msg, "Subject"), "message_id": header(msg, "Message-ID"),
        "received_at": received, "plain": plain, "html": html,
    }


def single_recipient(value: str) -> str:
    """Return exactly one clean address from a header/field value, or raise SendBlocked."""
    addrs = [a for _, a in getaddresses([value or ""]) if a]
    if len(addrs) != 1:
        raise SendBlocked(f"Expected exactly one recipient, got {len(addrs)}: {value!r}")
    a = addrs[0].strip()
    if not EMAIL_RE.match(a):
        raise SendBlocked(f"Not a valid email address: {a!r}")
    return a


def _build(to: str, subject: str, body: str, sender: str, in_reply_to: str = "",
           attachment: Path | None = None) -> EmailMessage:
    if not body.strip():
        raise SendBlocked("Empty message body")
    if attachment is not None:
        problem = attachment_problem(attachment)
        if problem:
            raise SendBlocked(f"{problem} Not sending an application without a ready CV.")
    m = EmailMessage()
    m["To"], m["From"] = single_recipient(to), single_recipient(sender)
    m["Subject"] = " ".join((subject or "").split())  # no CR/LF in headers
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(body)
    if attachment is not None:
        ctype, _ = mimetypes.guess_type(attachment.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        m.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=attachment.name)
    return m


def send(to: str, subject: str, body: str, sender: str, thread_id: str | None = None,
         in_reply_to: str = "", attachment: Path | None = None) -> str:
    """Send one message. Only called from the dashboard after an explicit human approval."""
    m = _build(to, subject, body, sender, in_reply_to, attachment)
    kind, user = backend()
    if kind == "imap":
        from email.utils import make_msgid
        from . import mailimap
        m["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
        return mailimap.send(user, m)
    raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return service().users().messages().send(userId="me", body=payload).execute()["id"]


def reply_headers(gmail_id: str) -> dict[str, str]:
    """Message-ID and Reply-To of a received message, from whichever backend is configured."""
    kind, user = backend()
    if kind == "imap":
        from . import mailimap
        return mailimap.headers_for(user, gmail_id)
    full = service().users().messages().get(userId="me", id=gmail_id, format="metadata",
                                            metadataHeaders=["Message-ID", "Reply-To"]).execute()
    return {"Message-ID": header(full, "Message-ID"), "Reply-To": header(full, "Reply-To")}


def reply_subject(subject: str) -> str:
    return subject if re.match(r"(?i)^re:", subject or "") else f"Re: {subject}"
