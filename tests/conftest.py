"""Shared pytest fixtures / setup.

Keep the GUI tests fast: the demo capture (File ▸ Open Demo Capture, MainWindow.load_demo)
is 1 s of audio (~24.6M UIs, ~2.5 s to decode) by default. Force a short demo for the
test process so loading one stays near-instant. Tests that need specific audio lengths
call Session.from_demo(N) with an explicit N, which this does not affect.
"""
import os
import sys

import perf_retry
import pytest
from perf_retry import pytest_runtest_call  # noqa: F401  (a hook: pytest registers it)

os.environ.setdefault("SWI3S_DEMO_SAMPLES", "300")   # short demo for MainWindow.load_demo
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Offscreen on Windows, Qt reads fonts only from its own fonts folder, which the PySide6 wheels
# do not ship, so every glyph is a missing-glyph box and text measures as boxes. Point it at the
# system's fonts, and resolve the offscreen default family to the one the native Windows
# platform uses, so a layout test measures the text a Windows user sees.
if sys.platform == "win32" and os.environ["QT_QPA_PLATFORM"] == "offscreen":
    os.environ.setdefault("QT_QPA_FONTDIR",
                          os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    from PySide6.QtGui import QFont  # noqa: E402

    QFont.insertSubstitution("Sans Serif", "Segoe UI")

# QSettings needs a non-empty organisation/application name to persist reliably on
# Windows (its registry scope is keyed on them). The real app sets these in app.py, but
# tests build a bare QApplication, so a TEST-scoped identity is set here (distinct from
# the app's, so it never touches a developer's real saved settings), before any test
# constructs a QApplication.
from PySide6.QtCore import QCoreApplication  # noqa: E402

# Tests use PySide directly, not only through the app's ui package, which pins them too.
from swi3s_studio.ui.singleton_pin import pin_singletons  # noqa: E402

pin_singletons()

QCoreApplication.setOrganizationName("MIPI SWI3S Test")
QCoreApplication.setApplicationName("SWI3S Studio Test")

# And keep them in a throwaway ini, not a file of the developer's. Every QSettings() of
# the run, the app's included, reads and writes this one, and each test starts with it
# empty (_fresh_settings).
import shutil  # noqa: E402
import tempfile  # noqa: E402

from PySide6.QtCore import QSettings  # noqa: E402

_SETTINGS_DIR = tempfile.mkdtemp(prefix="swi3s_test_settings_")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _SETTINGS_DIR)


# ---------------------------------------------------------------- no test may block on a modal
# Offscreen, a modal dialog has nobody to dismiss it: an un-patched QMessageBox or file
# picker is a hang until the CI job's timeout, not a failure. Every modal entry point the
# app uses is replaced for every test with one that answers "cancel" and records the call,
# and a test that opened one it did not patch FAILS at teardown, naming the dialog. A test
# that means to drive a dialog patches the entry point itself; its monkeypatch lands after
# this one. The guard holds its stubs in a MonkeyPatch of its own, undone at its own teardown,
# so an undo of the test's `monkeypatch` (by the test, or by the window teardown) leaves them
# in place.
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


def _dialog_title(args, instance: bool = False) -> str:
    """The title of the dialog a stub was called for. A static helper's first argument is
    the PARENT window, so its title comes from the first string argument; an exec() call
    on an instance carries the dialog itself."""
    for a in args:
        if isinstance(a, str):
            return a
    if instance:
        for a in args:
            for attr in ("windowTitle", "text", "labelText", "title"):   # a box's title is
                get = getattr(a, attr, None)                  # not kept on every platform
                if callable(get) and get():
                    return get()
    return "?"


@pytest.fixture(autouse=True)
def _no_unexpected_modal():
    from PySide6 import QtWidgets
    from PySide6.QtWidgets import QMessageBox
    monkeypatch = pytest.MonkeyPatch()
    opened = []
    for (cls_name, meth), value in _MODAL_CANCEL.items():
        if cls_name == "QMessageBox" and meth == "question":
            value = QMessageBox.StandardButton.NoButton
        if value == "invalid":
            from PySide6.QtGui import QColor
            value = QColor()

        def stub(*args, _where=f"{cls_name}.{meth}", _value=value, **_kw):
            opened.append(f"{_where}({_dialog_title(args, _where.endswith('.exec'))!r})")
            return _value
        monkeypatch.setattr(getattr(QtWidgets, cls_name), meth, stub)
    # QMenu.exec also has a STATIC overload, exec(actions, pos), and PySide 6.11 resolves
    # `menu.exec(...)` on an instance to the built-in method whatever the class attribute
    # holds, so the stub above alone catches only `QMenu.exec(menu, pos)`. Route the
    # instance lookup through the class attribute, so this stub, or a test's own patch,
    # answers.
    menu_getattr = QtWidgets.QMenu.__getattribute__

    def _menu_getattr(self, name):
        if name == "exec":
            return QtWidgets.QMenu.__dict__["exec"].__get__(self, type(self))
        return menu_getattr(self, name)
    monkeypatch.setattr(QtWidgets.QMenu, "__getattribute__", _menu_getattr)
    yield opened                          # the guard's own test reads (and clears) this
    monkeypatch.undo()
    assert not opened, (
        "the test opened a modal it did not patch (answered 'cancel'; offscreen it would "
        "have hung): " + ", ".join(opened))


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Every test starts with no saved settings: the default appearance, waveform colours
    and weights, Timing Diagrams settings, folders. They are shared by the whole run, so one
    test's saved choice (or one left behind by a failed assertion before its restore)
    would otherwise reach every test after it."""
    from swi3s_studio.ui import diagram_style, line_style
    QSettings().clear()
    line_style.reset_cache()
    diagram_style.reset_cache()
    yield
    QSettings().clear()
    line_style.reset_cache()
    diagram_style.reset_cache()


# The free memory every test sees unless it asks for the machine's own reading.
AMPLE_FREE_MEMORY = 64 << 30


@pytest.fixture
def real_free_memory():
    """Request it to read the machine's own free memory (saleae_sal.available_memory_bytes)
    instead of `AMPLE_FREE_MEMORY`."""


@pytest.fixture(autouse=True)
def _ample_free_memory(request, monkeypatch):
    """Every test reads `AMPLE_FREE_MEMORY` as the free memory, so the decode's memory
    watchdog and the load guards act the same on every machine and under any number of
    parallel test processes. A test of those rules sets its own reading with monkeypatch;
    a test of the reading itself requests `real_free_memory`."""
    if "real_free_memory" in request.fixturenames:
        return
    from swi3s_studio.ingest import saleae_sal
    monkeypatch.setattr(saleae_sal, "available_memory_bytes", lambda: AMPLE_FREE_MEMORY)


@pytest.fixture(autouse=True)
def _no_swallowed_slot_exception():
    """An exception raised in a Qt slot fails the test that raised it. PySide only
    prints it and carries on. The hook is held in a MonkeyPatch of its own, undone at this
    fixture's teardown, so it stays in place while the window teardown undoes the test's
    patches, empties the windows and collects them."""
    import sys
    import traceback
    raised = []

    def hook(etype, value, tb):
        raised.append("".join(traceback.format_exception(etype, value, tb)))
        sys.__excepthook__(etype, value, tb)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "excepthook", hook)
    yield raised
    monkeypatch.undo()
    assert not raised, "an exception in a Qt slot (PySide printed it and went on):\n" + raised[0]


@pytest.fixture(autouse=True)
def _close_the_tests_windows(_no_unexpected_modal, _no_swallowed_slot_exception, monkeypatch):
    """Close, after each test, the MainWindows it built, joining their workers, so they
    do not pile up under every later test that runs the event loop. A window from a
    wider-scoped fixture was built before the test, so it is left to its fixture.

    The windows are recorded as they are built, not found afterwards: listing the
    application's top-level widgets wraps every one of them, a half-destroyed one
    included, which can segfault. And they are DELETED, not just closed: a closed window
    is kept alive by its own Qt connections to its methods, with every Link it decoded.
    The window's timers have it as their context, so they die with it. The test's
    `monkeypatch` is undone here, after the windows close and before they are emptied, so
    the fixtures torn down after this one see the unpatched classes. This fixture requests
    the modal and slot-exception guards, so both are set up before it and torn down after it,
    and they hold their patches apart from the test's `monkeypatch`: both are in place while
    the windows close and are emptied and collected, and a test's patch of a guarded entry
    point is undone onto the guard's stub, which the guard's own undo then replaces with the
    original.

    A TimingView a test built on its own, with no parent, is deleted here too, the way Qt
    deletes a child; left to the collector below, one the test had clicked can segfault
    (Python 3.13, PySide 6.11).

    Every QDialog the test built, shown or not (a non-modal window it opened, a dialog it
    built and never closed), is closed and deleted before the windows: none survives into the
    next test of the process. Dialogs are recorded as they are built, like the windows, so
    ones built before the test began belong to a wider-scoped fixture and are left alone, and
    so is one a wider-scoped window built during the test and caches (a dialog whose parent
    chain ends in a MainWindow this test did not build).
    A dialog Qt builds inside a static function is modal and caught by `_no_unexpected_modal`."""
    from PySide6 import QtWidgets
    from PySide6.QtWidgets import QDialog
    from shiboken6 import isValid

    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.timing_view import TimingView
    made = []
    views = []
    dialogs = []

    def recording(cls, into):
        build = cls.__init__

        def recording_init(self, *args, **kwargs):
            build(self, *args, **kwargs)
            if not any(self is seen for seen in into):
                into.append(self)
        monkeypatch.setattr(cls, "__init__", recording_init)
    recording(MainWindow, made)
    recording(TimingView, views)
    # QDialog and each dialog class Qt itself defines (QMessageBox, QFileDialog, ...), whose
    # constructors do not pass through QDialog's; a Python subclass reaches one of them.
    for cls in [QDialog] + [c for c in vars(QtWidgets).values()
                            if isinstance(c, type) and c is not QDialog and issubclass(c, QDialog)]:
        recording(cls, dialogs)
    def owned_by_an_older_window(dialog):
        top = dialog.parentWidget()
        while top is not None and top.parentWidget() is not None:
            top = top.parentWidget()
        return isinstance(top, MainWindow) and not any(top is w for w in made)
    yield
    for d in dialogs:
        if not isValid(d) or owned_by_an_older_window(d):
            continue
        try:
            d.close()
            d.deleteLater()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
    dialogs.clear()
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
    # The test's patches are undone before the windows are emptied: undoing one set on a
    # window's instance deletes that attribute, which must still be there.
    monkeypatch.undo()
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
    after a key sequence, beyond the test and its windows, so a later test's row click
    would read as a Ctrl-click."""
    yield
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is not None:
        held = QApplication.keyboardModifiers()
        assert held == Qt.KeyboardModifier.NoModifier, (
            f"the test left {held} held: QTest.keyRelease it (QTest keeps a sequence's "
            "modifier pressed)")


@pytest.fixture
def no_toggle_ceiling(monkeypatch):
    """Lift the timing calculator's toggle ceiling (`toggle_ceiling_MHz`), for tests of the
    rate solver at rates past it."""
    import math

    from swi3s_studio.timing import calculator
    monkeypatch.setattr(calculator, "toggle_ceiling_MHz", lambda inp: math.inf)


class SharedCompute:
    """A Timing calculator function of one input, `compute` or the corner search's
    `_leg_margins`, with a memo held for one test module: an input the module evaluated before
    is read back, the least recently used dropped past `MAX`. Results are shared, so no test
    changes one. Off (`enabled` False) the real function runs. With
    $SWI3S_SHARED_COMPUTE_AUDIT set, every read is evaluated again and must equal the result
    held. The memo is read and written under a lock, as the Timing page's worker thread calls
    it too."""
    MAX = 4096
    hits = misses = 0               # every instance's reads, for a report on the memo

    def __init__(self, real):
        import collections
        import threading
        self.real = real
        self.memo = collections.OrderedDict()
        self._lock = threading.Lock()
        self.enabled = True
        self.audit = bool(os.environ.get("SWI3S_SHARED_COMPUTE_AUDIT"))

    def __call__(self, inp):
        if not self.enabled:
            return self.real(inp)
        key = inp
        with self._lock:
            held = self.memo.get(key)
            if held is not None:
                SharedCompute.hits += 1
                self.memo.move_to_end(key)
        if held is None:
            held = self.real(inp)
            with self._lock:
                SharedCompute.misses += 1
                held = self.memo.setdefault(key, held)
                if len(self.memo) > self.MAX:
                    self.memo.popitem(last=False)
        elif self.audit:
            assert self.real(inp) == held, f"compute is not a function of its input: {inp}"
        return held


@pytest.fixture(scope="module")
def shared_compute():
    """For a module that requests it (`pytest.mark.usefixtures("shared_compute")`), the
    calculator's `compute` is a `SharedCompute`, and so is the Timing page's binding of it
    in each test that runs with the page's module loaded (`_shared_compute_per_test`); the
    corner search's `_leg_margins` is another, its `margins`."""
    from swi3s_studio.timing import calculator
    shared = SharedCompute(calculator.compute)
    shared.margins = SharedCompute(calculator._leg_margins)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(calculator, "compute", shared)
        mp.setattr(calculator, "_leg_margins", shared.margins)
        yield shared


@pytest.fixture(autouse=True)
def _shared_compute_per_test(request):
    """In a module with `shared_compute`: a test whose function takes `monkeypatch`, or that
    uses `no_toggle_ceiling`, runs with it off, so a test that replaces calculator code
    computes every input afresh; and the Timing page's module, once imported, calls it too.
    (`monkeypatch` is in every test's fixtures through the autouse fixtures above, so the
    test function's own parameters decide.)"""
    import inspect
    if "shared_compute" not in request.fixturenames:
        yield
        return
    shared = request.getfixturevalue("shared_compute")
    own = inspect.signature(request.function).parameters
    shared.enabled = shared.margins.enabled = (
        "monkeypatch" not in own and "no_toggle_ceiling" not in request.fixturenames)
    with pytest.MonkeyPatch.context() as mp:
        page = sys.modules.get("swi3s_studio.ui.timing_view")
        if page is not None:
            mp.setattr(page, "compute", shared)
        yield
    shared.enabled = shared.margins.enabled = True


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


# ---------------------------------------------------------------- perf margins, pass or fail
# A wall-clock ceiling reports its measurement only when it fails. The ceilings that
# record here are printed at the end of the session, each as a fraction of its ceiling,
# whatever the outcome.
_PERF_MARGINS: list = []


def record_perf(name: str, median: float, ceiling: float,
                worst: float, worst_ceiling: float) -> None:
    """Note a perf test's measured median and worst against their ceilings (seconds), for the
    session summary. Call it BEFORE the asserts, so a failing run reports it too."""
    _PERF_MARGINS.append((name, median, ceiling, worst, worst_ceiling))


def _perf_margins_summary(terminalreporter):
    if not _PERF_MARGINS:
        return
    import platform
    import sys
    tr = terminalreporter
    tr.section("perf margins")
    tr.write_line(f"{sys.platform} {platform.machine()}, Python {platform.python_version()}, "
                  f"GITHUB_ACTIONS={os.environ.get('GITHUB_ACTIONS', '-')}")
    for name, median, ceiling, worst, worst_ceiling in _PERF_MARGINS:
        tr.write_line(f"{name}: median {median * 1000:.0f} / {ceiling * 1000:.0f} ms "
                      f"({median / ceiling:.0%}), worst {worst * 1000:.0f} / "
                      f"{worst_ceiling * 1000:.0f} ms ({worst / worst_ceiling:.0%})")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    _perf_margins_summary(terminalreporter)
    perf_retry.pytest_terminal_summary(terminalreporter, exitstatus, config)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Join every MainWindow's worker threads before the interpreter tears down.

    GUI tests build MainWindows and rarely close them, so their load / locate /
    TX-persistence QThreads can still be alive at exit. Qt calls abort() when a running
    QThread is destroyed, which turns a fully passing run into a SIGABRT (exit 134) —
    invisible under pytest, which reports its result before teardown, but fatal to
    tests/run_all.sh, which judges each suite by its exit code.

    Fixing it here rather than per-test covers every suite, including ones added later.

    Then every object tree Python still owns is deleted, as the app does on quit, so
    PySide's own sweep at exit has nothing left to destroy (see
    swi3s_studio/ui/teardown.py).
    """
    _join_all_main_windows()
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is not None:
        from swi3s_studio.ui.teardown import release_python_owned_qobjects
        release_python_owned_qobjects()
    shutil.rmtree(_SETTINGS_DIR, ignore_errors=True)


def _join_all_main_windows() -> None:
    import sys

    from PySide6.QtWidgets import QApplication

    # A process that never imported the main window built none.
    module = sys.modules.get("swi3s_studio.ui.main_window")
    if QApplication.instance() is None or module is None:
        return
    # The windows come from the collector's list of Python objects: listing the
    # application's top-level widgets wraps every one of them, a half-destroyed one
    # included, which can segfault.
    import gc

    from shiboken6 import isValid
    for w in [o for o in gc.get_objects() if isinstance(o, module.MainWindow)]:
        if not isValid(w):
            continue
        join = getattr(w, "join_worker_threads", None)
        if callable(join):
            try:
                join()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass


# ---------------------------------------------------------------- payload skipping in the demos
# The PHY1/PHY2/PHY3 demos carry DP0 at 44.1 kHz on 48 kHz transport opportunities: 13 of every
# 160 intervals are skipped (Section 14.1.10). 44100/48000 == 147/160 exactly, and 160 is the
# smallest denominator that can express it. Named here so a test states the RULE — a rate
# derived from the skipping fraction — instead of a bare 44_100.
DEMO_SKIP_NUMERATOR = 13
DEMO_SKIP_DENOMINATOR = 160
DEMO_PCM_OPPORTUNITY_HZ = 48_000.0


def demo_skipped_rate_hz(opportunity_hz: float = DEMO_PCM_OPPORTUNITY_HZ) -> float:
    """The effective rate of a demo port that skips: 48000 * 147/160 == 44100.0."""
    kept = DEMO_SKIP_DENOMINATOR - DEMO_SKIP_NUMERATOR
    return opportunity_hz * kept / DEMO_SKIP_DENOMINATOR


def demo_transported_samples(opportunities: int) -> tuple[int, int]:
    """(low, high) bound on how many of `opportunities` intervals actually transport.

    A bound, not an exact number. The skip decision is a Bresenham accumulator
    (CDataPort::advanceSkippingAccumulator) that RESTARTS at every SSP — Section 9.1.6.2.1 — so
    the exact count depends on how many SSPs the capture contains and which interval each lands
    on. The three demos differ there: at 1500 samples/channel PHY1 transports 1380 and PHY2/PHY3
    transport 1379, because PHY1's mid-capture reconfigure and their SSPA cadences reset the
    accumulator at different points.

    The value check is in test_demo_skipping: every transported VALUE equals the generated
    sine.
    """
    exact = opportunities * (DEMO_SKIP_DENOMINATOR - DEMO_SKIP_NUMERATOR) / DEMO_SKIP_DENOMINATOR
    # One extra sample per SSP at most; no demo has more than a handful.
    return (int(exact) - 4, int(exact) + 4)


# ---------------------------------------------------------------- two Links on one timeline
# Every native demo generator produces one bus, so the multi-Link tests use the app's own
# two-Link demo (File ▸ Open Demo Capture ▸ PHY2): the PHY2 demo, and the
# flow-control demo delayed by a known number of samples at the shared 500 MHz analyzer
# rate. The demo generators themselves are untouched: exact-value demo tests depend on
# them.
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


# ---------------------------------------------------------------- the tools under test
def symlink_or_skip(target, link) -> None:
    """os.symlink(target, link), or skip the test where the platform or the account refuses
    symlinks (a Windows account without the symlink privilege)."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are not available here: {exc}")


def load_tool(name: str):
    """tools/<name>.py, imported by path as a fresh module named `name`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def current_notes_path() -> str:
    """The release notes this checkout is written for: docs/releases/next.md during a cycle,
    and at a release commit, which renames it, docs/releases/v<__version__>.md."""
    from swi3s_studio import __version__
    releases = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "docs", "releases")
    nxt = os.path.join(releases, "next.md")
    return nxt if os.path.exists(nxt) else os.path.join(releases, f"v{__version__}.md")


def current_notes() -> str:
    """current_notes_path()'s text, its whitespace runs read as one space."""
    with open(current_notes_path(), encoding="utf-8") as f:
        return " ".join(f.read().split())


# ---------------------------------------------------------------- throwaway git repositories
# The environment under which git reads none of the machine's own configuration (signing,
# hooks, line-end conversion, default branch, path quoting).
# Isolated git reads no configuration, so it has no user identity: one is given here, or a commit
# fails wherever git cannot make one up from the host name.
ISOLATED_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
# The variables that point git at a repository other than the one its directory is in, which
# a git hook running the suite sets. Each is removed, so git works on the throwaway one.
_GIT_REPOSITORY_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE")


@pytest.fixture
def isolated_git(monkeypatch, tmp_path_factory):
    """Every git the test runs, a tool's own included, reads none of the machine's
    configuration and works on the repository of the directory it runs in. Every test that
    builds a git repository, or runs git in a directory that is not one, requests it, and
    builds a repository with `git_in` and `commit_files`. Git looks for a repository no
    higher than pytest's base temporary directory, so a test directory that is not a
    repository is not read as part of one enclosing it."""
    for key, value in ISOLATED_GIT_ENV.items():
        monkeypatch.setenv(key, value)
    for key in _GIT_REPOSITORY_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path_factory.getbasetemp()))


def isolated_git_env() -> dict:
    """This process's environment with git isolated as `isolated_git` isolates it."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPOSITORY_VARS}
    env.update(ISOLATED_GIT_ENV)
    return env


def git_in(repo, *args) -> str:
    """git `args` in `repo`, isolated (`isolated_git_env`), which must exit 0; its output,
    stripped."""
    import subprocess
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True, env=isolated_git_env()).stdout.strip()


def commit_files(repo, files, message: str = "change") -> str:
    """Write `files` into `repo` and commit everything, returning the commit. `files` maps a
    path to its content (str, written as UTF-8 with its line ends as given, or bytes), or to
    None to delete the file."""
    for rel, data in files.items():
        full = repo / rel
        if data is None:
            full.unlink()
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    git_in(repo, "add", "-A")
    git_in(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return git_in(repo, "rev-parse", "HEAD")


def publish_release_repo(repo, tool, app: dict) -> tuple[str, str, str]:
    """A new repository at `repo` standing in for this one to tools/publish_tree.py (`tool`,
    the loaded module): (up, rel, pub), where `up` holds the upstream-owned files, `rel` the
    release tree, `app` beside the maintainers' tier the tool prunes, and `pub` the publish
    branch as the tool builds it from the two."""
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    owned = {rel: f"owned: {rel}\n" for rel in tool._UPSTREAM_OWNED}
    up = commit_files(repo, owned, "upstream")
    tier = {tool._INTERNAL + "notes.md": "A note of the maintainers' tier.\n",
            **{rel: "A file of the maintainers' tier.\n" for rel in tool._INTERNAL_FILES}}
    rel = commit_files(repo, {**app, **tier, **{r: None for r in owned}}, "release")
    pub = commit_files(repo, {**owned, **{r: None for r in tier}}, "publish")
    return up, rel, pub


_PROTECTS = {"gui", "platform", "perf"}
# Core test files the platform scope runs whole besides the `gui` and `platform` tests, one per
# area, so each platform still decodes, replays registers, times and parses on its own
# interpreter. Their tests carry the `smoke` marker (pytest_collection_modifyitems).
CORE_SMOKE = ("tests/test_swi3score.py", "tests/test_register_model.py",
              "tests/test_timing_vs_reference.py", "tests/test_ingest_formats.py",
              "tests/test_regmap_import.py", "tests/test_publish_sync.py")
# The platform scope's -m expression, tools/gate.py's _GUEST_SCOPE: the guest gate and CI's
# platform jobs run it (docs/TESTING.md §1.1).
PLATFORM_SCOPE = "(gui or platform or smoke) and not perf"


def platform_scope(config) -> bool:
    """Whether this run is the platform scope: its -m expression is `PLATFORM_SCOPE`."""
    return (config.getoption("markexpr", "") or "").strip() == PLATFORM_SCOPE


# The longest test id the suite accepts. pytest puts the id in the PYTEST_CURRENT_TEST
# environment variable, which Windows refuses above 32767 characters; a parametrized case
# whose value is large data names itself with `ids=`.
MAX_TEST_ID = 1000


def pytest_collection_modifyitems(config, items):
    """A test marked none of gui, platform or perf is core (docs/TESTING.md §1.1), so
    `-m core` selects it, and a test in a `CORE_SMOKE` file is smoke. A test id longer than `MAX_TEST_ID` stops the run at collection,
    naming the test."""
    long_ids = [item.nodeid for item in items if len(item.nodeid) > MAX_TEST_ID]
    if long_ids:
        raise pytest.UsageError(
            f"{len(long_ids)} test id(s) longer than {MAX_TEST_ID} characters; give the "
            "parametrized values short ids=: " + ", ".join(i[:120] for i in long_ids[:5]))
    for item in items:
        if not _PROTECTS & {m.name for m in item.iter_markers()}:
            item.add_marker(pytest.mark.core)
        if item.nodeid.split("::", 1)[0] in CORE_SMOKE:
            item.add_marker(pytest.mark.smoke)


_SNR_WINDOW = 4096


def tone_snr(a) -> float:
    """Single-tone SNR (dB) of a decoded PCM stream: the dominant FFT bin against the rest,
    over its first `_SNR_WINDOW` samples. A cleanly decoded sine reads well above 0; one
    decoded at the wrong interval phase collapses below it. Fewer than 512 samples read -99."""
    import numpy as np
    a = np.asarray(a, dtype=np.float64)
    if a.size < 512:
        return -99.0
    a = a - a.mean()
    w = np.hanning(min(_SNR_WINDOW, a.size))
    A = np.abs(np.fft.rfft(a[:w.size] * w)) ** 2
    A[0] = 0.0
    k = int(np.argmax(A))
    sig = A[max(1, k - 2):k + 3].sum()
    return 10.0 * np.log10(sig / (A.sum() - sig + 1e-9))


def worst_snr(a, window: int = _SNR_WINDOW) -> float:
    """The lowest `tone_snr` over `window`-sample windows tiling `a`, the last one ending at
    the final sample (a stream of `window` samples or fewer is one window), so a phase slip
    anywhere in the stream shows, not only one at its start. A window is at most the
    `_SNR_WINDOW` samples `tone_snr` reads."""
    import numpy as np
    a = np.asarray(a)
    window = min(window, _SNR_WINDOW)
    if a.size <= window:
        return tone_snr(a)
    starts = list(range(0, a.size - window + 1, window))
    if starts[-1] + window < a.size:
        starts.append(a.size - window)
    return min(tone_snr(a[i:i + window]) for i in starts)
