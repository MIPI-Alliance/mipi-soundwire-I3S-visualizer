"""The conftest guard that keeps a headless test from hanging on a modal dialog: every
entry point the app uses answers "cancel" and is recorded, so a test that opens one it
did not patch fails at teardown instead of waiting for a click that never comes."""
import pytest
from conftest import _MODAL_CANCEL
from PySide6 import QtWidgets
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox


def test_every_static_helper_the_app_calls_is_guarded():
    """A helper the app starts calling but the guard does not know is a hang again. The
    list is read from the source, so a new call site cannot slip past."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "swi3s_studio"
    used = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r"\b(QMessageBox|QFileDialog|QInputDialog)\.([a-z]\w*)\(",
                               text))
    used = {u for u in used if u[1] not in ("setDefaultButton",)}
    unguarded = sorted(f"{c}.{m}" for c, m in used if (c, m) not in _MODAL_CANCEL
                       and callable(getattr(getattr(QtWidgets, c), m, None))
                       and m.startswith(("get", "question", "information", "warning",
                                         "critical", "about", "exec")))
    assert not unguarded, f"modal helpers the conftest guard does not cover: {unguarded}"


def test_an_unpatched_modal_is_cancelled_and_recorded(_no_unexpected_modal):
    QApplication.instance() or QApplication([])
    opened = _no_unexpected_modal
    assert QFileDialog.getOpenFileName(None, "Pick a capture") == ("", "")
    assert QInputDialog.getText(None, "Rename Link", "Name:")[1] is False
    assert QMessageBox.question(None, "Add Link?", "?") != QMessageBox.Yes
    box = QMessageBox()
    box.setText("Found 3")
    assert box.exec() == 0
    assert opened == ["QFileDialog.getOpenFileName('Pick a capture')",
                      "QInputDialog.getText('Rename Link')",
                      "QMessageBox.question('Add Link?')",
                      "QMessageBox.exec('Found 3')"]
    opened.clear()                          # handled here; the guard's teardown passes


def test_a_menu_opened_on_an_instance_is_cancelled(monkeypatch, _no_unexpected_modal):
    """QMenu.exec has a static overload, so PySide answers `menu.exec(pos)` with the
    built-in method even when the class attribute is patched: without the guard's
    __getattribute__ route this opened a real menu and hung."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QMenu
    QApplication.instance() or QApplication([])
    menu = QMenu("Stream")
    menu.addAction("Color…")
    assert menu.exec(QPoint(0, 0)) is None
    assert _no_unexpected_modal == ["QMenu.exec('Stream')"]
    _no_unexpected_modal.clear()
    monkeypatch.setattr(QMenu, "exec", lambda m, *_a: m.actions()[0])   # a test's own
    assert menu.exec(QPoint(0, 0)).text() == "Color…"


def test_a_test_s_own_patch_wins(monkeypatch, _no_unexpected_modal):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    assert QMessageBox.question(None, "Add Link?", "?") == QMessageBox.Yes
    assert _no_unexpected_modal == []


@pytest.mark.parametrize("key", sorted(_MODAL_CANCEL))
def test_every_guarded_name_exists(key):
    """A misspelt entry would patch nothing and guard nothing."""
    cls, meth = key
    assert callable(getattr(getattr(QtWidgets, cls), meth))
