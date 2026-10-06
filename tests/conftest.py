"""Shared pytest fixtures / setup.

Keep the GUI tests fast: the demo capture (File ▸ Open Demo Capture, MainWindow.load_demo)
is 1 s of audio (~24.6M UIs, ~2.5 s to decode) by default. Force a short demo for the
test process so loading one stays near-instant. Tests that need specific audio lengths
call Session.from_demo(N) with an explicit N, which this does not affect.
"""
import os

import pytest

os.environ.setdefault("SWI3S_DEMO_SAMPLES", "300")   # short demo for MainWindow.load_demo
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

# And keep them in a throwaway ini, not a file of the developer's: the test identity alone
# still wrote ~/Library/Preferences (or the registry) and kept what one run saved for the
# next. Every QSettings() of the run, the app's included, reads and writes this one, and
# each test starts with it empty (_fresh_settings).
import shutil  # noqa: E402
import tempfile  # noqa: E402

from PySide6.QtCore import QSettings  # noqa: E402

_SETTINGS_DIR = tempfile.mkdtemp(prefix="swi3s_test_settings_")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _SETTINGS_DIR)


# ---------------------------------------------------------------- no test may block on a modal
# Offscreen, a modal dialog has nobody to dismiss it: an un-patched QMessageBox or file
# picker is a hang until the CI job's timeout, not a failure (the Locate Sub-Capture test
# did exactly that on its "found N" box). Every modal entry point the app uses is replaced
# for every test with one that answers "cancel" and records the call, and a test that
# opened one it did not patch FAILS at teardown, naming the dialog. A test that means to
# drive a dialog patches the entry point itself; its monkeypatch lands after this one.
# The static helpers run in C++, so patching exec() alone would not catch them.
_MODAL_CANCEL = {
    ("QMessageBox", "question"): None, ("QMessageBox", "information"): None,
    ("QMessageBox", "warning"): None, ("QMessageBox", "critical"): None,
    ("QMessageBox", "about"): None,
    ("QMessageBox", "exec"): 0, ("QDialog", "exec"): 0, ("QMenu", "exec"): None,
    ("QFileDialog", "getOpenFileName"): ("", ""),
    ("QFileDialog", "getOpenFileNames"): ([], ""),
    ("QFileDialog", "getSaveFileName"): ("", ""),
    ("QFileDialog", "getExistingDirectory"): "",
    ("QInputDialog", "getText"): ("", False), ("QInputDialog", "getItem"): ("", False),
    ("QInputDialog", "getInt"): (0, False), ("QInputDialog", "getDouble"): (0.0, False),
    ("QColorDialog", "getColor"): "invalid",        # a cancelled pick: an invalid QColor
}


def _dialog_title(args) -> str:
    for a in args:
        if isinstance(a, str):
            return a
        for attr in ("windowTitle", "text", "labelText", "title"):   # a box's title is not kept
            get = getattr(a, attr, None)                     # on every platform
            if callable(get) and get():
                return get()
    return "?"


@pytest.fixture(autouse=True)
def _no_unexpected_modal(monkeypatch):
    from PySide6 import QtWidgets
    from PySide6.QtWidgets import QMessageBox
    opened = []
    for (cls_name, meth), value in _MODAL_CANCEL.items():
        if cls_name == "QMessageBox" and meth == "question":
            value = QMessageBox.StandardButton.NoButton
        if value == "invalid":
            from PySide6.QtGui import QColor
            value = QColor()

        def stub(*args, _where=f"{cls_name}.{meth}", _value=value, **_kw):
            opened.append(f"{_where}({_dialog_title(args)!r})")
            return _value
        monkeypatch.setattr(getattr(QtWidgets, cls_name), meth, stub)
    # QMenu.exec also has a STATIC overload, exec(actions, pos), and PySide 6.11 resolves
    # `menu.exec(...)` on an instance to the built-in method whatever the class attribute
    # holds: the stub above caught only `QMenu.exec(menu, pos)`, and a right-click menu
    # opened for real and hung. Route the instance
    # lookup through the class attribute, so this stub, or a test's own patch, answers.
    menu_getattr = QtWidgets.QMenu.__getattribute__

    def _menu_getattr(self, name):
        if name == "exec":
            return QtWidgets.QMenu.__dict__["exec"].__get__(self, type(self))
        return menu_getattr(self, name)
    monkeypatch.setattr(QtWidgets.QMenu, "__getattribute__", _menu_getattr)
    yield opened                          # the guard's own test reads (and clears) this
    assert not opened, (
        "the test opened a modal it did not patch (answered 'cancel'; offscreen it would "
        "have hung): " + ", ".join(opened))


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Every test starts with no saved settings: the default appearance, waveform colours
    and weights, folders, the PDM DC-bias choice. They are shared by the whole run, so one
    test's saved choice (or one left behind by a failed assertion before its restore)
    would otherwise reach every test after it."""
    from swi3s_studio.ui import line_style
    QSettings().clear()
    line_style.reset_cache()
    yield
    QSettings().clear()
    line_style.reset_cache()


@pytest.fixture(autouse=True)
def _no_swallowed_slot_exception(monkeypatch):
    """An exception raised in a Qt slot fails the test that raised it. PySide prints it
    and carries on, so the app was broken while the test passed: Open Capture's dialog
    raised an AttributeError as it was built, and every test of it was green."""
    import sys
    import traceback
    raised = []

    def hook(etype, value, tb):
        raised.append("".join(traceback.format_exception(etype, value, tb)))
        sys.__excepthook__(etype, value, tb)
    monkeypatch.setattr(sys, "excepthook", hook)
    yield raised
    assert not raised, "an exception in a Qt slot (PySide printed it and went on):\n" + raised[0]


@pytest.fixture(autouse=True)
def _close_the_tests_windows(monkeypatch):
    """Close, after each test, the MainWindows it built, joining their workers. Left open
    they pile up, and every later test that runs the event loop paid for all of them. A
    window from a wider-scoped fixture was built before the test, so it is left to its
    fixture.

    The windows are recorded as they are built, not found afterwards: listing the
    application's top-level widgets wraps every one of them, a half-destroyed one
    included, and that segfaulted the pooled run. And they are DELETED, not just closed:
    a closed window is kept alive by its own Qt connections to its methods, with every
    Link it decoded, and the pooled run reached 17 GB, enough to exhaust a 4 GB machine.
    Deleting them used to crash later tests through a QTimer.singleShot of the old window
    firing on it; the window's timers now have it as their context, so they die with it.

    A TimingView a test built on its own, with no parent, is deleted here too. Left to the
    collector below, one the test had clicked segfaulted it on Python 3.13 (PySide 6.11);
    deleted the way Qt deletes a child, it does not."""
    from shiboken6 import isValid

    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.timing_view import TimingView
    made = []
    views = []

    def recording(cls, into):
        build = cls.__init__

        def recording_init(self, *args, **kwargs):
            build(self, *args, **kwargs)
            into.append(self)
        monkeypatch.setattr(cls, "__init__", recording_init)
    recording(MainWindow, made)
    recording(TimingView, views)
    yield
    for w in made:
        if not isValid(w):
            continue
        try:
            w.close()                     # closeEvent stops audio and joins the workers
            w.deleteLater()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
    for v in views:                       # a window's own view dies with the window
        if isValid(v) and v.parent() is None:
            v.deleteLater()
    views.clear()
    from PySide6.QtCore import QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    # PySide keeps a cancelled singleShot's callable (a bound method of the window) after
    # the window is deleted, so the Python object outlives it; emptied, it holds nothing.
    for w in made:
        w.__dict__.clear()
    made.clear()
    import gc
    gc.collect()                          # the Python side of each window's cycles


@pytest.fixture(autouse=True)
def _no_modifier_left_held():
    """A test that pressed a modifier through QTest lets go of it. QTest keeps Ctrl held
    after a key sequence, beyond the test and its windows, and a later test's row click
    in another file read as a Ctrl-click (an order-dependent failure far from its cause)."""
    yield
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is not None:
        held = QApplication.keyboardModifiers()
        assert held == Qt.KeyboardModifier.NoModifier, (
            f"the test left {held} held: QTest.keyRelease it (QTest keeps a sequence's "
            "modifier pressed)")


def pump_loads(win, seconds: float = 120.0) -> None:
    """Run the event loop until `win` has no decode running or queued, failing if that
    takes longer than `seconds` (a load that never finishes is reported as itself, not as
    the assertion after it)."""
    import time

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    deadline = time.time() + seconds
    while getattr(win, "_load_thread", None) is not None or win._load_pending:
        assert time.time() < deadline, f"the loads did not finish in {seconds:.0f} s"
        app.processEvents()
        time.sleep(0.005)


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
    shutil.rmtree(_SETTINGS_DIR, ignore_errors=True)


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


# ---------------------------------------------------------------- payload skipping in the demos
# The PHY1/PHY2/PHY3 demos carry DP0 at 44.1 kHz on 48 kHz transport opportunities: 13 of every
# 160 intervals are skipped (Section 14.1.10). 44100/48000 == 147/160 exactly, and 160 is the
# smallest denominator that can express it. Named here rather than repeated as 44_100 in each
# test, so a test states the RULE — a rate derived from the skipping fraction — instead of a
# constant that says nothing about where it comes from.
DEMO_SKIP_NUMERATOR = 13
DEMO_SKIP_DENOMINATOR = 160
DEMO_PCM_OPPORTUNITY_HZ = 48_000.0


def demo_skipped_rate_hz(opportunity_hz: float = DEMO_PCM_OPPORTUNITY_HZ) -> float:
    """The effective rate of a demo port that skips: 48000 * 147/160 == 44100.0."""
    kept = DEMO_SKIP_DENOMINATOR - DEMO_SKIP_NUMERATOR
    return opportunity_hz * kept / DEMO_SKIP_DENOMINATOR


def demo_transported_samples(opportunities: int) -> tuple[int, int]:
    """(low, high) bound on how many of `opportunities` intervals actually transport.

    NOT an exact number, deliberately. The skip decision is a Bresenham accumulator
    (CDataPort::advanceSkippingAccumulator) that RESTARTS at every SSP — Section 9.1.6.2.1 — so
    the exact count depends on how many SSPs the capture contains and which interval each lands
    on. The three demos differ there: at 1500 samples/channel PHY1 transports 1380 and PHY2/PHY3
    transport 1379, because PHY1's mid-capture reconfigure and their SSPA cadences reset the
    accumulator at different points.

    A bound that is honest about what the phase depends on beats a constant that happens to
    match two demos out of three (a single-run count is wrong for PHY1 by one). The strong assertion is not the count anyway: it is
    that every transported VALUE equals the generated sine (see test_demo_skipping), which a
    mis-phased skip breaks immediately.
    """
    exact = opportunities * (DEMO_SKIP_DENOMINATOR - DEMO_SKIP_NUMERATOR) / DEMO_SKIP_DENOMINATOR
    # One extra sample per SSP at most; no demo has more than a handful.
    return (int(exact) - 4, int(exact) + 4)


# ---------------------------------------------------------------- two Links on one timeline
# No capture in or around the repository has more than one Link, and every native demo
# generator produces one bus, so the multi-Link tests use the app's own two-Link demo
# (File ▸ Open Demo Capture ▸ PHY2 (Two Links)): the PHY2 demo, and the flow-control demo
# delayed by a known number of samples at the shared 500 MHz analyzer rate. The demo
# generators themselves are untouched: exact-value demo tests depend on them.
from swi3s_studio.ingest.transitions import DEMO_LINK2_DELAY_SAMPLES  # noqa: E402

TWO_LINK_SHIFT_SAMPLES = DEMO_LINK2_DELAY_SAMPLES       # ~2.47 ms at 500 MHz


def two_link_captures(n: int = 300, shift_samples: int = TWO_LINK_SHIFT_SAMPLES):
    """(Link 1, Link 2) Captures: the PHY2 FBCSE demo, and the flow-control demo delayed
    by `shift_samples` on the same timeline, as one analyzer would record both buses."""
    from swi3s_studio.ingest.transitions import delay_capture, demo_capture
    a = demo_capture(n, cold_start=True, phy=2)
    b = demo_capture(n, cold_start=True, phy=2, variant="flow_control")
    return a, delay_capture(b, shift_samples)


def export_sal_links(captures, path: str) -> None:
    """Write several Links' captures into ONE .sal, as one Logic 2 recording of every bus
    would be: Link n's clock on channel 2n and its data on 2n+1 (named SW_CLK<n+1> /
    SW_DATA<n+1>), sharing the file's sample rate and its sample 0. The app never writes
    such a file, so this lives with the fixture it serves, on the module's one writer."""
    from swi3s_studio.ingest.sal_export import _write_sal
    captures = list(captures)
    if not captures:
        raise ValueError("export_sal_links: no captures")
    rates = {int(c.sample_rate_hz) for c in captures}
    if len(rates) != 1:
        raise ValueError(f"export_sal_links: one .sal has one sample rate (got {sorted(rates)})")
    channels = []
    for n, c in enumerate(captures):
        channels.append((2 * n, f"SW_CLK{n + 1}", c.initial_clock, c.clock_edges))
        channels.append((2 * n + 1, f"SW_DATA{n + 1}", c.initial_data, c.data_edges))
    _write_sal(path, rates.pop(), channels)


def open_via_dialog(monkeypatch, paths, edit=None) -> list:
    """Drive File ▸ Open Capture: the file picker answers `paths`, and the Open Capture
    dialog is shown to `edit(dlg)` (set names, channels, a window, Add…) and then accepted
    — unless `edit` returns False, which cancels it. Returns the dialogs shown, so a test
    can read what was offered. Patched through monkeypatch, so nothing leaks to the next
    test; the modal guard still fails a test that opens any OTHER dialog."""
    from PySide6.QtWidgets import QDialog, QFileDialog

    from swi3s_studio.ui.open_capture_dialog import OpenCaptureDialog
    shown: list = []
    monkeypatch.setattr(QFileDialog, "getOpenFileNames",
                        lambda *a, **k: (list(paths), ""))

    def run(dlg):
        shown.append(dlg)
        ok = edit(dlg) if edit is not None else None
        return QDialog.Rejected if ok is False else QDialog.Accepted
    monkeypatch.setattr(OpenCaptureDialog, "exec", run)
    return shown
