"""The suite's own fixtures in tests/conftest.py: the per-test teardown of the windows a test
built, and the guards that outlive it. The SNR helpers are tests/test_tooling_snr.py."""
import sys

import pytest

# The window teardown is checked on a MainWindow, so the file runs where widgets do.
pytestmark = pytest.mark.gui


def test_the_guards_stay_in_place_when_the_tests_patches_are_undone(monkeypatch,
                                                                    _no_unexpected_modal,
                                                                    _no_swallowed_slot_exception):
    """The window teardown undoes the test's `monkeypatch` before it empties and collects the
    windows; the modal stubs and the slot-exception hook are held apart, so both still answer
    then."""
    from PySide6.QtWidgets import QFileDialog
    stub, hook = QFileDialog.__dict__["getOpenFileName"], sys.excepthook
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("mine", ""))
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    monkeypatch.undo()
    # Compared before either is called: the unpatched originals would open a dialog, or let
    # the exception through.
    assert QFileDialog.__dict__["getOpenFileName"] is stub and sys.excepthook is hook
    assert QFileDialog.getOpenFileName(None, "Pick a capture") == ("", "")
    assert _no_unexpected_modal == ["QFileDialog.getOpenFileName('Pick a capture')"]
    _no_unexpected_modal.clear()
    try:
        raise RuntimeError("raised in a slot")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    assert len(_no_swallowed_slot_exception) == 1
    assert "raised in a slot" in _no_swallowed_slot_exception[0]
    _no_swallowed_slot_exception.clear()


def test_a_patch_on_a_window_is_undone_before_the_window_is_emptied():
    """The window teardown undoes the test's patches while the window still holds them: a
    pytest that undoes an instance patch by deleting the attribute fails otherwise. The test
    runs the teardown itself, on a MonkeyPatch of its own, and asserts after it."""
    import conftest
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    patches = pytest.MonkeyPatch()
    teardown = conftest._close_the_tests_windows.__wrapped__(
        _no_unexpected_modal=[], _no_swallowed_slot_exception=[], monkeypatch=patches)
    next(teardown)
    win = MainWindow()
    patches.setattr(win, "open_capture", lambda **_k: None)
    undo = patches.undo
    patched_at_undo = None

    def undo_while_patched():
        nonlocal patched_at_undo
        patched_at_undo = "open_capture" in vars(win)
        undo()

    patches.undo = undo_while_patched
    try:
        next(teardown, None)
    finally:
        undo()
    assert patched_at_undo is not None, "the window teardown did not undo the test's patches"
    assert patched_at_undo, "the window was emptied before the test's patches were undone"


# A scratch run under tests/conftest.py. The plugin keeps the real entry point from before any
# guard and reports, at the end of the run, what the class and the excepthook hold then.
_SCRATCH_PLUGIN = '''
import sys
from PySide6.QtWidgets import QFileDialog
REAL = QFileDialog.__dict__["getOpenFileName"]
SEEN = []

def pytest_sessionfinish(session):
    clean = QFileDialog.__dict__["getOpenFileName"] is REAL and sys.excepthook is sys.__excepthook__
    print(f"\\nSCRATCH {'clean' if clean else 'stale'} {SEEN}")
'''

_SCRATCH_TESTS = '''
import sys
import scratch_plugin
from PySide6.QtWidgets import QApplication, QFileDialog
from swi3s_studio.ui.main_window import MainWindow


def test_patches_a_guarded_entry_point(monkeypatch):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("mine", ""))


def test_sees_the_guards_from_the_window_teardown(monkeypatch):
    QApplication.instance() or QApplication([])
    close = MainWindow.close

    def recording_close(self):
        guarded = (QFileDialog.__dict__["getOpenFileName"] is not scratch_plugin.REAL
                   and sys.excepthook is not sys.__excepthook__)
        scratch_plugin.SEEN.append(guarded)
        return close(self)
    monkeypatch.setattr(MainWindow, "close", recording_close)
    MainWindow()
'''


@pytest.mark.platform
def test_the_guards_hold_through_the_window_teardown_and_leave_no_stub_behind(tmp_path):
    """The modal and slot-exception guards are in place while the window teardown closes the
    test's windows, and a test's patch of a guarded entry point leaves the original on the
    class once the run ends."""
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    (tmp_path / "scratch_plugin.py").write_text(_SCRATCH_PLUGIN, encoding="utf-8")
    (tmp_path / "test_scratch.py").write_text(_SCRATCH_TESTS, encoding="utf-8")
    path = [str(tmp_path), os.path.join(root, "tests"), root]
    if os.environ.get("PYTHONPATH"):
        path.append(os.environ["PYTHONPATH"])
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
               PYTHONPATH=os.pathsep.join(path))
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider",
                          "-p", "conftest", "-p", "scratch_plugin", "test_scratch.py"],
                         cwd=tmp_path, env=env, capture_output=True, encoding="utf-8",
                         errors="replace", timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    assert "SCRATCH clean [True]" in run.stdout, run.stdout[-3000:]


def test_every_test_reads_ample_free_memory_and_a_decode_runs_below_the_machines_floor(
        monkeypatch):
    """The free memory a test reads is `conftest.AMPLE_FREE_MEMORY`, so a demo decode is not
    stopped by the memory watchdog however little the machine has free; a test patching its
    own reading still wins."""
    from conftest import AMPLE_FREE_MEMORY

    from swi3s_studio.ingest import saleae_sal
    from swi3s_studio.session import Session
    assert saleae_sal.available_memory_bytes() == AMPLE_FREE_MEMORY
    monkeypatch.setattr(saleae_sal, "total_memory_bytes", lambda: 8 << 30)
    assert Session.from_demo(48, phy=2, cold_start=True).audio_count > 0
    monkeypatch.setattr(saleae_sal, "available_memory_bytes", lambda: 7)
    assert saleae_sal.available_memory_bytes() == 7


def test_a_test_requesting_real_free_memory_reads_the_machine(real_free_memory):
    from conftest import AMPLE_FREE_MEMORY

    from swi3s_studio.ingest import saleae_sal
    assert saleae_sal.available_memory_bytes.__name__ == "available_memory_bytes"
    assert saleae_sal.available_memory_bytes() != AMPLE_FREE_MEMORY


def test_shared_compute_reads_back_bounds_its_memo_and_audits(monkeypatch):
    """`SharedCompute` reads an input evaluated before, keeps the `MAX` most recently used
    inputs, calls the real function while off, and in audit mode refuses a read the real
    function no longer agrees with."""
    from conftest import SharedCompute
    calls = []

    def real(inp):
        calls.append(inp)
        return {"of": inp, "n": len(calls)}

    shared = SharedCompute(real)
    shared.MAX = 2
    first = shared("a")
    assert shared("a") is first and calls == ["a"]
    shared("b")
    shared("c")
    assert "a" not in shared.memo and len(shared.memo) == 2
    shared.enabled = False
    shared("c")
    assert calls[-1] == "c" and len(calls) == 4
    monkeypatch.setenv("SWI3S_SHARED_COMPUTE_AUDIT", "1")
    audited = SharedCompute(real)
    audited("d")
    with pytest.raises(AssertionError, match="not a function of its input"):
        audited("d")


_SHARED_COMPUTE_TESTS = '''
import pytest
from swi3s_studio.timing import CalcInputs, calculator

pytestmark = pytest.mark.usefixtures("shared_compute")
HELD = []


def test_reads_back():
    assert calculator.compute.enabled and calculator._leg_margins.enabled
    HELD.append(calculator.compute(CalcInputs()))
    assert calculator.compute(CalcInputs()) is HELD[0]
    margins = calculator._leg_margins(CalcInputs())
    assert calculator._leg_margins(CalcInputs()) is margins


def test_off_for_a_test_taking_monkeypatch(monkeypatch):
    assert not calculator.compute.enabled and not calculator._leg_margins.enabled
    assert calculator.compute(CalcInputs()) is not HELD[0]


def test_off_for_a_test_lifting_the_ceiling(no_toggle_ceiling):
    assert not calculator.compute.enabled


def test_shared_across_the_module():
    assert calculator.compute(CalcInputs()) is HELD[0]
    print("SCRATCH shared ok")
'''

_SHARED_COMPUTE_AFTER = '''
from swi3s_studio.timing import calculator


def test_the_real_compute_is_back():
    assert type(calculator.compute).__name__ == "function", calculator.compute
    assert type(calculator._leg_margins).__name__ == "function", calculator._leg_margins
    print("SCRATCH restored ok")
'''


@pytest.mark.platform
def test_shared_compute_is_off_where_a_test_patches_and_gone_after_its_module(tmp_path):
    """In a scratch run under tests/conftest.py: a module with `shared_compute` reads an
    input back across its tests, a test taking `monkeypatch` or lifting the toggle ceiling
    computes afresh, and the next module calls the real `compute` and `_leg_margins`."""
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    (tmp_path / "test_a_shared.py").write_text(_SHARED_COMPUTE_TESTS, encoding="utf-8")
    (tmp_path / "test_b_after.py").write_text(_SHARED_COMPUTE_AFTER, encoding="utf-8")
    path = [str(tmp_path), os.path.join(root, "tests"), root]
    if os.environ.get("PYTHONPATH"):
        path.append(os.environ["PYTHONPATH"])
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
               PYTHONPATH=os.pathsep.join(path))
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider",
                          "-p", "conftest", "test_a_shared.py", "test_b_after.py"],
                         cwd=tmp_path, env=env, capture_output=True, encoding="utf-8",
                         errors="replace", timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    assert "SCRATCH shared ok" in run.stdout and "SCRATCH restored ok" in run.stdout, \
        run.stdout[-3000:]


def test_the_default_font_is_an_installed_family_that_draws_text():
    """The suite's default font resolves to an installed, proportional family, so text measures
    as glyphs and not as missing-glyph boxes of one width; on Windows it is Segoe UI, the
    native platform's default."""
    from PySide6.QtGui import QFontDatabase, QFontInfo, QFontMetrics
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    font = app.font()
    family = QFontInfo(font).family()
    assert family in QFontDatabase.families(), family
    metrics = QFontMetrics(font)
    assert metrics.horizontalAdvance("i") < metrics.horizontalAdvance("W"), family
    if sys.platform == "win32":
        assert family == "Segoe UI"


def _run_the_window_teardown_around(build):
    """Run conftest's per-test window teardown on a MonkeyPatch of its own around `build()`,
    which makes widgets and returns them; the widgets, after the teardown has run."""
    import conftest
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    patches = pytest.MonkeyPatch()
    teardown = conftest._close_the_tests_windows.__wrapped__(
        _no_unexpected_modal=[], _no_swallowed_slot_exception=[], monkeypatch=patches)
    next(teardown)
    try:
        widgets = build()
        next(teardown, None)
    finally:
        patches.undo()
    return widgets


def test_no_top_level_dialog_survives_the_test_that_left_it():
    """A dialog a test shows and one it builds and never shows are both deleted by the
    teardown, and no top-level QDialog remains."""
    from PySide6.QtWidgets import QApplication, QDialog
    from shiboken6 import isValid

    def build():
        shown, built = QDialog(), QDialog()
        shown.show()
        return [shown, built]

    left = _run_the_window_teardown_around(build)
    assert not any(isValid(d) for d in left)
    assert not [w for w in QApplication.topLevelWidgets() if isinstance(w, QDialog)]


def test_a_dialog_class_qt_defines_does_not_survive_the_test_that_left_it():
    """A QMessageBox, a QDialog subclass Qt defines, is deleted by the teardown too."""
    from PySide6.QtWidgets import QMessageBox
    from shiboken6 import isValid

    def build():
        box = QMessageBox()
        box.show()
        return [box]

    left = _run_the_window_teardown_around(build)
    assert not any(isValid(d) for d in left)


@pytest.fixture(scope="module")
def _older_window():
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    w = MainWindow()
    yield w
    w.close()
    w.deleteLater()


def test_a_dialog_an_older_window_caches_survives_the_test_that_opened_it(_older_window):
    """The dialog belongs to the window's fixture, so the test teardown leaves it for the
    later tests that use the window. The test runs the teardown itself around the opening."""
    from shiboken6 import isValid
    _run_the_window_teardown_around(lambda: _older_window._filter_expression_box())
    assert isValid(_older_window._expr_dialog)
    assert _older_window._expr_edit.text() == ""


_NO_LISTING = '''
import sys

import pytest
from PySide6.QtWidgets import QApplication, QDialog

LISTED_BY = []


@pytest.fixture(scope="session", autouse=True)
def _record_who_lists_the_top_level_widgets():
    real = QApplication.topLevelWidgets
    QApplication.topLevelWidgets = staticmethod(
        lambda: (LISTED_BY.append(sys._getframe(1).f_code.co_name), real())[1])
    yield
    QApplication.topLevelWidgets = real
    print("SCRATCH listed by", sorted(set(LISTED_BY)))


def test_a_dialog_is_left_behind():
    QApplication.instance() or QApplication([])
    QDialog().show()
'''


@pytest.mark.platform
def test_the_window_teardown_does_not_list_the_top_level_widgets(tmp_path):
    """Listing QApplication.topLevelWidgets() wraps every top-level widget, a half-destroyed
    one included, which can segfault. The teardown closes the dialogs it recorded as they were
    built and never lists the widgets."""
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    (tmp_path / "test_scratch.py").write_text(_NO_LISTING, encoding="utf-8")
    path = [os.path.join(root, "tests"), root]
    if os.environ.get("PYTHONPATH"):
        path.append(os.environ["PYTHONPATH"])
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUTF8="1",
               PYTHONPATH=os.pathsep.join(path))
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider",
                          "-p", "conftest", "test_scratch.py"],
                         cwd=tmp_path, env=env, capture_output=True, encoding="utf-8",
                         errors="replace", timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    assert "SCRATCH listed by []" in run.stdout, run.stdout[-3000:]


def test_an_isolated_git_commits_where_git_cannot_make_up_an_identity(tmp_path, isolated_git):
    """`user.useConfigOnly` makes git refuse to derive an identity from the host name, as git
    does on a host whose name gives no email address: the isolated environment's own identity
    still lets a test commit."""
    from conftest import git_in
    git_in(tmp_path, "init", "-q")
    (tmp_path / "f.txt").write_text("x\n")
    git_in(tmp_path, "add", "f.txt")
    git_in(tmp_path, "-c", "user.useConfigOnly=true", "commit", "-qm", "one")
    assert git_in(tmp_path, "log", "--format=%an <%ae>") == "t <t@t>"
