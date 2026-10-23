"""The Import warnings window: a load, or any import a window runs, that raised import
warnings shows them in one non-modal window per import, with a count in the status bar."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from conftest import pump_loads
from PySide6.QtWidgets import QApplication

from swi3s_studio import import_notes


def _window():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    return MainWindow()


def _demo(win):
    from swi3s_studio.session import Session
    return Session.from_demo(300, cold_start=True, phy=2, register_map=win._rmap)


def test_a_load_with_warnings_opens_one_non_modal_window_listing_them():
    win = _window()

    def factory():
        import_notes.warn("cap.vcd", "12 lines were not read (first at line 40)")
        import_notes.warn("cap.vcd", "timestamps go backwards at 2 places; reordered")
        return _demo(win)

    win._load_async(factory)
    pump_loads(win)
    dlgs = win.import_warning_dialogs()
    assert len(dlgs) == 1
    dlg = dlgs[0]
    assert dlg.isVisible() and not dlg.isModal()
    assert dlg.windowTitle() == "Import warnings — cap.vcd"
    assert dlg.lines() == ["cap.vcd: 12 lines were not read (first at line 40)",
                           "cap.vcd: timestamps go backwards at 2 places; reordered"]
    assert "2 import warnings in cap.vcd" in win._status.text()
    assert [n.source for n in win._session.import_warnings] == ["cap.vcd", "cap.vcd"]


def test_a_clean_load_opens_no_window():
    win = _window()
    win._load_async(lambda: _demo(win))
    pump_loads(win)
    assert win.import_warning_dialogs() == []
    assert win._session.import_warnings == []
    assert "import warning" not in win._status.text()


def test_each_import_gets_its_own_window_and_closing_one_deletes_it():
    win = _window()
    with win.reporting_import():
        import_notes.warn("one.csv", "a")
    with win.reporting_import():
        import_notes.warn("two.json", "b")
    dlgs = win.import_warning_dialogs()
    assert [d.windowTitle() for d in dlgs] == ["Import warnings — one.csv",
                                               "Import warnings — two.json"]
    dlgs[0].close()
    QApplication.processEvents()
    QApplication.sendPostedEvents(None, 0)      # the deferred delete of WA_DeleteOnClose
    assert [d.windowTitle() for d in win.import_warning_dialogs()] == [
        "Import warnings — two.json"]


def test_a_failed_import_shows_its_error_not_a_warnings_window():
    win = _window()
    try:
        with win.reporting_import():
            import_notes.warn("bad.csv", "x")
            raise ValueError("unreadable")
    except ValueError:
        pass
    assert win.import_warning_dialogs() == []
