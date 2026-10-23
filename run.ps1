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
# $Rebuild is THIS script's flag; everything else is forwarded to the app verbatim
# (ValueFromRemainingArguments), so `.\run.ps1 -c cfg.csv -o m.json` reaches the CLI.
param(
    [switch]$Rebuild,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArgs = @()
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Pick a Python the launcher supports: 3.11-3.13. 3.14 on Windows x64 works through the
# manual setup in README.md. Prefer the newest supported via the py launcher, then fall
# back to `python` if it's in range.
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
    Write-Error ("Need Python 3.11-3.13 (the versions this launcher supports). " +
                 "Install one, e.g.:  winget install --id Python.Python.3.12 -e")
    exit 1
}

# Does an interpreter have the C API headers (Python.h)? The decode core is C++, so a
# build needs them. The python.org and Store builds ship them; an embeddable distribution
# or a stripped/managed install may not. sysconfig reports the BASE prefix, so a venv
# answers for its parent, which is correct: a venv doesn't supply headers.
function Test-PythonHeaders([string]$exe) {
    # Single quotes for the Python string literals and doubled "" for the cmd-level
    # quoting — the same idiom as the version probes above; \" does not escape a quote
    # through cmd /c.
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
    # No venv yet => the native core will have to be built. Check the headers before
    # creating the venv and downloading the wheels. Skipped when SKBUILD_CMAKE_DEFINE
    # already points the build at headers elsewhere.
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
    # Probe whether the native core imports; a failure (stale or missing .pyd) means
    # rebuild. Run via `cmd /c ... 2>nul` (like the Python-version probes above) so the
    # traceback on stderr cannot trip $ErrorActionPreference='Stop'; only the exit code
    # is read.
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
    # --force-reinstall because the native version is a fixed 0.1.0, so a plain install
    # no-ops and keeps a stale .pyd. --no-deps skips re-resolving the app deps.
    & $python -m pip install --force-reinstall --no-deps .\native
    if ($LASTEXITCODE -ne 0) {
        # List the usual causes; do not assert one.
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

# Announce the launch; the first launch after an install is slow while the OS scans the
# newly written Qt libraries. -o implies headless, so no window message is printed.
$headless = @($AppArgs | Where-Object { $_ -eq "-o" -or $_ -eq "--output" -or
                                        $_ -like "-o=*" -or $_ -like "--output=*" -or
                                        $_ -eq "-h" -or $_ -eq "--help" }).Count -gt 0
if ($headless) {
    # nothing to announce: no window opens
} elseif ($installedDeps) {
    Write-Host "Starting SWI3S Studio - the first launch after an install is slow while the OS"
    Write-Host "verifies the newly installed Qt libraries. Later runs start in about a second."
} else {
    Write-Host "Starting SWI3S Studio..."
}

$env:PYTHONPATH = "."
# The app names the launcher in its usage line — see cli.program_name().
$env:SWI3S_LAUNCHER = ".\run.ps1"
& $python -m swi3s_studio.app @AppArgs
exit $LASTEXITCODE        # -o returns 0/1/2; swallowing it would make it useless
