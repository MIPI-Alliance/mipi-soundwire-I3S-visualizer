#!/usr/bin/env python3
"""Install the app into a clean virtual environment and run it from outside the checkout.

    python3 tools/package_smoke.py             # build, install, run, check

The native core and the app are installed with pip into a new environment (as the README
says to), from a copy of the files a commit can carry (tracked, and untracked ones that are
not ignored) in a temporary directory: the copy omits build/, *.egg-info, ignored files and
files deleted from the working tree. What runs is the installed layout: the data files under
sys.prefix/share, the package from site-packages, and the console script. It then

  * runs `swi3s-studio-cli -c <config> -o <model.json>` and expects exit 0 and a bus model
    in the file: a JSON object with a positive row and column count and a non-empty bit map;
  * expects every data file the package names in [tool.setuptools.data-files] under
    share/swi3s-studio;
  * asks the installed interpreter where the register map, the demo workspaces and the
    register-map library resolve, and expects each under share/swi3s-studio, and expects
    every icon matched by [tool.setuptools.package-data] beside swi3s_studio/ui.

Exit 0 is a pass; any problem is printed and exits 1. The environment is removed afterwards.
The tool takes no options: any argument is refused with exit 2 before anything is built.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG = os.path.join("visualizer_examples", "spec_figures",
                       "PDM_Streams_with_PHY_Tail_Bits_No_Sample_Grouping.csv")


def data_files(root: str = _ROOT) -> list[str]:
    """The installed path, relative to sys.prefix, of every file in the data-files table, in
    this platform's separators (the table writes '/')."""
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        table = tomllib.load(f)["tool"]["setuptools"]["data-files"]
    return sorted(os.path.join(*target.split("/"), os.path.basename(src))
                  for target, sources in table.items() for src in sources)


def missing_data_files(prefix: str, root: str = _ROOT) -> list[str]:
    """The data files of `root`'s pyproject.toml that are absent under `prefix`."""
    return [rel for rel in data_files(root) if not os.path.isfile(os.path.join(prefix, rel))]


# Run by the installed interpreter: where each data lookup resolves, and where the package is.
_PROBE = """
import json, os
from swi3s_studio import workspace
from swi3s_studio.model import regmap_import, registers
import swi3s_studio.ui
print(json.dumps({"registers": registers.DEFAULT_REGISTERS_JSON,
                  "demos": workspace.demo_dir(),
                  "regmaps": regmap_import.library_dir(),
                  "ui": os.path.dirname(swi3s_studio.ui.__file__)}))
"""


def icon_patterns(root: str = _ROOT) -> list[str]:
    """The [tool.setuptools.package-data] globs of swi3s_studio.ui."""
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["tool"]["setuptools"]["package-data"]["swi3s_studio.ui"]


def lookup_problems(found: dict[str, str], prefix: str, root: str = _ROOT) -> list[str]:
    """What the installed lookups got wrong. `found` is the probe's report: each data lookup
    must resolve under `prefix`/share/swi3s-studio, and every icon in the source package must
    be present beside the installed swi3s_studio.ui."""
    share = os.path.normcase(os.path.realpath(os.path.join(prefix, "share", "swi3s-studio")))
    problems = []
    for key in ("registers", "demos", "regmaps"):
        where = os.path.normcase(os.path.realpath(found.get(key, "")))
        if not where.startswith(share + os.sep):
            problems.append(f"{key} resolves to {found.get(key)!r}, not under {share}")
    src_ui = os.path.join(root, "swi3s_studio", "ui")
    for pattern in icon_patterns(root):
        for path in glob.glob(os.path.join(src_ui, pattern)):
            rel = os.path.relpath(path, src_ui)
            if not os.path.isfile(os.path.join(found.get("ui", ""), rel)):
                problems.append(f"not installed beside swi3s_studio.ui: {rel}")
    return problems


def model_problem(path: str) -> str | None:
    """What is wrong with the bus model file at `path`, or None: it must be a JSON object with
    positive `num_rows` and `num_columns` and a non-empty `bits` map."""
    try:
        with open(path, encoding="utf-8") as f:
            model = json.load(f)
    except (OSError, ValueError) as e:
        return f"the model file does not read as JSON: {e}"
    if not isinstance(model, dict):
        return f"the model file holds a {type(model).__name__}, not a bus model object"
    for key in ("num_rows", "num_columns"):
        value = model.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return f"the model's {key} is {value!r}, not a positive count"
    if not isinstance(model.get("bits"), dict) or not model["bits"]:
        return "the model has no bits"
    return None


def export_source(dest: str, root: str = _ROOT) -> None:
    """Copy `root`'s files a commit can carry (tracked, and untracked ones that are not
    ignored) to `dest`, at their working-tree content; a tracked file deleted from the working
    tree is left out. Outside a git checkout, the tree is copied without build/, *.egg-info,
    .git and __pycache__."""
    try:
        listed = subprocess.run(["git", "ls-files", "--cached", "--others",
                                 "--exclude-standard", "-z"], cwd=root, capture_output=True)
    except FileNotFoundError:               # git is not installed
        listed = None
    if listed is None or listed.returncode != 0:
        shutil.copytree(root, dest, symlinks=True, ignore=shutil.ignore_patterns(
            "build", "*.egg-info", ".git", "__pycache__"))
        return
    for rel in sorted({os.fsdecode(p) for p in listed.stdout.split(b"\0") if p}):
        src = os.path.join(root, rel)
        if not (os.path.isfile(src) or os.path.islink(src)):
            continue
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(src, target, follow_symlinks=False)


def _bin(venv: str, name: str) -> str:
    return os.path.join(venv, "Scripts" if os.name == "nt" else "bin", name)


def main(argv: list[str]) -> int:
    if argv:
        print("usage: python3 tools/package_smoke.py  (takes no options)", file=sys.stderr)
        return 2
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="swi3s-package-smoke-") as work:
        venv = os.path.join(work, "venv")
        source = os.path.join(work, "source")
        export_source(source)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        steps = [[sys.executable, "-m", "venv", venv],
                 [_bin(venv, "python"), "-m", "pip", "install", os.path.join(source, "native")],
                 [_bin(venv, "python"), "-m", "pip", "install", source]]
        for cmd in steps:
            print("  " + " ".join(cmd))
            if subprocess.run(cmd, cwd=work, env=env).returncode != 0:
                print("package-smoke: FAIL — an install step failed")
                return 1
        model = os.path.join(work, "model.json")
        run = subprocess.run([_bin(venv, "swi3s-studio-cli"), "-c", os.path.join(_ROOT, _CONFIG),
                              "-o", model], cwd=work, env=env, capture_output=True, text=True)
        if run.returncode != 0 or not os.path.isfile(model):
            problems.append(f"swi3s-studio-cli exited {run.returncode}: "
                            f"{(run.stdout + run.stderr).strip()[-300:]}")
        elif model_problem(model):
            problems.append(f"swi3s-studio-cli: {model_problem(model)}")
        got = subprocess.run([_bin(venv, "python"), "-c", "import sys; print(sys.prefix)"],
                             cwd=work, env=env, capture_output=True, text=True)
        prefix = got.stdout.strip()
        if got.returncode != 0 or not prefix:
            problems.append("the installed interpreter could not report sys.prefix: "
                            f"{(got.stdout + got.stderr).strip()[-300:]}")
        else:
            problems += [f"not installed under {prefix}: {rel}"
                         for rel in missing_data_files(prefix)]
            probe = subprocess.run([_bin(venv, "python"), "-c", _PROBE], cwd=work, env=env,
                                   capture_output=True, text=True)
            try:
                problems += lookup_problems(json.loads(probe.stdout), prefix)
            except ValueError:
                problems.append(f"the installed interpreter's lookup probe failed: "
                                f"{(probe.stdout + probe.stderr).strip()[-300:]}")
    for p in problems:
        print(f"package-smoke: {p}")
    print("package-smoke: " + ("FAIL" if problems else "PASS"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
