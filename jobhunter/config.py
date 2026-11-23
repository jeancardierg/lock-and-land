from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

FROZEN = bool(getattr(sys, "frozen", False))  # running from the packaged desktop app (PyInstaller)
# ROOT: where the code and bundled read-only resources are (config.example.yaml, review page, templates)
ROOT = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parent.parent


def app_config_dir() -> Path:
    """Per-user folder for the app's own pointer file (home.txt). Not where your data lives."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "jobhunter"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "jobhunter"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jobhunter"


def default_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "jobhunter"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "jobhunter"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "jobhunter"


def _home() -> Path:
    """HOME: your config.yaml, cv/, facts/ and data/. Order: JOBHUNTER_HOME, the installer's home.txt, the
    platform default (packaged app), or the source folder itself (running from source, as before)."""
    if os.environ.get("JOBHUNTER_HOME"):
        return Path(os.environ["JOBHUNTER_HOME"]).expanduser()
    ptr = app_config_dir() / "home.txt"
    if FROZEN or os.environ.get("JOBHUNTER_APP"):
        try:
            txt = ptr.read_text(encoding="utf-8").strip()
            if txt:
                return Path(txt).expanduser()
        except OSError:
            pass
        return default_home()
    return ROOT


HOME = _home()
DATA = HOME / "data"
CV_DIR = HOME / "cv"

LEAD_RE = re.compile(r"\b(it\s+(lead|manager|director)|head\s+of\s+it|it\s+team\s+lead|responsable\s+de\s+(it|sistemas)|"
                     r"jefe\s+de\s+(it|sistemas)|it\s+operations\s+manager)\b", re.I)
SPANISH_HINTS = re.compile(r"\b(experiencia|requisitos|ofrecemos|buscamos|incorporación|funciones|imprescindible|"
                           r"valorable|jornada|empresa)\b", re.I)


def _load_env() -> None:
    """Running from source only: a .env next to the code still works. The desktop app never reads .env;
    secrets live in the OS keychain (see secrets.py) and an old .env is imported once and wiped."""
    if FROZEN or os.environ.get("JOBHUNTER_APP"):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:  # dotenv optional in tests
        pass


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    _load_env()
    p = Path(path) if path else HOME / "config.yaml"
    if not p.exists():
        p = ROOT / "config.example.yaml"
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    DATA.mkdir(exist_ok=True)
    return cfg


def env(name: str, default: str = "") -> str:
    """A setting or secret: the process environment first, then the OS keychain."""
    v = os.environ.get(name)
    if v:
        return v
    from . import secrets
    return secrets.get(name) or default


def resolve(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else HOME / p


def is_spanish(text: str) -> bool:
    return len(SPANISH_HINTS.findall(text or "")) >= 3


def cv_variant(job: dict | None) -> str:
    """Pick which CV a job should be drafted from: 'lead', 'es' or 'main'."""
    if not job:
        return "main"
    if LEAD_RE.search(job.get("title") or ""):
        return "lead"
    if is_spanish(f"{job.get('title') or ''} {job.get('description') or ''}"):
        return "es"
    return "main"


def _variant_cfg(cfg: dict, variant: str) -> dict:
    v = (cfg["profile"].get("cv_variants") or {})
    return v.get(variant) or v.get("main") or {
        "markdown": cfg["profile"].get("cv_markdown", "cv/cv_main.md"),
        "attachment": cfg["profile"].get("cv_attachment", "data/cv.pdf"),
    }


def read_cv(cfg: dict, variant: str = "main") -> str:
    p = resolve(_variant_cfg(cfg, variant)["markdown"])
    if not p.exists():
        raise FileNotFoundError(f"CV not found at {p}. Put your CV as markdown/plain text there.")
    return p.read_text(encoding="utf-8")


def cv_attachment(cfg: dict, variant: str = "main") -> Path:
    return resolve(_variant_cfg(cfg, variant)["attachment"])


def drafting_rules(cfg: dict) -> str:
    rules = cfg.get("drafting", {}).get("rules") or []
    return "\n".join(f"- {r}" for r in rules)


PLACEHOLDER_RE = re.compile(r"\[(phone|email|linkedin url|teléfono|telefono)\]", re.I)


def attachment_problem(path: Path) -> str | None:
    """Why this CV file must not be sent, or None if it looks ready (exists, readable, no [phone]/[email] placeholders)."""
    if not path.exists():
        return f"CV file missing: {path}"
    if path.suffix.lower() != ".pdf":
        return None
    st = path.stat()  # reading a PDF takes a while: check each version of the file once, not once per job card
    return _pdf_problem(str(path), st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=16)
def _pdf_problem(p: str, mtime_ns: int, size: int) -> str | None:
    path = Path(p)
    try:
        from pypdf import PdfReader
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    except Exception as e:  # unreadable PDF: block rather than send blind
        return f"Could not read {path.name}: {e}"
    m = PLACEHOLDER_RE.search(text)
    if m:
        return f"{path.name} still contains the placeholder {m.group(0)}. Fill in your contact details and re-export."
    return None


# ---------------- job criteria helpers (set by the setup wizard) ----------------

LANG_ALIASES = {
    "english": ["english", "inglés", "ingles", "anglais", "englisch"],
    "spanish": ["spanish", "castellano", "español", "espanol", "castilian"],
    "catalan": ["catalan", "català", "catalán"], "french": ["french", "français", "francés", "frances"],
    "german": ["german", "deutsch", "alemán", "aleman"], "portuguese": ["portuguese", "português", "portugués"],
    "italian": ["italian", "italiano"], "dutch": ["dutch", "nederlands", "flemish"],
}


def spoken_langs(cfg: dict) -> set[str]:
    """Every spelling of the languages in profile.languages ("English (native)" -> english, inglés, ...)."""
    out: set[str] = set()
    for l in (cfg.get("profile") or {}).get("languages") or ["English"]:
        w = str(l).split("(")[0].strip().lower().split()[0] if str(l).strip() else ""
        for key, names in LANG_ALIASES.items():
            if w in names:
                out.update(names)
                break
        else:
            if w:
                out.add(w)
    return out


def home_city(cfg: dict) -> str:
    hc = (cfg.get("rules") or {}).get("home_cities") or []
    if hc:
        return str(hc[0]).title()
    loc = (cfg.get("profile") or {}).get("location") or ""
    return loc.split(",")[0].strip() or "your city"
