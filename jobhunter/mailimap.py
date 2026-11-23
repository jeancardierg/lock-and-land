"""Gmail over IMAP + SMTP with an app password: no OAuth app, no 7-day token expiry.

Setup (once): Google Account -> 2-Step Verification on -> App passwords (myaccount.google.com/apppasswords) ->
create "Lock and Land", paste the 16 letters on the Configuration page (Email -> App password). That saves it in the
keychain as GMAIL_APP_PASSWORD and sets `mail.backend: imap` in config.yaml. This is the easy path for testers: no
Google Cloud project, nobody has to add them as a test user, and it doesn't expire every 7 days.
Reading uses Gmail's X-GM-RAW search, so the same Gmail search syntax works. Mailboxes are opened read-only.
"""
from __future__ import annotations

import email
import imaplib
import re
import smtplib
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Iterator

from .config import env

IMAP_HOST, SMTP_HOST = "imap.gmail.com", "smtp.gmail.com"
ALL_MAIL = '"[Gmail]/All Mail"'


APP_PW_LINK = "https://myaccount.google.com/apppasswords"
TWO_STEP_LINK = "https://myaccount.google.com/signinoptions/twosv"


def clean_password(pw: str) -> str:
    """Google shows app passwords as four groups of four letters; people paste them with spaces or dashes."""
    return re.sub(r"[\s\-]", "", pw or "")


def explain(err: Exception) -> str:
    """A login error in words a non-technical person can act on."""
    t = str(err)
    low = t.lower()
    if "application-specific password required" in low or "app password" in low:
        return ("That looks like your normal Google password. Lock & Land needs an app password instead: "
                "follow step 2 to make one, then paste the 16 letters here.")
    if "authenticationfailed" in low or "invalid credentials" in low or "username and password not accepted" in low:
        return ("Google didn't accept that email and app password. Check the email is your Gmail address, "
                "copy all 16 letters again, or delete the app password at myaccount.google.com/apppasswords "
                "and make a new one.")
    if "web login required" in low or "please log in via your web browser" in low:
        return ("Google wants you to sign in once in your web browser first (it noticed a new device). "
                "Open gmail.com, sign in, then press Save and test again.")
    if isinstance(err, (TimeoutError, OSError)) and not isinstance(err, imaplib.IMAP4.error):
        return "Couldn't reach Gmail. Check the internet connection and try again."
    return f"Gmail said: {t}"


def test_login(user: str, password: str) -> None:
    """Log in once and log out (raises with Google's message on failure). Opens nothing, reads nothing."""
    m = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=30)
    try:
        m.login(user, clean_password(password))
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _creds(user: str) -> tuple[str, str]:
    pw = clean_password(env("GMAIL_APP_PASSWORD"))
    if not pw:
        raise RuntimeError("Gmail app password missing: add it on the Configuration page (Email)")
    return user, pw


def _connect(user: str) -> imaplib.IMAP4_SSL:
    u, pw = _creds(user)
    m = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=60)
    m.login(u, pw)
    # All Mail has a localised name on some accounts; find it by its \All flag
    typ, boxes = m.list()
    box = ALL_MAIL
    for b in boxes or []:
        line = b.decode(errors="replace")
        if "\\All" in line:
            box = line.split(' "/" ')[-1]
            break
    m.select(box, readonly=True)
    return m


def _dec(v: str | None) -> str:
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v


def _bodies(msg: email.message.Message) -> tuple[str, str]:
    plain, html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ct = part.get_content_type()
        try:
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ct == "text/plain" and not plain:
            plain = text
        elif ct == "text/html" and not html:
            html = text
    return plain, html


def meta_from_raw(raw: bytes, gm_msgid: str, gm_thrid: str) -> dict[str, Any]:
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_dec(msg.get("From")))
    try:
        received = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        received = ""
    plain, html = _bodies(msg)
    return {"gmail_id": gm_msgid, "thread_id": gm_thrid, "sender_name": name, "sender_email": addr.lower(),
            "subject": _dec(msg.get("Subject")), "message_id": msg.get("Message-ID", ""),
            "reply_to": _dec(msg.get("Reply-To")), "received_at": received, "plain": plain, "html": html}


_FETCH_RE = re.compile(rb"X-GM-MSGID (\d+).*?X-GM-THRID (\d+)|X-GM-THRID (\d+).*?X-GM-MSGID (\d+)")


def search(user: str, query: str, max_results: int = 100) -> Iterator[dict]:
    m = _connect(user)
    try:
        typ, data = m.uid("SEARCH", "X-GM-RAW", f'"{query.replace(chr(34), chr(39))}"')
        uids = (data[0] or b"").split()[::-1][:max_results]  # newest first
        for uid in uids:
            typ, parts = m.uid("FETCH", uid, "(X-GM-MSGID X-GM-THRID BODY.PEEK[])")
            if not parts or not isinstance(parts[0], tuple):
                continue
            head, raw = parts[0]
            mm = _FETCH_RE.search(head)
            msgid = (mm.group(1) or mm.group(4)).decode() if mm else uid.decode()
            thrid = (mm.group(2) or mm.group(3)).decode() if mm else ""
            yield {"_meta": meta_from_raw(raw, msgid, thrid)}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def headers_for(user: str, gm_msgid: str) -> dict[str, str]:
    """Reply-To and Message-ID for one message, looked up by its Gmail id (used right before a reply)."""
    m = _connect(user)
    try:
        typ, data = m.uid("SEARCH", "X-GM-MSGID", gm_msgid)
        uids = (data[0] or b"").split()
        if not uids:
            raise RuntimeError(f"message {gm_msgid} not found")
        typ, parts = m.uid("FETCH", uids[0], "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REPLY-TO FROM)])")
        msg = email.message_from_bytes(parts[0][1])
        return {"Message-ID": msg.get("Message-ID", ""), "Reply-To": _dec(msg.get("Reply-To")),
                "From": _dec(msg.get("From"))}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def send(user: str, message: email.message.EmailMessage) -> str:
    u, pw = _creds(user)
    with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=60) as s:
        s.login(u, pw)
        s.send_message(message)
    return message.get("Message-ID", "") or "sent"
