# Opens the jobhunter dashboard: starts it hidden in the background if it isn't running, then opens the browser.
# Used by the desktop / Start-menu shortcut (scripts\install_shortcut.ps1).
$root = Split-Path $PSScriptRoot -Parent
$url = "http://localhost:8501"

function Test-Up { try { (Invoke-WebRequest "$url/_stcore/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $false } }

if (-not (Test-Up)) {
    $py = Join-Path $root ".venv\Scripts\python.exe"
    Start-Process -FilePath $py -WorkingDirectory $root -WindowStyle Hidden -ArgumentList @(
        "-m", "streamlit", "run", "jobhunter\dashboard.py",
        "--server.headless", "true", "--server.address", "127.0.0.1", "--server.port", "8501",
        "--browser.gatherUsageStats", "false", "--client.toolbarMode", "minimal")
    for ($i = 0; $i -lt 40 -and -not (Test-Up); $i++) { Start-Sleep -Milliseconds 500 }
}
Start-Process $url
