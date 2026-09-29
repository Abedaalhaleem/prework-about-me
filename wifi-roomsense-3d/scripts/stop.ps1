# Stop RoomSense started by scripts\start.ps1 (Windows).
#
# Windows has no SIGTERM. The graceful path sends a console Ctrl+C to the
# server's own (hidden) console from a short-lived helper process, so the app
# stops the source, closes serial ports, finalises an active recording and
# closes the database. If it has not exited within 15 s, the process tree is
# terminated forcefully with a warning. Removes data\roomsense.pid.
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Repo 'data\roomsense.pid'
$GraceSeconds = 15

if (-not (Test-Path $PidFile)) { Write-Host "RoomSense is not running (no $PidFile)."; exit 0 }
$pidText = (Get-Content $PidFile | Select-Object -First 1)
if (-not ($pidText -match '^\d+$')) { Remove-Item $PidFile -Force; Write-Error "stop: invalid pid file removed"; exit 1 }
$serverPid = [int]$pidText
$proc = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
if (-not $proc) { Remove-Item $PidFile -Force; Write-Host "RoomSense is not running (stale pid $serverPid)."; exit 0 }
if ($proc.ProcessName -notmatch '^(uv|python|pythonw|roomsense)$') {
    Write-Error "stop: pid $serverPid is '$($proc.ProcessName)', not RoomSense; not stopping it. Remove $PidFile if stale."
    exit 1
}

Write-Host "Stopping RoomSense (pid $serverPid)..."
# The helper attaches to the server's console and sends Ctrl+C there. It runs
# in its own process because detaching from a console would break this one.
$helper = @"
Add-Type -Namespace RoomSense -Name Con -MemberDefinition '
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool AttachConsole(uint p);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool FreeConsole();
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleCtrlHandler(System.IntPtr h, bool add);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool GenerateConsoleCtrlEvent(uint e, uint g);
'
[RoomSense.Con]::FreeConsole() | Out-Null
if ([RoomSense.Con]::AttachConsole($serverPid)) {
    [RoomSense.Con]::SetConsoleCtrlHandler([System.IntPtr]::Zero, `$true) | Out-Null
    [RoomSense.Con]::GenerateConsoleCtrlEvent(0, 0) | Out-Null
    Start-Sleep -Milliseconds 500
    [RoomSense.Con]::FreeConsole() | Out-Null
}
"@
$shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell' }
try {
    Start-Process -FilePath $shell -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', $helper) -WindowStyle Hidden -Wait
} catch {
    Write-Warning "could not send Ctrl+C to RoomSense: $($_.Exception.Message)"
}

$deadline = (Get-Date).AddSeconds($GraceSeconds)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "RoomSense stopped."
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

Write-Warning "RoomSense did not stop within $GraceSeconds s; terminating it forcefully."
Write-Warning "An active recording is left without its closing record. It stays replayable and is marked as interrupted (ERROR) at the next start."
& taskkill.exe /PID $serverPid /T /F | Out-Null
Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
exit 1
