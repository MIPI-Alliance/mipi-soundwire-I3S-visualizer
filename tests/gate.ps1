<#
    Thin wrapper — the gate lives in tools/gate.py so there is ONE implementation across
    macOS, Linux and Windows. See tests/gate.sh for the bash entry point.

    Prefers the repo's venv interpreter, because that is where ruff/mypy and the built
    swi3score .pyd live on the Windows test VM. Override with $env:PYTHON.

        .\tests\gate.ps1              # the commit gate
        .\tests\gate.ps1 --guest      # a test guest, per batch
        .\tests\gate.ps1 --release    # before a tag
        .\tests\gate.ps1 --quick      # the commit gate without the perf ceilings
#>
$ErrorActionPreference = "Stop"
Set-Location -Path (Join-Path $PSScriptRoot "..")

if ($env:PYTHON) {
    $py = $env:PYTHON
} elseif (Test-Path ".\.venv\Scripts\python.exe") {
    $py = ".\.venv\Scripts\python.exe"
} else {
    $py = "python"
}

& $py tools/gate.py @args
exit $LASTEXITCODE
