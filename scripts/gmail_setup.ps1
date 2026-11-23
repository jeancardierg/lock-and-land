# Guided Gmail OAuth setup for jobhunter. Run from the jobhunter folder:
#   powershell -ExecutionPolicy Bypass -File scripts\gmail_setup.ps1
# What it automates: Google Cloud project + Gmail API (if gcloud is installed), opening the exact console pages,
# picking up the downloaded client JSON, and the final consent run. What you click yourself: Google sign-in,
# the OAuth consent screen form, "Create client", and "Allow". Nothing here stores or sees your password.
param([string]$ProjectId = "")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$creds = "data\credentials.json"
New-Item -ItemType Directory -Force data | Out-Null

function Pause-Step($msg) { Write-Host "`n$msg" -ForegroundColor Cyan; Read-Host "Press Enter when done" | Out-Null }

if (Test-Path $creds) {
    Write-Host "$creds already exists, skipping to authorisation."
} else {
    $gcloud = Get-Command gcloud -ErrorAction SilentlyContinue
    if ($gcloud) {
        if (-not $ProjectId) { $ProjectId = "jobhunter-" + (Get-Random -Minimum 100000 -Maximum 999999) }
        Write-Host "Using gcloud. Sign in with your Google account if a browser opens." -ForegroundColor Cyan
        $acct = (& gcloud auth list --filter=status:ACTIVE --format="value(account)") 2>$null
        if (-not $acct) { & gcloud auth login }
        $exists = (& gcloud projects describe $ProjectId --format="value(projectId)") 2>$null
        if (-not $exists) { & gcloud projects create $ProjectId --name="jobhunter" }
        & gcloud services enable gmail.googleapis.com --project $ProjectId
        Write-Host "Project $ProjectId ready, Gmail API enabled." -ForegroundColor Green
    } else {
        Write-Host "gcloud not found: doing the project in the browser instead." -ForegroundColor Yellow
        Start-Process "https://console.cloud.google.com/projectcreate"
        Pause-Step "Create a project named 'jobhunter' (no organisation needed). Then come back here."
        $ProjectId = Read-Host "Paste the project ID shown on the dashboard (e.g. jobhunter-123456)"
        Start-Process "https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=$ProjectId"
        Pause-Step "Click ENABLE on the Gmail API page."
    }

    Start-Process "https://console.cloud.google.com/auth/overview/create?project=$ProjectId"
    Pause-Step @"
OAuth consent screen (Google Auth Platform):
  App name: jobhunter   User support email: your Gmail
  Audience: External     Contact email: your Gmail   -> agree and Create
"@
    Start-Process "https://console.cloud.google.com/auth/audience?project=$ProjectId"
    Pause-Step @"
Audience page: click PUBLISH APP and confirm (stays unverified, only you use it).
This stops the token expiring every 7 days. If you'd rather stay in Testing, add your Gmail under Test users instead.
"@
    Start-Process "https://console.cloud.google.com/auth/clients/create?project=$ProjectId"
    $since = Get-Date
    Pause-Step @"
Create client: Application type = Desktop app, Name = jobhunter -> Create -> DOWNLOAD JSON.
"@
    $dl = Join-Path $env:USERPROFILE "Downloads"
    $json = Get-ChildItem $dl -Filter "client_secret_*.json" -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -ge $since.AddMinutes(-10) } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $json) {
        $path = Read-Host "Couldn't find client_secret_*.json in Downloads. Paste the full path of the downloaded file"
        $json = Get-Item $path
    }
    Move-Item $json.FullName $creds -Force
    Write-Host "Saved OAuth client to $creds (moved out of Downloads)." -ForegroundColor Green
}

Write-Host "`nNow authorising: a browser opens, pick your Gmail, click Continue past 'Google hasn't verified this app', then Allow." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m jobhunter gmail-auth
