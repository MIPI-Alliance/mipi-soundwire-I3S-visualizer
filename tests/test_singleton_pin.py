"""None and True stay alive on Python 3.11 however often PySide over-releases them.

PySide's Linux builds release a reference to None for every empty value they return as None
(an unset item or index role, an invalid index, an unset QAction.data()) and one to True for
every emit() of a Python-defined signal. On Python 3.11 the interpreter aborts once either
count reaches zero, which a view reading roles on every rebuild, or a signal per cursor move,
does within one session. ui/singleton_pin.py pins None, True and False as 3.12 makes them
immortal. Each case runs a fresh interpreter, as the app does, so the pin is the one the ui
package makes; each prints the counts it starts from, so a failure says whether it was pinned.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from swi3s_studio.ui import singleton_pin

_ROOT = Path(__file__).resolve().parent.parent

# Far more over-releases than either object's own count, some tens of thousands: an
# unpinned Linux 3.11 process aborts well before the end.
_CALLS = 100_000

_COUNTS = 'print("COUNTS", sys.getrefcount(None), sys.getrefcount(True), flush=True)'


def _child(body: str) -> subprocess.CompletedProcess:
    path = os.pathsep.join(p for p in (str(_ROOT), os.environ.get("PYTHONPATH", "")) if p)
    env = dict(os.environ, PYTHONPATH=path, QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, "-W", "error::RuntimeWarning", "-c",
                           textwrap.dedent(body)], cwd=_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def _alive(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 0 and "ALIVE" in proc.stdout, (
        f"rc {proc.returncode}: {proc.stdout[-500:]} {proc.stderr[-1500:]}")


def test_the_ui_package_keeps_none_alive_through_empty_reads():
    _alive(_child(f"""
        import sys
        import swi3s_studio.ui                              # pins None, True and False
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QAction, QStandardItem, QStandardItemModel
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem
        app = QApplication([])
        {_COUNTS}
        item, tree_item, action = QStandardItem(), QTreeWidgetItem(), QAction()
        model = QStandardItemModel(1, 1)                    # kept: the index points into it
        index = model.index(0, 0)
        for _ in range({_CALLS} // 4):
            item.data(Qt.UserRole + 7)
            tree_item.data(0, Qt.ToolTipRole)
            index.data(Qt.UserRole + 7)
            action.data()
        print("ALIVE", flush=True)
    """))


def test_the_ui_package_keeps_true_alive_through_signal_emits():
    _alive(_child(f"""
        import sys
        import swi3s_studio.ui                              # pins None, True and False
        from PySide6.QtCore import QCoreApplication, QObject, Signal

        class Source(QObject):
            changed = Signal(int)

        app = QCoreApplication([])
        {_COUNTS}
        source = Source()
        source.changed.connect(lambda _v: None)
        for i in range({_CALLS}):
            source.changed.emit(i)
        print("ALIVE", flush=True)
    """))


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="None, True and False are immortal")
def test_a_pin_lost_after_the_fact_is_made_again_and_never_twice():
    """It goes by the counts, not by having run: a count brought back below the pin is
    pinned again by the next call, and a count already pinned is not pinned twice."""
    _alive(_child("""
        import ctypes, sys
        from swi3s_studio.ui.singleton_pin import _PIN, pin_singletons
        assert all(sys.getrefcount(o) > _PIN for o in (None, True, False))
        ctypes.c_ssize_t.from_address(id(None)).value -= _PIN      # as if it never took
        assert sys.getrefcount(None) < _PIN
        assert pin_singletons()
        counts = [sys.getrefcount(o) for o in (None, True, False)]
        assert all(_PIN < c < 2 * _PIN for c in counts), counts
        print("ALIVE", flush=True)
    """))


def test_a_pin_is_kept_whatever_other_references_do_meanwhile():
    """The write is judged by how far the count moved, give or take what the rest of the
    process frees or takes in the meantime; a move of about nothing means the wrong field."""
    pin, slack = singleton_pin._PIN, singleton_pin._SLACK
    assert singleton_pin._landed(pin)
    assert singleton_pin._landed(pin - 5000) and singleton_pin._landed(pin + 300)
    assert not singleton_pin._landed(0) and not singleton_pin._landed(pin - 2 * slack)


def test_the_pin_is_made_with_the_collector_off_and_leaves_it_as_it_was():
    _alive(_child("""
        import gc, sys
        import swi3s_studio.ui
        from swi3s_studio.ui.singleton_pin import pin_singletons
        for enabled in (True, False):
            (gc.enable if enabled else gc.disable)()
            assert pin_singletons() and gc.isenabled() is enabled
        print("ALIVE", flush=True)
    """))
