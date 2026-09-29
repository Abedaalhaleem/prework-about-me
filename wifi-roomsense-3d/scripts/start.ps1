# Start RoomSense in the background (Windows).
#
# Runs 'uv run roomsense serve' hidden, writes data\roomsense.pid, logs to
# logs\roomsense.log, waits until /api/health answers and prints the URL.
# Extra arguments are passed to 'roomsense serve' (e.g. --port 8766).
# Stop it with scripts\stop.ps1.
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Repo 'data\roomsense.pid'
$LogDir = Join-Path $Repo 'logs'
$LogFile = Join-Path $LogDir 'roomsense.log'
$OutFile = Join-Path $LogDir 'roomsense.out.log'
New-Item -ItemType Directory -Force -Path (Join-Path $Repo 'data'), $LogDir | Out-Null

if (Test-Path $PidFile) {
    $old = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($old -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) {
        Write-Host "RoomSense is already running (pid $old). Stop it with scripts\stop.ps1."
        exit 0
    }
    Remove-Item $PidFile -Force
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Error "start: uv not found; run scripts\setup.ps1 first"; exit 1 }

$HostName = '127.0.0.1'
$Port = '8765'
$cfg = Join-Path $Repo 'configs\roomsense.toml'
if (Test-Path $cfg) {
    foreach ($line in Get-Content $cfg) {
        if ($line -match '^\s*host\s*=\s*"([^"]*)"') { $HostName = $Matches[1] }
        if ($line -match '^\s*port\s*=\s*(\d+)') { $Port = $Matches[1] }
    }
}
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq '--host' -and $i + 1 -lt $args.Count) { $HostName = $args[$i + 1] }
    if ($args[$i] -eq '--port' -and $i + 1 -lt $args.Count) { $Port = $args[$i + 1] }
}
$CheckHost = if ($HostName -in @('0.0.0.0', '::', '')) { '127.0.0.1' } else { $HostName }
$UrlHost = if ($CheckHost.Contains(':')) { "[$CheckHost]" } else { $CheckHost }
$Url = "http://${UrlHost}:$Port"

# Keep the log bounded: rotate it (one old copy) when it has grown past 10 MB.
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 10MB)) {
    Move-Item -Force $LogFile "$LogFile.1"
}

$argList = @('run', '--frozen', 'roomsense', 'serve') + $args
$proc = Start-Process -FilePath 'uv' -ArgumentList $argList -WorkingDirectory (Join-Path $Repo 'backend') `
    -RedirectStandardError $LogFile -RedirectStandardOutput $OutFile -WindowStyle Hidden -PassThru
Set-Content -Path $PidFile -Value $proc.Id

for ($i = 0; $i -lt 60; $i++) {
    if ($proc.HasExited) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Error "RoomSense exited during startup. See $LogFile"
        exit 1
    }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health" -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $up = $true }
    } catch {
        # 401 means the server is up and a token is configured.
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) { $up = $true }
    }
    if ($up) {
        Write-Host "RoomSense is running (pid $($proc.Id)): $Url"
        Write-Host "Logs: $LogFile   Stop: scripts\stop.ps1"
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
Write-Error "RoomSense did not answer on $Url/api/health within 30 s. Check $LogFile."
exit 1
