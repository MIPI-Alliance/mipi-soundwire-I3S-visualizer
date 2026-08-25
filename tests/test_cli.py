"""The command-line interface: argument dispatch, and the headless model dump.

`-o/--output` is restored from the v2 Visualizer CLI, where it was the batch entry point for
regression tooling. Three properties matter more than the file's contents, and none of them is
visible from a passing run:

* **`-o` IMPLIES HEADLESS, LITERALLY.** Not "does not show a window" — does not import Qt or the
  UI package at all. That is what makes it usable from a build machine, and it is fragile in one
  direction: any module-level Qt import added to `app.py` silently breaks it while every other
  test stays green. Pinned in a subprocess, because sys.modules in THIS process is already
  polluted by the rest of the suite.
* **Each exit code means exactly one thing.** A batch caller branches on them. argparse's default
  for a usage error is SystemExit(2), which collided with "wrote a model, and it contains a bus
  clash" — so a misspelled flag would have been read as a finding about the bus.
* **The batch JSON and the UI's Export are the same bytes.** Both go through
  `viz_engine`/`JSONHandler.save_bus_model`; v2 grew a second hand-written reporting path here
  and it drifted from the GUI's notifications panel.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

import pytest

from swi3s_studio import cli
from swi3s_studio.model import viz_engine

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXAMPLES = sorted(glob.glob(os.path.join(_ROOT, "visualizer_examples", "**", "*.csv"),
                             recursive=True))


def _example(*parts):
    return os.path.join(_ROOT, "visualizer_examples", *parts)


# Both configs are NAMED, not searched for, so a corpus change that removes one fails loudly
# instead of quietly testing the wrong thing. Their properties are asserted below rather than
# assumed — the first draft of this file used "the first config in the corpus" as its clean one,
# and that config happens to carry a bus clash, so four tests asserted exit 0 against a config
# that correctly exits 2.
_CLEAN = _example("spec_figures",
                  "Figure_150_PDM_Streams_with_PHY_Tail_Bits_No_Sample_Grouping.csv")
_CLASH = _example("directed_tests", "clash_logic_1.csv")       # 14 bus clashes -> exit 2


def _run(*args):
    """Invoke the app as a SUBPROCESS: the exit code is what a caller sees, and the import
    check below needs a clean interpreter."""
    return subprocess.run([sys.executable, "-m", "swi3s_studio.app", *args],
                          cwd=_ROOT, capture_output=True, text=True)


def test_the_fixture_configs_have_the_properties_these_tests_assume():
    """Check the premise. Every test below depends on _CLEAN being clash-free and _CLASH
    carrying a BUS clash (not merely a device clash or a read overlap, which do not gate)."""
    assert len(_EXAMPLES) > 50, f"only {len(_EXAMPLES)} example configs found — corpus missing?"
    for path in (_CLEAN, _CLASH):
        assert os.path.isfile(path), f"{path} is gone; these tests would test nothing"
    clean, _i, _v = viz_engine.build_bus_model(_CLEAN)
    assert not clean.bus_clashes and not clean.warnings.drq_truncation_warnings, (
        f"{os.path.basename(_CLEAN)} is no longer clean — the exit-0 tests need a config that is")
    clash, _i, _v = viz_engine.build_bus_model(_CLASH)
    assert clash.bus_clashes, (
        f"{os.path.basename(_CLASH)} no longer has a bus clash — the exit-2 path would go "
        "untested while every assertion still passed")


# ---------------------------------------------------------------- headless means headless

def test_the_headless_path_imports_neither_qt_nor_the_ui(tmp_path):
    """The property that makes -o usable where there is no display, and the one a module-level
    import in app.py would break without reddening anything else."""
    out = tmp_path / "model.json"
    # The marker is prefixed and grepped for, because run_headless prints its own report to
    # stdout — parsing the whole stream would couple this test to that wording.
    probe = (
        "import sys, swi3s_studio.app as a;"
        f"rc = a.main(['swi3s-studio', '-c', {_CLEAN!r}, '-o', {str(out)!r}]);"
        "qt = [m for m in sys.modules if m.startswith('PySide6')];"
        "ui = [m for m in sys.modules if m.startswith('swi3s_studio.ui')];"
        "print('PROBE', rc, len(qt), len(ui), ','.join(sorted(qt + ui)))"
    )
    r = subprocess.run([sys.executable, "-c", probe], cwd=_ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line, f"probe produced no marker line; stdout={r.stdout!r} stderr={r.stderr!r}"
    _, rc, n_qt, n_ui, names = (line.split(" ", 4) + [""])[:5]
    assert rc == "0", f"headless run exited {rc}"
    assert n_qt == "0", (
        f"{n_qt} PySide6 module(s) imported on the headless path — '-o implies headless' is no "
        f"longer true. Check for a module-level Qt import in swi3s_studio/app.py. Imported: "
        f"{names}")
    assert n_ui == "0", (
        f"{n_ui} swi3s_studio.ui module(s) imported on the headless path: {names}")


def test_headless_writes_a_loadable_bus_model(tmp_path):
    out = tmp_path / "model.json"
    r = _run("-c", _CLEAN, "-o", str(out))
    assert r.returncode == 0, r.stderr
    assert out.is_file()
    # Round-trips through the loader, which is the reason the BUS model was chosen over the
    # legacy frame model (v2's help text said "frame"; its code wrote this).
    from swi3s_studio.swviz.io.json_handler import JSONHandler
    model = JSONHandler.load_bus_model(str(out))
    assert model.num_rows > 0 and model.num_columns > 0


def test_headless_creates_the_output_directory(tmp_path):
    """batch_mode=True: a caller writing into a fresh results tree should not have to mkdir."""
    out = tmp_path / "fresh" / "nested" / "model.json"
    assert _run("-c", _CLEAN, "-o", str(out)).returncode == 0
    assert out.is_file()


# ---------------------------------------------------------------- one meaning per exit code

def test_a_usage_error_exits_one_not_two(tmp_path):
    """2 means "wrote a model with a bus clash". argparse's default would have used it for a
    typo, so a batch script could not tell a mistake from a finding."""
    r = _run("-o", str(tmp_path / "m.json"))              # -o with no -c
    assert r.returncode == 1, f"exit {r.returncode}; stderr={r.stderr!r}"
    assert "requires -c" in r.stderr
    r = _run("-c")                                        # -c missing its value: argparse's own
    assert r.returncode == 1, f"exit {r.returncode}; stderr={r.stderr!r}"


def test_a_missing_config_exits_one_and_writes_nothing(tmp_path):
    out = tmp_path / "m.json"
    r = _run("-c", str(tmp_path / "nope.csv"), "-o", str(out))
    assert r.returncode == 1
    assert "not found" in r.stderr
    assert not out.exists(), "an unreadable config must not leave a partial model behind"


def test_a_bus_clash_exits_two_and_still_writes_the_model(tmp_path):
    """2 is not a failure to produce output — the model IS written, and the code says it
    contains something a human must look at."""
    out = tmp_path / "clash.json"
    r = _run("-c", _CLASH, "-o", str(out))
    assert r.returncode == 2, f"exit {r.returncode}; stdout={r.stdout!r}"
    assert out.is_file(), "exit 2 must still have written the model"
    assert "Bus Clash" in r.stdout


def test_a_clean_config_exits_zero():
    assert _run("-c", _CLEAN, "-o", os.devnull).returncode == 0


# ---------------------------------------------------------------- the batch and UI agree

@pytest.mark.parametrize("config", _EXAMPLES,
                         ids=[os.path.basename(c)[:-4] for c in _EXAMPLES])
def test_batch_json_is_identical_to_the_ui_export(config, tmp_path):
    """Every config in the corpus. The whole point of routing -o through viz_engine rather
    than re-implementing the dump is that these cannot diverge; asserting it on one config
    would not have caught a divergence that depends on a warning only some configs raise."""
    out = tmp_path / "m.json"
    rc = _run("-c", config, "-o", str(out)).returncode
    assert rc in (0, 2), f"{config} exited {rc}"
    assert json.load(open(out)) == viz_engine.model_json(config), (
        f"batch JSON differs from the UI's Export for {os.path.basename(config)}")


# ---------------------------------------------------------------- parsing

def test_qt_options_are_passed_through_not_rejected():
    """parse_known_args, deliberately: Qt reads -style/-platform off argv itself, and a strict
    parser would reject documented Qt invocations that have nothing to do with our flags."""
    args, qt_argv = cli.parse(["swi3s-studio", "-platform", "offscreen", "--demo"])
    assert args.demo is True
    assert "-platform" in qt_argv and "offscreen" in qt_argv
    assert qt_argv[0] == "swi3s-studio", "argv[0] must survive for QApplication"
    assert "--demo" not in qt_argv, "our own flags should not reach Qt"


def test_demo_flags_still_parse():
    """These were string matches against argv before the parser existed; the behaviour they
    drive is unchanged, so they must keep working exactly."""
    assert cli.parse(["x", "--demo"])[0].demo is True
    assert cli.parse(["x", "--demo-bringup"])[0].demo_bringup is True
    assert cli.parse(["x"])[0].demo is False


def test_help_describes_the_bus_model_not_a_frame_model():
    """v2's help said "frame model" while its code wrote the bus model. The behaviour is v2's;
    the text is corrected. If someone "fixes" the text back, this fails."""
    help_text = cli.build_parser().format_help()
    assert "bus model" in help_text.lower()
    assert "frame model" not in help_text.lower()


# ---------------------------------------------------------------- the launchers forward argv
# BOTH launchers swallowed every argument for the whole v3 line: `run.sh` ended in a bare
# `exec … -m swi3s_studio.app` with no "$@", and run.ps1 took only -Rebuild. Nothing noticed
# because there was nothing worth passing until the app grew a command line — and then the first
# real use of it (`./run.sh -c cfg.csv -o out.json`) opened the UI and wrote no file, which looks
# like the CLI is broken rather than the launcher.
#
# Read as TEXT rather than executed: run.sh builds a venv and can install ~200 MB of wheels, and
# run.ps1 does not run here at all. Both are thin wrappers, so the property is textual — the
# argument reaches the app, and the launcher's own flag does not.

_RUN_SH = os.path.join(_ROOT, "run.sh")
_RUN_PS1 = os.path.join(_ROOT, "run.ps1")


def test_run_sh_forwards_its_arguments_to_the_app():
    text = open(_RUN_SH, encoding="utf-8").read()
    launch = [ln for ln in text.splitlines() if "swi3s_studio.app" in ln and "#" not in ln[:2]]
    assert launch, "run.sh no longer launches swi3s_studio.app"
    assert any("app_args" in ln or '"$@"' in ln for ln in launch), (
        f"run.sh launches the app without forwarding arguments: {launch!r} — `./run.sh --demo` "
        "and `-c/-o` would be silently ignored, which is the bug this pins.")


def test_run_sh_keeps_its_own_flag_out_of_the_app():
    """--rebuild is the launcher's; argparse would not recognise it, and passing it through would
    make every rebuild print a usage error."""
    text = open(_RUN_SH, encoding="utf-8").read()
    assert '"$a" = "--rebuild"' in text or "--rebuild" in text.split("app_args=()")[1], (
        "run.sh must strip its own --rebuild before forwarding")


def test_run_ps1_forwards_its_arguments_to_the_app():
    text = open(_RUN_PS1, encoding="utf-8").read()
    assert "ValueFromRemainingArguments" in text, (
        "run.ps1 must collect the remaining arguments, or the Windows launcher silently drops "
        "them — the same defect run.sh had")
    launch = [ln for ln in text.splitlines() if "swi3s_studio.app" in ln]
    assert any("@AppArgs" in ln for ln in launch), (
        f"run.ps1 launches the app without splatting the arguments: {launch!r}")


def test_run_ps1_propagates_the_exit_code():
    """PowerShell does not exec: without an explicit exit, the script's own code is returned and
    the 0/1/2 contract is lost for every Windows caller."""
    text = open(_RUN_PS1, encoding="utf-8").read()
    assert "exit $LASTEXITCODE" in text, (
        "run.ps1 must propagate the app's exit code; -o's 0/1/2 is useless otherwise")


def test_both_launchers_agree_about_which_flags_open_no_window():
    """The 'Starting SWI3S Studio…' notice must not promise a window that -o will not open. Two
    scripts, one rule — they drift the moment only one is edited."""
    sh = open(_RUN_SH, encoding="utf-8").read()
    ps = open(_RUN_PS1, encoding="utf-8").read()
    for flag in ("-o", "--output", "-h", "--help"):
        assert flag in sh.split("headless=0")[1].split("fi")[0], f"run.sh: {flag} not detected"
        assert flag in ps.split("$headless =")[1].split("if (")[0], f"run.ps1: {flag} not detected"


# ------------------------------------------------- the launchers, EXECUTED rather than read
# The text guards above are drift guards, and this file's own neighbour (test_release_gate.py)
# documents at length why those are not enough: they assert that source LOOKS right. A quoting
# slip, a loop that builds the array but never uses it, or a `set -u` failure on an empty array
# all leave the text matching and the behaviour broken — which is the exact shape of the bug
# being fixed here, one level down.
#
# So run.sh is RUN, with every expensive step stubbed: a fake venv whose activate script puts a
# fake `python` on PATH, and stamps newer than their sources so the dependency install and the
# native build are both skipped. What executes is the real script, including the argument loop.
#
# run.ps1 CANNOT be executed here — there is no PowerShell on this platform — so it keeps the
# text guards only. That is a NOT-RUN-HERE gap, not a pass: the Windows guest is where it gets
# exercised, and `.\run.ps1 -c cfg.csv -o m.json` belongs in that run.

def _make_stub(tmp_path, passthrough=False):
    """A fake venv + fake python, so run.sh reaches its exec without installing anything.

    Returns (env, record). With passthrough=False the fake python RECORDS the argv it was handed
    and exits; with passthrough=True it execs the real interpreter, so the app actually runs while
    the launcher's venv and build steps stay stubbed.
    """
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    binroot = tmp_path / "bin"
    binroot.mkdir()
    record = tmp_path / "argv.txt"

    # `activate` only has to put our fake python first; run.sh sources it and then calls `python`.
    (venv / "bin" / "activate").write_text(f'export PATH="{binroot}:$PATH"\n', encoding="utf-8")

    # Answers every probe run.sh makes, and records the app invocation. `-c` covers the
    # version/header checks (exit 0 = supported, headers present); `-m pip` is never reached
    # because of the stamps, but exits 0 if it is.
    fake = binroot / "python"
    if passthrough:
        # Hand the app to the REAL interpreter. Used by the usage-line test, which needs the
        # actual argparse output — a recording stub cannot produce it.
        body = ("#!/bin/sh\n"
                'case " $* " in\n'
                f'  *" swi3s_studio.app "*) exec {sys.executable} "$@" ;;\n'
                "esac\n"
                "exit 0\n")
    else:
        body = ("#!/bin/sh\n"
                'case " $* " in\n'
                '  *" swi3s_studio.app "*)\n'
                # Everything AFTER the module name is what got forwarded. Printed one per line so
                # an argument containing a space stays one record — the point of the quoting test.
                '     printf "%s\\n" "$@" | sed -n "/^swi3s_studio.app$/,\\$p" | tail -n +2 '
                f'       > "{record}"\n'
                "     exit 0 ;;\n"
                "esac\n"
                "exit 0\n")
    fake.write_text(body, encoding="utf-8")
    fake.chmod(0o755)
    (binroot / "python3").symlink_to(fake)

    # Stamps newer than their sources -> skip `pip install` and the native build entirely.
    for name in (".deps-stamp", ".native-stamp"):
        (venv / name).write_text("", encoding="utf-8")
        os.utime(venv / name, None)

    env = dict(os.environ, SWI3S_VENV=str(venv), PYTHON=str(fake))
    return env, record


@pytest.fixture
def stub_launcher_env(tmp_path):
    return _make_stub(tmp_path)


@pytest.fixture
def stub_launcher_passthrough(tmp_path):
    return _make_stub(tmp_path, passthrough=True)


def _launch(env, record, *args):
    r = subprocess.run(["bash", _RUN_SH, *args], cwd=_ROOT, env=env,
                       capture_output=True, text=True)
    forwarded = (record.read_text(encoding="utf-8").split("\n") if record.exists() else [])
    return r, [a for a in forwarded if a]


@pytest.mark.skipif(sys.platform == "win32", reason="run.sh is the Unix launcher")
def test_run_sh_actually_forwards_what_it_was_given(stub_launcher_env):
    """The behavioural half. Not "the source mentions $@" — the arguments arrive."""
    env, record = stub_launcher_env
    r, forwarded = _launch(env, record, "-c", "cfg.csv", "-o", "out.json")
    assert r.returncode == 0, f"run.sh failed: {r.stdout!r} {r.stderr!r}"
    assert forwarded == ["-c", "cfg.csv", "-o", "out.json"], (
        f"run.sh forwarded {forwarded!r} — the launcher is dropping or mangling arguments")


@pytest.mark.skipif(sys.platform == "win32", reason="run.sh is the Unix launcher")
def test_run_sh_forwards_nothing_when_given_nothing(stub_launcher_env):
    """`set -u` plus an empty array is the classic way this breaks: the plain `./run.sh` case
    must still reach the app, with no stray empty argument."""
    env, record = stub_launcher_env
    r, forwarded = _launch(env, record)
    assert r.returncode == 0, f"bare ./run.sh failed: {r.stdout!r} {r.stderr!r}"
    assert forwarded == [], f"bare ./run.sh forwarded {forwarded!r}"


@pytest.mark.skipif(sys.platform == "win32", reason="run.sh is the Unix launcher")
def test_run_sh_keeps_rebuild_for_itself(stub_launcher_env):
    """--rebuild is the launcher's flag. Forwarding it would make every rebuild print a usage
    error from argparse."""
    env, record = stub_launcher_env
    r, forwarded = _launch(env, record, "--rebuild", "--demo")
    assert r.returncode == 0, f"run.sh failed: {r.stdout!r} {r.stderr!r}"
    assert forwarded == ["--demo"], f"forwarded {forwarded!r}; --rebuild must be consumed"


@pytest.mark.skipif(sys.platform == "win32", reason="run.sh is the Unix launcher")
def test_run_sh_preserves_arguments_containing_spaces(stub_launcher_env):
    """The quoting case a text guard cannot see at all: `"${app_args[@]}"` unquoted would split
    this into two arguments, and a config path with a space is ordinary on macOS."""
    env, record = stub_launcher_env
    r, forwarded = _launch(env, record, "-c", "my configs/bus one.csv")
    assert r.returncode == 0, f"run.sh failed: {r.stdout!r} {r.stderr!r}"
    assert forwarded == ["-c", "my configs/bus one.csv"], (
        f"forwarded {forwarded!r} — a path with a space was split")


@pytest.mark.skipif(sys.platform == "win32", reason="run.sh is the Unix launcher")
def test_run_sh_is_quiet_about_a_window_it_will_not_open(stub_launcher_env):
    """-o opens no window, so the 'Starting SWI3S Studio…' notice must not appear. It is the
    last thing a batch caller's log would show before silence."""
    env, record = stub_launcher_env
    r, _ = _launch(env, record, "-c", "cfg.csv", "-o", "out.json")
    assert "Starting SWI3S Studio" not in r.stdout, r.stdout
    r, _ = _launch(env, record, "--demo")
    assert "Starting SWI3S Studio" in r.stdout, "the GUI path must still say it is starting"


# ---------------------------------------------------------------- usage names the real command
# `./run.sh -h` printed `usage: swi3s-studio …` — a command that does not exist on a fresh
# checkout, while the launcher is how the README says to run this. A usage line whose first word
# is not retypable is worse than no usage line, because it sends the reader off to install
# something. The launcher announces itself via $SWI3S_LAUNCHER; everything else is inferred.

def test_the_launcher_name_reaches_the_usage_line():
    env_before = os.environ.get("SWI3S_LAUNCHER")
    try:
        for announced in ("./run.sh", ".\\run.ps1"):
            os.environ["SWI3S_LAUNCHER"] = announced
            assert cli.program_name() == announced
            assert cli.build_parser().format_usage().startswith(f"usage: {announced} ")
    finally:
        if env_before is None:
            os.environ.pop("SWI3S_LAUNCHER", None)
        else:
            os.environ["SWI3S_LAUNCHER"] = env_before


def test_module_invocation_names_something_runnable(monkeypatch):
    """`python -m swi3s_studio.app` sets argv[0] to app.py's full path, so argparse's own default
    would print `usage: app.py …` — also not runnable as written."""
    monkeypatch.delenv("SWI3S_LAUNCHER", raising=False)
    monkeypatch.setattr(sys, "argv", ["/some/where/swi3s_studio/app.py", "-h"])
    name = cli.program_name()
    assert "-m swi3s_studio.app" in name, name
    assert "app.py" not in name


def test_console_script_keeps_its_own_name(monkeypatch):
    """Installed via pip, argv[0] IS the command — infer it rather than overriding."""
    monkeypatch.delenv("SWI3S_LAUNCHER", raising=False)
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/swi3s-studio", "-h"])
    assert cli.program_name() == "swi3s-studio"


@pytest.mark.skipif(sys.platform == "win32", reason="run.sh is the Unix launcher")
def test_run_sh_really_produces_its_own_name_in_usage(stub_launcher_passthrough):
    """End to end through the actual launcher, not just the env-var contract.

    STUBBED, not bare. The first version invoked `bash run.sh -h` directly and passed — but only
    here: run.sh keys its venv cache on the checkout's PATH, so in this repo the cache was warm
    while anywhere else (a fresh clone, CI, the tree publish_tree assembles) it would start
    creating a venv and installing wheels. publish_tree's check 6 caught it on the assembled
    tree: a test that depends on this machine's caches is not a test.
    """
    env, record = stub_launcher_passthrough
    r = subprocess.run(["bash", _RUN_SH, "-h"], cwd=_ROOT, env=env,
                       capture_output=True, text=True)
    usage = next((ln for ln in r.stdout.splitlines() if ln.startswith("usage:")), "")
    assert usage.startswith("usage: ./run.sh "), f"got {usage!r}; stdout={r.stdout!r}"


def test_both_launchers_announce_themselves():
    """run.ps1 cannot be executed here (no PowerShell), so this is a text guard for the Windows
    half and the behavioural check above covers the Unix half. The guest run is what proves the
    PowerShell side — the same NOT-RUN-HERE split as the forwarding tests."""
    sh = open(_RUN_SH, encoding="utf-8").read()
    ps = open(_RUN_PS1, encoding="utf-8").read()
    assert "SWI3S_LAUNCHER=./run.sh" in sh, "run.sh must name itself for the usage line"
    assert 'SWI3S_LAUNCHER = ".\\run.ps1"' in ps, "run.ps1 must name itself for the usage line"
