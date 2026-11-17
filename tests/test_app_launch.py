"""App launch helper: focusing/activating the window on startup.

`app._activate` raises and focuses the window and, on macOS, activates an unbundled
`python -m swi3s_studio.app` process so it owns the native menu bar. This checks the
helper is import-safe and does not raise, on a real window and on a minimal stub
(PyObjC / macOS may be absent).

Run: QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_app_launch.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow

from swi3s_studio import app as _app

pytestmark = pytest.mark.gui


def test_activate_is_import_safe():
    QApplication.instance() or QApplication([])
    # Real window: raise_/activateWindow (+ the guarded macOS AppKit path) must not raise.
    w = QMainWindow()
    w.show()
    _app._activate(w)

    # Minimal stub exercises the cross-platform focus path with no Qt window state.
    class _Stub:
        raised = activated = False
        def raise_(self):
            self.raised = True
        def activateWindow(self):
            self.activated = True
    s = _Stub()
    _app._activate(s)
    assert s.raised and s.activated, "activate must raise + focus the window"


_LOG_PROBE = """
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QLoggingCategory
from PySide6.QtWidgets import QApplication
from swi3s_studio import app
app._set_qt_log_rules()
qt = QApplication([])
cat = QLoggingCategory("qt.multimedia.ffmpeg")
print(cat.isInfoEnabled(), cat.isWarningEnabled())
"""


@pytest.mark.parametrize("env_rules, want", [(None, "False True"),
                                             ("qt.multimedia.ffmpeg.info=true", "True True")])
def test_the_ffmpeg_backend_banner_is_hidden_and_its_warnings_are_not(env_rules, want):
    """The app's Qt logging rules turn off qt.multimedia.ffmpeg's info messages (Qt's FFmpeg
    version banner) and keep its warnings; QT_LOGGING_RULES can turn the banner back on."""
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if k != "QT_LOGGING_RULES"}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH", "")) if p)
    if env_rules:
        env["QT_LOGGING_RULES"] = env_rules
    r = subprocess.run([sys.executable, "-c", _LOG_PROBE], cwd=root, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.split()[-2:] == want.split()
