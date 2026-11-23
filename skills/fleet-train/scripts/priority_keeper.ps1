# Keeps the land train ahead of lane builds on a shared Windows machine. Every FLEET_PRIORITY_SECS (20):
# the train's process tree (state/train.pid, never a machine-wide command-line match) -> AboveNormal; every other
# process matching FLEET_LANE_PROC_RE -> BelowNormal, or Idle while the train marks a compile stage in
# state/train-stage.json (harness.train_stage: clippy, unit, quality; finish.py waits on the same mark).
# Measured on a pilot: with 7-8 lanes compiling, the train's quality stage took 19-28 min against 6-9 idle and its
# workspace clippy timed out; lanes also run builds outside finish.py, so priorities, not only the wait, keep it ahead.
# Run detached (Win32_Process Create 'powershell -NoProfile -File <this>' with HARNESS_HOME set); stop it by its PID.
$ErrorActionPreference = 'SilentlyContinue'
$state = Join-Path $env:HARNESS_HOME 'state'
$laneRe = if ($env:FLEET_LANE_PROC_RE) { $env:FLEET_LANE_PROC_RE } else { '^(cargo|rustc|clippy-driver|link|cc1|node)\.exe$' }
$secs = if ($env:FLEET_PRIORITY_SECS) { [int]$env:FLEET_PRIORITY_SECS } else { 20 }
$compileStages = @('clippy', 'unit', 'quality')
function Get-Tree([int]$id, $all) {
  $out = @($id)
  foreach ($p in ($all | Where-Object { $_.ParentProcessId -eq $id })) { $out += Get-Tree $p.ProcessId $all }
  return $out
}
function Set-Priority([int]$id, [string]$class) {
  $proc = Get-Process -Id $id
  if ($proc -and $proc.PriorityClass -ne $class) { $proc.PriorityClass = $class }
}
while ($true) {
  $all = Get-CimInstance Win32_Process
  $trainPid = [int](Get-Content (Join-Path $state 'train.pid') -TotalCount 1)
  $tree = if ($trainPid -and ($all | Where-Object { $_.ProcessId -eq $trainPid })) { Get-Tree $trainPid $all } else { @() }
  $mark = Get-Content (Join-Path $state 'train-stage.json') -Raw | ConvertFrom-Json
  $compiling = $mark -and ($compileStages -contains $mark.stage) -and ($all | Where-Object { $_.ProcessId -eq $mark.pid })
  $lane = if ($compiling) { 'Idle' } else { 'BelowNormal' }
  foreach ($p in $all) {
    if ($tree -contains $p.ProcessId) { Set-Priority $p.ProcessId 'AboveNormal' }
    elseif ($p.Name -match $laneRe) { Set-Priority $p.ProcessId $lane }
  }
  Start-Sleep -Seconds $secs
}
