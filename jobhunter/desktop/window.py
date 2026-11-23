"""The Lock & Land window: the review, Configuration and criteria pages in a native app window (WebView2 on Windows,
WebKit on macOS). No web server and no network port: the window gets its pages and data from the app over a
private local channel (see ipc.py), and the pages' fetch() calls go through the window's own bridge.

It runs as its own small process (`jobhunter.exe --window <path> <cmdfile>`), because the tray icon owns the tray
process's main thread and both window toolkits need one too. The tray asks the window to show a page by writing the
path to <cmdfile>; the window loads it and comes to the front. Closing the window doesn't stop the app: the tray
keeps fetching and drafting in the background. Links to job ads and other sites open in your normal browser.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from jobhunter.branding import APP_NAME as TITLE

log = logging.getLogger("jobhunter.window")


# ---------------- tray side ----------------

class WindowManager:
    """Owns the window process. open(path) shows that page, starting the window if it isn't running."""

    def __init__(self, data_dir: Path, enabled: bool = True):
        self.data = Path(data_dir)
        self.cmd = self.data / "window.cmd"
        self.enabled = enabled
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _argv(self, path: str) -> list[str]:
        from jobhunter.config import FROZEN
        if FROZEN:
            return [sys.executable, "--window", path, str(self.cmd)]
        return [sys.executable, "-m", "jobhunter.desktop", "--window", path, str(self.cmd)]

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def open(self, path: str = "/") -> str:
        """Returns 'shown' (existing window), 'started' or 'off'."""
        path = path if path.startswith("/") else "/" + path
        if not self.enabled:
            log.info("app window is turned off (desktop.window: false)")
            return "off"
        with self.lock:
            if self.alive():
                self.cmd.write_text(json.dumps({"path": path, "t": time.time()}), encoding="utf-8")
                return "shown"
            self.cmd.unlink(missing_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = subprocess.Popen(self._argv(path), creationflags=flags, close_fds=True)
            return "started"

    def close(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------- window process ----------------

# Put first in every page: fetch(), links, window.open, the clipboard and saved filters go through the bridge.
SHIM = r"""<script>(function(){
const ready=new Promise(r=>{if(window.pywebview&&window.pywebview.api&&window.pywebview.api.http)r();else window.addEventListener('pywebviewready',()=>r())});
const api=async()=>{await ready;return window.pywebview.api};
const b64=u8=>{let s='';for(let i=0;i<u8.length;i+=32768)s+=String.fromCharCode.apply(null,u8.subarray(i,i+32768));return btoa(s)};
const unb64=s=>{const b=atob(s||''),u=new Uint8Array(b.length);for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return u};
const realFetch=window.fetch.bind(window);
window.JH_APP=true;window.JH_PATH=__PATH__;window.JH_HASH=__HASH__;
window.fetch=async function(input,init){init=init||{};const url=typeof input==='string'?input:input.url;
  if(/^[a-z]+:/i.test(url))return realFetch(input,init);
  const headers={};new Headers(init.headers||{}).forEach((v,k)=>headers[k]=v);
  let body='';const b=init.body;
  if(b!=null){if(typeof b==='string')body=b64(new TextEncoder().encode(b));else if(b instanceof Blob)body=b64(new Uint8Array(await b.arrayBuffer()));
    else if(b instanceof ArrayBuffer)body=b64(new Uint8Array(b));else if(ArrayBuffer.isView(b))body=b64(new Uint8Array(b.buffer,b.byteOffset,b.byteLength));
    else body=b64(new TextEncoder().encode(String(b)))}
  const r=await (await api()).http(init.method||'GET',url,headers,body);
  if(!r||!r.ok)throw new TypeError((r&&r.error)||'The app is not answering');
  const nobody=[101,204,205,304].includes(r.status);
  return new Response(nobody?null:unb64(r.body),{status:r.status,headers:r.headers})};
window.jhGo=async p=>{const a=await api();a.go(p)};
window.jhReload=()=>window.jhGo(window.JH_PATH+(window.JH_HASH||''));
window.open=function(u){u=String(u||'');if(u.startsWith('/'))window.jhGo(u);else if(/^(https?|mailto):/i.test(u))api().then(a=>a.external(u));return null};
document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a[href]');if(!a||e.defaultPrevented)return;
  const h=(a.getAttribute('href')||'').trim();if(!h||h.startsWith('#'))return;
  e.preventDefault();if(h.startsWith('/'))window.jhGo(h);else if(/^(https?|mailto):/i.test(h))api().then(x=>x.external(h))},true);
window.jhCopy=async t=>{const ok=await (await api()).copy(String(t??''));if(ok===false)throw new Error('Could not copy to the clipboard');return true};
try{if(navigator.clipboard)navigator.clipboard.writeText=window.jhCopy;else Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:window.jhCopy}})}catch(e){}
window.__jhErrors=[];addEventListener('error',e=>window.__jhErrors.push(String(e.message||e.type)));
addEventListener('unhandledrejection',e=>window.__jhErrors.push('promise: '+String(e.reason&&e.reason.message||e.reason)));
addEventListener('securitypolicyviolation',e=>window.__jhErrors.push('CSP blocked '+e.violatedDirective+' '+e.blockedURI));
const PREFS=__PREFS__;
const store={getItem:k=>k in PREFS?PREFS[k]:null,setItem:(k,v)=>{PREFS[k]=String(v);api().then(a=>a.pref(k,String(v)))},
  removeItem:k=>{delete PREFS[k];api().then(a=>a.pref(k,null))},clear:()=>{},key:i=>Object.keys(PREFS)[i]??null,get length(){return Object.keys(PREFS).length}};
try{Object.defineProperty(window,'localStorage',{configurable:true,get:()=>store})}catch(e){}
})();</script>"""

LOADING = ("<!doctype html><meta charset=utf-8><body style='margin:0;display:grid;place-items:center;height:100vh;"
           "font:15px system-ui;background:#f7f7f5;color:#6b6b66'>Opening Lock &amp; Land…</body>")


# Content Security Policy for every page in the window. The pages are the app's own (inline script and style, the
# bundled map library added as an inline script); this stops anything else: remote scripts, frames, plugins, form
# posts and <base> tricks. fetch() goes through the bridge, so the page itself needs no network.
# JOBHUNTER_NO_CSP=1 turns it off if a WebView version ever objects.
CSP = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; "
       "img-src data: blob:; font-src data:; connect-src 'none'; media-src 'none'; frame-src 'none'; "
       "child-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")


def inject(html: str, path: str, prefs: dict | None = None) -> str:
    p, _, h = path.partition("#")
    csp = "" if os.environ.get("JOBHUNTER_NO_CSP") == "1" else f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
    shim = csp + (SHIM.replace("__PATH__", json.dumps(p or "/")).replace("__HASH__", json.dumps("#" + h if h else ""))
            .replace("__PREFS__", json.dumps(prefs or {}).replace("</", "<\\/")))
    low = html[:2000].lower()
    i = low.find("<head>")
    if i >= 0:
        i += len("<head>")
        return html[:i] + shim + html[i:]
    return shim + html


def _error_page(msg: str, path: str) -> str:
    from html import escape
    return (f"<!doctype html><meta charset=utf-8><title>Lock &amp; Land</title><body style='font:15px system-ui;padding:40px;background:#f7f7f5'>"
            f"<h2>Lock &amp; Land isn't answering</h2><p>{escape(msg)}</p>"
            f"<p><button onclick=\"jhGo({escape(json.dumps(path))})\">Try again</button></p></body>")


def _win_copy(text: str) -> bool:
    """Unicode text onto the Windows clipboard (CF_UNICODETEXT), so accents and symbols survive."""
    import ctypes
    from ctypes import wintypes
    k32, u32 = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("user32", use_last_error=True)
    k32.GlobalAlloc.argtypes, k32.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
    k32.GlobalLock.argtypes, k32.GlobalLock.restype = [wintypes.HGLOBAL], ctypes.c_void_p
    k32.GlobalUnlock.argtypes, k32.GlobalFree.argtypes = [wintypes.HGLOBAL], [wintypes.HGLOBAL]
    u32.OpenClipboard.argtypes, u32.SetClipboardData.argtypes = [wintypes.HWND], [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    data = (text + "\0").encode("utf-16-le")
    for _ in range(10):  # another app may hold the clipboard for a moment
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not h:
            return False
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(13, h):  # CF_UNICODETEXT; on success Windows owns the memory
            k32.GlobalFree(h)
            return False
        return True
    finally:
        u32.CloseClipboard()


class Bridge:
    """What the pages can call (window.pywebview.api.*). Everything else stays in the app."""

    def __init__(self, data_dir: Path):
        self._data = Path(data_dir)
        self._win = None
        self._prefs_file = self._data / "window" / "prefs.json"

    # --- called from JavaScript ---
    def http(self, method, path, headers, body):
        from jobhunter.desktop import ipc
        if not str(path).startswith("/"):
            return {"ok": False, "error": "only app paths"}
        t0 = time.perf_counter()
        try:
            r = ipc.call(self._data, {"op": "http", "method": method, "path": path, "headers": headers or {},
                                      "body": body or ""}, wait=15)
        except ConnectionError as e:
            return {"ok": False, "error": str(e)}
        ms = (time.perf_counter() - t0) * 1000
        if ms > 800:
            log.info("slow %s %s: %.0f ms round trip, %d bytes", method, str(path).split("?")[0], ms,
                     len(r.get("body") or "") * 3 // 4)
        return r

    def go(self, path):
        threading.Thread(target=self._navigate, args=(str(path or "/"),), daemon=True).start()
        return True

    def external(self, url):
        if str(url).startswith(("http://", "https://", "mailto:")):
            webbrowser.open(str(url))
        return True

    def copy(self, text):
        """The page's clipboard. The window's pages have no secure origin, so navigator.clipboard doesn't exist
        there: every Copy button comes here. Returns False if nothing could be copied."""
        text = str(text or "")
        try:
            if sys.platform == "win32":
                return _win_copy(text)
            if sys.platform == "darwin":
                return subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False).returncode == 0
        except OSError as e:
            log.warning("copy failed: %s", type(e).__name__)
            return False
        return False

    def pref(self, key, value):
        p = self._prefs()
        if value is None:
            p.pop(str(key), None)
        else:
            p[str(key)] = str(value)[:20000]
        try:
            self._prefs_file.parent.mkdir(parents=True, exist_ok=True)
            self._prefs_file.write_text(json.dumps(p), encoding="utf-8")
        except OSError:
            pass
        return True

    # --- window side ---
    def _prefs(self) -> dict:
        try:
            return json.loads(self._prefs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _page(self, path: str) -> str:
        from jobhunter.desktop import ipc
        try:
            r = ipc.call(self._data, {"op": "http", "method": "GET", "path": path.partition("#")[0]}, wait=20)
        except ConnectionError as e:
            return _error_page(f"{e}. Start Lock & Land again from the Start menu or Applications.", path)
        if not r.get("ok") or r.get("status") != 200:
            return _error_page(r.get("error") or f"That page answered {r.get('status')}", path)
        return base64.b64decode(r["body"]).decode("utf-8", "replace")

    def _navigate(self, path: str) -> None:
        if self._win is None:
            return
        try:
            html = self._page(path)
            self._win.load_html(inject(html, path, self._prefs()))
            log.info("showed %s (%d bytes)", path, len(html))
        except Exception:
            log.exception("couldn't show %s", path)


def _watch(win, bridge: Bridge, cmd: Path) -> None:
    """Follow the tray's requests: load the page asked for and bring the window to the front."""
    while True:
        time.sleep(0.8)  # a tray click shows the page within a second; fewer idle wake-ups
        try:
            raw = cmd.read_text(encoding="utf-8")
            cmd.unlink(missing_ok=True)
            path = json.loads(raw).get("path") or "/"
        except (OSError, ValueError):
            continue
        try:
            bridge._navigate(path)
            win.restore()
            win.show()
            win.on_top = True  # the usual trick to come to the front on Windows and macOS
            time.sleep(0.3)
            win.on_top = False
        except Exception as e:  # window closed meanwhile
            log.debug("window: %s", e)


def _macos_front() -> None:
    """The app is a menu-bar app (no Dock icon); a window needs a regular app to take focus."""
    try:
        from AppKit import NSApplication
        from PyObjCTools import AppHelper

        def go():
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular: Dock icon while the window is open
            app.activateIgnoringOtherApps_(True)
        AppHelper.callAfter(go)
    except Exception as e:
        log.debug("macOS activation: %s", e)


def check() -> int:
    """Build self-test: can this build open a window? (imports the platform's web view backend)."""
    try:
        import webview  # noqa: F401
        if sys.platform == "win32":
            import clr  # noqa: F401  (pythonnet, used for WebView2)
            from webview.platforms import edgechromium  # noqa: F401
        elif sys.platform == "darwin":
            from webview.platforms import cocoa  # noqa: F401
        print("window: ok")
        return 0
    except Exception as e:
        print(f"window: unavailable ({type(e).__name__}: {e})")
        return 1


def run(path: str, cmd: str) -> int:
    if sys.stdout is None or sys.stderr is None:  # windowed build: no console
        sys.stdout = sys.stdout or open(os.devnull, "w")
        sys.stderr = sys.stderr or open(os.devnull, "w")
    if path.startswith("http"):  # an older tray asked with a URL
        from urllib.parse import urlsplit
        u = urlsplit(path)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "") + (f"#{u.fragment}" if u.fragment else "")
    data = Path(cmd).parent
    try:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            handlers=[logging.FileHandler(data / "window.log", encoding="utf-8")])
    except OSError:
        pass
    bridge = Bridge(data)
    try:
        import webview
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # job ads, Google, Workday: your browser
        win = webview.create_window(TITLE, html=LOADING, js_api=bridge, width=1280, height=860, min_size=(420, 500),
                                    text_select=True, zoomable=True, background_color="#f7f7f5")
        bridge._win = win
        threading.Thread(target=_watch, args=(win, bridge, Path(cmd)), daemon=True).start()

        def started():
            if sys.platform == "darwin":
                _macos_front()
            # Wait until the web view has shown its first (loading) page. Loading ours earlier races the web view's
            # own start-up, which then puts the loading page back: the window stayed on "Opening…" forever.
            if not win.events.loaded.wait(30):
                log.warning("the web view took over 30 s to start")
            log.info("window ready, opening %s", path)
            for attempt in range(3):  # belt and braces: if the loading page is still there, load again
                bridge._navigate(path)
                time.sleep(4 + 4 * attempt)
                try:
                    if win.evaluate_js("document.title"):  # every app page has a title; the loading page has none
                        return
                except Exception as e:
                    log.debug("title check: %s", e)
                log.warning("page didn't appear, retrying (%d)", attempt + 1)
        kw = {"private_mode": False, "storage_path": str(data / "window")}
        if sys.platform == "win32":
            kw["gui"] = "edgechromium"
        webview.start(started, **kw)
        return 0
    except Exception as e:
        log.warning("no app window (%s)", e)
        _no_window(str(e))
        return 1


def _no_window(err: str) -> None:
    msg = (f"{TITLE} couldn't open its window ({err}).\n\nOn Windows, install the Microsoft Edge WebView2 Runtime "
           "(free, from microsoft.com), then open the app again. It keeps working in the background meanwhile.")
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, TITLE, 0x30)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'display alert "{TITLE}" message {json.dumps(msg)}'], timeout=60)
    except Exception:
        pass


# ---------------- self-test: every page in a real window, in a throwaway data folder ----------------

CHECKS = {
    "/": "!!document.querySelector('nav') && !/^\\s*Loading/.test((document.querySelector('#list,#main,main')||{}).innerText||'')",
    "/connections": "!!document.querySelector('#aicard') || !!document.querySelector('.card')",
    "/setup": "!!document.querySelector('#pk_home_city') && !!document.querySelector('#roles')",
}


def _listening_ports() -> list[str]:
    """TCP ports this process listens on (should be none)."""
    pid = str(os.getpid())
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return [ln.split()[1] for ln in out.splitlines() if "LISTENING" in ln and ln.split()[-1] == pid]
        out = subprocess.run(["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"], capture_output=True,
                             text=True, timeout=30).stdout
        return [ln.split()[8] for ln in out.splitlines()[1:] if len(ln.split()) > 8]
    except Exception as e:
        return [f"(couldn't check: {e})"]


def selftest(out: str | None = None) -> int:
    """jobhunter.exe --window-selftest [result.json]: opens the window on a throwaway data folder, shows each page,
    checks it rendered with no script or CSP errors, tries the map, checks no TCP port is open, then closes."""
    from jobhunter import connections
    from jobhunter.config import DATA, HOME
    from jobhunter.desktop import ipc
    from jobhunter.review.app import app
    connections.ensure_home()
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(DATA / "window.log", encoding="utf-8")])
    srv = ipc.Server(DATA, app)
    bridge = Bridge(DATA)
    res: dict = {"home": str(HOME), "pages": {}, "ok": False}
    import webview
    win = webview.create_window(TITLE + " self-test", html=LOADING, js_api=bridge, width=1100, height=800)
    bridge._win = win

    def js(code):
        try:
            return win.evaluate_js(code)
        except Exception as e:
            return f"error: {e}"

    def run_checks():
        try:
            if not win.events.loaded.wait(60):
                res["error"] = "web view didn't start in 60 s"
                return
            for path, check in CHECKS.items():
                t0 = time.time()
                bridge._navigate(path)
                ok = False
                while time.time() - t0 < 25:
                    time.sleep(1)
                    if js(f"Boolean({check})") is True:
                        ok = True
                        break
                time.sleep(1.5)
                res["pages"][path] = {"ok": ok, "seconds": round(time.time() - t0, 1), "title": js("document.title"),
                                      "errors": js("JSON.stringify(window.__jhErrors||[])")}
            js("toggleMap()")  # the map: bundled library + tiles through the app
            time.sleep(12)
            res["map"] = {"leaflet": js("!!window.L"), "tiles": js("document.querySelectorAll('.leaflet-tile-loaded').length"),
                          "errors": js("JSON.stringify(window.__jhErrors||[])")}
            res["listening_ports"] = _listening_ports()
            res["ok"] = (all(p["ok"] and p["errors"] in ("[]", None) for p in res["pages"].values())
                         and res["map"]["leaflet"] is True and not res["listening_ports"])
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                win.destroy()
            except Exception:
                pass

    kw = {"private_mode": True}
    if sys.platform == "win32":
        kw["gui"] = "edgechromium"
    try:
        webview.start(lambda: threading.Thread(target=run_checks, daemon=True).start(), **kw)
    except Exception as e:
        res["error"] = f"window: {type(e).__name__}: {e}"
    srv.close()
    text = json.dumps(res, indent=1)
    print(text)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    return 0 if res["ok"] else 1

