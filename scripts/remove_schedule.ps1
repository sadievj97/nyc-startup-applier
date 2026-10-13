# Removes the applier's tasks from Windows Task Scheduler. Nothing else is touched.
$tasks = Get-ScheduledTask -TaskPath '\NYC Applier\' -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Output 'No NYC Applier tasks are registered.'
    return
}
$tasks | Unregister-ScheduledTask -Confirm:$false
Write-Output ('Removed {0} task(s).' -f @($tasks).Count)
