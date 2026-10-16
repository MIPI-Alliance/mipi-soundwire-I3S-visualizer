"""None and True stay alive on Python 3.11 however often PySide over-releases them.

PySide's Linux builds release a reference to None for every empty value they return as None
(an unset item or index role, an invalid index, an unset QAction.data()) and one to True for
every emit() of a Python-defined signal. On Python 3.11 the interpreter aborts once either
count reaches zero, which a view reading roles on every rebuild, or a signal per cursor move,
does within one session. ui/singleton_pin.py pins None, True and False as 3.12 makes them
immortal. Each case runs a fresh interpreter, as the app does, so the pin is the one the ui
package makes.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Far more over-releases than either object's own count, some tens of thousands: an
# unpinned Linux 3.11 process aborts well before the end.
_CALLS = 100_000


def _child(body: str) -> subprocess.CompletedProcess:
    path = os.pathsep.join(p for p in (str(_ROOT), os.environ.get("PYTHONPATH", "")) if p)
    env = dict(os.environ, PYTHONPATH=path, QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, "-c", textwrap.dedent(body)], cwd=_ROOT,
                          env=env, capture_output=True, text=True, timeout=120)


def test_the_ui_package_keeps_none_alive_through_empty_reads():
    proc = _child(f"""
        import swi3s_studio.ui                              # pins None, True and False
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QAction, QStandardItem, QStandardItemModel
        from PySide6.QtWidgets import QApplication, QTreeWidgetItem
        app = QApplication([])
        item, tree_item, action = QStandardItem(), QTreeWidgetItem(), QAction()
        model = QStandardItemModel(1, 1)                    # kept: the index points into it
        index = model.index(0, 0)
        for _ in range({_CALLS} // 4):
            item.data(Qt.UserRole + 7)
            tree_item.data(0, Qt.ToolTipRole)
            index.data(Qt.UserRole + 7)
            action.data()
        print("ALIVE", flush=True)
    """)
    assert proc.returncode == 0 and "ALIVE" in proc.stdout, (
        f"rc {proc.returncode}: {proc.stderr[-1500:]}")


def test_the_ui_package_keeps_true_alive_through_signal_emits():
    proc = _child(f"""
        import swi3s_studio.ui                              # pins None, True and False
        from PySide6.QtCore import QCoreApplication, QObject, Signal

        class Source(QObject):
            changed = Signal(int)

        app = QCoreApplication([])
        source = Source()
        source.changed.connect(lambda _v: None)
        for i in range({_CALLS}):
            source.changed.emit(i)
        print("ALIVE", flush=True)
    """)
    assert proc.returncode == 0 and "ALIVE" in proc.stdout, (
        f"rc {proc.returncode}: {proc.stderr[-1500:]}")


def test_pinning_is_once_and_only_below_3_12():
    proc = _child("""
        import sys
        from swi3s_studio.ui.singleton_pin import _PIN, pin_singletons
        first = [sys.getrefcount(o) for o in (None, True, False)]
        assert pin_singletons() and pin_singletons()        # the ui package pinned already
        now = [sys.getrefcount(o) for o in (None, True, False)]
        assert all(b - a < 1000 for a, b in zip(first, now)), "pinned twice"
        if sys.version_info < (3, 12):
            assert all(n > _PIN for n in now)
        print("OK", flush=True)
    """)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stderr[-1500:]
