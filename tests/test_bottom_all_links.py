"""All Links in the bottom group: Capture and Audio show every Link, one lane each.

Each lane is the ordinary single-Link view, bound under that Link's `_as_link`, so it never
learns about Links. What these pin is what the stack adds: lane k shows Link k (its capture,
audio, bookmarks, stream colours); the lanes read ONE global time (each lane's X range and
axis labels are its own capture's, moved by its Link's offset); a click, drag or seek in lane
k counts in Link k; touching a lane makes its Link active; and leaving All Links, or going
down to one Link, gives back the pane's own view on the group's Link.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from test_links_gui import _with_added_link

from swi3s_studio.ui.eye_view import _cat_specs

_OFFSET_PS = 3_000_000_000          # Link 2 starts 3 ms after Link 1


@pytest.fixture
def two():
    win, first, second = _with_added_link()
    win._raw_dock.show()
    win._audio_dock.show()
    win.set_link_offset(1, _OFFSET_PS)
    win.set_bottom_scope_all(True)
    return win, first, second


def _lanes(win):
    return win._raw_lanes.shown(), win._audio_lanes.shown()


def test_each_lane_shows_its_own_link(two):
    win, first, second = two
    raws, audios = _lanes(win)
    assert len(raws) == len(audios) == 2
    assert raws[0] is win._raw_view and audios[0] is win._audio_view
    for k, sess in enumerate((first, second)):
        assert raws[k]._cds_provider == sess.cds_column_samples      # bound to Link k
        assert audios[k]._store is win.links[k].view.audio_store
    assert audios[0]._store is not audios[1]._store
    labels = [lbl.text() for lbl in win._raw_lanes._labels[:2]]
    assert labels == ["Link 1", "Link 2"]
    assert all(lbl.isVisibleTo(win._raw_lanes) for lbl in win._raw_lanes._labels[:2])


def test_the_lanes_read_one_global_time(two):
    """A range set on one lane moves the other by the offset between them, and both axes
    label a given instant with the same time."""
    win, first, second = two
    raws, audios = _lanes(win)
    off = _OFFSET_PS / 1e12
    raws[0].set_x_range_seconds(0.004, 0.005)          # Link 1's 4-5 ms = global 4-5 ms
    t0, t1 = raws[1].x_range_seconds()
    assert t0 == pytest.approx(0.004 - off, abs=1e-9) and t1 == pytest.approx(0.005 - off, abs=1e-9)
    a0 = raws[0]._plot.getAxis("bottom").tickStrings([0.0045], 1, 1e-4)
    a1 = raws[1]._plot.getAxis("bottom").tickStrings([0.0045 - off], 1, 1e-4)
    assert a0 == a1
    # Audio's X is capture samples, so the move goes through each lane's rate as well.
    audios[1].set_x_range_seconds(0.002, 0.003)        # Link 2's own 2-3 ms
    g0, g1 = audios[0].x_range_seconds()
    assert g0 == pytest.approx(0.002 + off, abs=1e-6) and g1 == pytest.approx(0.003 + off, abs=1e-6)


def test_the_cursor_is_one_instant_on_every_lane(two):
    win, first, second = two
    raws, audios = _lanes(win)
    s = int(first.commands[len(first.commands) // 2]["start_sample"])
    win._activate_link(0)
    win.cursor.set_sample(s)
    t_global = [raws[k]._cursor.value() + win.links[k].offset_ps / 1e12 for k in (0, 1)]
    assert t_global[0] == pytest.approx(t_global[1], abs=2 / first.sample_rate_hz)
    rate = [audios[k]._capture_rate for k in (0, 1)]
    a_global = [audios[k]._cursor_sample / rate[k] + win.links[k].offset_ps / 1e12 for k in (0, 1)]
    assert a_global[0] == pytest.approx(a_global[1], abs=2 / min(rate))


def test_each_lane_draws_its_own_links_bookmarks(two):
    win, first, second = two
    raws, audios = _lanes(win)
    win._bookmarks.clear()
    win._bookmarks.add(int(first.commands[5]["start_sample"]), link=0)
    win._bookmarks.add(int(second.commands[5]["start_sample"]), link=1)
    win._push_bookmarks()
    on = [set(raws[k]._bm_lines) for k in (0, 1)]
    assert on[0] and on[1] and not (on[0] & on[1])
    assert {lb for _p, lb in audios[1]._bm_lines} == on[1]


def test_a_seek_and_a_drag_in_a_lane_count_in_its_link(two):
    win, first, second = two
    raws, _audios = _lanes(win)
    win._activate_link(0)
    s2 = int(second.commands[20]["start_sample"])
    raws[1].sampleSelected.emit(s2)                    # a click in Link 2's lane
    assert win.links[1].to_ps(s2) == pytest.approx(
        win.links[win.links.active_index].to_ps(win.cursor.sample), abs=1e6)
    win._activate_link(1)                              # the group's own Link is Link 2 too
    s1 = int(first.commands[len(first.commands) // 2]["start_sample"])   # inside Link 2
    raws[0].sampleSelected.emit(s1)                    # so lane 1 must be read as Link 1
    assert win.links[0].to_ps(s1) == pytest.approx(
        win.links[win.links.active_index].to_ps(win.cursor.sample), abs=1e6)
    win._bookmarks.clear()
    bm = win._bookmarks.add(int(second.commands[5]["start_sample"]), link=1)
    win._push_bookmarks()
    raws[1].bookmarkMoved.emit(bm.display(), 7000, False)   # mid-drag: no edge snap
    assert [(b.link, b.sample) for b in win._bookmarks] == [(1, 7000)]


def test_touching_a_lane_makes_its_link_active(two):
    win, first, second = two
    raws, audios = _lanes(win)
    win._activate_link(0)
    win._on_pane_touched(raws[1])
    assert win.links.active_index == 1
    win._on_pane_touched(audios[0])
    assert win.links.active_index == 0


def test_a_lane_colour_is_its_links(two, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QColorDialog, QMenu
    win, first, second = two
    _raws, audios = _lanes(win)
    (dev, dp, _ch), cb = next(iter(audios[1]._checks.items()))
    monkeypatch.setattr(QMenu, "exec", lambda m, *_a: next(
        a for a in m.actions() if a.text() == "Color…"))        # by name, not position
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor("#123456"))
    audios[1]._stream_menu(cb, QPoint(0, 0), dev, dp)
    assert win.links[1].view.stream_colors == {(dev, dp, "dark"): "#123456"}
    assert win.links[0].view.stream_colors == {}


def test_one_lane_plays_at_a_time(two, monkeypatch):
    from swi3s_studio.ui.audio_view import AudioView
    win, first, second = two
    _raws, audios = _lanes(win)
    playing = {audios[0]}
    monkeypatch.setattr(AudioView, "is_playing", property(lambda self: self in playing))
    stopped = []
    monkeypatch.setattr(audios[0], "stop", lambda: (stopped.append(0), playing.discard(audios[0])))
    playing.add(audios[1])
    audios[1].playStarted.emit()
    assert stopped == [0] and playing == {audios[1]}


def test_an_offset_change_moves_the_lanes(two):
    win, first, second = two
    raws, _audios = _lanes(win)
    win.set_link_offset(1, 0)
    raws[0].set_x_range_seconds(0.004, 0.005)
    assert raws[1].x_range_seconds()[0] == pytest.approx(0.004, abs=1e-9)


def test_leaving_all_links_gives_back_the_panes_own_view(two):
    win, first, second = two
    win.set_group_link("bottom", 1)
    assert not win._bottom_all
    assert win._raw_lanes.shown() == [win._raw_view]
    assert win._audio_view._store is win.links[1].view.audio_store
    assert not win._raw_lanes._labels[0].isVisibleTo(win._raw_lanes)


def test_the_layout_round_trips(two):
    win, first, second = two
    prefs = win._link_view_prefs()
    assert prefs["bottom_all"] is True
    win.set_bottom_scope_all(False)
    win._apply_link_view_prefs(prefs, 1)
    assert win._bottom_all and len(win._raw_lanes.shown()) == 2


def test_one_link_left_leaves_all_links(two, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win, first, second = two
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    win._activate_link(1)
    win.remove_link()
    assert not win._bottom_all and win._raw_lanes.shown() == [win._raw_view]
    assert win._audio_view._store is win.links[0].view.audio_store


def test_removing_a_middle_link_moves_the_next_up_a_lane(two, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from test_links_gui import _flow_control
    win, first, second = two
    third = _flow_control(win)
    win.load_session(third, add_link=True)
    assert len(win._raw_lanes.shown()) == 3
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    win.remove_link(1)
    raws, audios = _lanes(win)
    assert len(raws) == 2 and win._bottom_all
    assert raws[1]._cds_provider == third.cds_column_samples
    assert audios[1]._store is win.links[1].view.audio_store
    assert [lbl.text() for lbl in win._raw_lanes._labels[:2]] == [lk.name for lk in win.links]


# ---- CDS and Samples: one table per Link, stacked ----------------------------------------

@pytest.fixture
def tables(two):
    win, first, second = two
    win.show()
    for dock in (win._symbol_dock, win._sample_dock):
        dock.show()
    win._activate_link(0)
    # Late enough to be past both bring-ups (Link 2's audio starts 3 ms + its own cold
    # start later), so each lane's Samples window has audio around the instant.
    win.cursor.set_sample(int(first.last_sample() * 0.85))
    for k, sess in enumerate((first, second)):
        assert win._in_link(k, win.cursor.sample) > sess.audio_start_sample, k
    return win, first, second


def _tables(win):
    return win._symbol_lanes.shown(), win._sample_lanes.shown()


def test_each_table_lane_windows_its_own_link_at_the_cursors_instant(tables):
    win, first, second = tables
    syms, smps = _tables(win)
    assert len(syms) == len(smps) == 2
    for k, sess in enumerate((first, second)):
        s_k = win._in_link(k, win.cursor.sample)
        assert syms[k]._symbols == sess.symbols_around(s_k)
        starts = [int(r["start_sample"]) for r in smps[k]._samples]
        assert starts and starts[0] <= s_k <= starts[-1], k     # windowed on Link k's instant
        assert smps[k]._rate == sess.sample_rate_hz


def test_the_time_columns_read_global_time(tables):
    win, first, second = tables
    syms, _smps = _tables(win)
    for k, sess in enumerate((first, second)):
        s = syms[k]._symbols[0]
        want = s["start_sample"] / sess.sample_rate_hz * 1e6 + win.links[k].offset_ps / 1e6
        assert syms[k].item(0, 1).text() == f"{want:,.2f}"


def test_a_row_picked_in_one_lane_moves_the_others_and_leaves_its_own(tables):
    win, first, second = tables
    syms, _smps = _tables(win)
    before = list(syms[1]._symbols)
    here = win._in_link(1, win.cursor.sample)
    later = [r for r in before if int(r["start_sample"]) > here]
    idx = before.index(later[5])                       # a few symbols on from the cursor
    syms[1].setCurrentCell(idx, 2)                     # a click on that row
    assert win.links.active_index == 1                 # the lane's Link, as a touch makes it
    assert syms[1]._symbols == before                  # not re-windowed under the click
    assert syms[1].currentRow() == idx                 # nor snapped to its command's comma
    s0 = win._in_link(0, win.cursor.sample)
    assert syms[0]._symbols == first.symbols_around(s0)


def test_leaving_all_links_gives_back_the_tables(tables):
    win, first, second = tables
    win.set_group_link("bottom", 1)
    assert win._symbol_lanes.shown() == [win._symbol_view]
    assert win._symbol_view._time_offset_us == 0.0
    assert win._sample_lanes.shown() == [win._sample_view]


# ---- Timing: one pane per Link, each keeping its edge-flavour colours ---------------------
#
# Not one overlay with a colour per Link: that would take away the four edge-flavour colours
# the pane is read by.

@pytest.fixture
def timing(two):
    win, first, second = two
    win.show()
    win._eye_dock.show()
    win._eye_dock.raise_()
    for view in win._eye_lanes.shown():
        view._render()
    return win, first, second


def _curve_colours(plot):
    return {item.opts["pen"].color().name() for item in plot.listDataItems()}


def test_each_timing_lane_measures_its_own_link_in_flavour_colours(timing):
    win, first, second = timing
    eyes = win._eye_lanes.shown()
    assert len(eyes) == 2 and eyes[0] is win._eye_view
    flavours = {c.lower() for *_x, c in _cat_specs()}
    for k, sess in enumerate((first, second)):
        assert eyes[k]._capture is sess.capture
        assert eyes[k]._timing is not None
        drawn = _curve_colours(eyes[k]._setup_plot)
        assert drawn and drawn <= flavours, k               # flavour colours, not a Link's
    assert [lbl.text() for lbl in win._eye_lanes._labels[:2]] == ["Link 1", "Link 2"]


def test_each_timing_lane_is_the_single_pane_of_its_link(timing):
    win, first, second = timing
    lanes = [eye._timing for eye in win._eye_lanes.shown()]
    for k in (0, 1):
        win.set_group_link("bottom", k)
        win._eye_view._render()
        alone = win._eye_view._timing
        assert np.array_equal(alone.tr_setup_ns, lanes[k].tr_setup_ns), k


def test_a_worst_ui_jump_in_a_lane_goes_to_its_link(timing):
    win, first, second = timing
    eye1 = win._eye_lanes.shown()[1]
    assert eye1._worst
    win._activate_link(0)
    eye1._view_worst()
    assert win.links.active_index == 1
    assert win.cursor.sample == eye1._worst[0][0]


def test_leaving_all_links_gives_back_the_timing_pane(timing):
    win, first, second = timing
    win.set_group_link("bottom", 1)
    assert win._eye_lanes.shown() == [win._eye_view]
    assert win._eye_view._capture is second.capture


# ---- the way in: the bottom group's picker and View ▸ Links -----------------------------

def test_the_bottom_picker_offers_all_links_and_the_grid_s_does_not():
    win, first, second = _with_added_link()
    names = lambda p: [p.itemText(i) for i in range(p.count())]   # noqa: E731
    for picker in win._pane_pickers["bottom"]:
        assert names(picker) == ["Link 1", "Link 2", "All Links"]
    for group in ("right", "grid"):
        for picker in win._pane_pickers[group]:
            assert names(picker) == ["Link 1", "Link 2"]
    win._on_group_picker("bottom", 2)                  # the last entry
    assert win._bottom_all and win._bottom_all_action.isChecked()
    assert all(p.currentText() == "All Links" for p in win._pane_pickers["bottom"])
    win._on_group_picker("bottom", 0)                  # a Link: back to one
    assert not win._bottom_all and win._pane_link["bottom"] == 0
    assert all(p.currentText() == "Link 1" for p in win._pane_pickers["bottom"])
    assert not win._bottom_all_action.isChecked()


def test_view_links_toggles_all_links_and_only_with_two(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win, first, second = _with_added_link()
    assert win._bottom_all_action.isEnabled()
    win._bottom_all_action.trigger()
    assert win._bottom_all
    win._bottom_all_action.trigger()
    assert not win._bottom_all
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    win.remove_link(1)
    assert not win._bottom_all_action.isEnabled()
    for picker in win._pane_pickers["bottom"]:
        assert "All Links" not in [picker.itemText(i) for i in range(picker.count())]


def test_lanes_of_different_lengths_share_one_window_and_its_ticks(two):
    """Each view used to cap its zoom at its own capture, so a shorter Link's lane could
    not show the other's span and the lanes parted; and ticks chosen in each lane's own
    time fell at different instants under one another."""
    win, first, second = two
    raws, audios = _lanes(win)
    for lanes in (raws, audios):
        lanes[0].reset_zoom()
        g = [tuple(t + o for t in v.x_range_seconds())
             for v, o in zip(lanes, (0.0, _OFFSET_PS / 1e12))]
        assert g[0] == pytest.approx(g[1], abs=1e-6), lanes[0]
    off = _OFFSET_PS / 1e12
    for lane, o in ((raws[0], 0.0), (raws[1], off)):
        ax = lane._plot.getAxis("bottom")
        lo, hi = lane.x_range_seconds()
        major = ax.tickValues(lo, hi, 800)[0][1]
        globals_ = [round(v + o, 9) for v in major]
        if lane is raws[0]:
            want = globals_
    assert globals_ == want and len(want) > 1        # the same global ticks on both


def test_leaving_all_links_gives_the_bottom_panes_their_one_link_height():
    """Two lanes need more room, so Qt grows the bottom docks on the way in; it never
    shrinks a dock by itself, so they stayed tall after going back to one Link."""
    win, first, second = _with_added_link()
    win.resize(1500, 1000)
    win.show()
    win._mode_mgr.switch_to("Analysis")
    for _ in range(3):                                 # the first-time layout raises the grid
        QApplication.processEvents()
    win._raw_dock.raise_()                             # Capture in front: it has the lanes
    for _ in range(3):
        QApplication.processEvents()
    assert win._raised_bottom_dock() is win._raw_dock
    single = win._raw_dock.height()
    win._on_group_picker("bottom", 2)                  # All Links, from the picker
    for _ in range(3):
        QApplication.processEvents()
    assert win._raw_dock.height() > single             # the premise: it grew
    win._on_group_picker("bottom", 1)                  # back to one Link
    for _ in range(5):
        QApplication.processEvents()
    assert abs(win._raw_dock.height() - single) <= 2


# ---- the lanes stay true to their Links ------------------------------------------

def _all_links_win():
    from test_links_gui import _with_added_link
    win, first, second = _with_added_link()
    win.set_bottom_scope_all(True)
    return win, first, second


def test_playback_decimation_lists_and_sets_the_active_links_streams():
    win, first, second = _all_links_win()
    win._activate_link(0)
    win._play_rate_menu.aboutToShow.emit()                    # as opening the menu does
    subs = [a.text() for a in win._play_rate_menu.actions()]
    lane0 = win._audio_lanes.views[0]
    assert subs == [f"Device {d} · DP{p}" for d, p in lane0.streams()]
    dev, dp = lane0.streams()[0]
    # (QAction.menu() answers a deleted wrapper on PySide 6.11; the submenus are held.)
    assert len(win._play_rate_subs) == len(subs)
    rate = next(a for a in win._play_rate_subs[0].actions() if a.text() == "48 kHz")
    rate.trigger()
    assert lane0.play_rate_target_for(dev, dp) == 48000
    assert win._audio_lanes.views[1].play_rate_target_for(dev, dp) != 48000


def test_an_offset_change_keeps_the_lanes_on_one_window():
    win, _first, _second = _all_links_win()
    raw = win._raw_lanes.views
    raw[0].set_x_range_seconds(0.004, 0.0045)
    win.set_link_offset(1, win.links[1].offset_ps + 100_000_000)       # +0.1 ms
    offs = [link.offset_ps / 1e12 for link in win.links]
    g = [tuple(t + o for t in raw[k].x_range_seconds()) for k, o in enumerate(offs)]
    assert g[0] == pytest.approx(g[1], abs=1e-9)


def test_showing_a_link_everywhere_keeps_its_lanes_audio_choices():
    win, _first, _second = _all_links_win()
    lane0 = win._audio_lanes.views[0]
    key = next(iter(lane0._checks))
    lane0._checks[key].setChecked(False)
    lane0._vzoom_btn.setChecked(True)
    before = list(lane0.channel_selection())
    win.switch_link(0)
    assert list(lane0.channel_selection()) == before and lane0._vzoom


def test_a_removed_links_lane_lets_its_session_go(monkeypatch):
    import gc
    import weakref
    win, _first, second = _all_links_win()
    from PySide6.QtWidgets import QMessageBox
    gone = weakref.ref(second)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    win.remove_link(1)
    del second, _first
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
    assert gone() is None


def test_a_theme_switch_reaches_every_lane():
    from swi3s_studio.ui.theme import VizTheme
    win, _first, _second = _all_links_win()
    was = VizTheme.MODE
    try:
        win.apply_theme("light" if was == "dark" else "dark")
        label = win._raw_lanes._labels[1]
        assert VizTheme.TEXT in label.styleSheet()
        lane1 = win._raw_lanes.views[1]
        lane0 = win._raw_lanes.views[0]
        pen = lambda v: pg.mkPen(v._dp_curve.opts["pen"]).color().name()   # noqa: E731
        assert pen(lane1) == pen(lane0)
    finally:
        win.apply_theme(was)


def test_a_link_name_in_a_lane_label_is_text_not_markup():
    win, _first, _second = _all_links_win()
    win.links.rename(1, "<b>bus</b>")
    win._sync_lanes()
    label = win._raw_lanes._labels[1]
    assert label.textFormat() == Qt.PlainText and label.text() == "<b>bus</b>"


def _count_binds(win, monkeypatch):
    calls = []
    real = win._bind_lane
    monkeypatch.setattr(win, "_bind_lane", lambda raw, audio: (calls.append(raw), real(raw, audio)))
    return calls


def test_replacing_the_links_from_all_links_binds_the_panes_once(monkeypatch):
    from swi3s_studio.session import Session
    win, _first, _second = _all_links_win()
    calls = _count_binds(win, monkeypatch)
    win.load_session(Session.from_demo(300, cold_start=True, register_map=win._rmap))
    assert calls == [win._raw_view] and not win._bottom_all
    assert len(win._raw_lanes.views) == 1


def test_removing_down_to_one_link_from_all_links_binds_the_panes_once(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win, _first, _second = _all_links_win()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    calls = _count_binds(win, monkeypatch)
    win.remove_link(1)
    assert calls == [win._raw_view] and not win._bottom_all and len(win.links) == 1


def test_entering_all_links_unseen_remembers_no_height():
    win, _first, _second = _all_links_win()                 # never shown: nothing to measure
    assert win._bottom_single_height == 0
