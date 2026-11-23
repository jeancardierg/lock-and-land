# One-time setup on Windows. Run from the jobhunter folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .venv\Scripts\python.exe)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 -m venv .venv
        if ($LASTEXITCODE -ne 0) { & py -3 -m venv .venv }
    } else { & python -m venv .venv }
}
if (-not (Test-Path .venv\Scripts\python.exe)) { throw "venv creation failed - install Python 3.11+" }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path config.yaml)) { Copy-Item config.example.yaml config.yaml }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
New-Item -ItemType Directory -Force data | Out-Null

# v0.3 local models (free, run on the GPU). qwen2.5:14b writes, the 7B extracts and checks, bge-m3 embeds.
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    foreach ($m in @("qwen2.5:14b", "qwen2.5:7b-instruct", "bge-m3")) {
        if (-not ((& ollama list) -match [regex]::Escape($m))) { & ollama pull $m }
    }
} else { Write-Warning "Ollama not found: install it from ollama.com, then rerun this script." }

# Migrate the database (makes a backup first) and check the master profile.
.\.venv\Scripts\python.exe -m jobhunter migrate
.\.venv\Scripts\python.exe -m jobhunter profile-check
Write-Host "`nDone. Next: edit config.yaml and .env, fill in contact details in the CV PDFs in ..\CV, then run:"
Write-Host "  .\.venv\Scripts\python.exe -m jobhunter doctor"
Write-Host 'Then read facts\master_profile.json, fix anything wrong and set "status": "APPROVED".'
Write-Host "Start the queues and review page: scripts\review.cmd  (at logon: scripts\register_workers.ps1)"
