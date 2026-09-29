# WiFi RoomSense 3D: one-time setup (Windows PowerShell 5.1+ / PowerShell 7).
#
# * checks Python >= 3.11, Node.js >= 22.12 and uv (explains how to get them;
#   installs nothing system-wide and needs no administrator rights)
# * installs the backend's pinned dependencies:  cd backend; uv sync --frozen
# * builds the web UI:                          cd frontend; npm ci; npm run build
# * copies configs\roomsense.example.toml to configs\roomsense.toml only if missing
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-SkipFrontend]
param([switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot

function Fail([string]$msg) { Write-Error "setup: $msg"; exit 1 }
function Info([string]$msg) { Write-Host "setup: $msg" }

# --- uv ---------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "uv is required to install the pinned backend dependencies. See https://docs.astral.sh/uv/getting-started/installation/ (installs per user; no administrator rights)."
}
Info "uv OK ($(uv --version))"

# --- Python -----------------------------------------------------------------
# Either a Python >= 3.11 on PATH or one managed by uv is fine: uv sync picks it.
$py = $null
foreach ($cand in @('python', 'py', 'python3')) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
}
$pyOk = $false
if ($py) {
    & $py -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    $pyOk = ($LASTEXITCODE -eq 0)
}
if ($pyOk) {
    Info "Python OK ($(& $py -V 2>&1))"
} else {
    $uvPy = (uv python find '>=3.11' 2>$null)
    if ($LASTEXITCODE -eq 0 -and $uvPy) {
        Info "Python OK (found by uv: $uvPy)"
    } else {
        Fail "Python 3.11 or newer is required. Install it from python.org, or let uv provide one (per user): 'uv python install 3.11'."
    }
}

# --- Node.js ----------------------------------------------------------------
if (-not $SkipFrontend) {
    if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Fail "Node.js 22.12 or newer (with npm) is required to build the web UI. Install it from nodejs.org, or re-run with -SkipFrontend (the API still works)."
    }
    $nodeV = (node -v).TrimStart('v')
    $parts = $nodeV.Split('.')
    $major = [int]$parts[0]; $minor = [int]$parts[1]
    if (-not ($major -gt 22 -or ($major -eq 22 -and $minor -ge 12))) { Fail "Node.js >= 22.12 is required (found $nodeV)." }
    Info "Node.js OK ($nodeV)"
}

# --- backend ----------------------------------------------------------------
Info "installing backend dependencies (uv sync --frozen)"
Push-Location (Join-Path $Repo 'backend')
try { uv sync --frozen; if ($LASTEXITCODE -ne 0) { Fail "uv sync failed" } } finally { Pop-Location }

# --- frontend ---------------------------------------------------------------
if (-not $SkipFrontend) {
    Info "building the web UI (npm ci; npm run build)"
    Push-Location (Join-Path $Repo 'frontend')
    try {
        npm ci; if ($LASTEXITCODE -ne 0) { Fail "npm ci failed" }
        npm run build; if ($LASTEXITCODE -ne 0) { Fail "npm run build failed" }
    } finally { Pop-Location }
} else {
    Info "skipping the web UI build (-SkipFrontend)"
}

# --- config and local folders -----------------------------------------------
$cfg = Join-Path $Repo 'configs\roomsense.toml'
if (-not (Test-Path $cfg)) {
    # The example's receiver block names a sample Linux port (/dev/ttyUSB0). Copy it
    # commented out, so no receiver looks configured until the user sets a real
    # port (e.g. COM5). Ports are never guessed.
    $out = New-Object System.Collections.Generic.List[string]
    $inRx = $false; $noted = $false
    foreach ($line in Get-Content (Join-Path $Repo 'configs\roomsense.example.toml')) {
        if ($line -match '^\[\[acquisition\.receivers\]\]') {
            if (-not $noted) {
                $out.Add("# NOT CONFIGURED YET: set each receiver's serial port (list ports with: cd backend; uv run roomsense ports),")
                $out.Add('# then remove the leading "# " from its block. Until then no receiver is configured and')
                $out.Add('# starting the LIVE source is refused with NO_RECEIVERS_CONFIGURED.')
                $noted = $true
            }
            $inRx = $true
            $out.Add("# $line")
            continue
        }
        if ($inRx -and ($line -match '^\s*$' -or $line -match '^\[')) { $inRx = $false }
        if ($inRx) { $out.Add("# $line") } else { $out.Add($line) }
    }
    $tmp = Join-Path $Repo 'configs\.roomsense.toml.tmp'
    [System.IO.File]::WriteAllLines($tmp, $out)
    Push-Location (Join-Path $Repo 'backend')
    try {
        uv run --frozen python -c "import sys; from roomsense.config import load_config; c = load_config(sys.argv[1]); sys.exit(0 if not c.acquisition.receivers else 1)" $tmp
        $ok = ($LASTEXITCODE -eq 0)
    } finally { Pop-Location }
    if (-not $ok) { Remove-Item $tmp -Force; Fail "could not prepare configs\roomsense.toml; copy configs\roomsense.example.toml by hand and edit the receiver ports" }
    Move-Item $tmp $cfg
    Info "created configs\roomsense.toml from the example with the receiver block commented out; set your boards' serial ports (e.g. COM5) there"
} else {
    Info "configs\roomsense.toml exists; left unchanged"
}
New-Item -ItemType Directory -Force -Path (Join-Path $Repo 'data'), (Join-Path $Repo 'logs') | Out-Null
Info "done. Start with scripts\start.ps1"
