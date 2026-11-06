"""MainWindow's state is declared in __init__, its filter changes are batched, and the
timeline ribbon, the Audio, Capture and Samples views and the dock title bars answer
through public accessors that agree with what they hold. That main_window.py reads them
only through those accessors is test_main_window_source.py's."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

import pytest
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui


def _window(samples=300):
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_session(Session.from_demo(samples, cold_start=True, phy=2, register_map=win._rmap))
    return win


def test_a_new_window_declares_its_load_and_locate_state():
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    for name, want in (("_closing", False), ("_probing", False), ("_load_thread", None),
                       ("_load_worker", None), ("_load_dlg", None), ("_load_kind", "open"),
                       ("_locate_thread", None), ("_locate_worker", None),
                       ("_locate_dlg", None), ("_locate_session", None), ("_guide", None)):
        assert getattr(win, name) == want, name
    assert win._dock_titlebars and win._needs_capture and win._file_mode_menus
    win.join_worker_threads()


def _tick(menu, value) -> None:
    next(a for a in menu._actions if a.data() == value).setChecked(True)


def test_clearing_every_filter_clears_each_criterion_and_tests_each_command_once(
        monkeypatch):
    """Clear All Filters, after the kind, device and group menus, Errors Only and the text
    each set a criterion: every row shows again with no criterion active and the title
    plain; the rows are re-tested once and the timeline ticks refresh once."""
    from swi3s_studio.ui.main_window import MainWindow
    win = _window()
    proxy = win._cmd_proxy
    rows = win._cmd_model.rowCount()
    assert rows > 20
    _tick(win._kind_menu, "WriteA32")
    _tick(win._dev_menu, 1)
    _tick(win._group_menu, 0)
    win._errors_only_act.setChecked(True)
    proxy.set_text("a")
    assert proxy.rowCount() < rows and len(proxy.describe_active()) >= 4
    refreshed: list = []
    tested = _count_row_tests(monkeypatch, proxy)
    real_nav = MainWindow._update_nav_starts
    monkeypatch.setattr(MainWindow, "_update_nav_starts",
                        lambda self: refreshed.append(1) or real_nav(self))
    win._clear_all_filters()
    assert sorted(tested) == list(range(rows)), f"{len(tested)} row tests for {rows} commands"
    assert len(refreshed) == 1
    assert proxy.rowCount() == rows and proxy.describe_active() == []
    assert not win._errors_only_act.isChecked()
    assert not any(a.isChecked() for menu in (win._kind_menu, win._dev_menu, win._group_menu)
                   for a in menu._actions)
    assert win._cmd_title.text() == "Commands"


def _count_row_tests(monkeypatch, proxy) -> list:
    """A list that gains one entry per row `proxy` tests. The count is taken where the
    test reads its row, the source model's command_at, called from filterAcceptsRow: that is
    a virtual Qt calls through its own binding, which a patch of the Python class misses,
    and other readers of command_at (the timeline's visible-command list) are not row
    tests."""
    model = proxy.sourceModel()
    tested: list = []
    real = model.command_at

    def command_at(row):
        if sys._getframe(1).f_code.co_name == "filterAcceptsRow":
            tested.append(row)
        return real(row)
    monkeypatch.setattr(model, "command_at", command_at)
    return tested


def _rewrite_current_values(proxy):
    proxy.set_kinds(set(proxy._kinds), none=proxy._kinds_none)
    proxy.set_errors_only(proxy._errors_only)
    proxy.set_text(proxy.filter_text())
    proxy.set_devices(set(proxy._devices))
    proxy.set_groups(set(proxy._groups))


def _change_and_restore(proxy):
    proxy.set_kinds({"WriteA32"})
    proxy.set_errors_only(True)
    proxy.set_text("a")
    _restore_defaults(proxy)


def _restore_defaults(proxy):
    proxy.set_kinds(set())
    proxy.set_errors_only(False)
    proxy.set_text("")


@pytest.mark.parametrize("body", [lambda proxy: None, _rewrite_current_values,
                                  _change_and_restore],
                         ids=["empty", "current values", "changed and restored"])
def test_an_unchanged_batch_does_not_retest_the_rows(monkeypatch, body):
    """A batch whose criteria end as they began tests no row: an empty batch, setters that
    write the current values, and a change undone inside the batch."""
    win = _window()
    proxy = win._cmd_proxy
    before = proxy.rowCount()
    tested = _count_row_tests(monkeypatch, proxy)
    with proxy.batch():
        body(proxy)
    assert tested == [] and proxy.rowCount() == before


def test_a_batch_that_changes_the_criteria_tests_each_row_once_at_its_end(monkeypatch):
    win = _window()
    proxy = win._cmd_proxy
    rows = win._cmd_model.rowCount()
    tested = _count_row_tests(monkeypatch, proxy)
    with proxy.batch():
        proxy.set_kinds({"WriteA32"})
        proxy.set_errors_only(True)
        proxy.set_text("a")
        assert tested == [] and proxy.rowCount() == rows   # nothing re-tested inside it
    assert sorted(tested) == list(range(rows))
    assert proxy.rowCount() < rows


def test_binding_lanes_without_a_sync_leaves_the_lanes_alone(monkeypatch):
    win = _window()
    synced = []
    real = win._sync_lanes
    monkeypatch.setattr(win, "_sync_lanes", lambda: synced.append(1) or real())
    win._bind_lane_of(0, sync=False)
    assert synced == []
    win._bind_lane_of(0)
    assert synced == [1]


def test_the_ribbon_and_the_views_answer_through_public_accessors():
    win = _window()
    ribbon = win._timeline
    assert ribbon.total >= 1 and ribbon.view_range[0] <= ribbon.view_range[1]
    ribbon.set_peer_cursor(5)
    assert ribbon._cursor == 5
    assert win._audio_view.store is win._audio_view._store
    assert win._audio_view.shown_state is not None and win._raw_view.shown_state is not None
    assert win._sample_view.table_viewport() is win._sample_view._table.viewport()
    assert win._dock_titlebars
    assert all(bar.dock is bar._dock for bar in win._dock_titlebars)
