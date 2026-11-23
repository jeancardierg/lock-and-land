"""Passwords for job-site accounts (Workday, SuccessFactors, ...) saved by earlier versions of the app.

The app no longer makes or signs in to accounts: bot protection blocks automated sign-up, so you make accounts
yourself in your normal browser. Passwords an earlier version generated are kept so you don't lose access: the
Waiting tab lists them, and copying or forgetting one asks in an operating-system pop-up first.

Passwords live only in the OS keychain (secrets.py), one entry per site; the list of sites (no passwords) is in
the keychain too.
"""
from __future__ import annotations

import json

from . import secrets

INDEX = "ATS_ACCOUNTS"
PREFIX = "ATS_PW:"


def index() -> dict[str, dict]:
    raw = secrets.get(INDEX)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    if data:
        secrets.set(INDEX, json.dumps(data, sort_keys=True))
    else:
        secrets.delete(INDEX)


def get(site: str) -> dict | None:
    a = index().get(site)
    return {**a, "site": site} if a else None


def listing() -> list[dict]:
    return [{**v, "site": k} for k, v in sorted(index().items())]


def password(site: str) -> str | None:
    return secrets.get(PREFIX + site) if site else None


def forget(site: str) -> bool:
    data = index()
    known = site in data or bool(password(site))
    secrets.delete(PREFIX + site)
    data.pop(site, None)
    _save(data)
    return known
