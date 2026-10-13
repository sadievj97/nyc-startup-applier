# Runs one applier job for Windows Task Scheduler and appends its output to logs\scheduled_<year-month>.log.
# Usage (Task Scheduler does this for you):  applier_task.ps1 daily   or   applier_task.ps1 digest
# Each job checks for itself whether it is due, so running it at log on or unlock never repeats a finished run.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('daily', 'digest')]
    [string]$Job
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ('scheduled_{0:yyyy-MM}.log' -f (Get-Date))

('==== {0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Job) | Out-File -Append -Encoding utf8 $log
$env:PYTHONIOENCODING = 'utf-8'
$arguments = @('-m', 'applier', $Job, '--scheduled')
& $python @arguments 2>&1 | ForEach-Object { "$_" } | Out-File -Append -Encoding utf8 $log
('==== finished with code {0}' -f $LASTEXITCODE) | Out-File -Append -Encoding utf8 $log
