param([switch]$NoDashboard)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
Set-Location $root
if (!(Test-Path '.venv\Scripts\python.exe')) { py -3 -m venv .venv }
if (Test-Path 'data\worker.lock') { throw 'Worker lock exists. Run .\scripts\status.ps1, then stop.ps1 if it is stale.' }
New-Item -ItemType Directory -Force data | Out-Null
Start-Process -WindowStyle Hidden -FilePath (Resolve-Path '.venv\Scripts\python.exe') -ArgumentList '-m','trading_agent.worker' -WorkingDirectory $root
if (!$NoDashboard) { $dashboard=Start-Process -WindowStyle Hidden -PassThru -FilePath (Resolve-Path '.venv\Scripts\python.exe') -ArgumentList 'local_dashboard.py' -WorkingDirectory $root; Set-Content -Path 'data\dashboard.pid' -Value $dashboard.Id; Start-Sleep -Seconds 1; Start-Process 'http://127.0.0.1:8501' }
