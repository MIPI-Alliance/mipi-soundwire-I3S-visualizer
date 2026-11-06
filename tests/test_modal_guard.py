"""The conftest guard that keeps a headless test from hanging on a modal dialog: every
entry point the app uses answers "cancel" and is recorded, so a test that opens one it
did not patch fails at teardown instead of waiting for a click that never comes."""
import pytest
from conftest import _MODAL_CANCEL
from PySide6 import QtWidgets
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

pytestmark = pytest.mark.gui


def test_every_static_helper_the_app_calls_is_guarded():
    """A helper the app starts calling but the guard does not know is a hang again. The
    list is read from the source: every static helper called on a Qt dialog class (a class
    named Q...Dialog or QMessageBox) is checked."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "swi3s_studio"
    used = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r"\b(Q\w*Dialog|QMessageBox)\.([a-z]\w*)\(", text))
    used = {u for u in used if u[1] not in ("setDefaultButton",)}
    unguarded = sorted(f"{c}.{m}" for c, m in used if (c, m) not in _MODAL_CANCEL
                       and callable(getattr(getattr(QtWidgets, c, None), m, None))
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


def test_a_static_helper_with_a_parent_is_recorded_under_its_caption(_no_unexpected_modal):
    """A static helper's first argument is the parent window, so the recorded name carries
    the dialog's caption (the first string argument), not the parent's title."""
    from PySide6.QtWidgets import QWidget
    QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.setWindowTitle("SWI3S Studio")
    QFileDialog.getOpenFileName(parent, "Pick a capture")
    QMessageBox.question(parent, "Add Link?", "?")
    assert _no_unexpected_modal == ["QFileDialog.getOpenFileName('Pick a capture')",
                                    "QMessageBox.question('Add Link?')"]
    _no_unexpected_modal.clear()


def test_a_menu_opened_on_an_instance_is_cancelled(monkeypatch, _no_unexpected_modal):
    """QMenu.exec has a static overload, so PySide answers `menu.exec(pos)` with the
    built-in method even when the class attribute is patched; the guard routes it through
    __getattribute__."""
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
