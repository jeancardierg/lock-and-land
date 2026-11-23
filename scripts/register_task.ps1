# Registers a Windows scheduled task that runs a jobhunter cycle every N hours (default 3)
# while you're logged in. Never applies/sends; only fills the approval queue.
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 [-Hours 3]
param([int]$Hours = 3)
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
$action = New-ScheduledTaskAction -Execute $python -Argument "-m jobhunter run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours $Hours)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "jobhunter" -Action $action -Trigger $trigger -Settings $settings `
    -Description "jobhunter: fetch/score/draft jobs and recruiter replies" -Force | Out-Null
Write-Host "Scheduled 'jobhunter' every $Hours h. Remove with: Unregister-ScheduledTask jobhunter -Confirm:`$false"
