# Builds dist\LockAndLand-setup-<version>.exe on Windows. Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Needs: Python 3.11 (py launcher) and Inno Setup 6 (winget install JRSoftware.InnoSetup).
param([string]$Version = "0.4.1", [switch]$SkipTests)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$venv = Join-Path $root ".build-venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) { py -3.11 -m venv $venv }
$py = "$venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements-app.txt
if ($LASTEXITCODE) { throw "pip install failed" }
& $py -c "import sqlcipher3, keyring; print('sqlcipher', sqlcipher3.connect(':memory:').execute('pragma cipher_version').fetchone()[0], '| keyring', type(keyring.get_keyring()).__name__)"
if (-not $SkipTests) { & $py -m pytest -q; if ($LASTEXITCODE) { throw "tests failed" } }
& $py -m jobhunter.desktop.icon build\assets
& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyi packaging\jobhunter.spec
if ($LASTEXITCODE) { throw "PyInstaller failed" }
# quick self-test of the frozen build in a throwaway data folder (output in build\selftest\data\console.log)
$env:JOBHUNTER_HOME = Join-Path $root "build\selftest"
$p = Start-Process dist\jobhunter\jobhunter.exe -ArgumentList "connections" -Wait -PassThru
Remove-Item Env:JOBHUNTER_HOME
if ($p.ExitCode) { throw "frozen self-test failed: see build\selftest\data\console.log" }
$w = Start-Process dist\jobhunter\jobhunter.exe -ArgumentList "--window-check" -Wait -PassThru
if ($w.ExitCode) { Write-Warning "App window (WebView2) not available in this build: pages will open in the browser" } else { Write-Host "App window: OK (WebView2)" }
Write-Host "Self-test OK (build\selftest\data\console.log)"
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { Write-Warning "Inno Setup not found: the app is in dist\jobhunter\ but no installer was made. winget install JRSoftware.InnoSetup"; exit 0 }
& $iscc /Q "/DAppVersion=$Version" packaging\jobhunter.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }
Write-Host "Installer: $root\dist\LockAndLand-setup-$Version.exe"
