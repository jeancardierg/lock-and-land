# PyInstaller spec for the jobhunter desktop app (Windows .exe folder / macOS .app).
# Build with packaging\build_windows.ps1 or packaging/build_mac.sh, not by hand.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
ASSETS = ROOT / "build" / "assets"
APP_VERSION = "0.4.0"

datas = [
    (str(ROOT / "config.example.yaml"), "."),
    (str(ROOT / "jobhunter" / "review" / "index.html"), "jobhunter/review"),
    (str(ROOT / "jobhunter" / "review" / "connections.html"), "jobhunter/review"),
    (str(ROOT / "jobhunter" / "review" / "setup.html"), "jobhunter/review"),
    (str(ROOT / "jobhunter" / "review" / "static"), "jobhunter/review/static"),  # Leaflet map library, bundled
]
datas += collect_data_files("googleapiclient")
datas += collect_data_files("certifi")
datas += collect_data_files("docx")                # python-docx templates (reading Word CVs)

# pywebview ships backends for every OS (GTK, Qt, CEF, Android): keep only this platform's (EdgeChromium/WinForms on
# Windows, Cocoa on macOS) and the shared modules
_WEBVIEW_DROP = ("webview.platforms.gtk", "webview.platforms.qt", "webview.platforms.cef", "webview.platforms.android")
_webview = [m for m in collect_submodules("webview") if not m.startswith(_WEBVIEW_DROP)]
hidden = (collect_submodules("jobhunter") + collect_submodules("keyring.backends")
          + collect_submodules("pystray") + collect_submodules("huey") + _webview + ["sqlcipher3", "sqlcipher3.dbapi2"])
if sys.platform == "win32":
    hidden += ["win11toast", "clr"]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["streamlit", "tkinter", "matplotlib", "pytest", "IPython", *_WEBVIEW_DROP, "PyQt5", "PyQt6", "PySide2",
              "PySide6", "gi", "cefpython3", "playwright"],
    noarchive=False,
)
# googleapiclient ships ~110 MB of discovery documents (its own hook collects them all); only Gmail's is needed
a.datas = [d for d in a.datas
           if not (d[0].replace("\\", "/").startswith("googleapiclient/discovery_cache/documents/")
                   and "gmail.v1" not in d[0])]
pyz = PYZ(a.pure)

VERSION_INFO = None
if sys.platform == "win32":  # what Task Manager, the taskbar and file properties call the exe
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    _v = tuple(int(x) for x in APP_VERSION.split(".")) + (0,)
    VERSION_INFO = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_v, prodvers=_v),
        kids=[StringFileInfo([StringTable("040904B0", [
                  StringStruct("FileDescription", "Lock & Land"), StringStruct("ProductName", "Lock & Land"),
                  StringStruct("CompanyName", "Lock & Land (personal)"), StringStruct("InternalName", "jobhunter"),
                  StringStruct("OriginalFilename", "jobhunter.exe"), StringStruct("FileVersion", APP_VERSION),
                  StringStruct("ProductVersion", APP_VERSION)])]),
              VarFileInfo([VarStruct("Translation", [1033, 1200])])])
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="jobhunter",
    console=False,                      # tray app: no console window
    icon=str(ASSETS / ("jobhunter.ico" if sys.platform == "win32" else "jobhunter.icns")),
    version=VERSION_INFO,
)
coll = COLLECT(exe, a.binaries, a.datas, name="jobhunter")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Lock & Land.app",
        icon=str(ASSETS / "jobhunter.icns"),
        bundle_identifier="app.jobhunter",
        version=APP_VERSION,
        info_plist={
            "CFBundleName": "Lock & Land",
            "CFBundleDisplayName": "Lock & Land",
            "LSUIElement": True,                 # menu-bar app, no Dock icon
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": APP_VERSION,
            "NSAppleEventsUsageDescription": "Lock & Land shows notifications.",
        },
    )
