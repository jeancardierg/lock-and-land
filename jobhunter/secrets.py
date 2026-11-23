"""Secrets in the OS keychain: Windows Credential Manager, macOS Keychain (Secret Service on Linux).

Nothing secret is written to config.yaml, .env or any other plain file by the desktop app. Stored here:
  ADZUNA_APP_ID / ADZUNA_APP_KEY   Adzuna developer keys
  GMAIL_CLIENT                     Gmail OAuth client JSON (the "credentials.json" from Google Cloud)
  GMAIL_TOKEN                      Gmail OAuth token JSON (refresh token), written after you sign in
  GMAIL_APP_PASSWORD               only for the IMAP backend
  TELEGRAM_BOT_TOKEN               optional notifications
  DB_KEY                           256-bit key for the encrypted database (see sqldb.py)

Windows Credential Manager caps one entry at 2560 bytes, so long values are split over numbered entries.
`import_legacy()` moves an old .env / token.json / credentials.json into the keychain and wipes the files.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)
SERVICE = "jobhunter"
CHUNK = 900  # characters per entry; Windows stores UTF-16, 900 chars stays well under 2560 bytes
KNOWN = ["ADZUNA_APP_ID", "ADZUNA_APP_KEY", "GMAIL_CLIENT", "GMAIL_TOKEN", "GMAIL_APP_PASSWORD",
         "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "DB_KEY", "JOOBLE_API_KEY",
         "CAREERJET_API_KEY", "FINDWORK_API_KEY", "MUSE_API_KEY"]
_cache: dict[str, str | None] = {}


class KeychainUnavailable(RuntimeError):
    pass


def _kr():
    import keyring
    from keyring.backends import fail
    k = keyring.get_keyring()
    if isinstance(k, fail.Keyring):
        raise KeychainUnavailable("No OS keychain available (on Linux install gnome-keyring or KWallet).")
    return keyring


def backend_name() -> str:
    try:
        k = _kr().get_keyring()
        return f"{type(k).__module__}.{type(k).__name__}"
    except Exception as e:
        return f"unavailable: {e}"


def get(name: str) -> str | None:
    if name in _cache:
        return _cache[name]
    try:
        kr = _kr()
        head = kr.get_password(SERVICE, name)
    except KeychainUnavailable:
        return None
    if head and head.startswith("chunks:"):
        n = int(head.split(":", 1)[1])
        parts = [kr.get_password(SERVICE, f"{name}#{i}") or "" for i in range(n)]
        head = "".join(parts)
    _cache[name] = head
    return head


def set(name: str, value: str) -> None:  # noqa: A001 - mirrors keyring's API
    kr = _kr()
    delete(name)
    value = value or ""
    if len(value) <= CHUNK:
        kr.set_password(SERVICE, name, value)
    else:
        parts = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)]
        for i, p in enumerate(parts):
            kr.set_password(SERVICE, f"{name}#{i}", p)
        kr.set_password(SERVICE, name, f"chunks:{len(parts)}")
    _cache[name] = value


def delete(name: str) -> None:
    _cache.pop(name, None)
    try:
        kr = _kr()
    except KeychainUnavailable:
        return
    from keyring.errors import PasswordDeleteError
    head = kr.get_password(SERVICE, name)
    if head and head.startswith("chunks:"):
        for i in range(int(head.split(":", 1)[1])):
            try:
                kr.delete_password(SERVICE, f"{name}#{i}")
            except PasswordDeleteError:
                pass
    if head is not None:
        try:
            kr.delete_password(SERVICE, name)
        except PasswordDeleteError:
            pass


def has(name: str) -> bool:
    return bool(get(name))


def clear_cache() -> None:
    _cache.clear()


# ---------------- keeping secrets out of logs ----------------

_KEY_IN_URL = re.compile(r"(?i)([?&](?:app_key|api_key|apikey|key|token|access_token|client_secret)=)[^&\s'\"]+")


def redact(text) -> str:
    """Hide secret values in text bound for a log file or the screen. API keys travel in URLs (Jooble puts the key
    in the path, Adzuna and The Muse in the query), and requests' errors quote the full URL."""
    text = str(text)
    values = [v for v in _cache.values() if v and len(v) >= 6]
    values += [os.environ[n] for n in KNOWN if len(os.environ.get(n) or "") >= 6]
    for v in sorted(dict.fromkeys(values), key=len, reverse=True):  # (set() is this module's own function)
        text = text.replace(v, "[redacted]")
    return _KEY_IN_URL.sub(r"\1[redacted]", text)


class RedactFilter:
    """logging filter: run every record's final message through redact()."""

    def filter(self, record) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg)
        if record.exc_info and not record.exc_text:
            import logging
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        return True


def log_handler(path) -> "object":
    """The app's log file: secrets redacted, rotated at 5 MB (3 old files kept) so it can't grow forever."""
    from logging.handlers import RotatingFileHandler
    h = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    h.addFilter(RedactFilter())
    return h


# ---------------- legacy import ----------------

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = ENV_LINE.match(line)
        if m:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if v:
                out[m.group(1)] = v
    return out


def wipe(path: Path) -> None:
    """Overwrite a small plain-text secret file with zeros, then delete it. On SSDs the old blocks may
    survive wear levelling; BitLocker/FileVault is what really protects them."""
    try:
        size = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(b"\0" * size)
            f.flush()
            os.fsync(f.fileno())
        path.unlink()
    except FileNotFoundError:
        pass
    except PermissionError:
        log.warning("could not wipe %s (no delete permission); remove it by hand", path)


def import_legacy(env_file: Path | None = None, token_file: Path | None = None, creds_file: Path | None = None,
                  wipe_files: bool = True) -> dict:
    """Move secrets from plain files into the keychain. Only keys with a value are imported; an existing keychain
    value is never overwritten by an older file. Returns what was imported (names only, never values)."""
    done: dict[str, list[str]] = {"imported": [], "kept_existing": [], "wiped": []}

    def put(name: str, value: str):
        if has(name):
            done["kept_existing"].append(name)
        else:
            set(name, value)
            done["imported"].append(name)

    if env_file and env_file.exists():
        for k, v in parse_env(env_file.read_text(encoding="utf-8", errors="replace")).items():
            put(k, v)
    if creds_file and creds_file.exists():
        put("GMAIL_CLIENT", creds_file.read_text(encoding="utf-8"))
    if token_file and token_file.exists():
        put("GMAIL_TOKEN", token_file.read_text(encoding="utf-8"))
    if wipe_files:
        for f in (env_file, token_file, creds_file):
            if f and f.exists():
                wipe(f)
                if not f.exists():
                    done["wiped"].append(str(f))
    return done
