"""The repository's .gitignore: every rule is a pattern git applies as written.

Git starts a comment only in column 0, so text after a pattern on the same line is part of
the pattern, and that rule matches nothing it was written for.
"""
import os
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rules():
    with open(os.path.join(_ROOT, ".gitignore"), encoding="utf-8") as f:
        for n, line in enumerate(f.read().splitlines(), 1):
            if line.strip() and not line.startswith("#"):
                yield n, line


def test_no_rule_carries_a_trailing_comment():
    bad = [f".gitignore:{n}: {line}" for n, line in _rules() if " #" in line or "\t#" in line]
    assert not bad, "a comment goes on its own line:\n  " + "\n  ".join(bad)


@pytest.mark.parametrize("path", ["frame_model.json", "bus_config.csv", "init.csv"])
def test_the_root_scratch_files_are_ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=_ROOT,
                       capture_output=True, text=True)
    if r.returncode == 128:
        pytest.skip("not a git checkout")
    assert r.returncode == 0, f"{path} is not ignored by .gitignore"
