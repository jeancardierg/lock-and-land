# Registers 'jobhunter-workers': starts the local queues + review page at logon (replaces the 3-hourly 'jobhunter'
# task, which this script disables so the two don't both fetch). Run it yourself in PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\register_workers.ps1
# Undo: Unregister-ScheduledTask jobhunter-workers -Confirm:$false ; Enable-ScheduledTask jobhunter
$root = Split-Path $PSScriptRoot -Parent
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\start_workers.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "jobhunter-workers" -Action $action -Trigger $trigger -Settings $settings `
    -Description "jobhunter v0.3: local queues and review page (never sends or submits)" -Force | Out-Null
if (Get-ScheduledTask -TaskName "jobhunter" -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName "jobhunter" | Out-Null; Write-Host "Disabled the old 'jobhunter' task." }
Write-Host "Registered 'jobhunter-workers' (at logon). Start now: powershell -File scripts\start_workers.ps1"
