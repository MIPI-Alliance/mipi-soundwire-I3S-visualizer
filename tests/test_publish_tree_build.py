"""tools/publish_tree.py: the export `_build` writes, the listing and comparison of check 7,
the diff `_hunks` reads, the command line, and a graft that shadows a file.

The export, listing, hunks and graft tests run on throwaway git repositories, with the git
settings that change `git` output (prefixes, colour, attributes, line-end conversion) set the
way a maintainer's machine may set them. The command-line tests run the tool read-only
against this checkout, and the `_usage_problems` tests read no repository.
"""
import importlib.util
import os
import stat
import subprocess
import sys

import pytest
from conftest import commit_files, git_in, publish_release_repo, symlink_or_skip

# The throwaway repositories, and the tool's git, read none of the machine's git settings.
pytestmark = [pytest.mark.platform, pytest.mark.usefixtures("isolated_git")]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, "tools", "publish_tree.py")


def _tool():
    spec = importlib.util.spec_from_file_location("publish_tree_build", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PT = _tool()


_git, _commit = git_in, commit_files


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


def _git_settings(monkeypatch, **settings):
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(settings)))
    for i, (key, value) in enumerate(settings.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{i}", key.replace("__", "."))
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{i}", value)


# ------------------------------------------------------------------------------ _build

def test_the_export_holds_the_blob_bytes_whatever_the_attributes_say(repo, tmp_path):
    """An eol, ident or export-subst attribute converts what `git archive` writes; the
    export is written from the blobs, so none of them applies."""
    (repo / ".gitattributes").write_text(
        "*.txt text eol=crlf\n*.id ident\n*.sub export-subst\n", encoding="utf-8")
    files = {"a.txt": b"one\ntwo\n", "b.id": b"$Id$\n", "c.sub": b"$Format:%H$\n",
             "d.bin": b"\x00\r\n\xff"}
    _commit(repo, {**files, "keep.md": "x\n"})
    dest = tmp_path / "dest"
    dest.mkdir()
    PT._build("HEAD", str(dest), git_dir=str(repo))
    for rel, data in files.items():
        assert (dest / rel).read_bytes() == data, rel
    assert PT.check_published(str(dest), "HEAD", git_dir=str(repo)) == []


def test_the_export_keeps_the_exec_bit_and_symlinks(repo, tmp_path):
    _commit(repo, {"run.sh": "#!/bin/sh\n", "doc/guide.md": "g\n"})
    _git(repo, "update-index", "--chmod=+x", "run.sh")
    symlink_or_skip("doc/guide.md", repo / "guide-link.md")
    _git(repo, "add", "guide-link.md")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "modes")
    dest = tmp_path / "dest"
    dest.mkdir()
    PT._build("HEAD", str(dest), git_dir=str(repo))
    assert os.readlink(dest / "guide-link.md") == "doc/guide.md"
    # A Windows file carries no execute bit, so the modes git records are given there.
    modes = None
    if sys.platform == "win32":
        modes = {"run.sh": "100755"}
    else:
        assert os.stat(dest / "run.sh").st_mode & 0o111 == 0o111
        assert os.stat(dest / "doc" / "guide.md").st_mode & 0o111 == 0
    assert PT.check_published(str(dest), "HEAD", git_dir=str(repo), modes=modes) == []


def test_a_blob_git_cannot_return_stops_the_export_naming_its_path(repo, tmp_path):
    _commit(repo, {"keep.md": "k\n", "docs/lost.md": "a file whose blob goes missing\n"})
    blob = _git(repo, "rev-parse", "HEAD:docs/lost.md")
    loose = repo / ".git" / "objects" / blob[:2] / blob[2:]
    os.chmod(loose, stat.S_IWRITE)                # git writes it read-only; Windows needs this
    os.remove(loose)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(SystemExit, match="docs/lost.md"):
        PT._build("HEAD", str(dest), git_dir=str(repo))


def test_the_listing_is_from_the_repository_root_whatever_directory_git_runs_in(repo):
    _commit(repo, {"README.md": "r\n", "docs/guide.md": "g\n"})
    assert set(PT._ls_tree("HEAD", str(repo / "docs"))) == {"README.md", "docs/guide.md"}
    assert set(PT._ls_tree("HEAD", str(repo / "docs"), ["README.md"])) == {"README.md"}


def test_check_published_reads_a_repository_that_names_its_objects_by_sha256(tmp_path):
    repo = tmp_path / "sha256"
    repo.mkdir()
    try:
        _git(repo, "init", "-q", "-b", "main", "--object-format=sha256")
    except subprocess.CalledProcessError:
        pytest.skip("this git cannot make a sha256 repository")
    _commit(repo, {"README.md": "r\n", "docs/guide.md": "g\n"})
    dest = tmp_path / "dest"
    dest.mkdir()
    PT._build("HEAD", str(dest), git_dir=str(repo))
    assert PT.check_published(str(dest), "HEAD", git_dir=str(repo)) == []
    (dest / "README.md").write_text("changed\n", encoding="utf-8")
    assert PT.check_published(str(dest), "HEAD", git_dir=str(repo)) == [
        "README.md: HEAD holds different bytes from the assembled tree"]


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="permissions do not hide a file here")
def test_an_assembled_file_or_directory_that_cannot_be_read_is_named_not_missing(repo, tmp_path):
    _commit(repo, {"README.md": "r\n", "docs/guide.md": "g\n", "sealed/a.md": "a\n"})
    dest = tmp_path / "dest"
    dest.mkdir()
    PT._build("HEAD", str(dest), git_dir=str(repo))
    (dest / "README.md").chmod(0)
    (dest / "sealed").chmod(0)
    try:
        problems = PT.check_published(str(dest), "HEAD", git_dir=str(repo))
    finally:
        (dest / "README.md").chmod(0o644)
        (dest / "sealed").chmod(0o755)
    assert problems == ["README.md: could not be read in the assembled tree",
                        "sealed/: could not be read in the assembled tree"]


# ------------------------------------------------------------------------------ _hunks

_BEFORE = "alpha line of the file that is long enough\n-- a rule line that starts with dashes\n"
_AFTER = "alpha line of the file that is long enough\n++ a plus line that starts with plusses\n"


@pytest.mark.parametrize("settings", [
    {},
    {"diff__noprefix": "true"},
    {"diff__mnemonicPrefix": "true"},
    {"color__ui": "always"},
    {"color__diff": "always"},
    {"core__quotePath": "false"},
])
def test_the_hunks_read_the_same_whatever_git_is_configured_to_print(
        repo, monkeypatch, settings):
    _commit(repo, {"docs/é guide.md": _BEFORE, "b.md": "keep\n"})
    sha = _commit(repo, {"docs/é guide.md": _AFTER})
    if settings:
        _git_settings(monkeypatch, **settings)
    hunks = PT._hunks(sha, str(repo))
    assert hunks == {"docs/é guide.md": [(["-- a rule line that starts with dashes"],
                                           ["++ a plus line that starts with plusses"])]}


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows file name holds no tab or quote")
@pytest.mark.parametrize("name", ['docs/a"b.md', "docs/tab\tname.md", "docs/back\\slash é.md",
                                  "docs/a \"quoted\" name.md"])
def test_the_hunks_read_a_path_git_quotes(repo, name):
    _commit(repo, {name: _BEFORE, "b.md": "keep\n"})
    sha = _commit(repo, {name: _AFTER})
    assert PT._hunks(sha, str(repo)) == {name: [(["-- a rule line that starts with dashes"],
                                                 ["++ a plus line that starts with plusses"])]}


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows file name holds no tab or quote")
def test_a_revert_of_a_file_whose_path_git_quotes_is_caught(repo, tmp_path):
    name = 'docs/tab\t"guide".md'
    old = "Record a deferred finding in the maintainers' register meanwhile.\n"
    new = "Open an issue with the tech-debt label for a deferred finding.\n"
    _commit(repo, {name: old})
    change = _commit(repo, {name: new}, "contribution")
    reverted = tmp_path / "tree"
    (reverted / "docs").mkdir(parents=True)
    (reverted / name).write_text(old, encoding="utf-8")
    assert PT._undone(change, str(reverted), str(repo)) != []


def test_a_revert_is_caught_on_a_machine_that_prints_diffs_without_prefixes(
        repo, tmp_path, monkeypatch):
    old = "Record a deferred finding in the maintainers' register meanwhile.\n"
    new = "Open an issue with the tech-debt label for a deferred finding.\n"
    _commit(repo, {"docs/guide.md": old})
    change = _commit(repo, {"docs/guide.md": new}, "contribution")
    reverted = tmp_path / "tree"
    (reverted / "docs").mkdir(parents=True)
    (reverted / "docs" / "guide.md").write_text(old, encoding="utf-8")
    _git_settings(monkeypatch, diff__noprefix="true", color__ui="always")
    assert PT._undone(change, str(reverted), str(repo)) != []


# ------------------------------------------------------------------------- command line

def _run(*argv):
    return subprocess.run([sys.executable, _TOOL, *argv], cwd=_ROOT, capture_output=True,
                          text=True)


@pytest.mark.parametrize("arg", ["-h", "-k", "-x=1"])
def test_a_single_dash_argument_is_an_unknown_option(arg):
    assert PT._usage_problems([arg]) == [
        f"unknown option {arg.partition('=')[0]}: the options are "
        f"{', '.join(PT._FLAGS + PT._VALUE_OPTIONS)}"]


@pytest.mark.parametrize("option", ["--message-out=m.txt", "--approve-names=abc", "--preview",
                                    "--record-sync"])
def test_check_published_refuses_an_option_only_the_other_checks_read(option):
    problems = PT._usage_problems(["abc123", "--check-published=public/x", option])
    assert problems == [f"--check-published runs check 7 alone, which reads no "
                        f"{option.partition('=')[0]}"]
    assert PT._usage_problems(["abc123", "--check-published=public/x", "--upstream=u",
                               "--keep"]) == []


@pytest.mark.parametrize("argv", [["--keep"], ["abc123"], ["--message-out=m.txt"]])
def test_record_sync_refuses_anything_but_the_upstream_ref(argv):
    assert PT._usage_problems(["--record-sync", "--upstream=u"]) == []
    problems = PT._usage_problems(["--record-sync", *argv])
    assert len(problems) == 1 and problems[0].startswith("--record-sync reads --upstream only")


def test_a_single_dash_argument_exits_with_a_usage_error_not_a_traceback():
    run = _run("-h")
    assert run.returncode == 2
    assert "unknown option -h" in run.stderr and "Traceback" not in run.stderr
    assert "building published tree" not in run.stdout


def test_a_commit_that_does_not_resolve_exits_with_a_usage_error():
    run = _run("0123456789abcdef-not-a-commit")
    assert run.returncode == 2
    assert "is not a commit" in run.stderr and "Traceback" not in run.stderr
    assert "building published tree" not in run.stdout


# ---------------------------------------------------------------- a graft that shadows

def test_a_release_commit_carrying_an_upstream_owned_file_fails_check_published(
        tmp_path, monkeypatch, capsys):
    """The graft overwrites our copy, so the bytes match the pushed branch; the shadowing is
    still a failure."""
    repo = tmp_path / "release"
    up, rel, pub = publish_release_repo(repo, PT, {"README.md": "x\n"})
    shadow = PT._UPSTREAM_OWNED[0]
    monkeypatch.setattr(PT, "_ROOT", str(repo))
    monkeypatch.setattr(PT.sys, "argv", ["publish_tree.py", rel, f"--upstream={up}",
                                         f"--check-published={pub}"])
    assert PT.main() == 0                              # the control: no shadow, no failure
    capsys.readouterr()
    shadowed = _commit(repo, {shadow: "ours\n"}, "release with a shadow")
    monkeypatch.setattr(PT.sys, "argv", ["publish_tree.py", shadowed, f"--upstream={up}",
                                         f"--check-published={pub}"])
    assert PT.main() == 1
    assert "which upstream owns" in capsys.readouterr().out
