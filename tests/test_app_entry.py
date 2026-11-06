"""The PyInstaller spec's entry script: it starts when run by path, as the frozen app runs it.

It starts a subprocess, as each OS's frozen app does, so it is marked platform. No Qt is
built (`--help` exits before any is).

Run: python3 -m pytest tests/test_app_entry.py
"""
import os

import pytest

pytestmark = pytest.mark.platform


def _spec_entry_script() -> str:
    """The script the PyInstaller spec names as Analysis' entry point."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "swi3s-studio.spec"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'Analysis\(\s*\[os\.path\.join\("([^"]+)",\s*"([^"]+)"\)\]', text)
    assert m, "the spec's Analysis() entry script was not found"
    return os.path.join(root, m.group(1), m.group(2))


def test_spec_entry_script_runs_as_a_script():
    """PyInstaller runs the entry script by path, as `__main__` with no parent package, so it
    must start from there (`--help` exits 0 before any Qt is built)."""
    import subprocess
    import sys
    script = _spec_entry_script()
    assert os.path.isfile(script)
    root = os.path.dirname(os.path.dirname(script))
    env = dict(os.environ, PYTHONPATH=root)
    r = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True,
                       env=env, cwd=os.path.dirname(script), timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    assert "usage" in r.stdout.lower()
