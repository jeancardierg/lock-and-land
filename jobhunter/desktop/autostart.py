"""Start at login, per user, no admin rights: HKCU Run key (Windows), a LaunchAgent (macOS), XDG autostart (Linux)."""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

NAME = "jobhunter"
LABEL = "app.jobhunter.tray"


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--tray"]  # at login: tray only, the window opens when you click the icon
    return [sys.executable, "-m", "jobhunter.desktop", "--tray"]


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _xdg() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / f"{NAME}.desktop"


def enabled() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, NAME)
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _mac_plist().exists()
    return _xdg().exists()


def enable() -> None:
    cmd = _command()
    if sys.platform == "win32":
        import subprocess
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
    elif sys.platform == "darwin":
        p = _mac_plist()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            plistlib.dump({"Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True,
                           "ProcessType": "Interactive", "LimitLoadToSessionType": "Aqua"}, f)
    else:
        p = _xdg()
        p.parent.mkdir(parents=True, exist_ok=True)
        import shlex
        p.write_text(f"[Desktop Entry]\nType=Application\nName=jobhunter\nExec={shlex.join(cmd)}\n"
                     "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")


def disable() -> None:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, NAME)
        except OSError:
            pass
    elif sys.platform == "darwin":
        _mac_plist().unlink(missing_ok=True)
    else:
        _xdg().unlink(missing_ok=True)
