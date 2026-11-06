"""tools/publish_tree.py: check 3b (the release tree keeps what was merged upstream),
`--record-sync`, the docs/releases/next.md rules of check 4, and check 7 (`--check-published`).

Publishing replaces the published tree wholesale, so anything merged upstream that the release
tree has not taken in is reverted. These run the check on a throwaway git repository standing
in for the upstream branch, and on assembled trees built by hand.
"""
import os
import subprocess
import sys
import types

import pytest
from conftest import commit_files, git_in, load_tool, publish_release_repo, symlink_or_skip

# The throwaway repositories, and the tool's git, read none of the machine's git settings.
pytestmark = [pytest.mark.platform, pytest.mark.usefixtures("isolated_git")]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PT = load_tool("publish_tree")

OLD = "Record a deferred finding in the maintainers' register meanwhile.\n"
NEW = "Open an issue with the tech-debt label for a deferred finding.\n"
INTRO = "This project decodes captures of a two-wire audio bus for review.\n"
GUIDE = "Report a problem with the steps that show it and the file it needs.\n"



def _subprocess_with(run):
    """The `subprocess` module as the tool sees it, with `run` replaced: patched on the tool's
    own binding, so no other caller in the process sees the stub."""
    return types.SimpleNamespace(**{**vars(subprocess), "run": run})


def _publish(repo, files, version):
    return commit_files(repo, files, f"Release {version}\n\n{PT._PUBLISH_MARK} It is the tree.")


@pytest.fixture
def upstream(tmp_path):
    """An upstream branch: a publish (P0), then a contributor's change (P1) that rewrites a
    sentence of DEVELOPMENT.md, adds CONTRIBUTING.md and edits the upstream-owned
    CODEOWNERS."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    p0 = _publish(repo, {"DEVELOPMENT.md": INTRO + OLD, "CODEOWNERS": "* @a\n",
                         "tool.py": "x = 1\n"}, "1.0.0")
    p1 = commit_files(repo, {"DEVELOPMENT.md": INTRO + NEW, "CONTRIBUTING.md": GUIDE,
                        "CODEOWNERS": "* @b\n"}, "docs: contributing")
    return repo, p0, p1


def _tree(tmp_path, files, name="dest"):
    dest = tmp_path / name
    dest.mkdir()
    for rel, text in files.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(text.encode("utf-8"))
    return str(dest)


def test_no_recorded_sync_point_is_refused(upstream, tmp_path):
    repo, _p0, _p1 = upstream
    problems = PT.check_upstream_sync(_tree(tmp_path, {}), "main", [], git_dir=str(repo))
    assert problems and "--record-sync" in problems[0]


def test_upstream_commits_not_taken_in_are_refused_and_listed(upstream, tmp_path):
    repo, p0, _p1 = upstream
    problems = PT.check_upstream_sync(_tree(tmp_path, {}), "main", [p0], git_dir=str(repo))
    assert len(problems) == 1
    assert "1 commit(s)" in problems[0] and "docs: contributing" in problems[0]


def test_the_old_sentence_back_beside_release_edits_is_a_revert(upstream, tmp_path):
    """A file that carries the release's own edits and the contributor's old sentence matches
    neither side whole; the sentence alone shows the revert."""
    repo, p0, p1 = upstream
    dest = _tree(tmp_path, {"DEVELOPMENT.md": INTRO + "A line the release added.\n" + OLD,
                            "CONTRIBUTING.md": GUIDE})
    problems = PT.check_upstream_sync(dest, "main", [p0, p1], git_dir=str(repo))
    assert len(problems) == 1 and problems[0].startswith("DEVELOPMENT.md undoes"), problems
    assert "old text is back" in problems[0]


def test_a_contributed_deletion_brought_back_is_a_revert(upstream, tmp_path):
    """A commit that only removes a line is undone when the tree holds the line more often
    than the commit's own file does; kept, or the same line kept where the commit kept it,
    passes."""
    repo, _p0, _p1 = upstream
    stale = "A stale sentence that the contributor deleted from the guide.\n"
    commit_files(repo, {"CONTRIBUTING.md": GUIDE + stale + GUIDE}, "docs: a sentence")
    p2 = _publish(repo, {"tool.py": "x = 2\n"}, "1.1.0")
    commit_files(repo, {"CONTRIBUTING.md": GUIDE + GUIDE}, "docs: drop the stale sentence")
    p3 = _publish(repo, {"tool.py": "x = 3\n"}, "1.2.0")
    points = [p2, p3]                                  # the window of the deletion alone
    for name, guide, undone in (("back", GUIDE + stale + GUIDE, True),
                                ("kept", GUIDE + GUIDE, False),
                                ("rewrapped", GUIDE + stale.replace(" that ", "\nthat ")
                                 + GUIDE, True)):
        dest = _tree(tmp_path, {"DEVELOPMENT.md": INTRO + NEW, "CONTRIBUTING.md": guide,
                                "tool.py": "x = 3\n"}, name)
        problems = PT.check_upstream_sync(dest, "main", points, git_dir=str(repo))
        if undone:
            assert len(problems) == 1 and "drop the stale sentence" in problems[0], name
            assert "old text is back" in problems[0], name
        else:
            assert problems == [], name


def test_a_deleted_line_the_commit_kept_elsewhere_is_not_a_revert(upstream, tmp_path):
    repo, _p0, _p1 = upstream
    twice = "A sentence the guide carried in two places for a while.\n"
    commit_files(repo, {"CONTRIBUTING.md": twice + GUIDE + twice}, "docs: repeat")
    p2 = _publish(repo, {"tool.py": "x = 2\n"}, "1.1.0")
    commit_files(repo, {"CONTRIBUTING.md": GUIDE + twice}, "docs: once is enough")
    p3 = _publish(repo, {"tool.py": "x = 3\n"}, "1.2.0")
    for name, guide, undone in (("once", GUIDE + twice, False),
                                ("twice", twice + GUIDE + twice, True)):
        dest = _tree(tmp_path, {"DEVELOPMENT.md": INTRO + NEW, "CONTRIBUTING.md": guide,
                                "tool.py": "x = 3\n"}, name)
        problems = PT.check_upstream_sync(dest, "main", [p2, p3], git_dir=str(repo))
        assert bool(problems) == undone, (name, problems)


def test_a_file_the_contributor_deleted_or_renamed_and_the_tree_carries_is_a_revert(
        upstream, tmp_path):
    """A commit that deletes a whole file, or renames it away, is undone when the tree still
    carries the old path with its deleted text; the path absent, or reused for other text,
    passes."""
    repo, _p0, _p1 = upstream
    stale = "A stale page that the contributor deleted from the project.\n"
    commit_files(repo, {"STALE.md": stale, "OLD_NAME.md": GUIDE}, "docs: two pages")
    p2 = _publish(repo, {"tool.py": "x = 2\n"}, "1.1.0")
    commit_files(repo, {"STALE.md": None}, "docs: drop the stale page")
    commit_files(repo, {"OLD_NAME.md": None, "NEW_NAME.md": GUIDE}, "docs: rename the guide")
    p3 = _publish(repo, {"tool.py": "x = 3\n"}, "1.2.0")
    base = {"DEVELOPMENT.md": INTRO + NEW, "CONTRIBUTING.md": GUIDE, "NEW_NAME.md": GUIDE,
            "tool.py": "x = 3\n"}
    for name, extra, expected in (
            ("deleted", {"STALE.md": stale}, ["STALE.md", "drop the stale page"]),
            ("renamed", {"OLD_NAME.md": GUIDE}, ["OLD_NAME.md", "rename the guide"]),
            ("absent", {}, None),
            ("reused", {"STALE.md": "A different page that the release added later on.\n"},
             None)):
        dest = _tree(tmp_path, {**base, **extra}, name)
        problems = PT.check_upstream_sync(dest, "main", [p2, p3], git_dir=str(repo))
        if expected is None:
            assert problems == [], (name, problems)
        else:
            path, subject = expected
            assert len(problems) == 1, (name, problems)
            assert problems[0].startswith(f"{path} is back in the release tree, undoing ")
            assert subject in problems[0], (name, problems)


def test_a_file_the_contributor_added_and_the_tree_lacks_is_a_revert(upstream, tmp_path):
    repo, p0, p1 = upstream
    dest = _tree(tmp_path, {"DEVELOPMENT.md": INTRO + NEW})
    problems = PT.check_upstream_sync(dest, "main", [p0, p1], git_dir=str(repo))
    assert problems == [f"CONTRIBUTING.md is absent from the release tree, undoing "
                        f"{p1[:7]} docs: contributing"], problems


def test_kept_rewrapped_or_edited_later_passes(upstream, tmp_path):
    """The contributor's text kept, re-wrapped with CRLF line ends, or edited later by the
    release (neither side whole) passes. The upstream-owned file is grafted, so it is not
    checked."""
    repo, p0, p1 = upstream
    for name, dev in (("kept", INTRO + NEW),
                      ("wrapped", INTRO.replace("\n", "\r\n")
                       + NEW.replace(" for a ", "\r\nfor a ").replace("\n", "\r\n")),
                      ("edited", INTRO + "Open a tech-debt issue, grouped by area.\n")):
        dest = _tree(tmp_path, {"DEVELOPMENT.md": dev, "CONTRIBUTING.md": GUIDE}, name)
        assert PT.check_upstream_sync(dest, "main", [p0, p1], git_dir=str(repo)) == [], name


def test_publish_commits_are_not_held_against_the_tree(upstream, tmp_path):
    """A publish between two points is the tree's own change: undoing it is not a revert."""
    repo, p0, p1 = upstream
    p2 = _publish(repo, {"tool.py": "x = 2  # a value this release picked for the tool\n"},
                  "1.1.0")
    dest = _tree(tmp_path, {"DEVELOPMENT.md": INTRO + NEW, "CONTRIBUTING.md": GUIDE,
                            "tool.py": "x = 1\n"})
    assert PT.check_upstream_sync(dest, "main", [p0, p1, p2], git_dir=str(repo)) == []


def test_every_recorded_window_is_checked(upstream, tmp_path):
    """A contributor's change in an earlier window, undone later, is still found."""
    repo, p0, p1 = upstream
    p2 = _publish(repo, {"tool.py": "x = 2\n"}, "1.1.0")
    dest = _tree(tmp_path, {"DEVELOPMENT.md": INTRO + OLD, "CONTRIBUTING.md": GUIDE})
    problems = PT.check_upstream_sync(dest, "main", [p0, p1, p2], git_dir=str(repo))
    assert any(p.startswith("DEVELOPMENT.md undoes") for p in problems), problems


def test_a_sync_point_not_on_the_upstream_branch_is_refused(upstream, tmp_path):
    repo, p0, _p1 = upstream
    git_in(repo, "checkout", "-q", "-b", "side")
    stray = commit_files(repo, {"tool.py": "x = 3\n"}, "elsewhere")
    git_in(repo, "checkout", "-q", "main")
    for points in ([stray], [stray, p0]):
        problems = PT.check_upstream_sync(_tree(tmp_path, {}, f"d{len(points)}"), "main",
                                          points, git_dir=str(repo))
        assert problems and "history was rewritten" in problems[0], points


def test_an_unknown_earlier_sync_point_is_refused(upstream, tmp_path):
    repo, _p0, p1 = upstream
    problems = PT.check_upstream_sync(_tree(tmp_path, {}), "main", ["0" * 40, p1],
                                      git_dir=str(repo))
    assert problems and "is not a commit here" in problems[0]


def _sync_file(repo, *points):
    path = repo / PT._SYNC_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# header\n" + "".join(f"{p}  x\n" for p in points), encoding="utf-8")


def test_record_sync_refuses_until_the_changes_are_in(upstream):
    """Recording checks the working tree: the contributor's sentence must be in it."""
    repo, p0, p1 = upstream
    _sync_file(repo, p0)
    (repo / "DEVELOPMENT.md").write_text(INTRO + OLD, encoding="utf-8")
    with pytest.raises(SystemExit, match="not recorded"):
        PT.record_sync("main", git_dir=str(repo))
    (repo / "DEVELOPMENT.md").write_text(INTRO + NEW, encoding="utf-8")
    assert "recorded" in PT.record_sync("main", git_dir=str(repo))
    assert "already records" in PT.record_sync("main", git_dir=str(repo))
    git_in(repo, "add", "-A")
    git_in(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "record")
    assert PT.read_sync_points("HEAD", git_dir=str(repo)) == [p0, p1]


def test_record_sync_starts_a_line_of_its_own_after_a_file_without_a_final_newline(upstream):
    repo, p0, p1 = upstream
    (repo / PT._SYNC_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / PT._SYNC_FILE).write_text(f"# header\n{p0}  the baseline", encoding="utf-8")
    (repo / "DEVELOPMENT.md").write_text(INTRO + NEW, encoding="utf-8")
    assert "recorded" in PT.record_sync("main", git_dir=str(repo))
    git_in(repo, "add", "-A")
    git_in(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "record")
    assert PT.read_sync_points("HEAD", git_dir=str(repo)) == [p0, p1]


def test_record_sync_refuses_a_last_point_that_is_not_on_the_upstream_branch(upstream):
    """A last point off the upstream branch (its history rewritten) or unknown here records
    nothing."""
    repo, p0, _p1 = upstream
    git_in(repo, "checkout", "-q", "-b", "side")
    stray = commit_files(repo, {"tool.py": "x = 3\n"}, "elsewhere")
    git_in(repo, "checkout", "-q", "main")
    (repo / "DEVELOPMENT.md").write_text(INTRO + NEW, encoding="utf-8")
    for last, refusal in ((stray, "history was rewritten"), ("0" * 40, "is not a commit here")):
        _sync_file(repo, p0, last)
        with pytest.raises(SystemExit, match=refusal):
            PT.record_sync("main", git_dir=str(repo))
        assert (repo / PT._SYNC_FILE).read_text(encoding="utf-8").count("\n") == 3


def test_the_first_sync_point_must_be_a_publish(upstream):
    repo, _p0, _p1 = upstream
    with pytest.raises(SystemExit, match="first point must be one"):
        PT.record_sync("main", git_dir=str(repo))


def _notes_tree(tmp_path, version, files):
    pkg = tmp_path / "swi3s_studio"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    rel = tmp_path / "docs" / "releases"
    rel.mkdir(parents=True)
    for name, text in files.items():
        (rel / name).write_text(text, encoding="utf-8")
    return str(tmp_path)


@pytest.mark.parametrize("notes", ["", "\n\n"])
def test_an_empty_release_notes_file_is_a_problem_not_a_crash(tmp_path, notes):
    dest = _notes_tree(tmp_path, "9.9.9", {"v9.9.9.md": notes})
    _text, problems = PT.check_release_notes(dest)
    assert problems == ["docs/releases/v9.9.9.md does not name 9.9.9 in its title line: ''"]


def test_a_release_tree_carrying_next_md_is_refused(tmp_path):
    dest = _notes_tree(tmp_path, "9.9.9", {
        "v9.9.9.md": "# SWI3S Studio v9.9.9\n\nBody.\n",
        "next.md": "# SWI3S Studio — next release (in development)\n\nMore.\n"})
    _text, problems = PT.check_release_notes(dest)
    assert any("next.md" in p and "--preview" in p for p in problems), problems


def test_a_preview_describes_itself_from_next_md(tmp_path):
    dest = _notes_tree(tmp_path, "9.9.9", {
        "v9.9.9.md": "# SWI3S Studio v9.9.9\n\nBody.\n",
        "next.md": "# SWI3S Studio — next release (in development)\n\nMore.\n"})
    assert PT.release_notes_rel(dest, preview=True) == "docs/releases/next.md"
    text, problems = PT.check_release_notes(dest, preview=True)
    assert not problems and text.startswith("# SWI3S Studio — next release")
    message = PT.branch_message(text, "9.9.9", [], [], "public/main", preview=True,
                                notes_rel="docs/releases/next.md")
    assert "PREVIEW of the release after 9.9.9" in message
    assert "`docs/releases/next.md`" in message


def test_the_next_release_notes_are_marked_in_development():
    """While a cycle is open its notes are docs/releases/next.md, marked (in development);
    the version sites keep the last release's number until the release commit."""
    path = os.path.join(_ROOT, "docs", "releases", "next.md")
    if not os.path.isfile(path):
        pytest.skip("no release in development")
    with open(path, encoding="utf-8") as f:
        title = f.readline()
    assert title.startswith("# ") and "(in development)" in title, title


# --------------------------------------------------------------- check 7: the pushed tree

def _pushed(tmp_path, files):
    """A repository whose HEAD holds `files`, standing in for the publish branch as pushed."""
    repo = tmp_path / "pushed"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    commit_files(repo, files, "publish")
    return repo


def test_the_pushed_branch_matches_the_assembled_tree(tmp_path):
    files = {"README.md": INTRO, "tests/test_a.py": "x = 1\n"}
    repo = _pushed(tmp_path, files)
    assert PT.check_published(_tree(tmp_path, files), "HEAD", git_dir=str(repo)) == []


def test_a_hand_edited_missing_or_extra_file_is_named(tmp_path):
    """A file of the pushed branch that is not the assembled tree's, byte for byte, fails
    the check by name, as does a file only one side holds."""
    repo = _pushed(tmp_path, {"README.md": INTRO, "tests/test_a.py": "x = 2\n",
                              "extra.md": GUIDE})
    dest = _tree(tmp_path, {"README.md": INTRO, "tests/test_a.py": "x = 1\n",
                            "kept.md": OLD})
    problems = PT.check_published(dest, "HEAD", git_dir=str(repo))
    assert problems == [
        "extra.md: in HEAD but not in the assembled tree",
        "kept.md: in the assembled tree but not in HEAD",
        "tests/test_a.py: HEAD holds different bytes from the assembled tree"]


def test_an_unresolved_published_ref_is_refused(tmp_path):
    repo = _pushed(tmp_path, {"README.md": INTRO})
    (problem,) = PT.check_published(_tree(tmp_path, {"README.md": INTRO}), "no/such",
                                    git_dir=str(repo))
    assert "does not resolve" in problem


def test_a_tree_built_where_git_converts_line_ends_holds_the_bytes_git_stores(tmp_path):
    """With core.autocrlf=true a plain `git archive` writes CRLF while the blobs hold LF; the
    tree the tool builds holds the blobs' bytes, so check 7 passes on it."""
    files = {"README.md": INTRO + GUIDE, "tests/test_a.py": "x = 1\ny = 2\n"}
    repo = _pushed(tmp_path, files)
    git_in(repo, "config", "core.autocrlf", "true")
    plain = subprocess.run(["git", "archive", "HEAD"], cwd=repo, capture_output=True,
                           check=True).stdout
    assert b"\r\n" in plain
    dest = tmp_path / "dest"
    dest.mkdir()
    PT._build("HEAD", str(dest), git_dir=str(repo))
    assert (dest / "README.md").read_bytes() == (INTRO + GUIDE).encode("utf-8")
    assert PT.check_published(str(dest), "HEAD", git_dir=str(repo)) == []


def test_a_symlink_is_compared_by_its_target(tmp_path):
    """A symlink on both sides passes when the targets are the same and is named when they
    differ, whatever the files they point to hold: COPY.md holds README.md's bytes."""
    repo = _pushed(tmp_path, {"README.md": INTRO, "GUIDE.md": GUIDE, "COPY.md": INTRO})
    symlink_or_skip("README.md", repo / "link.md")
    git_in(repo, "add", "link.md")
    git_in(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "link")
    files = {"README.md": INTRO, "GUIDE.md": GUIDE, "COPY.md": INTRO}
    same = _tree(tmp_path, files, name="same")
    os.symlink("README.md", os.path.join(same, "link.md"))
    assert PT.check_published(same, "HEAD", git_dir=str(repo)) == []
    for target in ("GUIDE.md", "COPY.md"):
        other = _tree(tmp_path, files, name=f"other-{target}")
        os.symlink(target, os.path.join(other, "link.md"))
        assert PT.check_published(other, "HEAD", git_dir=str(repo)) == [
            "link.md: HEAD holds different bytes from the assembled tree"], target


def test_a_mode_the_pushed_branch_changed_is_named(tmp_path):
    """A file whose bytes match but whose mode differs fails the check by name, with the
    mode `modes` gives the assembled path or, for a path it does not name, the file's own."""
    repo = _pushed(tmp_path, {"run.sh": "echo\n", "README.md": INTRO})
    git_in(repo, "update-index", "--chmod=+x", "run.sh")
    git_in(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "chmod")
    dest = _tree(tmp_path, {"run.sh": "echo\n", "README.md": INTRO})
    assert PT.check_published(dest, "HEAD", git_dir=str(repo),
                              modes={"run.sh": "100644"}) == [
        "run.sh: HEAD has mode 100755, the assembled tree 100644"]
    assert PT.check_published(dest, "HEAD", git_dir=str(repo),
                              modes={"run.sh": "100755"}) == []
    assert PT.check_published(dest, "HEAD", git_dir=str(repo),
                              modes={"run.sh": "100755", "README.md": "100755"}) == [
        "README.md: HEAD has mode 100644, the assembled tree 100755"]


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A repository standing in for this one: `up` holds the upstream-owned files, `rel` the
    release tree (with a maintainers' tier to prune), and `pub` the publish branch as the tool
    builds it from them. The tool reads it as its own repository, and fails the test if any
    check other than 7 runs."""
    repo = tmp_path / "release"
    up, rel, pub = publish_release_repo(repo, PT, {"README.md": INTRO,
                                                   "tests/test_a.py": "x = 1\n"})
    monkeypatch.setattr(PT, "_ROOT", str(repo))

    def not_check_7(*args, **kwargs):
        raise AssertionError("--check-published ran a check other than 7")

    for name in ("check_dangling", "check_upstream_sync", "check_release_notes",
                 "check_leak_scan", "build_core"):
        monkeypatch.setattr(PT, name, not_check_7)
    run = PT.subprocess.run

    def git_only(cmd, *args, **kwargs):
        # The name review, the suite and the core probe run in a Python of their own.
        if cmd[0] == PT.sys.executable:
            not_check_7()
        return run(cmd, *args, **kwargs)

    monkeypatch.setattr(PT, "subprocess", _subprocess_with(git_only))
    return repo, up, rel, pub


def _main(monkeypatch, *argv):
    monkeypatch.setattr(PT.sys, "argv", ["publish_tree.py", *argv])
    return PT.main()


def test_check_published_runs_check_7_alone_and_exits_with_its_status(release, monkeypatch,
                                                                       capsys):
    repo, up, rel, pub = release
    assert _main(monkeypatch, rel, f"--upstream={up}", f"--check-published={pub}") == 0
    assert "PASS" in capsys.readouterr().out
    edited = commit_files(repo, {"README.md": INTRO + GUIDE}, "a hand edit")
    assert _main(monkeypatch, rel, f"--upstream={up}", f"--check-published={edited}") == 1
    out = capsys.readouterr().out
    assert f"{edited} is not the assembled tree: README.md: {edited} holds different bytes" \
        in out


def test_check_published_compares_the_modes_git_records(release, monkeypatch, capsys):
    """The assembled tree's modes are the release commit's and the upstream ref's, whatever
    the files on disk carry: a pushed branch that made a file executable fails."""
    repo, up, rel, pub = release
    git_in(repo, "update-index", "--chmod=+x", "README.md")
    git_in(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "chmod")
    chmod = git_in(repo, "rev-parse", "HEAD")
    assert _main(monkeypatch, rel, f"--upstream={up}", f"--check-published={chmod}") == 1
    out = capsys.readouterr().out
    assert f"README.md: {chmod} has mode 100755, the assembled tree 100644" in out
    assert "holds different bytes" not in out


def test_a_grafted_file_takes_the_mode_the_upstream_ref_records(release, monkeypatch, capsys):
    repo, up, rel, pub = release
    owned = PT._UPSTREAM_OWNED[0]
    git_in(repo, "update-index", "--chmod=+x", owned)
    git_in(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "chmod")
    up_x = git_in(repo, "rev-parse", "HEAD")
    assert _main(monkeypatch, rel, f"--upstream={up_x}", f"--check-published={up_x}") == 0
    assert _main(monkeypatch, rel, f"--upstream={up_x}", f"--check-published={pub}") == 1
    assert f"{owned}: {pub} has mode 100644, the assembled tree 100755" \
        in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows files carry no execute bit, so no file's mode on disk "
                           "can disagree with git's there")
def test_the_assembled_modes_come_from_git_not_from_the_disk(release, monkeypatch, capsys):
    """Files whose mode on disk disagrees with the one git records are compared by git's."""
    repo, up, rel, pub = release
    build = PT._build

    def build_executable(rev, dest):
        pruned = build(rev, dest)
        os.chmod(os.path.join(dest, "README.md"), 0o755)
        return pruned

    monkeypatch.setattr(PT, "_build", build_executable)
    assert _main(monkeypatch, rel, f"--upstream={up}", f"--check-published={pub}") == 0
    assert "PASS" in capsys.readouterr().out


def test_a_grafted_file_is_executable_on_disk_when_the_upstream_ref_says_so(release, tmp_path):
    repo, up, rel, pub = release
    owned = PT._UPSTREAM_OWNED[0]
    git_in(repo, "update-index", "--chmod=+x", owned)
    git_in(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "chmod")
    up_x = git_in(repo, "rev-parse", "HEAD")
    dest = str(tmp_path / "graft-dest")
    os.makedirs(dest)
    grafted, problems = PT._graft_upstream(up_x, dest, git_dir=str(repo))
    assert problems == [] and owned in grafted
    other = next(r for r in PT._UPSTREAM_OWNED if r != owned)
    # What the assembled tree records is the upstream ref's mode, on every platform.
    modes = PT.assembled_modes(None, up_x, grafted, git_dir=str(repo))
    assert modes[owned] == "100755" and modes[other] == "100644"
    if sys.platform != "win32":                 # Windows files carry no execute bit
        assert os.stat(os.path.join(dest, owned)).st_mode & 0o111 == 0o111
        assert os.stat(os.path.join(dest, other)).st_mode & 0o111 == 0


def test_a_graft_that_fails_fails_check_published(release, monkeypatch, capsys):
    repo, up, rel, pub = release
    assert _main(monkeypatch, rel, "--upstream=no-such-ref", f"--check-published={pub}") == 1
    assert "does not resolve" in capsys.readouterr().out
    # a ref that resolves but lacks the owned files
    assert _main(monkeypatch, rel, f"--upstream={rel}", f"--check-published={pub}") == 1
    assert "declared upstream-owned but absent" in capsys.readouterr().out


@pytest.mark.parametrize("ours_cites", [False, True])
def test_dangling_references_in_grafted_files_are_not_ours_to_fix(
        tmp_path, monkeypatch, capsys, ours_cites):
    """Check 2 reads this tree's own files: an upstream-owned file that cites the pruned tier
    is not a failure, one of ours is."""
    repo = tmp_path / "dangling"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    cites = f"See {PT._INTERNAL}notes.md for more.\n"
    owned = {r: cites for r in PT._UPSTREAM_OWNED}
    up = commit_files(repo, owned, "upstream")
    tier = {PT._INTERNAL + "notes.md": OLD, **{r: GUIDE for r in PT._INTERNAL_FILES}}
    with open(os.path.join(_ROOT, "tools", "leak_scan.py"), encoding="utf-8") as f:
        scanner = f.read()
    app = {"README.md": INTRO + (cites if ours_cites else ""), "tests/test_a.py": "x = 1\n",
           "tools/leak_scan.py": scanner}
    rel = commit_files(repo, {**app, **tier, **{r: None for r in owned}}, "release")
    monkeypatch.setattr(PT, "_ROOT", str(repo))
    quiet = {"read_sync_points": lambda rev: [],
             "check_upstream_sync": lambda *a, **k: [],
             "check_release_notes": lambda *a, **k: ("", []),
             "check_leak_scan": lambda dest: [],
             "build_core": lambda dest, work: (None, "stubbed")}
    for name, fn in quiet.items():
        monkeypatch.setattr(PT, name, fn)
    run = PT.subprocess.run

    def run_git_only(cmd, *args, **kwargs):
        if cmd[0] == PT.sys.executable:
            return subprocess.CompletedProcess(cmd, 0, stdout="stub\n", stderr="")
        return run(cmd, *args, **kwargs)

    monkeypatch.setattr(PT, "subprocess", _subprocess_with(run_git_only))
    _main(monkeypatch, rel, f"--upstream={up}")      # exits 1: the stubbed core build
    out = capsys.readouterr().out
    if ours_cites:
        assert "dangling references" in out and "README.md" in out
        line = out.split("dangling references", 1)[1].splitlines()[0]
        assert not [r for r in PT._UPSTREAM_OWNED if r in line], line
    else:
        assert "dangling references" not in out


@pytest.mark.parametrize("keep", [False, True])
def test_a_publish_build_that_stops_removes_its_working_directories(release, monkeypatch,
                                                                    tmp_path, keep):
    """A helper that exits midway (here the export) leaves neither the assembled tree nor the
    native core's work directory behind; --keep keeps the tree only."""
    repo, up, rel, pub = release
    made = []

    def mkdtemp(prefix):
        made.append(tmp_path / f"{prefix}{len(made)}")
        made[-1].mkdir()
        return str(made[-1])

    def stop(*_a, **_k):
        raise SystemExit("git cat-file did not return blob x")

    monkeypatch.setattr(PT, "tempfile", types.SimpleNamespace(mkdtemp=mkdtemp))
    monkeypatch.setattr(PT, "_assemble", stop)
    with pytest.raises(SystemExit, match="did not return blob"):
        _main(monkeypatch, rel, f"--upstream={up}", *(["--keep"] if keep else []))
    assert len(made) == 2
    assert [d.exists() for d in made] == [keep, False]


def test_check_published_lists_twenty_differences_and_counts_the_rest(release, monkeypatch,
                                                                      capsys):
    repo, up, rel, pub = release
    extra = commit_files(repo, {f"extra/{i:02d}.md": GUIDE for i in range(25)}, "extras")
    assert _main(monkeypatch, rel, f"--upstream={up}", f"--check-published={extra}") == 1
    out = capsys.readouterr().out
    assert out.count(f"{extra} is not the assembled tree: ") == 20
    assert "extra/19.md: in" in out and "extra/20.md" not in out
    assert "... and 5 more difference(s)" in out


@pytest.mark.parametrize("argv", [["--check-published"], ["--check-published", "pub"],
                                  ["--check-published="]])
def test_check_published_without_its_ref_after_an_equals_sign_is_refused(release, monkeypatch,
                                                                         capsys, argv):
    repo, up, rel, pub = release
    monkeypatch.setattr(PT, "_build", lambda *a, **k: pytest.fail("a tree was built"))
    assert _main(monkeypatch, rel, *argv) == 2
    assert "--check-published takes a value" in capsys.readouterr().err


@pytest.mark.parametrize("arg, message", [
    ("--check-publish=pub", "unknown option --check-publish"),
    ("--check_published=pub", "unknown option --check_published"),
    ("--kep", "unknown option --kep"),
    ("--keep=yes", "--keep takes no value")])
def test_an_unknown_option_or_a_flag_given_a_value_is_refused(release, monkeypatch, capsys,
                                                              arg, message):
    repo, up, rel, pub = release
    monkeypatch.setattr(PT, "_build", lambda *a, **k: pytest.fail("a tree was built"))
    assert _main(monkeypatch, rel, arg) == 2
    assert message in capsys.readouterr().err
