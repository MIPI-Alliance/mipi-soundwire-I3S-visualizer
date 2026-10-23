<#
    Set up a checkout for testing (Windows / PowerShell): a venv with requirements-dev.txt
    installed and the swi3score decode core built into it. Re-run any time; it refreshes both.

        .\tools\setup_dev.ps1           # install
        .\tools\setup_dev.ps1 -Test     # install, then run the functional suite

    The venv is the one .\run.ps1 uses (same location, same SWI3S_VENV override), so the app
    and the tests share one install. If scripts are disabled on this system, run it with
    powershell -ExecutionPolicy Bypass -File .\tools\setup_dev.ps1
#>
param([switch]$Test)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -Path $repo

# The interpreters run.ps1 supports: 3.11-3.13, newest first via the py launcher.
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
    Write-Error "Need Python 3.11-3.13. Install one, e.g.:  winget install --id Python.Python.3.12 -e"
    exit 1
}

# The decode core is C++ and needs the interpreter's C API headers (Python.h).
$probe = "import os,sysconfig,sys; sys.exit(0 if os.path.isfile(os.path.join(sysconfig.get_paths()['include'],'Python.h')) else 1)"
& cmd /c "$pyCmd -c ""$probe"" 2>nul"
if (($LASTEXITCODE -ne 0) -and (-not $env:SKBUILD_CMAKE_DEFINE)) {
    Write-Error ("$pyCmd has no Python.h, which the decode core build needs. Install Python " +
                 "from python.org (the full installer); .\run.ps1 prints the full list of options.")
    exit 1
}

# Same location as run.ps1: off any cloud-synced folder, keyed to this checkout's path.
$sha = [System.Security.Cryptography.SHA1]::Create()
$key = ([System.BitConverter]::ToString($sha.ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes($repo))) -replace '-', '').Substring(0, 12).ToLower()
$venvDir = if ($env:SWI3S_VENV) { $env:SWI3S_VENV } else { Join-Path $env:LOCALAPPDATA "swi3s-studio\venv-$key" }
New-Item -ItemType Directory -Force -Path (Split-Path $venvDir) | Out-Null
if (-not (Test-Path $venvDir)) {
    Write-Host "Creating virtual environment with $pyCmd at $venvDir ..."
    & cmd /c "$pyCmd -m venv ""$venvDir"""
}
$python = Join-Path $venvDir "Scripts\python.exe"

function Invoke-Checked([string]$what) {
    if ($LASTEXITCODE -ne 0) { Write-Error "$what failed (exit $LASTEXITCODE)"; exit 1 }
}

Write-Host "Installing development dependencies (requirements-dev.txt)..."
& $python -m pip install --upgrade pip | Out-Null
Invoke-Checked "pip upgrade"
& $python -m pip install -r requirements-dev.txt
Invoke-Checked "pip install -r requirements-dev.txt"

Write-Host "Building the swi3score decode core..."
# --force-reinstall: the core's version is fixed, so a plain install keeps a stale build.
& $python -m pip install --force-reinstall --no-deps .\native
Invoke-Checked "the decode core build"
# run.ps1's stamps: the dependencies and the core it would install are now current.
foreach ($s in ".deps-stamp", ".native-stamp") {
    New-Item -ItemType File -Path (Join-Path $venvDir $s) -Force | Out-Null
}

$rootCore = & $python -c "import sysconfig; print('swi3score' + sysconfig.get_config_var('EXT_SUFFIX'))"
if (Test-Path $rootCore) {
    Write-Host "Note: .\$rootCore at the checkout root is imported before the venv's build."
    Write-Host "  Delete it to use the venv's build; tests\gate.ps1 rebuilds it."
}

Write-Host ""
Write-Host "Ready. From the checkout root, the functional suite (perf deselected), the perf"
Write-Host "benchmarks in their own process, and the release gate (suite, perf, ruff, mypy, ABI):"
Write-Host "  & `"$venvDir\Scripts\Activate.ps1`""
Write-Host "  python -m pytest"
Write-Host "  python -m pytest -m perf"
Write-Host "  `$env:PYTHON = `"$python`"; .\tests\gate.ps1"
Write-Host "See docs\TESTING.md."

if ($Test) {
    & $python -m pytest -q
    exit $LASTEXITCODE
}
