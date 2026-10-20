$root=Split-Path -Parent $PSScriptRoot
$lock=Join-Path $root 'data\worker.lock'
if (Test-Path $lock) { $pidText=Get-Content $lock; $p=Get-Process -Id $pidText -ErrorAction SilentlyContinue; if ($p) { Stop-Process -Id $p.Id }; Remove-Item $lock -Force; 'Worker stopped.' } else { 'No worker lock found.' }
$dashboardPid=Join-Path $root 'data\dashboard.pid'
if (Test-Path $dashboardPid) { $pidText=Get-Content $dashboardPid; $p=Get-Process -Id $pidText -ErrorAction SilentlyContinue; if ($p) { Stop-Process -Id $p.Id }; Remove-Item $dashboardPid -Force; 'Dashboard stopped.' }
