"""run.ps1 run for real under PowerShell, against a stub environment: the choice between the
headless forms (no launch notice, no window) and the GUI start, and the forwarding of every
argument to the app. Windows only; the same rule for run.sh is pinned as text in test_cli.py.

The stub environment is a throwaway copy of the launcher beside an empty requirements.txt, an
empty native/ and a swi3s_studio.app that prints its argv; the venv is a real, pip-less venv
with a stub swi3score module and stamps newer than their inputs, so the launcher installs and
builds nothing."""
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

# `platform`: the launcher is the platform's own, so the guest scope runs this file.
pytestmark = [pytest.mark.platform,
              pytest.mark.skipif(sys.platform != "win32", reason="runs run.ps1 under PowerShell")]

_ROOT = pathlib.Path(__file__).resolve().parent.parent

_NOTICE = "Starting SWI3S Studio"

_HEADLESS = [
    ["-o", "m.json"], ["-om.json"], ["-o=m.json"], ["--output", "m.json"], ["--output=m.json"],
    ["--o", "m.json"], ["--ou=m.json"], ["--out", "m.json"], ["--outp", "m.json"],
    ["--outpu=m.json"], ["-c", "cfg.csv", "-o", "m.json"],
    ["-h"], ["--h"], ["--he"], ["--hel"], ["--help"],
]
_GUI = [[], ["-c", "cfg.csv"], ["capture.sal"], ["--demo"]]


@pytest.fixture(scope="module")
def launcher(tmp_path_factory):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("no PowerShell on PATH")
    base = tmp_path_factory.mktemp("run_ps1")
    home = base / "checkout"
    (home / "native").mkdir(parents=True)
    (home / "swi3s_studio").mkdir()
    shutil.copy(_ROOT / "run.ps1", home / "run.ps1")
    (home / "requirements.txt").write_text("", encoding="utf-8")
    (home / "swi3s_studio" / "__init__.py").write_text("", encoding="utf-8")
    (home / "swi3s_studio" / "app.py").write_text(
        "import sys\nprint('APP-ARGS ' + '|'.join(sys.argv[1:]))\n", encoding="utf-8")
    venv = base / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    site = venv / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "swi3score.py").write_text("", encoding="utf-8")
    later = time.time() + 3600
    for name in (".deps-stamp", ".native-stamp"):
        stamp = venv / name
        stamp.write_text("", encoding="utf-8")
        os.utime(stamp, (later, later))
    env = dict(os.environ, SWI3S_VENV=str(venv))
    env.pop("SKBUILD_CMAKE_DEFINE", None)

    def run(args):
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(home / "run.ps1"), *args],
            capture_output=True, text=True, env=env, cwd=str(base), timeout=300)
    return run


@pytest.mark.parametrize("args", _HEADLESS, ids=" ".join)
def test_the_headless_forms_print_no_launch_notice(launcher, args):
    r = launcher(args)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _NOTICE not in r.stdout, (args, r.stdout)
    assert "APP-ARGS " + "|".join(args) in r.stdout, (args, r.stdout)


@pytest.mark.parametrize("args", _GUI, ids=lambda a: " ".join(a) or "no-args")
def test_the_gui_forms_announce_the_window(launcher, args):
    r = launcher(args)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _NOTICE in r.stdout, (args, r.stdout)
    assert "APP-ARGS " + "|".join(args) in r.stdout, (args, r.stdout)
