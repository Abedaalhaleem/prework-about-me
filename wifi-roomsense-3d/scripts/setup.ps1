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
        # Project-local npm cache: a shared npm cache with files owned by another
        # user makes 'npm ci' fail with EACCES; a cache inside the project never does.
        $NpmCache = Join-Path $Repo '.cache\npm'
        New-Item -ItemType Directory -Force -Path $NpmCache | Out-Null
        npm ci --cache $NpmCache --no-audit --no-fund; if ($LASTEXITCODE -ne 0) { Fail "npm ci failed (see the npm error above)" }
        npm run build; if ($LASTEXITCODE -ne 0) { Fail "npm run build failed" }
    } finally { Pop-Location }
} else {
    Info "skipping the web UI build (-SkipFrontend)"
}

# --- config and local folders -----------------------------------------------
$cfg = Join-Path $Repo 'configs\roomsense.toml'
if (-not (Test-Path $cfg)) {
    # The example ships its receiver blocks commented out (their ports are only
    # samples), so it is copied verbatim. It is checked first: it must load, and
    # it must not configure a receiver, because ports are never guessed.
    $tmp = Join-Path $Repo 'configs\.roomsense.toml.tmp'
    Copy-Item -LiteralPath (Join-Path $Repo 'configs\roomsense.example.toml') -Destination $tmp -Force
    Push-Location (Join-Path $Repo 'backend')
    try {
        uv run --frozen python -c "import sys; from roomsense.config import load_config; c = load_config(sys.argv[1]); sys.exit(0 if not c.acquisition.receivers else 1)" $tmp
        $ok = ($LASTEXITCODE -eq 0)
    } finally { Pop-Location }
    if (-not $ok) { Remove-Item $tmp -Force; Fail "configs\roomsense.example.toml does not load, or it configures a receiver with a sample port; copy it to configs\roomsense.toml by hand and set the receiver ports" }
    Move-Item $tmp $cfg
    Info "created configs\roomsense.toml from the example. No receiver is configured yet: uncomment one [[acquisition.receivers]] block there and set your board's serial port, e.g. COM5 (list ports with: cd backend; uv run roomsense ports)"
} else {
    Info "configs\roomsense.toml exists; left unchanged"
}
New-Item -ItemType Directory -Force -Path (Join-Path $Repo 'data'), (Join-Path $Repo 'logs') | Out-Null
Info "done. Start with scripts\start.ps1"
