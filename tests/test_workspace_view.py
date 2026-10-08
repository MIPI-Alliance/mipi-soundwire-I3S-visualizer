"""The whole Analysis view is a Link's or the window's, and nothing a user set is lost by a
Link switch, a re-decode, or a save and reopen. Offscreen Qt."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui import view_state


def _session(win, variant=""):
    from swi3s_studio.session import Session
    return Session.from_demo(300, cold_start=True, phy=2, variant=variant,
                             register_map=win._rmap)


@pytest.fixture
def two():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.resize(1400, 900)
    win.show()
    win.load_session(_session(win))
    win.load_session(_session(win, "flow_control"), add_link=True)
    win.switch_link(0)
    QApplication.processEvents()
    return win


def _set_audio_and_capture(win):
    """Change everything the Audio and Capture panes let a user change. Both zooms take in
    the cursor, as they do when a user zooms where they clicked: a pane brings a cursor
    outside its window back into view, which is not losing the zoom."""
    av, rv = win._audio_view, win._raw_view
    rate = float(win._session.sample_rate_hz)
    t0, t1 = av.x_range_seconds()
    win.cursor.set_sample(int((t0 + (t1 - t0) * 0.28) * rate))
    first = sorted(av._checks)[0]
    av._checks[first].setChecked(False)
    dev, dp = av.streams()[0]
    av.set_play_rate_target(dev, dp, 16000)
    av._vzoom_btn.setChecked(True)
    av._splitter.setSizes([240, 700])
    t0, t1 = av.x_range_seconds()
    av.set_x_range_seconds(t0 + (t1 - t0) * 0.25, t0 + (t1 - t0) * 0.5)
    rv._cds_btn.setChecked(not rv._cds_btn.isChecked())
    rv._legend_btn.setChecked(not rv._legend_btn.isChecked())
    r0, r1 = rv.x_range_seconds()
    rv.set_x_range_seconds(r0 + (r1 - r0) * 0.2, r0 + (r1 - r0) * 0.4)
    QApplication.processEvents()
    return view_state.audio_state(av), view_state.capture_state(rv)


def _close(a, b):
    """Two pane states equal, the zooms to within a sample of floating point."""
    assert set(a) == set(b)
    for k in a:
        if k.endswith("_range_s"):
            assert a[k] == pytest.approx(b[k], rel=1e-6, abs=1e-9), k
        else:
            assert a[k] == b[k], k


def test_a_link_switch_keeps_each_links_audio_and_capture_view(two):
    audio, capture = _set_audio_and_capture(two)
    two.switch_link(1)
    assert view_state.audio_state(two._audio_view)["vertical_zoom"] is False   # Link 2's own
    two.switch_link(0)
    QApplication.processEvents()
    _close(view_state.audio_state(two._audio_view), audio)
    _close(view_state.capture_state(two._raw_view), capture)


def test_a_re_decode_keeps_the_audio_and_capture_view(two):
    audio, capture = _set_audio_and_capture(two)
    two.load_session(two._session)                    # an SSP step, an override, a toggle
    QApplication.processEvents()
    _close(view_state.audio_state(two._audio_view), audio)
    _close(view_state.capture_state(two._raw_view), capture)


def test_a_new_capture_starts_from_the_panes_defaults(two):
    _set_audio_and_capture(two)
    two.load_session(_session(two))                   # an open replaces the analysis
    st = view_state.audio_state(two._audio_view)
    assert st["vertical_zoom"] is False and st["decimation"] == []
    assert len(st["channels"]) == len(two._audio_view._checks)


def test_a_damaged_pane_state_is_ignored(two):
    for bad in (None, [], {"channels": "x", "x_range_s": [5, 1], "decimation": [[1]],
                           "channel_list_width": "a", "vertical_zoom": True}):
        view_state.restore_audio(two._audio_view, bad)
        view_state.restore_capture(two._raw_view, bad)
    assert two._audio_view._checks                      # nothing blanked


# ------------------------------------------------- a saved workspace brings the view back
_DOCKS = ("_timeline_dock", "_reg_dock", "_grid_dock", "_audio_dock", "_symbol_dock",
          "_sample_dock", "_raw_dock", "_eye_dock", "_meas_dock", "_pair_dock")


def _raised(win, dock):
    """Is this dock the shown tab of its group (or alone and shown)?"""
    return not dock.isHidden() and not dock.visibleRegion().isEmpty()


def full_snapshot(win) -> dict:
    """Everything a workspace promises to restore, as the window shows it."""
    from window_checks import snapshot
    win._keep_shown_views()
    links = []
    for link in win.links:
        st = link.view
        links.append({"filters": view_state.filters_to_json(st.filters),
                      "col_widths": list(st.col_widths or []),
                      "audio": st.audio_view, "capture": st.capture_view,
                      "processing": dict(st.stream_processing),
                      "colors": dict(st.stream_colors)})
    a = win.links.active_index
    r = win._ribbons[a]
    return {
        "links_and_bookmarks": snapshot(win),
        "links": links,
        "timeline_ps": [win.links[a].to_ps(round(r._view_lo)), win.links[a].to_ps(round(r._view_hi))],
        "commands_header": view_state.commands_header_state(win._cmd_view),
        "samples": view_state.samples_state(win._sample_view),
        "registers": view_state.registers_state(win._reg_view),
        "statistics": view_state.statistics_state(win._meas_view),
        "timing": view_state.timing_state(win._eye_view),
        "docks": {name: (getattr(win, name).isHidden(), getattr(win, name).isFloating(),
                         int(win.dockWidgetArea(getattr(win, name)).value),
                         _raised(win, getattr(win, name))) for name in _DOCKS},
        "mode": win._mode_mgr.current(),
    }


def _change_everything(win):
    from swi3s_studio.store.audio_store import StreamProcessing
    _set_audio_and_capture(win)                                 # Link 1's Audio and Capture
    dev, dp = win._audio_view.streams()[0]
    win.set_stream_processing(dev, dp, StreamProcessing(highpass_hz=40.0, gain_db=3.0))
    # Commands: a kind filter, errors only, a text filter, a column width, a hidden column
    # and a sort.
    snap = win._filter_snapshot()
    kinds = [a.data() for a in win._kind_menu._actions]
    snap.update(kinds={kinds[0]}, errors_only=False, text="device == 1")
    win._restore_filters(snap)
    win._cmd_view.setColumnWidth(2, 211)
    win._cmd_view.setColumnHidden(3, True)
    win._cmd_view.sortByColumn(1, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.DescendingOrder)
    # Link 2 gets its own Audio view.
    win.switch_link(1)
    win._audio_view._vzoom_btn.setChecked(True)
    win.switch_link(0)
    # Bookmarks, the timeline's zoom, the side panes.
    win._bookmarks.add(1000, link=0)
    win._bookmarks.add(2000, link=1)
    win._push_bookmarks()
    r = win._ribbons[0]
    span = r._view_hi - r._view_lo
    r._set_view(r._view_lo + span * 0.3, r._view_lo + span * 0.6, emit=True)
    win._show_dock(win._sample_dock)
    QApplication.processEvents()
    if win._sample_view._port_cb._menu.actions():
        win._sample_view._port_cb._menu.actions()[0].setChecked(True)
    win._sample_view._op_cb.setCurrentIndex(1)
    win._sample_view._val_edit.setText("12")
    win._meas_view._collapsed_titles = {"Clock"}
    win._meas_view._apply_folds()
    win._eye_view._cat_hidden = {(True, False)}
    reg = win._reg_view
    win._show_dock(win._reg_dock)
    QApplication.processEvents()
    if reg._device.count() > 1:
        reg._device.setCurrentIndex(reg._device.count() - 1)
    root = reg._tree.invisibleRootItem()
    assert root.childCount() > 1, "the demo's Registers pane should list blocks"
    for i in range(root.childCount()):
        root.child(i).setExpanded(i == root.childCount() - 1)
    # Docks: Capture raised in the bottom group, CDS closed, Bookmarks closed.
    win._raw_dock.raise_()
    win._symbol_dock.hide()
    win._pair_dock.hide()
    win.resize(1320, 860)
    QApplication.processEvents()


def test_a_saved_workspace_reopens_exactly_as_it_was(two, tmp_path, monkeypatch):
    from conftest import pump_loads
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    _change_everything(two)
    before = full_snapshot(two)
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    two.save_workspace()

    again = MainWindow()
    again.resize(1000, 700)
    again.show()
    again.open_workspace()
    pump_loads(again)
    for _ in range(5):
        QApplication.processEvents()
    after = full_snapshot(again)
    for key in before:
        assert after[key] == pytest.approx(before[key]) if key == "timeline_ps" else \
            after[key] == before[key], (key, before[key], after[key])


def test_the_window_geometry_is_handed_back_to_qt_unchanged(two, tmp_path, monkeypatch):
    """Size and position are Qt's to restore: restoreGeometry fits a window onto the screen
    it lands on, and the offscreen test screen (800 x 800) is narrower than the window's
    minimum width, so no size survives here. What is the app's part: the bytes saveGeometry
    gave at save time reach restoreGeometry on reopen, after the deferred layout passes."""
    import json

    from conftest import pump_loads
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    two.resize(1300, 850)
    QApplication.processEvents()
    saved = bytes(two.saveGeometry())
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    two.save_workspace()
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["view"]["window"]["geometry"]
    got = []
    real = MainWindow.restoreGeometry
    monkeypatch.setattr(MainWindow, "restoreGeometry",
                        lambda self, g: (got.append(bytes(g)), real(self, g))[1])
    again = MainWindow()
    again.show()
    again.open_workspace()
    pump_loads(again)
    for _ in range(5):
        QApplication.processEvents()
    assert got and all(g == saved for g in got) and len(got) == 2      # now, and once settled



def test_the_all_links_commands_filter_and_lanes_come_back(two, tmp_path, monkeypatch):
    """With Commands and the signal panes on All Links, the shared filter is the window's,
    and each lane keeps its Link's channels and Vertical Zoom (the lanes share one zoom)."""
    from conftest import pump_loads
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    two.set_commands_scope_all(True)
    two.set_bottom_scope_all(True)
    snap = two._filter_snapshot()
    snap.update(kinds={[a.data() for a in two._kind_menu._actions][-1]}, text="dp == 1")
    two._restore_filters(snap, kinds=two._all_kinds())
    lane = two._audio_lanes.views[1]
    lane._vzoom_btn.setChecked(True)
    key = sorted(lane._checks)[-1]
    lane._checks[key].setChecked(False)
    QApplication.processEvents()
    want_filter = two._filter_snapshot()
    want_lanes = [(v._vzoom_btn.isChecked(), sorted(v.channel_selection()))
                  for v in two._audio_lanes.views[:2]]
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    two.save_workspace()
    again = MainWindow()
    again.show()
    again.open_workspace()
    pump_loads(again)
    QApplication.processEvents()
    assert again._cmd_all and again._bottom_all
    assert again._filter_snapshot() == want_filter
    assert [(v._vzoom_btn.isChecked(), sorted(v.channel_selection()))
            for v in again._audio_lanes.views[:2]] == want_lanes
