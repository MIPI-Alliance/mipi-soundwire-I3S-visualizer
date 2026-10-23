"""tools/publish_tree.py: check 3b (the release tree keeps what was merged upstream),
`--record-sync`, and the docs/releases/next.md rules of check 4.

Publishing replaces the published tree wholesale, so anything merged upstream that the release
tree has not taken in is reverted. These run the check on a throwaway git repository standing
in for the upstream branch, and on assembled trees built by hand.
"""
import importlib.util
import os
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool():
    spec = importlib.util.spec_from_file_location(
        "publish_tree", os.path.join(_ROOT, "tools", "publish_tree.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PT = _tool()

OLD = "Record a deferred finding in the maintainers' register meanwhile.\n"
NEW = "Open an issue with the tech-debt label for a deferred finding.\n"
INTRO = "This project decodes captures of a two-wire audio bus for review.\n"
GUIDE = "Report a problem with the steps that show it and the file it needs.\n"


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, files, message):
    for rel, text in files.items():
        full = repo / rel
        if text is None:
            full.unlink()
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _publish(repo, files, version):
    return _commit(repo, files, f"Release {version}\n\n{PT._PUBLISH_MARK} It is the tree.")


@pytest.fixture
def upstream(tmp_path):
    """An upstream branch: a publish (P0), then a contributor's change (P1) that rewrites a
    sentence of DEVELOPMENT.md, adds CONTRIBUTING.md and edits the upstream-owned
    CODEOWNERS."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    p0 = _publish(repo, {"DEVELOPMENT.md": INTRO + OLD, "CODEOWNERS": "* @a\n",
                         "tool.py": "x = 1\n"}, "1.0.0")
    p1 = _commit(repo, {"DEVELOPMENT.md": INTRO + NEW, "CONTRIBUTING.md": GUIDE,
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
    _git(repo, "checkout", "-q", "-b", "side")
    stray = _commit(repo, {"tool.py": "x = 3\n"}, "elsewhere")
    _git(repo, "checkout", "-q", "main")
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
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "record")
    assert PT.read_sync_points("HEAD", git_dir=str(repo)) == [p0, p1]


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
