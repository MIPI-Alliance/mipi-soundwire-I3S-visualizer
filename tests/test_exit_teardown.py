"""Nothing Python owns is left for PySide's sweep at interpreter exit.

At exit PySide destroys every QObject Python still owns, in an order unrelated to the parent
tree, and PySide 6.12 crashed doing it after fully passing runs (exit 139, 138 or 134) and
could on quitting the app. swi3s_studio/ui/teardown.py deletes those trees first; the app
calls it once its event loop returns and conftest at the end of a session.

The crash itself depends on what the freed memory holds, so it is not reproducible on
demand, and not at all below 6.12. What is checked is the state that causes it: the number
of Python-owned objects when the sweep would begin, taken from an atexit hook registered
after PySide's own, so it runs first. Each case runs in its own interpreter, because the
helper deletes every such tree in the process, a pooled run's fixtures included.
"""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

_PRELUDE = """
import atexit, os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["SWI3S_DEMO_SAMPLES"] = "300"
from PySide6.QtCore import QSettings
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tempfile.mkdtemp(prefix="swi3s_exit_"))
from swi3s_studio.ui.teardown import python_owned_qobjects

def _report():
    print("PY-OWNED-AT-EXIT", len(python_owned_qobjects()), flush=True)
atexit.register(_report)                  # after PySide's: atexit runs it first
"""


def _env() -> dict:
    """The child's environment: this tree first on the path, then whatever the run already
    had there, which is where a build outside the tree puts the native core."""
    path = os.pathsep.join(p for p in (str(_ROOT), os.environ.get("PYTHONPATH", "")) if p)
    return dict(os.environ, PYTHONPATH=path)


def _run(body: str) -> subprocess.CompletedProcess:
    env = _env()
    return subprocess.run([sys.executable, "-c", _PRELUDE + textwrap.dedent(body)],
                          cwd=_ROOT, env=env, capture_output=True, text=True, timeout=180)


def _left(proc: subprocess.CompletedProcess) -> int:
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PY-OWNED-AT-EXIT")]
    assert lines, f"the child never reached exit (rc {proc.returncode}):\n{proc.stderr[-3000:]}"
    return int(lines[-1].split()[1])


def test_quitting_the_app_leaves_nothing_for_the_exit_sweep():
    """Through app.main with the demo loaded: without the release, 335 Python-owned
    objects (labels, spin boxes, menus, plot items) were still alive at this point on
    PySide 6.12, and 68 on 6.11."""
    proc = _run("""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        _exec = QApplication.exec
        def _exec_then_quit(*_):
            QTimer.singleShot(1500, QApplication.quit)
            return _exec()
        QApplication.exec = _exec_then_quit
        from swi3s_studio import app
        sys.exit(app.main(["swi3s-studio", "--demo"]))
    """)
    assert _left(proc) == 0, proc.stdout
    assert proc.returncode == 0, proc.stderr[-3000:]


def test_a_tree_reparented_through_a_layout_is_deleted_from_its_root():
    """The shape that crashed: children built with no parent and added to a layout that has
    none either. PySide 6.11 handed their ownership to C++ on addWidget; 6.12 does not, so
    they stay Python-owned under a C++ parent, and only there is there anything to delete."""
    proc = _run("""
        from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel,
                                       QVBoxLayout, QWidget)
        from swi3s_studio.ui.teardown import release_python_owned_qobjects
        app = QApplication([])
        top = QWidget()
        bar = QHBoxLayout()
        kids = [QLabel("Port:"), QComboBox()]
        for k in kids:
            bar.addWidget(k)
        QVBoxLayout(top).addLayout(bar)
        print("LEFT", release_python_owned_qobjects(), flush=True)
    """)
    assert "LEFT 0" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    assert _left(proc) == 0
    assert proc.returncode == 0, proc.stderr[-3000:]


def test_a_running_thread_is_left_alone():
    """Destroying a running QThread aborts the process, so the helper skips one and counts
    it as left over rather than deleting it."""
    proc = _run("""
        from PySide6.QtCore import QCoreApplication, QThread
        from swi3s_studio.ui.teardown import release_python_owned_qobjects
        app = QCoreApplication([])
        th = QThread()
        th.start()
        print("LEFT", release_python_owned_qobjects(), flush=True)
        th.quit()
        th.wait()
        print("AFTER-JOIN", release_python_owned_qobjects(), flush=True)
    """)
    assert "LEFT 1" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    assert "AFTER-JOIN 0" in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stderr[-3000:]


# The app under test, with `before` run as its event loop starts and the loop ended with 7
# should it still be running 15 s later.
_IN_THE_LOOP = """
    import signal, threading, time
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    _exec = QApplication.exec
    def _exec_with(*_):
        {before}
        QTimer.singleShot(15000, lambda: QApplication.instance().exit(7))
        return _exec()
    QApplication.exec = _exec_with
    from swi3s_studio import app
    from swi3s_studio.ui import main_window as mw
"""
_MAIN = """
    sys.exit(app.main(["swi3s-studio", "--demo"]))
"""
_POSIX_KILL = pytest.mark.skipif(sys.platform == "win32",
                                 reason="asserts the POSIX default action, killed by SIGINT")


def _app(before: str, patch: str = "") -> str:
    return (textwrap.dedent(_IN_THE_LOOP).format(before=before) + textwrap.dedent(patch)
            + textwrap.dedent(_MAIN))


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Popen.send_signal cannot send SIGINT on Windows")
def test_ctrl_c_in_the_terminal_quits_the_idle_app():
    """Ctrl+C from the terminal while the event loop waits, with no Python running, closes
    the window as File ▸ Quit does, through app.main, leaving nothing for the exit sweep."""
    import signal
    env = _env()
    body = _PRELUDE + _app('QTimer.singleShot(0, lambda: print("READY", flush=True))')
    proc = subprocess.Popen([sys.executable, "-c", body], cwd=_ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY", "the event loop never started"
        time.sleep(1.0)                             # the window up and idle
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("the app was still running 20 s after Ctrl+C")
    assert proc.returncode == 0, err[-3000:]
    assert "KeyboardInterrupt" not in err, err[-3000:]
    assert "PY-OWNED-AT-EXIT 0" in out, out


def test_ctrl_c_before_the_event_loop_quits_once_it_runs():
    """Pressed while the window is still being built (the demo loads before the loop), it
    is kept and served when the loop starts, instead of being lost."""
    proc = _run(_app("pass", """
        _load = mw.MainWindow.load_demo
        def _load_pressed(self, *a, **k):
            signal.raise_signal(signal.SIGINT)
            return _load(self, *a, **k)
        mw.MainWindow.load_demo = _load_pressed
    """))
    assert proc.returncode == 0, f"rc {proc.returncode} (7: still running)\n{proc.stderr[-3000:]}"
    assert "KeyboardInterrupt" not in proc.stderr, proc.stderr[-3000:]
    assert _left(proc) == 0


def test_ctrl_c_closes_the_window_from_the_event_loop_not_inside_a_callback():
    """Pressed while a Python callback runs, the close waits for the callback to return,
    so no closeEvent runs nested in the code the signal interrupted."""
    proc = _run(_app("QTimer.singleShot(300, _busy)", """
        state = {"busy": False}
        def _busy():
            state["busy"] = True
            signal.raise_signal(signal.SIGINT)
            sum(range(200_000))                       # the callback carries on
            win = next(w for w in QApplication.topLevelWidgets()
                       if isinstance(w, mw.MainWindow))
            print("CALLBACK-ENDS visible", win.isVisible(), flush=True)
            state["busy"] = False
        _close = mw.MainWindow.closeEvent
        def _close_noted(self, event):
            print("CLOSE-EVENT in-callback", state["busy"], flush=True)
            return _close(self, event)
        mw.MainWindow.closeEvent = _close_noted
    """))
    assert "CALLBACK-ENDS visible True" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    assert "CLOSE-EVENT in-callback False" in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stderr[-3000:]


def test_a_refused_close_leaves_ctrl_c_quitting():
    """A close the window refuses is not a quit under way: the next Ctrl+C asks again
    rather than killing the process."""
    proc = _run(_app("QTimer.singleShot(300, lambda: signal.raise_signal(signal.SIGINT)); "
                     "QTimer.singleShot(900, lambda: signal.raise_signal(signal.SIGINT))", """
        _close = mw.MainWindow.closeEvent
        refused = []
        def _refuse_once(self, event):
            if not refused:
                refused.append(1)
                print("REFUSED", flush=True)
                event.ignore()
                return None
            return _close(self, event)
        mw.MainWindow.closeEvent = _refuse_once
    """))
    assert "REFUSED" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    assert proc.returncode == 0, f"rc {proc.returncode}\n{proc.stderr[-3000:]}"
    assert _left(proc) == 0


@_POSIX_KILL
def test_a_second_ctrl_c_ends_a_quit_that_hangs():
    """Once the close is under way, Ctrl+C is the default again: a close stuck joining a
    worker (in C++, where no Python handler runs) ends at the second press."""
    import signal
    proc = _run(_app("QTimer.singleShot(300, lambda: signal.raise_signal(signal.SIGINT))", """
        def _hang(self, event):
            print("CLOSING", flush=True)
            threading.Timer(0.3, signal.raise_signal, (signal.SIGINT,)).start()
            time.sleep(20)
            print("HANG-ENDED", flush=True)
        mw.MainWindow.closeEvent = _hang
    """))
    assert "CLOSING" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    assert "HANG-ENDED" not in proc.stdout
    assert proc.returncode == -signal.SIGINT, f"rc {proc.returncode}\n{proc.stderr[-3000:]}"


@_POSIX_KILL
def test_ctrl_c_after_the_event_loop_ends_the_process():
    """Past the loop, while app.main joins the workers, Ctrl+C ends the process: nothing is
    left to serve it from the loop."""
    import signal
    proc = _run(_app("QTimer.singleShot(300, QApplication.quit)", """
        _join = mw.MainWindow.join_worker_threads
        joins = []
        def _join_hangs_after_the_loop(self):
            joins.append(1)
            if len(joins) < 2:                        # closeEvent's, in the loop
                return _join(self)
            print("JOINING", flush=True)
            threading.Timer(0.3, signal.raise_signal, (signal.SIGINT,)).start()
            time.sleep(20)
            print("JOIN-ENDED", flush=True)
            return _join(self)
        mw.MainWindow.join_worker_threads = _join_hangs_after_the_loop
    """))
    assert "JOINING" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    assert "JOIN-ENDED" not in proc.stdout
    assert proc.returncode == -signal.SIGINT, f"rc {proc.returncode}\n{proc.stderr[-3000:]}"


def test_closing_the_window_quits_with_another_top_level_window_open():
    """The app ends with its window. Qt's own rule (quit when the last window closes) counts
    every visible top-level window, so one with no parent left open, such as pyqtgraph's
    Export dialog from a plot's right-click menu, kept the event loop running with nothing
    on screen and the terminal never returned."""
    proc = _run("""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QWidget
        _exec = QApplication.exec
        def _exec_then_close(*_):
            stray = QWidget()                       # no parent: a top-level window
            stray.setWindowTitle("stray")
            stray.show()
            win = next(w for w in QApplication.topLevelWidgets()
                       if type(w).__name__ == "MainWindow")
            QTimer.singleShot(500, win.close)
            QTimer.singleShot(15000, lambda: (print("STILL-RUNNING", flush=True),
                                              QApplication.exit(7)))
            return _exec()
        QApplication.exec = _exec_then_close
        from swi3s_studio import app
        sys.exit(app.main(["swi3s-studio", "--demo"]))
    """)
    assert "STILL-RUNNING" not in proc.stdout, "the event loop outlived the window"
    assert proc.returncode == 0, proc.stderr[-3000:]
