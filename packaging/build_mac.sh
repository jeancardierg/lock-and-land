#!/usr/bin/env bash
# Builds dist/LockAndLand-<version>.dmg (the app is "Lock & Land.app"; internal name jobhunter) on a Mac (Apple Silicon or Intel builds for that architecture only).
#   bash packaging/build_mac.sh
# Needs: Python 3.11 (python.org installer or `brew install python@3.11`).
# Not signed/notarized: first launch needs System Settings > Privacy & Security > Open Anyway (right-click > Open no
# longer works on macOS 15+). If macOS says the app "is damaged": xattr -cr "/Applications/Lock & Land.app". See README.
# To sign: set CODESIGN_ID="Developer ID Application: ..." (Apple Developer account) and notarize with notarytool.
set -euo pipefail
VERSION="${1:-0.4.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
APP="dist/Lock & Land.app"
PY="${PYTHON:-python3.11}"
[ -x .build-venv/bin/python ] || "$PY" -m venv .build-venv
.build-venv/bin/python -m pip install --quiet --upgrade pip
.build-venv/bin/python -m pip install --quiet -r requirements-app.txt
.build-venv/bin/python -c "import sqlcipher3; print('sqlcipher', sqlcipher3.connect(':memory:').execute('pragma cipher_version').fetchone()[0])"
[ "${SKIP_TESTS:-0}" = 1 ] || .build-venv/bin/python -m pytest -q
.build-venv/bin/python -m jobhunter.desktop.icon build/assets
iconutil -c icns build/assets/jobhunter.iconset -o build/assets/jobhunter.icns
.build-venv/bin/python -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyi packaging/jobhunter.spec
if [ -n "${CODESIGN_ID:-}" ]; then
  codesign --force --deep --options runtime --timestamp -s "$CODESIGN_ID" "$APP"
else
  codesign --force --deep -s - "$APP"   # ad-hoc signature (required on Apple Silicon)
fi
"$APP/Contents/MacOS/jobhunter" --window-check || echo "WARNING: app window (WebKit) not available in this build"
rm -rf build/dmg && mkdir -p build/dmg && cp -R "$APP" build/dmg/ && ln -s /Applications build/dmg/Applications
hdiutil create -volname "Lock & Land" -srcfolder build/dmg -ov -format UDZO "dist/LockAndLand-$VERSION.dmg"
echo "DMG: $ROOT/dist/LockAndLand-$VERSION.dmg"
