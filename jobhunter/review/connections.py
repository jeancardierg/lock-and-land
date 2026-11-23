"""Configuration page and API (/connections). Only answers requests from this computer (127.0.0.1), with a matching
Host header and the X-Jobhunter header, so a web page in your browser can't read or change secrets (CSRF /
DNS rebinding), even when the review page is bound to a Tailscale IP for your phone."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import connections as C
from .. import secrets
from ..config import load_config

LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}  # testclient = FastAPI TestClient peer
SETTABLE = {"ADZUNA_APP_ID", "ADZUNA_APP_KEY", "GMAIL_APP_PASSWORD", "TELEGRAM_BOT_TOKEN", "JOOBLE_API_KEY",
            "CAREERJET_API_KEY", "FINDWORK_API_KEY", "MUSE_API_KEY"}


def local_only(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host not in LOCAL:
        raise HTTPException(403, "Configuration can only be changed on this computer")
    hdr = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if hdr not in LOCAL:
        raise HTTPException(403, "Bad Host header")
    if request.method != "GET" and request.headers.get("x-jobhunter") != "1":
        raise HTTPException(403, "Missing X-Jobhunter header")


router = APIRouter(dependencies=[Depends(local_only)])


def _page() -> str:
    from pathlib import Path
    return (Path(__file__).parent / "connections.html").read_text(encoding="utf-8")


@router.get("/connections", response_class=HTMLResponse)
@router.get("/configuration", response_class=HTMLResponse)
def page() -> str:
    return _page()


@router.get("/api/connections")
def get_status(refresh: int = 0) -> dict:
    return C.status(refresh=bool(refresh))


@router.post("/api/connections/secret")
def set_secret(body: dict = Body(...)) -> dict:
    name, value = body.get("name"), (body.get("value") or "").strip()
    if name not in SETTABLE:
        raise HTTPException(400, f"Unknown setting {name!r}")
    if name == "TELEGRAM_BOT_TOKEN":
        from ..notify import clean_token
        value = clean_token(value)
    if value:
        secrets.set(name, value)
    else:
        secrets.delete(name)
    out = {"ok": True, "name": name, "stored": bool(value)}
    src = next((k for k, m in C.SOURCES.items() if m.get("secret") == name and not m.get("optional_key")), None)
    if src:  # a saved key means you want that source searched
        out["source"] = C.set_source(src, bool(value))
        if value:  # tell the person straight away whether the key works
            out["test"] = C.source_test(src)
    if name == "TELEGRAM_BOT_TOKEN" and value:
        out["test"] = C.telegram_token_saved()
    return out


@router.post("/api/connections/telegram/connect")
def telegram_connect() -> dict:
    return C.telegram_connect()


@router.post("/api/connections/telegram/chat")
def telegram_chat(body: dict = Body(...)) -> dict:
    try:
        return C.telegram_set_chat(str(body.get("chat_id") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connections/telegram/enabled")
def telegram_enabled(body: dict = Body(...)) -> dict:
    return C.telegram_enable(bool(body.get("enabled")))


@router.post("/api/connections/telegram/test")
def telegram_test() -> dict:
    return C.telegram_test()


@router.post("/api/connections/source/test")
def source_test(body: dict = Body(...)) -> dict:
    return C.source_test(str(body.get("key") or ""))


@router.post("/api/connections/gmail/app-password")
def gmail_app_password(body: dict = Body(...)) -> dict:
    try:
        return C.gmail_app_password(str(body.get("email") or ""), str(body.get("password") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connections/gmail/mode")
def gmail_mode(body: dict = Body(...)) -> dict:
    try:
        return C.gmail_mode(str(body.get("mode") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connections/gmail/app-password/forget")
def gmail_forget() -> dict:
    return C.gmail_forget_app_password()


@router.post("/api/connections/recovery-key/saved")
def recovery_saved() -> dict:
    return C.mark_recovery_saved()


@router.post("/api/connections/adzuna")
def adzuna(body: dict = Body(...)) -> dict:
    app_id, key = (body.get("app_id") or "").strip(), (body.get("app_key") or "").strip()
    if not (app_id and key):
        raise HTTPException(400, "Both App ID and App key are needed")
    secrets.set("ADZUNA_APP_ID", app_id)
    secrets.set("ADZUNA_APP_KEY", key)
    try:
        return C.adzuna_test(load_config())
    except Exception as e:
        return {"ok": False, "detail": f"saved, but the test call failed: {e}"}


@router.post("/api/connections/adzuna/test")
def adzuna_test() -> dict:
    try:
        return C.adzuna_test(load_config())
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@router.post("/api/connections/gmail/client")
def gmail_client(body: dict = Body(...)) -> dict:
    from .. import gmail
    try:
        gmail.save_client(body.get("json") or "")
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "That isn't the right file. It must be the JSON you download from Google Cloud for an "
                                 f"OAuth client of type Desktop app (client_secret_….json). ({e})")
    return {"ok": True}


@router.post("/api/connections/gmail/signin")
def gmail_signin() -> dict:
    return C.gmail_signin()


@router.post("/api/connections/gmail/signout")
def gmail_signout() -> dict:
    from .. import gmail
    gmail.sign_out()
    C._gmail_cache["t"] = 0.0
    return {"ok": True}


@router.get("/setup", response_class=HTMLResponse)
def setup_page() -> str:
    from pathlib import Path
    return (Path(__file__).parent / "setup.html").read_text(encoding="utf-8")


@router.post("/api/setup/cv-text")
def setup_cv_text(body: dict = Body(...)) -> dict:
    """Read a CV file chosen on the setup page and return its text (nothing is saved here)."""
    import base64
    from .. import profile_setup
    raw = str(body.get("b64") or "").split(",")[-1]
    try:
        data = base64.b64decode(raw)
        if len(data) > 15_000_000:
            raise ValueError("The file is over 15 MB")
        return {"text": profile_setup.text_from_file(data, str(body.get("name") or ""))}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # a damaged or protected file
        raise HTTPException(400, f"Couldn't read that file ({type(e).__name__}). Paste the CV text instead")


@router.get("/static/{path:path}")
def static_file(path: str):
    """Files the pages need offline (the Leaflet map library)."""
    from fastapi.responses import FileResponse
    root = (Path(__file__).parent / "static").resolve()
    f = (root / path).resolve()
    if root not in f.parents or not f.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(f, headers={"Cache-Control": "max-age=86400"})


def _geo(fn, *a):
    from .. import geo
    try:
        return fn(*a)
    except geo.GeoError as e:
        raise HTTPException(503, str(e))


@router.get("/api/geo/search")
def geo_search(q: str = "") -> list:
    from .. import geo
    return _geo(geo.search, q)


@router.get("/api/geo/reverse")
def geo_reverse(lat: float, lon: float) -> dict:
    from .. import geo
    return _geo(geo.reverse, lat, lon) or {}


@router.get("/api/geo/towns")
def geo_towns(lat: float, lon: float, km: float = 20) -> list:
    from .. import geo
    return _geo(geo.towns, lat, lon, km)


@router.get("/api/geo/tile/{z}/{x}/{y}.png")
def geo_tile(z: int, x: int, y: int):
    from fastapi.responses import Response
    from .. import geo
    return Response(_geo(geo.tile, z, x, y), media_type="image/png", headers={"Cache-Control": "max-age=604800"})


@router.post("/api/setup/cv-fill")
def setup_cv_fill(body: dict = Body(...)) -> dict:
    """Criteria read from the CV text, each checked against it (nothing is saved here).
    {"background": true} starts it on a thread and returns at once; poll GET /api/setup/cv-fill."""
    from .. import cv_criteria
    try:
        if body.get("background"):
            return C.cv_fill_start(str(body.get("text") or ""), load_config())
        return cv_criteria.extract(str(body.get("text") or ""), load_config())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/setup/cv-fill")
def setup_cv_fill_state() -> dict:
    return C.cv_fill_state()


@router.get("/api/setup")
def setup_get() -> dict:
    from .. import criteria
    return criteria.from_config()


@router.post("/api/setup")
def setup_save(body: dict = Body(...)) -> dict:
    from .. import criteria
    try:
        return criteria.apply(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connections/source")
def set_source(body: dict = Body(...)) -> dict:
    try:
        return C.set_source(body.get("key") or "", bool(body.get("enabled")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/window")
def show_window(body: dict = Body(...)) -> dict:
    """A second start of the app asks the running one to show its window."""
    path = body.get("path") or "/"
    if not path.startswith("/"):
        raise HTTPException(400, "path must start with /")
    if not C.WINDOW_HOOK:
        return {"ok": False, "detail": "no app window here"}
    return {"ok": True, "how": C.WINDOW_HOOK(path)}


@router.post("/api/connections/home")
def change_home(body: dict = Body(...)) -> dict:
    from ..desktop.confirm import require
    require("Change the data folder and restart Lock & Land?",
            f"New folder: {body.get('path') or ''}\nMode: {body.get('mode') or 'move'}")
    try:
        return C.change_home(body.get("path") or "", body.get("mode") or "move")
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))


@router.get("/api/connections/ai")
def ai_advice(refresh: int = 0) -> dict:
    return C.ai_advice(load_config(), refresh=bool(refresh))


@router.post("/api/connections/ai/apply")
def ai_apply(body: dict = Body(default={})) -> dict:
    return C.ai_apply(use_alternative=bool(body.get("alternative")), pull=body.get("pull", True) is not False)


@router.post("/api/connections/ollama/start")
def ollama_start() -> dict:
    return C.ollama_start()


@router.post("/api/connections/ollama/pull")
def ollama_pull() -> dict:
    return C.ollama_pull(load_config())


@router.post("/api/connections/recovery-key")
def recovery_key(body: dict = Body(...)) -> dict:
    if body.get("confirm") != "show":
        raise HTTPException(400, "confirm with {'confirm': 'show'}")
    from ..desktop.confirm import require
    require("Show the database recovery key on screen?",
            "Only say Yes if you just pressed 'Show recovery key' yourself.")
    return {"key": C.recovery_key()}


@router.post("/api/connections/recovery-key/restore")
def restore_key(body: dict = Body(...)) -> dict:
    from ..desktop.confirm import require
    require("Replace the database key with the recovery key you typed?",
            "Only say Yes if you just pressed 'Restore' yourself.")
    try:
        return C.restore_key(body.get("key") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connections/encrypt")
def encrypt() -> dict:
    return C.encrypt_all()


@router.post("/api/connections/import")
def import_install(body: dict = Body(...)) -> dict:
    try:
        return C.import_install(body.get("path") or "", wipe_secrets=bool(body.get("wipe")))
    except (FileNotFoundError, FileExistsError) as e:
        raise HTTPException(400, str(e))


@router.post("/api/connections/retire")
def retire(body: dict = Body(...)) -> dict:
    path = body.get("path") or ""
    if path not in C.legacy_candidates() and path not in C.legacy_candidates_with_secrets():
        from pathlib import Path
        if not (Path(path) / "data" / "jobhunter.db").exists():
            raise HTTPException(400, "Not an older jobhunter folder")
    return C.retire_legacy(path)


@router.post("/api/connections/backup/restore")
def backup_restore(body: dict = Body(...)) -> dict:
    """Put a daily backup back in place of jobhunter.db (at the next start; the current file is kept)."""
    if body.get("confirm") != "restore":
        raise HTTPException(400, "confirm with {'confirm': 'restore'}")
    name = str(body.get("name") or "")
    from ..desktop.confirm import require
    require("Replace your database with this backup and restart Lock & Land?",
            f"Backup: {name.splitlines()[0][:80] if name else '?'}\nThe current database is kept, renamed.")
    try:
        return C.restore_backup(name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connections/backup/now")
def backup_now() -> dict:
    from .. import backup
    return backup.daily(force=True)
