<#
    SWI3S Studio launcher (Windows / PowerShell). Creates a local venv on first run, installs
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

# Does an interpreter have the C API headers (Python.h)? The decode core is C++, so a
# build needs them. The python.org and Store builds ship them, but an embeddable
# distribution or a stripped/managed install may not — and without this the failure lands
# ~20 lines into compiling bindings.cpp as "fatal error: Python.h: No such file or
# directory". sysconfig reports the BASE prefix, so a venv answers for its parent, which
# is correct: a venv doesn't supply headers.
function Test-PythonHeaders([string]$exe) {
    # Single quotes for the Python string literals and doubled "" for the cmd-level
    # quoting — the same idiom as the version probes above. (Using \" here instead broke
    # the argument and reported FALSE on a machine whose Python.h was present, which
    # would have refused to build on every healthy Windows install.)
    $probe = "import os,sysconfig,sys; sys.exit(0 if os.path.isfile(os.path.join(sysconfig.get_paths()['include'],'Python.h')) else 1)"
    # Callers pass the interpreter UNQUOTED — either a path ("...\python.exe", which may
    # contain spaces and so needs quoting) or a launcher invocation ("py -3.12", which
    # must NOT be quoted or cmd treats the whole thing as one executable name).
    $invoke = if ($exe -match '[\\/]') { "`"$exe`"" } else { $exe }
    & cmd /c "$invoke -c ""$probe"" 2>nul"
    return ($LASTEXITCODE -eq 0)
}

# Printed when a build is needed but the headers are absent.
function Show-HeaderHelp([string]$exe) {
    Write-Host ""
    Write-Host "The decode core is C++ and needs the Python development headers (Python.h)."
    Write-Host "  Interpreter : $exe"
    Write-Host ""
    Write-Host "The interpreter works; only its C API headers are missing. A virtualenv does"
    Write-Host "NOT supply them (it inherits its base prefix)."
    Write-Host ""
    Write-Host "Fixes:"
    Write-Host "  * Reinstall Python from python.org - the standard installer includes the"
    Write-Host "    headers (tick 'Download debug binaries' / the development files if asked),"
    Write-Host "    or:  winget install --id Python.Python.3.12 -e"
    Write-Host "  * An 'embeddable package' zip has NO headers - use the full installer."
    Write-Host "  * Already have headers elsewhere? Point the build at them:"
    Write-Host "      `$env:SKBUILD_CMAKE_DEFINE = 'Python_INCLUDE_DIR=C:\path\to\include'"
    Write-Host "See the README section 'No root / no sudo' for the cross-platform detail."
}

# Keep the venv OFF any cloud-synced folder (OneDrive): Qt cannot enumerate its plugin
# directories on those, so the GUI fails with "Could not find the Qt platform plugin
# windows" even though the plugins are present. Put it in a local cache dir keyed to this
# checkout's path (override with $env:SWI3S_VENV).
$sha = [System.Security.Cryptography.SHA1]::Create()
$key = ([System.BitConverter]::ToString($sha.ComputeHash(
    [System.Text.Encoding]::UTF8.GetBytes($PSScriptRoot))) -replace '-', '').Substring(0, 12).ToLower()
$venvDir = if ($env:SWI3S_VENV) { $env:SWI3S_VENV } else { Join-Path $env:LOCALAPPDATA "swi3s-studio\venv-$key" }
New-Item -ItemType Directory -Force -Path (Split-Path $venvDir) | Out-Null

if (-not (Test-Path $venvDir)) {
    # No venv yet => the native core will certainly have to be built. Check the headers
    # NOW rather than after creating the venv and downloading ~200 MB of wheels: on a
    # headerless install that work is wasted and the real error arrives minutes later.
    # Skipped when SKBUILD_CMAKE_DEFINE already points the build at headers elsewhere.
    if ((-not $env:SKBUILD_CMAKE_DEFINE) -and (-not (Test-PythonHeaders $pyCmd))) {
        Show-HeaderHelp $pyCmd
        Write-Error "Cannot build the decode core with $pyCmd - Python.h is missing."
        exit 1
    }
    Write-Host "Creating virtual environment with $pyCmd at $venvDir ..."
    & cmd /c "$pyCmd -m venv ""$venvDir"""
}
$python = Join-Path $venvDir "Scripts\python.exe"

# Install/refresh deps only when requirements.txt is newer than the stamp.
$stamp = Join-Path $venvDir ".deps-stamp"
$installedDeps = $false
if (-not (Test-Path $stamp) -or (Get-Item "requirements.txt").LastWriteTime -gt (Get-Item $stamp).LastWriteTime) {
    Write-Host "Installing Python dependencies..."
    & $python -m pip install --upgrade pip | Out-Null
    & $python -m pip install -r requirements.txt
    New-Item -ItemType File -Path $stamp -Force | Out-Null
    $installedDeps = $true
}

# Build the native decode core when it's missing, out of date, or -Rebuild was asked
# for. "Out of date" = a native\ source is newer than the last build (a stamp in the
# venv), so a git pull or edit that touches the C++ auto-rebuilds - the user never has
# to know a rebuild is needed. (build\ artefacts are excluded from the staleness check.)
$nativeStamp = Join-Path $venvDir ".native-stamp"
$needNative = [bool]$Rebuild
if (-not $needNative) {
    # Probe whether the native core imports. This is EXPECTED to fail when the .pyd is
    # stale/missing (e.g. right after a source sync) — that's the signal to rebuild. Run
    # it via `cmd /c ... 2>nul` (like the Python-version probes above) so the traceback on
    # stderr can't trip $ErrorActionPreference='Stop' and abort the launcher before the
    # build step; we only care about the exit code.
    & cmd /c """$python"" -c ""import swi3score"" 2>nul"
    if ($LASTEXITCODE -ne 0) { $needNative = $true }
}
if (-not $needNative) {
    if (-not (Test-Path $nativeStamp)) {
        $needNative = $true
    } else {
        $stampTime = (Get-Item $nativeStamp).LastWriteTime
        $newer = Get-ChildItem native -Recurse -File -ErrorAction SilentlyContinue |
                 Where-Object { $_.FullName -notmatch '\\build\\' -and $_.LastWriteTime -gt $stampTime } |
                 Select-Object -First 1
        if ($newer) { $needNative = $true }
    }
}
if ($needNative) {
    # Same check as the fresh-venv path, for the cases that reach a build with the venv
    # already in place (-Rebuild, or a native\ edit).
    if ((-not $env:SKBUILD_CMAKE_DEFINE) -and (-not (Test-PythonHeaders $python))) {
        Show-HeaderHelp $python
        Write-Error "Cannot build the decode core - Python.h is missing."
        exit 1
    }
    Write-Host "Building the swi3score decode core (needs a C++ compiler - see the README)..."
    # --force-reinstall because the native version is a fixed 0.1.0: a plain install
    # no-ops ("Requirement already satisfied") and silently keeps a stale .pyd after a
    # source change or a git-archive sync, which then trips the ABI check at import.
    # --no-deps keeps it from re-resolving/reinstalling the app deps on every rebuild.
    & $python -m pip install --force-reinstall --no-deps .\native
    if ($LASTEXITCODE -ne 0) {
        # Don't guess at the cause (this used to assert a missing C++ compiler; the
        # most common real failure is a working compiler but no Python headers).
        Write-Error @"
swi3score build failed. Not stamping - will retry next run.
  The build output above names the cause. The usual ones are:
    * 'Python.h: No such file or directory' -> the Python you are running has no
      development headers. Reinstall it from python.org, or tick the debug/dev
      binaries in the installer.
    * 'No CMAKE_CXX_COMPILER could be found' -> install the Visual Studio Build
      Tools 'Desktop development with C++' workload (see the README).
"@
        exit 1
    }
    New-Item -ItemType File -Path $nativeStamp -Force | Out-Null
}

# Say that the launch has started. Everything above prints as it works, so without this
# the last thing on screen is a pip line and the window is the next event - which reads as
# a stall, especially right after an install: the OS scans the several hundred MB of newly
# written Qt libraries the first time they load. A warm start is ~1s.
if ($installedDeps) {
    Write-Host "Starting SWI3S Studio - the first launch after an install is slow while the OS"
    Write-Host "verifies the newly installed Qt libraries. Later runs start in about a second."
} else {
    Write-Host "Starting SWI3S Studio..."
}

$env:PYTHONPATH = "."
& $python -m swi3s_studio.app
