# Stop only the MissionDeck process recorded in .orca-runs/app.pid.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root ".orca-runs\app.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "No PID file at $PidFile"
    exit 0
}

$raw = (Get-Content -Path $PidFile -Raw).Trim()
if (-not ($raw -match '^\d+$')) {
    Write-Error "Invalid PID file contents"
    exit 1
}
$targetPid = [int]$raw

try {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $targetPid"
} catch {
    Write-Host "Process $targetPid not found"
    Remove-Item -Force $PidFile -ErrorAction SilentlyContinue
    exit 0
}

if (-not $proc) {
    Write-Host "Process $targetPid not found"
    Remove-Item -Force $PidFile -ErrorAction SilentlyContinue
    exit 0
}

$cmd = $proc.CommandLine
if ($null -eq $cmd -or $cmd -notmatch 'app\.main:app') {
    Write-Error "Refusing to stop PID $targetPid; command line does not contain app.main:app"
    exit 1
}

Stop-Process -Id $targetPid -Force
Remove-Item -Force $PidFile -ErrorAction SilentlyContinue
Write-Host "Stopped PID $targetPid"
