"""The version the application shows: the release number, and in a development checkout the
commits since its tag (swi3s_studio/build_id.py)."""
import subprocess

import pytest

from swi3s_studio import build_id


@pytest.mark.parametrize("described,shown", [
    ("v3.2.0-0-gabc1234", "3.2.0"),
    ("v3.2.0-0-gabc1234-dirty", "3.2.0 (abc1234, modified)"),
    ("v3.2.0-12-gabc1234", "3.2.0+12 (abc1234)"),
    ("v3.2.0-12-gabc1234-dirty\n", "3.2.0+12 (abc1234, modified)"),
    ("v3.1.0-5-gabc1234", "3.2.0"),
    ("", "3.2.0"),
])
def test_a_checkout_past_the_release_tag_names_its_commit(described, shown):
    assert build_id.describe_label(described, "3.2.0") == shown


def test_no_checkout_or_no_git_shows_the_release_number(monkeypatch, tmp_path):
    build_id.display_version.cache_clear()
    monkeypatch.setattr(build_id, "_ROOT", tmp_path)
    assert build_id.display_version() == build_id.__version__
    build_id.display_version.cache_clear()
    (tmp_path / ".git").mkdir()

    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", no_git)
    assert build_id.display_version() == build_id.__version__
    build_id.display_version.cache_clear()


def test_git_output_that_is_not_utf8_gives_the_release_number(monkeypatch, tmp_path):
    """The output is decoded here, not by the locale code page, and never raises."""
    build_id.display_version.cache_clear()
    monkeypatch.setattr(build_id, "_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()

    def run(cmd, **kw):
        if kw.get("text") or kw.get("encoding"):
            raise UnicodeDecodeError("charmap", b"\x8f", 0, 1, "undefined")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"v3.2.0-4-g\xff\xfe", stderr=b"")
    monkeypatch.setattr(subprocess, "run", run)
    assert build_id.display_version() == build_id.__version__
    build_id.display_version.cache_clear()


def test_a_checkout_without_the_release_tag_shows_the_release_number(
        isolated_git, monkeypatch, tmp_path):
    from conftest import commit_files, git_in
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    commit_files(repo, {"a.txt": "one\n"})
    git_in(repo, "tag", "v0.0.0-other")
    commit_files(repo, {"a.txt": "two\n"})
    monkeypatch.setattr(build_id, "_ROOT", repo)
    build_id.display_version.cache_clear()
    try:
        assert build_id.display_version() == build_id.__version__
    finally:
        build_id.display_version.cache_clear()


def test_the_docs_say_what_the_build_id_shows_at_the_tag():
    import os

    from conftest import current_notes, current_notes_path
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = " ".join(build_id.__doc__.split())
    assert "no git, no checkout, no release tag in the checkout or a slow git" in doc
    for name in (current_notes_path(), os.path.join(root, "docs/TESTING.md")):
        with open(name, encoding="utf-8") as f:
            text = " ".join(f.read().split())
        assert "while files differ" not in text, name
    assert "while tracked files differ from it" in current_notes()


def test_display_version_in_a_real_repository(isolated_git, monkeypatch, tmp_path):
    from conftest import commit_files, git_in
    repo = tmp_path / "repo"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    commit_files(repo, {"a.txt": "one\n"})
    git_in(repo, "tag", f"v{build_id.__version__}")
    monkeypatch.setattr(build_id, "_ROOT", repo)
    v = build_id.__version__

    def shown() -> str:
        build_id.display_version.cache_clear()
        return build_id.display_version()
    try:
        assert shown() == v
        sha = commit_files(repo, {"a.txt": "two\n"})
        short = git_in(repo, "rev-parse", "--short", "HEAD")
        assert sha.startswith(short)
        assert shown() == f"{v}+1 ({short})"
        (repo / "a.txt").write_text("three\n")
        assert shown() == f"{v}+1 ({short}, modified)"
        git_in(repo, "checkout", "a.txt")
        git_in(repo, "reset", "-q", "--hard", f"v{v}")
        (repo / "a.txt").write_text("changed\n")
        tagged = git_in(repo, "rev-parse", "--short", "HEAD")
        assert shown() == f"{v} ({tagged}, modified)"
    finally:
        build_id.display_version.cache_clear()


@pytest.mark.gui
def test_the_window_title_and_about_show_it(monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from swi3s_studio.ui import main_window
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "display_version", lambda: "9.9.9+SENTINEL")
    win = main_window.MainWindow()
    assert "9.9.9+SENTINEL" in win.windowTitle()
    about: list = []
    monkeypatch.setattr(QMessageBox, "about", lambda *a: about.append(a[2]))
    win.show_about()
    assert about and "9.9.9+SENTINEL" in about[0]


@pytest.mark.gui
def test_the_results_export_calls_it():
    import inspect

    from swi3s_studio.ui import timing_view
    assert "display_version()" in inspect.getsource(timing_view)
