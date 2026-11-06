"""The packaging, CI and launcher configuration files agree with each other and with the app.

Every rule here is a statement the files make about one another: the same requirements in
pyproject.toml and requirements.txt, an entry point for the headless mode, a data file for
every runtime lookup of the installed layout, and a launcher that reads `-o` the way the
command line does.
"""
import glob
import os
import re
import subprocess
import tomllib

import pytest
from conftest import commit_files, git_in, load_tool
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _text(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _pyproject():
    return tomllib.loads(_text("pyproject.toml"))


def _requirements(lines):
    """{canonical name: Requirement} of the requirement lines in `lines`. Comments, blank
    lines and option lines (-r, --index-url) are not requirements."""
    found = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            req = Requirement(line)
            found[canonicalize_name(req.name)] = req
    return found


def _requirement_floors(lines):
    """{canonical name: the version of its >= specifier} of the requirement lines in `lines`."""
    return {name: spec.version for name, req in _requirements(lines).items()
            for spec in req.specifier if spec.operator == ">="}


def _differences(a, b):
    """{name: (a's, b's)} for every requirement the two sets do not state alike: a name in one
    only, or a different specifier, extra or marker (trailing zeros of a version aside)."""
    def form(req):
        return None if req is None else (req.specifier, req.extras, str(req.marker or ""))
    return {n: (str(a.get(n)), str(b.get(n))) for n in sorted(set(a) | set(b))
            if form(a.get(n)) != form(b.get(n))}


def test_the_runtime_requirements_are_the_same_in_pyproject_and_requirements():
    declared = _requirements(_pyproject()["project"]["dependencies"])
    required = _requirements(_text("requirements.txt").splitlines())
    assert declared, "no requirements read from pyproject.toml"
    differ = _differences(declared, required)
    assert not differ, f"pyproject.toml vs requirements.txt: {differ}"


def test_the_build_requirement_on_setuptools_is_the_one_the_dev_requirements_install():
    build = _requirements(_pyproject()["build-system"]["requires"])
    dev = _requirements(_text("requirements-dev.txt").splitlines())
    assert _differences({"setuptools": build["setuptools"]},
                        {"setuptools": dev["setuptools"]}) == {}


def test_two_requirement_sets_differ_by_a_name_in_either_one_or_by_any_part_of_a_specifier():
    def reqs(*lines):
        return _requirements(lines)
    assert _differences(reqs("numpy>=1.24", "pyarrow>=14"),
                        reqs("numpy>=1.24.0", "pyarrow>=14.0")) == {}
    assert set(_differences(reqs("numpy>=1.24"), reqs("numpy>=1.24", "scipy>=1.11"))) == {"scipy"}
    assert set(_differences(reqs("numpy>=1.24", "scipy>=1.11"), reqs("numpy>=1.24"))) == {"scipy"}
    assert _differences(reqs("PySide6>=6.6"), reqs("PySide6>=6.6,<7"))
    assert _differences(reqs("pkg[a]>=1"), reqs("pkg>=1"))
    assert _differences(reqs("pkg>=1; sys_platform == 'win32'"), reqs("pkg>=1"))
    assert _requirement_floors(["# c", "-r x.txt", "pytest-cov>=7.0  # note", "", "mypy==2.3.0"]) \
        == {"pytest-cov": "7.0"}


def test_the_native_core_is_not_a_package_index_dependency():
    """swi3score is published to no index, so a dependency on its name would be resolved from
    one; it is installed from ./native first, and the app checks its ABI when a session opens."""
    deps = _pyproject()["project"]["dependencies"]
    assert not [d for d in deps if d.lower().startswith("swi3score")]


def test_the_headless_mode_has_a_console_script():
    """A GUI launcher has no console on Windows, so the headless `-o` mode and its exit codes
    need a console-script entry point that runs the same function."""
    project = _pyproject()["project"]
    gui = project["gui-scripts"]["swi3s-studio"]
    cli = project["scripts"]["swi3s-studio-cli"]
    assert cli == gui == "swi3s_studio.app:main"


def test_every_installed_data_file_exists_and_the_user_guide_is_among_them():
    data = _pyproject()["tool"]["setuptools"]["data-files"]
    listed = {src for sources in data.values() for src in sources}
    assert "docs/USER_GUIDE.md" in listed
    assert data["share/swi3s-studio/docs"] == ["docs/USER_GUIDE.md"]
    missing = sorted(src for src in listed if not os.path.isfile(os.path.join(_ROOT, src)))
    assert not missing, f"named in [tool.setuptools.data-files] but absent: {missing}"


def test_the_gitignore_repeats_no_rule():
    seen, repeated = {}, []
    for line in _text(".gitignore").splitlines():
        if line.strip() and not line.startswith("#"):
            if line in seen:
                repeated.append(line)
            seen[line] = True
    assert not repeated, f"repeated in .gitignore: {repeated}"


def test_no_gitignore_comment_tells_a_story():
    comments = " ".join(ln for ln in _text(".gitignore").splitlines() if ln.startswith("#"))
    assert not re.search(r"\b(it did|was|were|used to|no longer|previously)\b", comments), comments


def test_ci_pins_every_action_to_a_commit_and_scans_for_leaks():
    """Every workflow this tree owns pins each action to a full commit. A workflow the
    upstream project owns (publish_tree._UPSTREAM_OWNED, grafted into the published tree) is
    not this tree's to pin."""
    upstream = set(load_tool("publish_tree")._UPSTREAM_OWNED)
    ci = _text(".github", "workflows", "ci.yml")
    workflows = sorted(os.listdir(os.path.join(_ROOT, ".github", "workflows")))
    assert {"ci.yml", "cla.yml"} <= set(workflows), workflows
    uses = [u for name in workflows if name.endswith((".yml", ".yaml"))
            and f".github/workflows/{name}" not in upstream
            for u in re.findall(r"^\s*-?\s*uses:\s*(\S+)", _text(".github", "workflows", name),
                               re.M)]
    assert any("contributor-assistant" in u for u in uses), uses
    loose = [u for u in uses if not re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u)]
    assert not loose, f"actions not pinned to a full commit: {loose}"
    lint = ci.split("\n  lint:", 1)[1]
    lint = re.split(r"\n  [A-Za-z_][\w-]*:", lint, maxsplit=1)[0]     # up to the next job
    assert "python3 tools/leak_scan.py" in lint


def test_ci_gives_its_jobs_read_only_access_to_the_repository():
    ci = _text(".github", "workflows", "ci.yml")
    top = re.search(r"^permissions:\s*\n((?:[ ]+\S.*\n)+)", ci, re.M)
    assert top, "ci.yml has no top-level permissions block"
    assert re.findall(r"^\s+([\w-]+):\s*(\S+)", top.group(1), re.M) == [("contents", "read")]
    # A job's own permissions block replaces the top-level one, so no job has one.
    jobs = ci.split("\njobs:\n", 1)[1]
    assert not re.search(r"^\s+permissions\s*:", jobs, re.M), "a CI job sets its own permissions"


def test_the_cla_job_runs_on_pull_request_events_and_on_a_pull_requests_recheck_or_signing():
    """The job's one condition: a pull_request_target event, or a comment on a pull request
    (not an issue) that is `recheck` or the sign phrase the action is configured with. Every
    bot account is exempt from signing through the one `*[bot]` pattern."""
    wf = _text(".github", "workflows", "cla.yml")
    conds = [ln.strip()[3:] for ln in wf.splitlines() if ln.strip().startswith("if:")]
    sign = re.search(r"^\s*custom-pr-sign-comment:\s*'([^']+)'\s*$", wf, re.M)
    assert len(conds) == 1 and sign, conds
    assert " ".join(conds[0].split()) == (
        "github.event_name == 'pull_request_target' || (github.event.issue.pull_request && "
        "(github.event.comment.body == 'recheck' || "
        f"github.event.comment.body == '{sign.group(1)}'))")
    assert re.findall(r"^\s*allowlist:\s*(.+?)\s*$", wf, re.M) == ["'*[bot]'"]


def test_the_dev_requirements_floor_the_coverage_release_the_subprocess_option_needs():
    cfg = _pyproject()["tool"]["coverage"]["run"]
    assert cfg["patch"] == ["subprocess"]
    floors = _requirement_floors(_text("requirements-dev.txt").splitlines())
    assert tuple(int(x) for x in floors["coverage"].split(".")) >= (7, 10)
    assert tuple(int(x) for x in floors["pytest-cov"].split(".")) >= (7, 0)


def test_ci_installs_the_package_into_a_clean_environment_and_runs_it():
    ci = _text(".github", "workflows", "ci.yml")
    job = ci.split("  package:", 1)[1].split("\n  coverage:", 1)[0]
    assert "python tools/package_smoke.py" in job
    assert 'PYTHONPATH: ""' in job, "the checkout must not be on the import path"
    # The console launcher and the installed layout differ on Windows.
    assert "ubuntu-latest" in job and "windows-latest" in job


def test_the_smoke_tool_names_the_installed_data_files_and_reports_the_absent_ones(tmp_path):
    tool = load_tool("package_smoke")
    wanted = tool.data_files()
    assert os.path.join("share", "swi3s-studio", "docs", "USER_GUIDE.md") in wanted
    assert all(os.path.normpath(rel) == rel for rel in wanted), wanted
    assert tool.missing_data_files(str(tmp_path)) == wanted
    for rel in wanted:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    assert tool.missing_data_files(str(tmp_path)) == []


@pytest.mark.platform
def test_the_smoke_tool_checks_where_the_installed_lookups_resolve_and_that_the_icons_shipped(
        tmp_path):
    tool = load_tool("package_smoke")
    share = tmp_path / "prefix" / "share" / "swi3s-studio"
    ui = tmp_path / "site" / "swi3s_studio" / "ui"
    found = {"registers": str(share / "registers.json"), "demos": str(share / "demos"),
             "regmaps": str(share / "regmaps"), "ui": str(ui)}
    icons = [os.path.basename(p) for p in
             glob.glob(os.path.join(_ROOT, "swi3s_studio", "ui", "assets", "*.svg"))]
    assert icons
    (ui / "assets").mkdir(parents=True)
    for name in icons:
        (ui / "assets" / name).write_text("<svg/>", encoding="utf-8")
    prefix = str(tmp_path / "prefix")
    assert tool.lookup_problems(found, prefix) == []
    # A lookup that resolves to the repository layout, and an icon that was not installed.
    bad = dict(found, demos=os.path.join(_ROOT, "data", "demos"))
    (ui / "assets" / icons[0]).unlink()
    problems = tool.lookup_problems(bad, prefix)
    assert any(p.startswith("demos resolves to") for p in problems)
    assert any(icons[0] in p for p in problems)
    assert len(problems) == 2


def test_the_smoke_tool_reads_the_cli_model_file_as_a_bus_model(tmp_path):
    """The headless run passes only with a bus model in its output file: a JSON object with
    positive row and column counts and a non-empty bit map."""
    import json
    tool = load_tool("package_smoke")
    path = tmp_path / "model.json"
    good = {"num_rows": 4, "num_columns": 16, "bits": {"0,1": {"device": 1}}, "warnings": {}}
    cases = [(good, None), ({}, "num_rows"), ([], "not a bus model"),
             (dict(good, num_columns=0), "num_columns"), (dict(good, num_rows=True), "num_rows"),
             (dict(good, bits={}), "no bits"), (dict(good, bits=[1]), "no bits")]
    for model, problem in cases:
        path.write_text(json.dumps(model), encoding="utf-8")
        found = tool.model_problem(str(path))
        assert (found is None) if problem is None else (problem in (found or "")), (model, found)
    path.write_text("", encoding="utf-8")
    assert "does not read as JSON" in tool.model_problem(str(path))


@pytest.mark.platform
def test_the_smoke_tool_installs_from_the_files_a_commit_can_carry(tmp_path, isolated_git):
    """A build/ or *.egg-info left in the checkout, and a tracked file deleted from the working
    tree, are not in the copy pip installs from; an untracked file that is not ignored is."""
    tool = load_tool("package_smoke")
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q")
    commit_files(repo, {".gitignore": "build/\n*.egg-info/\n", "pkg/kept.py": "a = 1\n",
                        "pkg/gone.py": "b = 2\n"}, "base")
    (repo / "pkg" / "gone.py").unlink()
    (repo / "pkg" / "kept.py").write_text("a = 3\n", encoding="utf-8")
    (repo / "pkg" / "new.py").write_text("c = 4\n", encoding="utf-8")
    for rel in ("build/lib/pkg/gone.py", "pkg.egg-info/SOURCES.txt"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("stale\n", encoding="utf-8")
    dest = tmp_path / "source"
    tool.export_source(str(dest), str(repo))
    copied = sorted(os.path.relpath(os.path.join(base, n), dest).replace(os.sep, "/")
                    for base, _dirs, names in os.walk(dest) for n in names)
    assert copied == [".gitignore", "pkg/kept.py", "pkg/new.py"]
    assert (dest / "pkg" / "kept.py").read_text(encoding="utf-8") == "a = 3\n"


@pytest.mark.platform
@pytest.mark.parametrize("git_state", ["missing", "not_a_checkout"])
def test_the_smoke_tool_copies_the_tree_when_git_is_not_usable(tmp_path, monkeypatch, git_state):
    """Outside a git checkout the whole tree is copied, without build/, *.egg-info, .git and
    __pycache__."""
    tool = load_tool("package_smoke")
    repo = tmp_path / "repo"
    for rel in ("pkg/a.py", "build/lib/pkg/gone.py", "pkg.egg-info/SOURCES.txt", ".git/HEAD",
                "pkg/__pycache__/a.pyc"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x = 1\n", encoding="utf-8")

    def no_git(cmd, **kw):
        if git_state == "missing":
            raise FileNotFoundError("git")
        return subprocess.CompletedProcess(cmd, 128, b"", b"not a git repository")
    monkeypatch.setattr(tool.subprocess, "run", no_git)
    dest = tmp_path / "source"
    tool.export_source(str(dest), str(repo))
    copied = sorted(os.path.relpath(os.path.join(base, n), dest).replace(os.sep, "/")
                    for base, _dirs, names in os.walk(dest) for n in names)
    assert copied == ["pkg/a.py"]


def test_the_smoke_tool_names_an_interpreter_that_cannot_report_its_prefix(monkeypatch, capsys):
    tool = load_tool("package_smoke")
    monkeypatch.setattr(tool, "export_source", lambda dest, root=None: None)

    def fake_run(cmd, **kw):
        failed = "sys.prefix" in " ".join(map(str, cmd))
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, "", "broken" if failed else "")
    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    assert tool.main([]) == 1
    out = capsys.readouterr().out
    assert "could not report sys.prefix" in out
    assert "not installed under" not in out


# ------------------------------------------------------------------------------ run.sh

def _headless_by_launcher(*args):
    """What run.sh's argument loop decides for `args`: the loop alone, run in bash."""
    script = _text("run.sh")
    loop = script[script.index("app_args=()"):script.index("# Pick a Python")]
    out = subprocess.run(["bash", "-c", loop + '\necho "$headless"', "--", *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip() == "1"


@pytest.mark.platform
@pytest.mark.skipif(os.name == "nt", reason="needs bash")
@pytest.mark.parametrize("args", [
    ["-o", "m.json"], ["-om.json"], ["-o=m.json"], ["--output", "m.json"],
    ["--output=m.json"], ["--out", "m.json"], ["--outp=m.json"], ["--o", "m.json"],
    ["-h"], ["--help"], ["--he"],
])
def test_the_launcher_treats_every_spelling_the_parser_accepts_as_headless(args):
    from swi3s_studio import cli
    if args[0] != "-h" and not args[0].startswith("--he"):
        parsed = cli.build_parser().parse_known_args(["-c", "x.csv", *args])[0]
        assert parsed.output, f"the parser does not read {args} as -o"
    assert _headless_by_launcher(*args)


@pytest.mark.platform
@pytest.mark.skipif(os.name == "nt", reason="needs bash")
@pytest.mark.parametrize("args", [[], ["--demo"], ["-c", "x.csv"], ["--rebuild"]])
def test_the_launcher_opens_a_window_for_everything_else(args):
    assert not _headless_by_launcher(*args)
