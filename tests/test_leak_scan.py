"""The structural leak patterns must catch real leaks and not fire on prose.

`tools/leak_scan.py` is a release gate, so both of its failure modes are
expensive and neither is visible from a green run:

* a **false negative** publishes the thing the scanner exists to stop;
* a **false positive** fails the gate on a legitimate file, and the tempting
  fix — an allowlist entry — is exactly what `docs/DEVELOPMENT.md` forbids,
  because it silences the pattern for that file permanently.

This file pins the discrimination itself, in both directions: a spec-section
citation (`§10.1.4.4.2`) must not match the private-IPv4 pattern, and a genuine
address at the end of a sentence ("the host is 10.1.4.4.") must.

Only STRUCTURAL patterns are tested here. The site-specific list lives outside
the repository on purpose (see docs/DEVELOPMENT.md); a test that asserted
against its contents would republish exactly what it is meant to hide.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _patterns():
    spec = importlib.util.spec_from_file_location(
        "leak_scan", os.path.join(_ROOT, "tools", "leak_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._STRUCTURAL


def _hits(text: str) -> bool:
    """Case-insensitive, as the scanner compiles every pattern."""
    return any(re.search(rx, text, re.I) for rx, _ in _patterns())


# Real leakage, in the shapes it actually appears in: bare, in a URL, followed
# by a port, and ending a sentence.
@pytest.mark.parametrize("text", [
    "ssh user@10.0.1.42",
    "the lab host is 10.1.4.4 today",
    "10.20.30.40",
    "192.168.1.1",
    "172.16.5.9",
    "connect to 10.1.4.4:8080",
    "IP=10.1.4.4.",
    "reachable at 10.1.4.4, then",
    "(10.1.4.4)",
    "/Users/somebody/secrets",
    r"C:\Users\somebody\cap.sal",
    r'{"path": "C:\\Users\\somebody\\cap.sal"}',
    r"d:\\users\\somebody",
    "/home/somebody/gate-run/log.txt",
    "id_ed25519",
    "ssh -i ~/.ssh/somekey host",
    "/Volumes/Scratch Disk/work/capture.sal",
    "/Volumes/0A1B2C3D-0000-4000-8000-00000000AAAA/Library/x",
    "~/Library/Mobile Documents/com~apple~CloudDocs/project",
    r"C:\Users\x\iCloudDrive\Mobile Documents\y",
    "com~apple~CloudDocs",
    "/private/var/Library/Daemon Containers/0000/Data",
    "~/Library/Containers/some.app/Data",
])
def test_a_real_leak_is_caught(text):
    assert _hits(text), f"structural scan MISSED a real leak: {text!r}"


# Spec citations. The repository cites SWI3S sections constantly, and §10.x is
# the family that collides with 10.0.0.0/8. A four-part §10 citation is still
# rejected by the § prefix; a five-part one by the octet count.
@pytest.mark.parametrize("text", [
    "the bus keeper holds the level (§10.1.4.4.2)",
    "§10.1.4.4",
    "spec §10.1.2.3.4",
    "Safe-Lock CDS position (§12.1.10.1)",
    "handover duration (§11.1.1.1)",
    "descrambler against spec §14.1.3.2",
    "version 10.1.4.4.2",
    "/home/runner/work/swi3s-studio",
    "the volumes of data each capture holds",
    "https://example.invalid/volumes/3/",
    "mobile documents and desktop documents",
])
def test_a_spec_citation_is_not_a_leak(text):
    assert not _hits(text), f"structural scan FALSE-POSITIVED on: {text!r}"


def test_the_octet_guard_rejects_five_parts_not_a_trailing_period():
    """Five parts is a citation; a trailing period is not, pinned on its own.

    A dot means "not an address" only when a DIGIT follows it. A sentence-final
    period after a valid four-octet address is still a leak.
    """
    assert _hits("the host is 10.1.4.4.")          # sentence end — still a leak
    assert not _hits("§10.1.4.4.2")                # five parts — a citation


# --------------------------------------------------------------- the CLI's own contract
# Everything above tests the patterns. These test the two ways the tool could report a pass
# about something it did not actually check.

def _run(*args, cwd=_ROOT):
    return subprocess.run([sys.executable, os.path.join(_ROOT, "tools", "leak_scan.py"), *args],
                          cwd=cwd, capture_output=True, text=True)


def test_an_unrecognised_option_is_refused_not_ignored():
    """An unrecognised option (e.g. a mistyped `--text=<f>`) exits 2 instead of falling
    back to scanning the working tree and printing "no leaks found" about input the caller
    never named."""
    for bad in ("--tex=/etc/hosts", "--txt=/etc/hosts", "--verbose"):
        r = _run(bad)
        assert r.returncode == 2, f"{bad} exited {r.returncode}, not 2:\n{r.stdout}"
        assert "no leaks found" not in r.stdout, f"{bad} reported a PASS: {r.stdout!r}"


def test_a_missing_text_file_is_refused_not_read_as_empty():
    r = _run("--text=/nonexistent/description.md")
    assert r.returncode == 2
    assert "no leaks found" not in r.stdout


def test_the_scanner_scans_its_own_prose_for_site_patterns():
    """The self-exemption is per pattern KIND, not per file.

    tools/leak_scan.py and this file must skip the STRUCTURAL patterns — they contain them
    as literal source. They must NOT skip the site-specific ones, which cannot self-match
    because they live outside the repository, so both files stay in the scanned list.
    """
    spec = importlib.util.spec_from_file_location(
        "leak_scan", os.path.join(_ROOT, "tools", "leak_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "tools/leak_scan.py" in mod._SELF and "tests/test_leak_scan.py" in mod._SELF
    # Both are in the scanned file list; exemption happens per pattern, not by omission.
    files = mod._files(None)
    for name in mod._SELF:
        assert name in files, (
            f"{name} is excluded from the file list, so NO pattern reaches it — the "
            "exemption must be per pattern kind (see _SELF)")


def test_a_tree_that_is_not_a_checkout_skips_the_core_its_gate_built(tmp_path, monkeypatch):
    """A gate guest scans an exported tree with the native core built into its top level;
    that file is gitignored, never published, and named for its platform, so the walk leaves
    it out (top level only, as .gitignore has it) and keeps everything else."""
    spec = importlib.util.spec_from_file_location(
        "leak_scan", os.path.join(_ROOT, "tools", "leak_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("swi3score.cpython-312-someplatform.so", "swi3score.cp312-win_amd64.pyd",
                 "README.md", "native/swi3score_notes.so", "docs/swi3score.so.md"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "_ROOT", str(tmp_path))
    assert mod._files(None) == ["README.md", "docs/swi3score.so.md", "native/swi3score_notes.so"]


def test_the_working_tree_scan_includes_untracked_files_and_skips_ignored_ones(
        tmp_path, monkeypatch):
    """The gate runs before committing, so a file not yet added is part of what it scans;
    a file .gitignore excludes is not."""
    spec = importlib.util.spec_from_file_location(
        "leak_scan", os.path.join(_ROOT, "tools", "leak_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("x", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "b"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, env=env, capture_output=True)
    (tmp_path / "new_fixture.json").write_text("{}", encoding="utf-8")
    (tmp_path / "run.log").write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "_ROOT", str(tmp_path))
    assert mod._files(None) == [".gitignore", "new_fixture.json", "tracked.txt"]


def _load():
    spec = importlib.util.spec_from_file_location(
        "leak_scan", os.path.join(_ROOT, "tools", "leak_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("pad", [0, 1])
def test_a_stray_nul_does_not_turn_a_text_file_into_utf16(pad):
    """One NUL in an 8-bit text file, at even and odd length: the text is read as text, so
    an address and a home path in it are still seen."""
    body = b"time,value\n# captured on 192.168.1.20 by /Users/alice\n" + b"0,1\n" * 40
    body += b"x" * pad + b"\0"
    text, is_binary = _load().decode_bytes(body)
    assert "192.168.1.20" in text and "/Users/alice" in text, text[:80]
    assert not is_binary


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_is_read_with_or_without_a_byte_order_mark(codec):
    text, is_binary = _load().decode_bytes("host 10.1.4.4 in /Users/alice\n".encode(codec))
    assert "10.1.4.4" in text and "/Users/alice" in text and not is_binary


def test_a_binary_is_still_read_by_its_printable_runs():
    raw = bytes(range(32)) * 8 + b"/Users/alice" + bytes(64)
    text, is_binary = _load().decode_bytes(raw)
    assert is_binary and "/Users/alice" in text


def _tree_with_scanner(tmp_path):
    """An exported tree (no .git) holding a copy of the scanner."""
    import shutil
    dest = tmp_path / "tree"
    (dest / "tools").mkdir(parents=True)
    shutil.copy2(os.path.join(_ROOT, "tools", "leak_scan.py"), dest / "tools")
    return dest


def test_the_scan_catches_the_absolute_path_of_the_tree_it_scans(tmp_path):
    """Wherever the tree lives, a file recording its absolute path is a hit; a relative path
    to the same file is not."""
    dest = _tree_with_scanner(tmp_path)
    (dest / "README.md").write_text("See tools/leak_scan.py.\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "SWI3S_LEAK_PATTERNS"}
    run = [sys.executable, str(dest / "tools" / "leak_scan.py")]
    r = subprocess.run(run, cwd=dest, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout
    (dest / "golden.json").write_text(
        '{"source": "%s/data/x.csv"}\n' % os.path.realpath(dest), encoding="utf-8")
    r = subprocess.run(run, cwd=dest, capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "absolute path of the tree being scanned" in r.stdout, r.stdout
    assert "golden.json:1" in r.stdout


def test_the_walk_skips_what_the_trees_gitignore_excludes_and_nothing_by_name(
        tmp_path, monkeypatch):
    """An exported tree is walked whole: a directory named build, dist, env or scratch is
    read unless the tree's own .gitignore excludes it, and so is a `._name` file with no
    `name` beside it. What .gitignore excludes is counted, not silently dropped."""
    mod = _load()
    files = {
        ".gitignore": "__pycache__/\n*.py[cod]\n/out/\nnative/build/\n"
                      "/frame.json          # trailing text makes this pattern literal\n",
        "examples/env/notes.md": "x", "packaging/dist/readme.md": "x",
        "scratch/keep.md": "x", "native/dev_docs/a.md": "x", "._lonely.md": "x",
        "frame.json": "{}", "notes.md": "x", "._notes.md": "x",
        "pkg/__pycache__/m.cpython-312.pyc": "x", "pkg/m.pyc": "x", "out/a.txt": "x",
        "sub/out/b.txt": "x", "native/build/c.o": "x", "build/native/build/d.o": "x",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(mod, "_ROOT", str(tmp_path))
    assert mod._files(None) == sorted([
        ".gitignore", "._lonely.md", "build/native/build/d.o", "examples/env/notes.md",
        "frame.json", "native/dev_docs/a.md", "notes.md", "packaging/dist/readme.md",
        "scratch/keep.md", "sub/out/b.txt"])
    assert mod._SKIPPED == sorted(["._notes.md", "native/build/", "out/", "pkg/__pycache__/",
                                   "pkg/m.pyc"])


def test_a_gitignore_with_a_re_include_skips_nothing(tmp_path, monkeypatch):
    """`!` re-includes are not evaluated, so a .gitignore using one excludes nothing rather
    than too much."""
    mod = _load()
    (tmp_path / ".gitignore").write_text("build/\n!build/keep.md\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "keep.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "_ROOT", str(tmp_path))
    assert mod._files(None) == [".gitignore", "build/keep.md"]


def test_an_empty_pattern_file_is_reported_as_covering_nothing(tmp_path):
    pats = tmp_path / "patterns.txt"
    pats.write_text("# comments only\n\n", encoding="utf-8")
    dest = _tree_with_scanner(tmp_path)
    env = dict(os.environ, SWI3S_LEAK_PATTERNS=str(pats))
    r = subprocess.run([sys.executable, str(dest / "tools" / "leak_scan.py")], cwd=dest,
                       capture_output=True, text=True, env=env)
    assert "0 site-specific" in r.stdout and "holds NO patterns" in r.stdout, r.stdout


# --------------------------------------------------------------- symlinks and --tree=DIR

def test_a_symlink_to_a_directory_is_listed_and_scanned_by_its_target(tmp_path, monkeypatch):
    """git stores a symlink as its target path, whatever it points at, so the walk lists a
    link to a directory as a file, does not descend into it, and scans the target path."""
    mod = _load()
    dest = _tree_with_scanner(tmp_path)
    (dest / "data").mkdir()
    (dest / "data" / "a.txt").write_text("x\n", encoding="utf-8")
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "inner" / "b.txt").write_text("x\n", encoding="utf-8")
    os.symlink(str(dest / "elsewhere"), dest / "data" / "captures")
    (dest / "elsewhere").mkdir()
    os.symlink(str(outside), dest / "data" / "linked")
    monkeypatch.setattr(mod, "_ROOT", str(dest))
    files = mod._files(None)
    assert files == ["data/a.txt", "data/captures", "data/linked", "tools/leak_scan.py"]
    assert mod._content("data/captures", None) == (str(dest / "elsewhere"), False)
    env = {k: v for k, v in os.environ.items() if k != "SWI3S_LEAK_PATTERNS"}
    r = subprocess.run([sys.executable, os.path.join(_ROOT, "tools", "leak_scan.py"),
                        f"--tree={dest}"], cwd=_ROOT, capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "data/captures:1: absolute path of the tree" in r.stdout, \
        r.stdout
    assert ": 4 files" in r.stdout, r.stdout


def test_tree_scans_a_directory_with_this_checkouts_patterns(tmp_path):
    """--tree=DIR walks DIR with the scanner's own patterns, including the absolute path of
    the checkout the scanner is in, whatever scanner copy DIR holds."""
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / "README.md").write_text("A bus analyzer.\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "SWI3S_LEAK_PATTERNS"}
    run = [sys.executable, os.path.join(_ROOT, "tools", "leak_scan.py"), f"--tree={dest}"]
    r = subprocess.run(run, cwd=str(tmp_path), capture_output=True, text=True, env=env)
    assert r.returncode == 0 and f"the tree at {dest}: 1 files" in r.stdout, r.stdout
    (dest / "golden.json").write_text('{"source": "%s/data/x.csv"}\n' % _ROOT,
                                      encoding="utf-8")
    r = subprocess.run(run, cwd=str(tmp_path), capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "absolute path of the scanner's checkout" in r.stdout, r.stdout


@pytest.mark.parametrize("args", [("--tree=/nonexistent/tree/dir",),
                                  ("--tree=.", "HEAD"),
                                  ("--tree=.", "--text=README.md")])
def test_a_tree_option_that_cannot_be_honoured_is_refused(args):
    r = _run(*args)
    assert r.returncode == 2 and "no leaks found" not in r.stdout, r.stdout
