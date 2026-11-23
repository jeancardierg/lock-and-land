"""What this computer can run: OS, CPU, RAM, GPUs and their VRAM (or Apple Silicon unified memory), plus what the
local Ollama has installed and how it is actually loading models. No extra packages: ctypes, sysctl, nvidia-smi,
the Windows display-adapter registry keys and /proc. Every probe is best effort and never raises.

detect() is cached for the life of the process (hardware doesn't change while the app runs); ollama_info() is live.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests

log = logging.getLogger(__name__)
GB = 1024 ** 3
_cache: dict | None = None
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN).stdout
    except Exception as e:  # missing tool, timeout, access denied
        log.debug("%s failed: %s", cmd[0], e)
        return ""


def _gb(n) -> float:
    try:
        return round(float(n) / GB, 1)
    except (TypeError, ValueError):
        return 0.0


# ---------------- RAM ----------------

def _ram_windows() -> tuple[float, float]:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return _gb(m.ullTotalPhys), _gb(m.ullAvailPhys)


def _ram_mac() -> tuple[float, float]:
    total = _gb(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    free = 0.0
    vm = _run(["vm_stat"])
    page = re.search(r"page size of (\d+)", vm)
    if page:
        pages = sum(int(n) for k, n in re.findall(r"Pages (free|inactive|speculative):\s+(\d+)", vm))
        free = _gb(pages * int(page.group(1)))
    return total, free


def _ram_linux() -> tuple[float, float]:
    info = dict(re.findall(r"(\w+):\s+(\d+) kB", Path("/proc/meminfo").read_text()))
    return _gb(int(info.get("MemTotal", 0)) * 1024), _gb(int(info.get("MemAvailable", 0)) * 1024)


# ---------------- CPU ----------------

def _cpu_name() -> str:
    if sys.platform == "darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or platform.processor()
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"]).strip()
        return out.splitlines()[0].strip() if out else platform.processor()
    try:
        m = re.search(r"model name\s*:\s*(.+)", Path("/proc/cpuinfo").read_text())
        return m.group(1).strip() if m else platform.processor()
    except OSError:
        return platform.processor()


# ---------------- GPUs ----------------
# Ollama runs on NVIDIA (CUDA), AMD (ROCm: RX 6800 and newer on Windows, more on Linux) and Apple Silicon (Metal).
# Intel GPUs and most integrated graphics are not used by the standard Ollama build: models then run on the CPU.
AMD_ROCM_WIN = re.compile(r"RX\s*(6[89]\d\d|7\d\d\d|9\d\d\d)|PRO\s*W(6[89]|7)\d\d\d|Instinct", re.I)
INTEGRATED = re.compile(r"Intel\(R\) (UHD|HD|Iris)|Intel.*Graphics$|Radeon\(TM\) Graphics|Radeon Graphics|"
                        r"Vega \d+ Graphics|Basic (Display|Render)|Virtual|Parsec|Remote", re.I)


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if sys.platform == "win32" else "")
    if not exe or (sys.platform == "win32" and not Path(exe).exists()):
        return []
    out = _run([exe, "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                gpus.append({"vendor": "NVIDIA", "name": parts[0], "vram_gb": round(int(parts[1]) / 1024, 1),
                             "vram_used_gb": round(int(parts[2]) / 1024, 1),
                             "driver": parts[3] if len(parts) > 3 else "", "source": "nvidia-smi"})
            except ValueError:
                pass
    return gpus


def _windows_adapters() -> list[dict]:
    """Display adapters from the registry: HardwareInformation.qwMemorySize is the real 64-bit VRAM size
    (Win32_VideoController.AdapterRAM stops at 4 GB)."""
    ps = (r"Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*'"
          r" -ErrorAction SilentlyContinue | Where-Object { $_.DriverDesc } | ForEach-Object { [pscustomobject]@{"
          r" name = $_.DriverDesc; q = $_.'HardwareInformation.qwMemorySize'; d = $_.'HardwareInformation.MemorySize' }"
          r" } | ConvertTo-Json -Compress")
    raw = _run(["powershell", "-NoProfile", "-Command", ps], timeout=20).strip()
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows = rows if isinstance(rows, list) else [rows]
    out, seen = [], set()
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        size = r.get("q")
        if not isinstance(size, int):
            d = r.get("d")
            size = d if isinstance(d, int) else int.from_bytes(bytes(d), "little") if isinstance(d, list) else 0
        vendor = "NVIDIA" if re.search(r"NVIDIA|GeForce|Quadro|RTX", name, re.I) else \
            "AMD" if re.search(r"AMD|Radeon", name, re.I) else "Intel" if "Intel" in name else "Other"
        out.append({"vendor": vendor, "name": name, "vram_gb": _gb(size), "source": "registry"})
    return out


def _linux_amd() -> list[dict]:
    out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"]) if shutil.which("rocm-smi") else ""
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        return []
    gpus = []
    for card in data.values():
        if isinstance(card, dict) and "VRAM Total Memory (B)" in card:
            gpus.append({"vendor": "AMD", "name": card.get("Card series") or card.get("Card SKU") or "AMD GPU",
                         "vram_gb": _gb(card["VRAM Total Memory (B)"]), "source": "rocm-smi"})
    return gpus


def _classify(g: dict) -> dict:
    name = g["name"]
    integrated = bool(INTEGRATED.search(name)) or (g["vendor"] in ("Intel", "Other") and "Arc A" not in name)
    if g["vendor"] == "NVIDIA":
        usable, why = True, "CUDA"
    elif g["vendor"] == "AMD" and not integrated:
        usable = bool(AMD_ROCM_WIN.search(name)) or sys.platform.startswith("linux")
        why = "ROCm" if usable else "this Radeon is not on Ollama's ROCm list"
    elif g["vendor"] == "Apple":
        usable, why = True, "Metal"
    else:
        usable, why = False, "integrated graphics" if integrated else "Intel/other GPUs are not used by Ollama"
    return {**g, "integrated": integrated, "ollama_can_use": usable and not integrated, "why": why}


def _apple_gpu_limit_gb(ram_gb: float) -> float:
    """How much unified memory macOS lets the GPU wire down. Default is about 2/3 of RAM (3/4 from 64 GB);
    `sudo sysctl iogpu.wired_limit_mb=N` overrides it until the next restart."""
    try:
        mb = int(_run(["sysctl", "-n", "iogpu.wired_limit_mb"]).strip() or 0)
    except ValueError:
        mb = 0
    if mb > 0:
        return round(mb / 1024, 1)
    return round(ram_gb * (0.75 if ram_gb >= 64 else 0.67), 1)


def detect(refresh: bool = False) -> dict:
    global _cache
    if _cache is not None and not refresh:
        return _cache
    hw: dict = {"os": platform.system(), "os_version": platform.release(), "arch": platform.machine(),
                "cores": os.cpu_count() or 0, "cpu": "", "ram_gb": 0.0, "ram_free_gb": 0.0, "gpus": [],
                "unified_memory": False, "apple_silicon": False}
    try:
        hw["cpu"] = _cpu_name()
        if sys.platform == "win32":
            hw["ram_gb"], hw["ram_free_gb"] = _ram_windows()
            nv = _nvidia()
            others = [a for a in _windows_adapters() if a["vendor"] != "NVIDIA" or not nv]
            hw["gpus"] = nv + others
        elif sys.platform == "darwin":
            hw["ram_gb"], hw["ram_free_gb"] = _ram_mac()
            if platform.machine() == "arm64" or "Apple" in hw["cpu"]:
                hw.update(apple_silicon=True, unified_memory=True)
                hw["gpus"] = [{"vendor": "Apple", "name": f"{hw['cpu']} GPU", "vram_gb": _apple_gpu_limit_gb(hw["ram_gb"]),
                               "source": "unified memory (GPU share)"}]
        else:
            hw["ram_gb"], hw["ram_free_gb"] = _ram_linux()
            hw["gpus"] = _nvidia() + _linux_amd()
    except Exception as e:
        log.warning("hardware detection incomplete: %s", e)
        hw["error"] = str(e)
    hw["gpus"] = [_classify(g) for g in hw["gpus"]]
    _cache = hw
    return hw


# ---------------- Ollama ----------------

def ollama_paths() -> list[Path]:
    if sys.platform == "win32":
        la = Path(os.environ.get("LOCALAPPDATA", ""))
        return [la / "Programs" / "Ollama" / "ollama.exe", la / "Programs" / "Ollama" / "ollama app.exe"]
    if sys.platform == "darwin":
        return [Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app",
                Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama")]
    return [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]


def ollama_info(host: str) -> dict:
    """Installed? Running? Version, installed models (with sizes) and loaded models (how much sits in VRAM)."""
    local = re.match(r"https?://(localhost|127\.0\.0\.1|\[::1\])(:|/|$)", host) is not None
    installed = bool(shutil.which("ollama")) or any(p.exists() for p in ollama_paths())
    info = {"host": host, "local": local, "installed": installed or not local, "running": False, "version": "",
            "models": {}, "loaded": []}
    try:
        info["version"] = requests.get(f"{host}/api/version", timeout=4).json().get("version", "")
        info["running"] = True
        info["installed"] = True
        tags = requests.get(f"{host}/api/tags", timeout=6).json().get("models", [])
        info["models"] = {m["name"]: _gb(m.get("size", 0)) for m in tags}
        for m in requests.get(f"{host}/api/ps", timeout=4).json().get("models", []):
            size, vram = m.get("size", 0) or 0, m.get("size_vram", 0) or 0
            info["loaded"].append({"name": m.get("name"), "size_gb": _gb(size), "vram_gb": _gb(vram),
                                   "gpu_share": round(vram / size, 2) if size else 0.0,
                                   "context": m.get("context_length")})
    except Exception as e:
        info["error"] = f"{type(e).__name__}"
    return info


def start_ollama() -> bool:
    """Start the Ollama desktop app (it runs the server). True if something was launched."""
    try:
        if sys.platform == "win32":
            for p in ollama_paths()[::-1]:  # prefer "ollama app.exe" (tray), else ollama.exe serve
                if p.exists():
                    args = [str(p)] if p.name == "ollama app.exe" else [str(p), "serve"]
                    subprocess.Popen(args, creationflags=_NOWIN | getattr(subprocess, "DETACHED_PROCESS", 0))
                    return True
        elif sys.platform == "darwin":
            return subprocess.run(["open", "-a", "Ollama"], timeout=10).returncode == 0
        elif shutil.which("ollama"):
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception as e:
        log.warning("could not start Ollama: %s", e)
    return False
