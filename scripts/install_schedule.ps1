# Registers the applier's two tasks in Windows Task Scheduler, for you only, running only while you are
# logged on (the browser needs your session):
#   "NYC Applier\Daily run"  at 8:15 AM, plus at log on and on unlock (catch-up)
#   "NYC Applier\Digest"     at 7:30 AM, plus at log on and on unlock (catch-up)
# This PC uses Modern Standby, so no wake timers: a missed time runs as soon as the PC is awake again
# ("Run task as soon as possible after a scheduled start is missed", log on and unlock triggers), and each job
# checks its last successful run so it never runs twice. Submissions still happen only 08:00 to 20:00.
# Remove everything again with scripts\remove_schedule.ps1.
param(
    [string]$DigestTime = '7:30AM',
    [string]$DailyTime = '8:15AM'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$wrapper = Join-Path $PSScriptRoot 'applier_task.ps1'
$user = "$env:USERDOMAIN\$env:USERNAME"

if ((Get-TimeZone).Id -ne 'Eastern Standard Time') {
    Write-Warning 'This PC is not on New York time; the times above are local times. The jobs still check New York time themselves.'
}

function New-AppTriggers([string]$At) {
    $daily = New-ScheduledTaskTrigger -Daily -At $At
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $class = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
    $unlock = New-CimInstance -CimClass $class -ClientOnly
    $unlock.StateChange = 8   # 8 = workstation unlock
    $unlock.UserId = $user
    $unlock.Enabled = $true
    return @($daily, $logon, $unlock)
}

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

$jobs = @(
    @{ Name = 'Daily run'; Job = 'daily'; At = $DailyTime; About = 'NYC startup applier: find, score, email, prepare, submit (rules checked in the app).' },
    @{ Name = 'Digest'; Job = 'digest'; At = $DigestTime; About = 'NYC startup applier: write the morning digest and show urgent notifications.' }
)
foreach ($j in $jobs) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $root `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`" $($j.Job)"
    Register-ScheduledTask -TaskPath '\NYC Applier\' -TaskName $j.Name -Action $action -Trigger (New-AppTriggers $j.At) `
        -Settings $settings -Principal $principal -Description $j.About -Force | Out-Null
    Write-Output ("Registered: NYC Applier\{0} at {1}, at log on and on unlock" -f $j.Name, $j.At)
}
