# Start MissionDeck on loopback only (foreground).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Runs = Join-Path $Root ".orca-runs"
New-Item -ItemType Directory -Force -Path $Runs | Out-Null
$PidFile = Join-Path $Runs "app.pid"

$python = Get-Command python -ErrorAction Stop
$args = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8765")

$proc = Start-Process -FilePath $python.Source -ArgumentList $args -WorkingDirectory $Root -PassThru -NoNewWindow
$proc.Id | Set-Content -Path $PidFile -Encoding ascii
Write-Host "MissionDeck PID $($proc.Id) written to $PidFile"
Wait-Process -Id $proc.Id
