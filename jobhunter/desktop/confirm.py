"""Native yes/no prompts for the app's most sensitive actions.

The window's pages can call every route through the bridge, and a JavaScript confirm() can be skipped by any script
that ends up in a page. These prompts come from the tray process as an operating-system dialog, which page script
cannot see or click. Used for: showing or restoring the recovery key, sending an email, moving the data folder.

Outside the desktop app (`jobhunter review`, tests) there is no tray: ask() returns True and the pages' own
confirmations stay the only check, as before.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

from jobhunter.branding import APP_NAME

log = logging.getLogger(__name__)


def active() -> bool:
    return os.environ.get("JOBHUNTER_DESKTOP") == "1" and sys.platform in ("win32", "darwin")


def ask(question: str, detail: str = "", timeout_s: int = 180) -> bool:
    """True only when the person presses Yes/Allow. Any error, timeout or close counts as No."""
    if not active():
        return True
    text = question + (f"\n\n{detail}" if detail else "")
    try:
        if sys.platform == "win32":
            import ctypes
            # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 (No is the default) | MB_SETFOREGROUND | MB_TOPMOST
            flags = 0x4 | 0x30 | 0x100 | 0x10000 | 0x40000
            return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags) == 6  # IDYES
        script = (f"display dialog {json.dumps(text)} with title {json.dumps(APP_NAME)} "
                  'buttons {"Cancel", "Allow"} default button "Cancel" cancel button "Cancel" with icon caution')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout_s)
        return r.returncode == 0 and "Allow" in r.stdout
    except Exception as e:
        log.warning("native confirmation failed (%s): treated as No", type(e).__name__)
        return False


def require(question: str, detail: str = "") -> None:
    """Raise HTTP 403 unless the person confirms in the native dialog."""
    if not ask(question, detail):
        from fastapi import HTTPException
        raise HTTPException(403, "Not confirmed on this computer. Nothing was changed.")
