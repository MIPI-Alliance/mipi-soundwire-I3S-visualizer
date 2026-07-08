<#
    SWI3S Studio launcher (Windows / PowerShell). Creates a .venv on first run, installs
    the Python deps + builds the C++ decode core, then starts the app. Re-run any time -
    the venv and build are reused. Pass -Rebuild to force a fresh build of the core.

    If you get "running scripts is disabled on this system", allow local scripts once for
    your user (does NOT lower security for downloaded/unsigned scripts):

        Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

    or run this one without changing the policy:

        powershell -ExecutionPolicy Bypass -File .\run.ps1
#>
param([switch]$Rebuild)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Pick a Python the wheels support: PySide6 / pyarrow / numpy publish wheels for
# 3.11-3.13, not 3.14+ (pip would fall back to building from source and fail). Prefer
# the newest supported via the py launcher, then fall back to `python` if it's in range.
$pyCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in "3.13", "3.12", "3.11") {
        & cmd /c "py -$v -c ""import sys"" 2>nul"
        if ($LASTEXITCODE -eq 0) { $pyCmd = "py -$v"; break }
    }
}
if (-not $pyCmd) {
    & cmd /c "python -c ""import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)"" 2>nul"
    if ($LASTEXITCODE -eq 0) { $pyCmd = "python" }
}
if (-not $pyCmd) {
    Write-Error ("Need Python 3.11-3.13 (PySide6/pyarrow/numpy have no 3.14+ wheels yet). " +
                 "Install one, e.g.:  winget install --id Python.Python.3.12 -e")
    exit 1
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment (.venv) with $pyCmd..."
    & cmd /c "$pyCmd -m venv .venv"
}
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# Install/refresh deps only when requirements.txt is newer than the stamp.
$stamp = ".venv\.deps-stamp"
if (-not (Test-Path $stamp) -or (Get-Item "requirements.txt").LastWriteTime -gt (Get-Item $stamp).LastWriteTime) {
    Write-Host "Installing Python dependencies..."
    & $python -m pip install --upgrade pip | Out-Null
    & $python -m pip install -r requirements.txt
    New-Item -ItemType File -Path $stamp -Force | Out-Null
}

# Build the native decode core if missing or if -Rebuild was asked for.
& $python -c "import swi3score" 2>$null
if ($Rebuild -or $LASTEXITCODE -ne 0) {
    Write-Host "Building the swi3score decode core (needs a C++ compiler - see the README)..."
    & $python -m pip install .\native
}

$env:PYTHONPATH = "."
& $python -m swi3s_studio.app
