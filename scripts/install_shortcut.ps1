# Creates "jobhunter" shortcuts on the Desktop and in the Start menu that open the dashboard.
#   powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
$root = Split-Path $PSScriptRoot -Parent
$launch = Join-Path $PSScriptRoot "launch.ps1"
$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "jobhunter.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "jobhunter.lnk")
)
foreach ($t in $targets) {
    $s = $shell.CreateShortcut($t)
    $s.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launch`""
    $s.WorkingDirectory = $root
    $s.IconLocation = "$env:SystemRoot\System32\shell32.dll,22"
    $s.Description = "Open the jobhunter dashboard"
    $s.Save()
    Write-Host "Created $t"
}
