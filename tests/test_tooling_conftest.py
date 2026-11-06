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
