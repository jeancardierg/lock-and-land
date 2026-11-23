# Stops jobhunter workers and the review page (any python process running "jobhunter workers" or "jobhunter review").
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match "-m jobhunter (workers|review)" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped $($_.ProcessId)" }
# forget the last heartbeat, or start_workers.ps1 would think the workers are still alive for a minute
Remove-Item (Join-Path (Split-Path $PSScriptRoot -Parent) "data\workers.heartbeat") -ErrorAction SilentlyContinue
