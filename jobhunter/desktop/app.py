"""jobhunter desktop app: one process with a tray icon, the review page and the background queues.

    jobhunter.exe                 start the tray app (what the Start menu / login item runs)
    jobhunter.exe <command> ...   any CLI command, e.g. `jobhunter.exe doctor`, `jobhunter.exe gmail-auth`

Replaces the PowerShell scheduled tasks: fetching every 3 h, letters every 2 min, and recruiter mail every 30 min all
run on the built-in Huey scheduler while the app is open. After sleep or a late login, a fetch
that is overdue runs a minute after start. Nothing is ever sent or submitted without you.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from jobhunter.branding import APP_NAME

log = logging.getLogger("jobhunter.desktop")


def _cli(argv: list[str]) -> int:
    from jobhunter.__main__ import main as cli
    return cli(argv)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    argv = [a for a in argv if not a.startswith("-psn_")]  # macOS Finder adds a process serial number
    if argv and argv[0] == "--window" and len(argv) >= 3:  # the app window (started by the tray)
        from jobhunter.desktop import window
        return window.run(argv[1], argv[2])
    if argv and argv[0] == "--window-selftest":  # opens a real window on a throwaway data folder
        if not os.environ.get("JOBHUNTER_HOME"):
            import tempfile
            os.environ["JOBHUNTER_HOME"] = tempfile.mkdtemp(prefix="lockandland-selftest-")
        from jobhunter.desktop import window
        return window.selftest(argv[1] if len(argv) > 1 else None)
    if argv and argv[0] == "--window-check":
        from jobhunter.desktop import window
        return window.check()
    if argv and argv[0] not in ("--tray", "--restart"):
        return _cli(argv)
    if argv and argv[0] == "--restart":
        time.sleep(1.5)  # let the old instance release the port and its files
    from jobhunter.desktop import move
    if not os.environ.get("JOBHUNTER_HOME"):
        move.run_pending()  # a data-folder change requested on the Configuration page (before HOME is fixed)
    return Tray(restarting=bool(argv and argv[0] == "--restart"), quiet=bool(argv and argv[0] == "--tray")).run()


def _show_running(data_dir: Path) -> bool:
    """A second start: ask the running app (over its private channel) to show its window."""
    from jobhunter.desktop import ipc
    try:
        return bool(ipc.call(data_dir, {"op": "show", "path": "/"}).get("ok"))
    except Exception:
        return False


def _fatal(msg: str) -> None:
    """Show a blocking error without a console (Windows message box / macOS dialog)."""
    log.error(msg)
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["osascript", "-e", f'display alert "{APP_NAME}" message {msg!r}'], timeout=60)
        else:
            print(msg, file=sys.stderr)
    except Exception:
        print(msg, file=sys.stderr)


class Tray:
    def __init__(self, restarting: bool = False, quiet: bool = False):
        self.restarting = restarting
        self.quiet = quiet  # started at login: no window unless something needs you
        self.consumers: list = []
        self.server = None
        self.icon = None
        self.win = None
        self.stop_evt = threading.Event()

    # ---------- setup ----------
    def _logging(self) -> None:
        from jobhunter.config import DATA
        from jobhunter.secrets import log_handler
        DATA.mkdir(parents=True, exist_ok=True)
        if sys.stdout is None or sys.stderr is None:  # windowed build: no console
            con = open(DATA / "console.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or con
            sys.stderr = sys.stderr or con
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            handlers=[log_handler(DATA / "jobhunter.log")])
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def run(self) -> int:
        os.environ["JOBHUNTER_DESKTOP"] = "1"
        from jobhunter import connections, secrets, sqldb
        from jobhunter.config import FROZEN, HOME, load_config
        connections.ensure_home()
        self._logging()
        log.info("starting jobhunter desktop, data folder %s", HOME)
        try:  # a backup restore asked for on the Configuration page, before anything opens the database
            from jobhunter import backup
            backup.run_pending_restore()
        except Exception as e:
            log.exception("restore failed: %s", e)
        if FROZEN:
            try:
                sqldb.require()
            except RuntimeError as e:
                _fatal(str(e))
                return 2
        if secrets.backend_name().startswith("unavailable"):
            _fatal("No OS keychain is available, so Lock & Land can't store sign-ins safely.\n" + secrets.backend_name())
            return 2
        cfg = load_config()
        from jobhunter.config import DATA
        from jobhunter.desktop import ipc
        for _ in range(40 if self.restarting else 0):  # restart: wait for the old instance to stop
            if not ipc.running(DATA):
                break
            time.sleep(0.5)
        if ipc.running(DATA):  # already running: ask it to show its window
            _show_running(DATA)
            return 0
        first_run = not (HOME / "data" / "jobhunter.db").exists()
        from jobhunter.desktop.window import WindowManager
        self.win = WindowManager(DATA, enabled=(cfg.get("desktop") or {}).get("window", True) is not False)
        connections.RESTART_HOOK = self.restart
        connections.WINDOW_HOOK = self.win.open
        try:
            self._start_server()
        except OSError as e:
            _fatal(f"{APP_NAME} couldn't open its private window channel ({e}). Is it already running?")
            return 3
        self._start_workers()
        threading.Thread(target=self._heartbeat, name="heartbeat", daemon=True).start()
        threading.Thread(target=self._catch_up, name="catch-up", daemon=True).start()
        threading.Thread(target=self._watch, name="watch", daemon=True).start()
        from jobhunter import criteria
        if not criteria.done():
            threading.Timer(1.0, lambda: self.win.open("/setup")).start()  # ask for job criteria once
        elif first_run or connections.needs_attention() or self.restarting:
            threading.Timer(1.0, lambda: self.win.open("/connections")).start()
        elif not self.quiet:
            threading.Timer(1.0, lambda: self.win.open("/")).start()
        return self._tray()

    def _start_server(self) -> None:
        """No web server: the window reaches the pages through a private pipe/socket (desktop/ipc.py)."""
        from jobhunter.config import DATA
        from jobhunter.desktop import ipc
        from jobhunter.review.app import app
        self.server = ipc.Server(DATA, app, on_show=self.win.open)

    def _start_workers(self) -> None:
        from jobhunter.__main__ import start_workers
        try:
            self.consumers = start_workers()
        except Exception as e:
            log.exception("workers failed to start")
            _fatal(f"Background workers failed to start: {e}")

    def _heartbeat(self) -> None:
        from jobhunter.__main__ import beat
        while not self.stop_evt.is_set():
            try:
                beat()
            except OSError:
                pass
            self.stop_evt.wait(5)

    def _catch_up(self) -> None:
        """Run a fetch soon after start if the last one is older than the schedule (PC was off or asleep)."""
        from datetime import datetime, timedelta, timezone
        if self.stop_evt.wait(60):
            return
        try:
            from jobhunter import tasks
            from jobhunter.db import DB
            runs = DB().last_runs(1)
            last = datetime.fromisoformat(runs[0]["finished_at"]) if runs else None
            if last is None or datetime.now(timezone.utc) - last > timedelta(hours=3):
                log.info("catch-up fetch (last run %s)", last)
                tasks.ingest_all()
        except Exception as e:
            log.warning("catch-up: %s", e)

    def _watch(self) -> None:
        """Keep the tray icon colour current: amber = a sign-in needs you, grey = paused."""
        from jobhunter import connections
        while not self.stop_evt.is_set():
            try:
                need = connections.needs_attention()
                self._set_state(need)
            except Exception as e:
                log.debug("watch: %s", e)
            self.stop_evt.wait(120)

    # ---------- tray ----------
    def _state(self, need: list[str] | None = None) -> str:
        from jobhunter.tasks import paused
        if paused():
            return "paused"
        return "attention" if need else "ok"

    def _set_state(self, need: list[str]) -> None:
        if not self.icon:
            return
        from jobhunter.desktop.icon import image
        st = self._state(need)
        self.icon.icon = image(64, st)
        self.icon.title = {"ok": APP_NAME, "paused": f"{APP_NAME} (paused)",
                           "attention": f"{APP_NAME}: sign-in needed (" + ", ".join(need) + ")"}[st]

    def _tray(self) -> int:
        try:
            import pystray
        except Exception as e:  # no tray support (e.g. headless Linux): keep running until Ctrl+C
            log.warning("no tray (%s); running headless", e)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                self.quit()
            return 0
        from jobhunter.desktop import autostart
        from jobhunter.desktop.icon import image
        from jobhunter.tasks import PAUSE, paused

        def open_url(path: str):
            return lambda *_: self.win.open(path)

        def fetch_now(*_):
            from jobhunter import tasks
            tasks.ingest_all()  # enqueue on the main queue
            self._notify("Fetching jobs now", "New jobs appear in the review page as they are scored.")

        def toggle_pause(*_):
            if paused():
                PAUSE.unlink(missing_ok=True)
            else:
                PAUSE.write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
            self._set_state([])

        def toggle_login(*_):
            autostart.disable() if autostart.enabled() else autostart.enable()

        def open_folder(*_):
            from jobhunter.config import HOME
            _open_path(HOME)

        def open_log(*_):
            from jobhunter.config import DATA
            _open_path(DATA / "jobhunter.log")

        menu = pystray.Menu(
            pystray.MenuItem(f"Open {APP_NAME}", open_url("/"), default=True),
            pystray.MenuItem("Configuration and sign-in", open_url("/connections")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Fetch jobs now", fetch_now),
            pystray.MenuItem("Pause background work", toggle_pause, checked=lambda _: paused()),
            pystray.MenuItem("Start at login", toggle_login, checked=lambda _: autostart.enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open data folder", open_folder),
            pystray.MenuItem("Open log", open_log),
            pystray.MenuItem(f"Quit {APP_NAME}", lambda *_: self.quit()),
        )
        self.icon = pystray.Icon("jobhunter", image(64, self._state()), APP_NAME, menu)
        self.icon.run()
        return 0

    def _notify(self, title: str, body: str) -> None:
        from jobhunter import notify
        notify.toast(title, body)

    def restart(self) -> None:
        """Start a fresh instance (it waits for this one to exit), then quit."""
        import subprocess
        from jobhunter.config import FROZEN
        cmd = [sys.executable, "--restart"] if FROZEN else [sys.executable, "-m", "jobhunter.desktop", "--restart"]
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
        threading.Timer(0.5, self.quit).start()

    def quit(self) -> None:
        log.info("quitting")
        self.stop_evt.set()
        from jobhunter.__main__ import stop_workers
        stop_workers(self.consumers)
        if self.win:
            self.win.close()
        if self.server:
            self.server.close()
        if self.icon:
            self.icon.stop()


def _open_path(p: Path) -> None:
    import subprocess
    if sys.platform == "win32":
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


if __name__ == "__main__":
    sys.exit(main())
