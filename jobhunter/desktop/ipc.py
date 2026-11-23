"""How the app window talks to the app, with no network port at all.

The tray process owns everything (database, queues, settings). The window process asks it for pages and data over a
private local channel: a named pipe on Windows, a Unix socket in your own temp folder on macOS. Neither is a TCP
port, so nothing listens on the network, not even on 127.0.0.1. A random key (data folder, readable only by you)
must be presented on every connection.

The tray answers each request by calling the review app (the same FastAPI routes as before) directly in-process.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import sys
import tempfile
import threading
from multiprocessing.connection import Client, Listener
from pathlib import Path

log = logging.getLogger("jobhunter.ipc")
FAMILY = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"


def _tag(data_dir: Path) -> str:
    return hashlib.sha256(str(Path(data_dir).resolve()).lower().encode()).hexdigest()[:12]


def address(data_dir: Path) -> str:
    if sys.platform == "win32":
        return rf"\\.\pipe\lockandland-{_tag(data_dir)}"
    return os.path.join(tempfile.gettempdir(), f"lockandland-{_tag(data_dir)}.sock")  # macOS: per-user temp


def _key_file(data_dir: Path) -> Path:
    return Path(data_dir) / "window" / "ipc.key"


def _key(data_dir: Path, new: bool = False) -> bytes:
    f = _key_file(data_dir)
    if new or not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        k = secrets.token_bytes(32)
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(k)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(f)
        return k
    return f.read_bytes()


# ---------------- the review app, called in-process ----------------

class AsgiDispatcher:
    """Calls an ASGI app (FastAPI) without a server: one event loop on its own thread."""

    def __init__(self, app):
        self.app = app
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="asgi", daemon=True).start()

    def request(self, method: str, path: str, headers: dict | None = None, body: bytes = b"",
                timeout: float = 600) -> dict:
        fut = asyncio.run_coroutine_threadsafe(self._call(method, path, headers or {}, body), self.loop)
        return fut.result(timeout)

    async def _call(self, method: str, path: str, headers: dict, body: bytes) -> dict:
        path, _, query = path.partition("?")
        path = path.split("#")[0] or "/"
        hdrs = {k.lower(): str(v) for k, v in headers.items()}
        hdrs.update({"host": "localhost", "x-jobhunter": "1"})  # synthetic: nothing listens; the window is this app
        if body:
            hdrs.setdefault("content-type", "application/json")
        hdrs["content-length"] = str(len(body))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method.upper(),
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                 "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
                 "client": ("localhost", 0), "server": ("localhost", None)}
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        out = {"status": 500, "headers": {}, "body": b""}
        chunks: list[bytes] = []

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = {k.decode(): v.decode() for k, v in msg.get("headers", [])}
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))

        await self.app(scope, receive, send)
        out["body"] = b"".join(chunks)
        return out


# ---------------- tray side ----------------

class Server:
    """Answers the window: {"op": "http"|"show"|"ping", ...}."""

    def __init__(self, data_dir: Path, app, on_show=None):
        self.data_dir, self.on_show = Path(data_dir), on_show
        self.http = AsgiDispatcher(app)
        self.addr = address(self.data_dir)
        if FAMILY == "AF_UNIX" and os.path.exists(self.addr):
            os.unlink(self.addr)  # left over from a crash (callers check nothing answers first)
        self.listener = Listener(self.addr, family=FAMILY, authkey=_key(self.data_dir, new=True))
        if FAMILY == "AF_UNIX":
            os.chmod(self.addr, 0o600)
        threading.Thread(target=self._accept, name="ipc", daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn = self.listener.accept()
            except OSError:
                return  # closed
            except Exception as e:  # wrong key or a broken client: ignore it
                log.warning("ipc: refused a connection (%s)", type(e).__name__)
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn) -> None:
        try:
            while True:
                try:
                    msg = conn.recv()
                except (EOFError, OSError):
                    return
                conn.send(self.handle(msg))
        finally:
            conn.close()

    def handle(self, msg: dict) -> dict:
        op = msg.get("op")
        try:
            if op == "ping":
                return {"ok": True, "app": "lockandland"}
            if op == "show":
                return {"ok": True, "how": self.on_show(msg.get("path") or "/") if self.on_show else None}
            if op == "http":
                import time
                t0 = time.perf_counter()
                r = self.http.request(msg.get("method") or "GET", msg.get("path") or "/", msg.get("headers") or {},
                                      base64.b64decode(msg.get("body") or ""))
                ms = (time.perf_counter() - t0) * 1000
                if ms > 800:  # slow pages and actions show up in the log with their size
                    log.info("slow request %s %s: %.0f ms, %d bytes", msg.get("method"),
                             str(msg.get("path")).split("?")[0], ms, len(r["body"]))
                return {"ok": True, "status": r["status"], "headers": r["headers"],
                        "body": base64.b64encode(r["body"]).decode()}
        except Exception as e:
            log.exception("ipc %s failed", op)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": False, "error": f"unknown op {op!r}"}

    def close(self) -> None:
        try:
            self.listener.close()
        except OSError:
            pass
        if FAMILY == "AF_UNIX":
            try:
                os.unlink(self.addr)
            except OSError:
                pass


# ---------------- window / second-start side ----------------

def call(data_dir: Path, msg: dict, wait: float = 0) -> dict:
    """Send one request to the running app. wait: seconds to keep retrying while it (re)starts."""
    import time
    end = time.time() + wait
    while True:
        try:
            with Client(address(data_dir), family=FAMILY, authkey=_key_file(data_dir).read_bytes()) as c:
                c.send(msg)
                return c.recv()
        except (OSError, EOFError, ConnectionError) as e:
            if time.time() >= end:
                raise ConnectionError(f"{type(e).__name__}: the app isn't running") from e
            time.sleep(0.4)


def running(data_dir: Path) -> bool:
    try:
        return bool(call(data_dir, {"op": "ping"}).get("ok"))
    except Exception:
        return False
