"""Shared pytest fixtures / setup.

Keep the GUI tests fast: MainWindow preloads the demo capture, which is 1 s of audio
(~24.6M UIs, ~2.5 s to decode) by default. Force a short demo for the test process so
constructing a MainWindow stays near-instant. Tests that need specific audio lengths
call Session.from_demo(N) with an explicit N, which this does not affect.
"""
import os

os.environ.setdefault("SWI3S_DEMO_SAMPLES", "300")   # short preload demo for MainWindow()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# QSettings needs a non-empty organisation/application name to persist reliably on
# Windows (its registry scope is keyed on them). The real app sets these in app.py, but
# tests build a bare QApplication — so per-mode dir-memory (capture/last_dir_*) tests
# didn't round-trip on Windows and fell back to the default examples dir. Set a
# TEST-scoped identity here (distinct from the app's, so it never touches a developer's
# real saved settings), before any test constructs a QApplication.
from PySide6.QtCore import QCoreApplication  # noqa: E402

QCoreApplication.setOrganizationName("MIPI SWI3S Test")
QCoreApplication.setApplicationName("SWI3S Studio Test")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Join every MainWindow's worker threads before the interpreter tears down.

    GUI tests build MainWindows and rarely close them, so their load / locate /
    TX-persistence QThreads can still be alive at exit. Qt calls abort() when a running
    QThread is destroyed, which turns a fully passing run into a SIGABRT (exit 134) —
    invisible under pytest, which reports its result before teardown, but fatal to
    tests/run_all.sh, which judges each suite by its exit code.

    Fixing it here rather than per-test covers every suite, including ones added later.
    """
    _join_all_main_windows()


def _join_all_main_windows() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        join = getattr(w, "join_worker_threads", None)
        if callable(join):
            try:
                join()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass
