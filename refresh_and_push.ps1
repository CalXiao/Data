<#
    refresh_and_push.ps1 — daily: refresh every feed in this repo, then commit & push.

    This is the "boss side": run it on the machine with Bloomberg + Citi creds. It
    pulls the latest of each source (public APIs need only internet; econ surveys +
    CitiVelocity need BBG/Citi), then commits and pushes so the desk can `git pull`.

    Run:      powershell -ExecutionPolicy Bypass -File .\refresh_and_push.ps1
    Options:  -Python <path>   python to use (default: python on PATH)
              -SkipBBG         skip the Bloomberg econ-survey pull
              -SkipCiti        skip the CitiVelocity daily refresh
              -NoPush          refresh + commit locally but don't push
    Schedule: see register_refresh_task.ps1
#>
param(
    [string]$Python = "python",
    [switch]$SkipBBG,
    [switch]$SkipCiti,
    [switch]$NoPush
)
$ErrorActionPreference = "Continue"
$Repo = $PSScriptRoot
# Log OUTSIDE the repo so the daily run doesn't commit its own logs.
$LogDir = Join-Path $env:LOCALAPPDATA "DataRefresh"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("refresh_{0}.log" -f (Get-Date -Format yyyyMMdd))

function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Add-Content -Path $Log -Value $line; Write-Output $line
}

# Public-API fixings (internet only) + Bloomberg econ surveys.
$pulls = @(
    "nyfed\sofr_fixings_pull.py",
    "boc\corra_fixings_pull.py",
    "boe\sonia_fixings_pull.py",
    "boj\tona_fixings_pull.py",
    "ecb\estr_fixings_pull.py",
    "rba\aonia_fixings_pull.py"
)
if (-not $SkipBBG) { $pulls += "bbg_econ\econ_surveys_pull.py" }

Log "=== refresh start (python=$Python) ==="
git -C $Repo pull --ff-only 2>&1 | ForEach-Object { Log "git pull: $_" }

foreach ($p in $pulls) {
    $full = Join-Path $Repo $p
    if (-not (Test-Path $full)) { Log "SKIP (missing): $p"; continue }
    Log "pull: $p"
    & $Python $full 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) { Log "  WARN: $p exited $LASTEXITCODE" }
}

if (-not $SkipCiti) {
    $citi = Join-Path $Repo "CitiVelocity\historical\run_daily.bat"
    if (Test-Path $citi) { Log "pull: CitiVelocity daily"; & cmd /c "`"$citi`"" 2>&1 | ForEach-Object { Log "  $_" } }
}

# Commit & push whatever changed.
$dirty = git -C $Repo status --porcelain
if ([string]::IsNullOrWhiteSpace($dirty)) {
    Log "no changes to commit"
} else {
    git -C $Repo add -A 2>&1 | Out-Null
    $msg = "daily data {0}" -f (Get-Date -Format "yyyy-MM-dd")
    git -C $Repo commit -m $msg 2>&1 | ForEach-Object { Log "commit: $_" }
    if (-not $NoPush) {
        git -C $Repo push 2>&1 | ForEach-Object { Log "push: $_" }
        if ($LASTEXITCODE -ne 0) { Log "PUSH FAILED (exit $LASTEXITCODE)" }
    } else { Log "committed locally (--NoPush)" }
}
Log "=== refresh done ==="
