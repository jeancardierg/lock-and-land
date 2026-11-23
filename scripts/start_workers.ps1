# Starts the local queues (ingest, GPU letters) and the review page, hidden, if not running.
# Used at logon by the 'jobhunter-workers' task (scripts\register_workers.ps1). Nothing here sends or submits.
$root = Split-Path $PSScriptRoot -Parent
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$hb = Join-Path $root "data\workers.heartbeat"
$alive = (Test-Path $hb) -and ((Get-Date) - (Get-Item $hb).LastWriteTime).TotalSeconds -lt 20
if (-not $alive) { Start-Process -FilePath $pyw -WorkingDirectory $root -ArgumentList "-m", "jobhunter", "workers" }
$up = $false
try { $up = (Invoke-WebRequest "http://127.0.0.1:8502/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}
if (-not $up) { Start-Process -FilePath $pyw -WorkingDirectory $root -ArgumentList "-m", "jobhunter", "review" }
Write-Host "jobhunter workers and review page running. Review: http://127.0.0.1:8502"
