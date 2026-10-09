"""MainWindow's Link wiring: the per-Link attributes follow the active Link, a re-decode keeps
its Link, and opening a capture replaces the analysis. Offscreen Qt."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import pump_loads
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.link_state import LinkPanelState


def _win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    return win


def test_a_loaded_capture_is_one_active_link():
    win = _win()
    assert len(win.links) == 1
    link = win.links.active
    assert link.name == "Link 1" and link.session is win._session
    assert isinstance(link.view, LinkPanelState)
    assert link.view.starts is win._starts and win._starts        # the state is the Link's


def test_a_re_decode_reload_keeps_the_link_and_its_state():
    win = _win()
    link, state = win.links.active, win.links.active.view
    win.load_session(win._session)                    # what an SSP step / override does
    assert win.links.active is link and link.view is state


def test_opening_another_capture_replaces_the_link():
    win = _win()
    first = win.links.active
    win.load_demo()
    assert len(win.links) == 1 and win.links.active is not first
    assert win.links.active.view is not first.view


def test_per_link_attributes_read_and_write_the_active_link():
    win = _win()
    first_starts = win._starts
    other = win.links.add(object(), view=LinkPanelState(starts=[7]))
    assert win._starts is first_starts                # adding a Link does not switch
    win.links.set_active(1)
    assert win._session is other.session and win._starts == [7]
    win._meas_dirty = True
    assert other.view.meas_dirty is True
    win.links.set_active(0)
    assert win._starts is first_starts


def test_with_no_link_the_attributes_use_the_idle_state():
    win = _win()
    win._session = None
    assert len(win.links) == 0 and win._session is None
    assert win._cmd_model is win._idle_link.cmd_model     # the empty model built at startup


def _two_links(offset_ps):
    """The demo as Link 1 and the SAME decode again as Link 2, shifted by `offset_ps`.
    Sharing the session keeps the test fast and makes every rate equal by construction."""
    win = _win()
    other = win.links.add(win._session, view=LinkPanelState())
    other.offset_ps = offset_ps
    return win, other


def _fmt(seconds):
    from swi3s_studio.ui.main_window import _fmt_duration
    return _fmt_duration(seconds)


def test_a_pair_across_links_is_timed_in_global_time():
    win, _ = _two_links(offset_ps=1_000_000)          # Link 2 started 1 µs later
    s = int(win._session.commands[5]["start_sample"])
    win._bookmarks.add(s, link=0)
    win._bookmarks.add(s, link=1)                      # same sample number, 1 µs later
    win._push_bookmarks()
    delta = win._bookmark_measure_rows()[-1]
    assert delta[0] == "A1-A2" and delta[3] == _fmt(1e-6)
    # Shown, at the rates IN FORCE at that command (its region's), not the capture's final
    # ones: command 5 sits in the 2-column region, rowing at 12.288 MRows/s, where the
    # capture ends at 16 columns and 1.536. tests/test_bookmark_deltas.py has the rest.
    ui_hz, row_hz = next((u, r) for a, b, u, r in win._session.rate_regions() if a <= s < b)
    assert delta[1] == f"{1e-6 * ui_hz:,.1f}"                    # equal UI rates: shown
    assert delta[2] == f"{1e-6 * row_hz:,.1f}"                   # equal row rates: shown


def test_cross_link_rows_and_uis_are_withheld_when_the_rates_differ():
    win, other = _two_links(offset_ps=0)

    class _Slower:                                     # Link 2 at another row/UI rate
        def __init__(self, s):
            self._s = s

        def __getattr__(self, name):
            return getattr(self._s, name)
        def rate_regions(self):                        # every region at half the rate
            return [(a, b, u and u / 2, r and r / 2) for a, b, u, r in
                    self._s.rate_regions()]
    slow = _Slower(win._session)
    other.session = slow
    seg = win._session.segments[-1]                    # the 16-column region, where both
    s = int(seg["start_sample"]) + 200_000             # Links have a measured rate
    win._bookmarks.add(s, link=0)
    win._bookmarks.add(s + 1000, link=1)
    delta = win._bookmark_measure_rows()[-1]
    assert delta[1] == "—" and delta[2] == "—" and delta[3] != "—"


def test_another_links_bookmark_is_shown_at_its_instant_on_the_active_link():
    win, _ = _two_links(offset_ps=2_000_000)           # 2 µs
    bm = win._bookmarks.add(0, link=1)
    rate = win._session.sample_rate_hz
    assert win._bm_sample(bm) == round(2e-6 * rate)


def test_a_toggle_on_one_link_does_not_remove_anothers_bookmark():
    win, _ = _two_links(offset_ps=0)
    s = int(win._session.commands[5]["start_sample"])
    win._bookmarks.add(s, link=1)
    win.cursor.set_sample(s)
    win.toggle_bookmark()                              # active is Link 1 (index 0)
    assert sorted(b.link for b in win._bookmarks) == [0, 1]


def test_dragging_another_links_bookmark_keeps_it_in_its_own_samples():
    win, other = _two_links(offset_ps=4_000_000)       # 4 µs
    bm = win._bookmarks.add(0, link=1)
    rate = win._session.sample_rate_hz
    target_active = round(10e-6 * rate)                # dragged to 10 µs, in Link 1's samples
    win._on_bookmark_moved(bm.display(), target_active, final=False)
    assert bm.link == 1 and bm.sample == other.to_sample(win.links[0].to_ps(target_active))


def test_apply_workspace_restores_the_links_name_and_offset():
    from swi3s_studio.workspace import Workspace
    win = _win()
    ws = Workspace.single(source=win._session.source, name="Speaker bus", offset_ps=-5,
                          bookmarks=[{"sample": 3, "group": "A", "index": 1, "label": ""},
                                     {"sample": 4, "group": "A", "index": 2, "label": "",
                                      "link": 3}])            # a Link that is not loaded
    win.apply_workspace(ws)
    assert win.links.active.name == "Speaker bus" and win.links.active.offset_ps == -5
    assert [(b.sample, b.link) for b in win._bookmarks] == [(3, 0)]


def test_saving_writes_every_link(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.workspace import Workspace
    win, other = _two_links(offset_ps=7)
    win.links.rename(1, "Amp bus")
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    saved = Workspace.load(path)
    assert [(x.name, x.offset_ps) for x in saved.links] == [("Link 1", 0), ("Amp bus", 7)]


def test_a_multi_link_workspace_reopens_every_link(tmp_path, monkeypatch):
    """Save two Links, reopen through the real async load queue: both decode, in order,
    with their names, offsets and bookmarks, and the saved active Link on screen."""

    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 2_500
    win.links.rename(1, "Amp bus")
    win._bookmarks.add(10, link=0)
    win._bookmarks.add(20, link=1)
    win.switch_link(0)
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()

    win2 = MainWindow()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    win2.open_workspace()
    pump_loads(win2)
    assert getattr(win2, "_load_thread", None) is None, "the Links never finished loading"
    assert [(e.name, e.offset_ps) for e in win2.links] == [("Link 1", 0), ("Amp bus", 2_500)]
    assert [e.session.source for e in win2.links] == [first.source, second.source]
    assert win2.links.active_index == 0
    assert sorted((b.sample, b.link) for b in win2._bookmarks) == [(10, 0), (20, 1)]


def _saved_two_link_workspace(tmp_path, monkeypatch, offset_ps=0):
    """Two Links saved to a workspace file: Link 1 renamed "Main" with a bookmark, Link 2 at
    `offset_ps`. Returns the path, with the open dialog already answering it."""
    from PySide6.QtWidgets import QFileDialog
    win, _first, _second = _with_added_link()
    win.links.rename(0, "Main")
    win.links[1].offset_ps = offset_ps
    win._bookmarks.add(10, link=0)
    win.switch_link(0)
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    return path


def test_a_reopened_workspace_draws_each_band_at_its_saved_offset(tmp_path, monkeypatch):
    """Each band's domain is the union of the Links' spans in global time, so it depends on
    the offsets. Reopened, it must be the one the saved offsets give, not the one computed
    as each Link was added at offset 0: otherwise Link 2's band draws unshifted, "whole
    capture" uses the wrong bounds, and the first zoom makes the bands jump."""
    from swi3s_studio.ui.main_window import MainWindow
    _saved_two_link_workspace(tmp_path, monkeypatch, offset_ps=5_000_000_000)   # +5 ms
    win = MainWindow()
    win.open_workspace()
    pump_loads(win)
    assert win.links[1].offset_ps == 5_000_000_000
    reopened = [r._bounds() for r in win._ribbons[:2]]
    win.set_link_offset(1, win.links[1].offset_ps)      # the same offset, applied afresh
    assert reopened == [r._bounds() for r in win._ribbons[:2]], (
        "the reopened bands kept the domain computed at offset 0")


def test_an_offset_change_is_not_undone_by_the_next_link_activation():
    """The cursor's instant is remembered across Link activations (so a clamp does not lose
    it), keyed by Link and sample. Moving the active Link keeps both, so the memo has to be
    dropped: reused, activating the other Link put the cursor back at the old instant."""
    win, _first, _second = _with_added_link()
    a, b = win.links[0], win.links[1]
    s = min(int(a.session.last_sample()), int(b.session.last_sample())) // 2
    win._activate_link(0)
    win.cursor.set_sample(s)
    win._activate_link(1)                               # remembers (Link 2, sample, instant)
    shift = a.to_ps(s // 2) - a.to_ps(0)                # a quarter of the way: still inside
    win.set_link_offset(1, b.offset_ps + shift)
    moved = b.to_ps(win.cursor.sample)                  # the instant the cursor now shows
    win._activate_link(0)
    assert win.cursor.sample == a.to_sample(moved), (
        "activating Link 1 put the cursor back at the instant before the offset change")


@pytest.mark.parametrize("became_a_link", [False, True])
def test_a_link_whose_views_fail_to_build_still_lets_the_workspace_apply(
        tmp_path, monkeypatch, became_a_link):
    """A reopened workspace applies its saved state once every Link has settled. A Link
    whose views raised as they were built must settle too, as failed if it never became a
    Link and as loaded if it did; otherwise the rest keep default names, offsets and no
    bookmarks, with nothing saying the saved state was lost."""
    from PySide6.QtWidgets import QMessageBox

    from swi3s_studio.ui.main_window import MainWindow
    _saved_two_link_workspace(tmp_path, monkeypatch, offset_ps=2_500)
    said = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: said.append(a[2]))
    win = MainWindow()
    build = win.load_session

    def failing(session, *a, add_link=False, **k):
        if add_link and became_a_link:
            build(session, *a, add_link=add_link, **k)
        if add_link:
            raise RuntimeError("view build failed")
        return build(session, *a, add_link=add_link, **k)
    monkeypatch.setattr(win, "load_session", failing)
    win.open_workspace()
    pump_loads(win)
    assert said and "view build failed" in said[0]
    assert win.links[0].name == "Main", "the saved state was never applied"
    assert sorted((b.sample, b.link) for b in win._bookmarks) == [(10, 0)]
    if became_a_link:
        assert len(win.links) == 2 and win.links[1].offset_ps == 2_500
    else:
        assert len(win.links) == 1


# ---- phase 3: a second Link, switching, background re-decodes, the load queue ----

def _flow_control(win):
    """A genuinely different second Link: the PHY2 flow-control demo."""
    from swi3s_studio.session import Session
    return Session.from_demo(300, cold_start=True, phy=2, variant="flow_control",
                             register_map=win._rmap)


def _with_added_link():
    win = _win()
    first = win._session
    second = _flow_control(win)
    win.load_session(second, add_link=True)
    return win, first, second


def test_a_link_added_while_all_links_is_on_joins_the_table():
    win, _first, _second = _with_added_link()
    win.set_commands_scope_all(True)
    win.load_session(_flow_control(win), add_link=True)       # any way of adding a Link
    model = win._all_model
    assert win._cmd_all and win._cmd_proxy.sourceModel() is model
    assert {model.link_at(r) for r in range(model.rowCount())} == {0, 1, 2}


def test_add_link_appends_switches_to_it_and_shows_the_switcher():
    win, first, second = _with_added_link()
    assert [e.session for e in win.links] == [first, second]
    assert win.links.active_index == 1 and win._session is second
    assert not win._link_combo.isHidden()
    assert [win._link_combo.itemText(i) for i in range(win._link_combo.count())] == \
        ["Link 1", "Link 2", "All Links"]
    assert win._link_combo.currentText() == "Link 2"
    assert [a.text() for a in win._link_actions] == ["Link 1", "Link 2"]
    assert win._cmd_proxy.sourceModel() is win.links[1].view.cmd_model
    win._mode_mgr.switch_to("Analysis")
    win._update_title()
    assert "Link 2:" in win.windowTitle()


def test_a_single_link_keeps_the_switcher_hidden():
    win = _win()
    assert win._link_combo.isHidden()
    assert [a.text() for a in win._link_actions] == ["Link 1"]


def test_switching_rebinds_the_panes_without_rebuilding_the_links_state():
    win, first, second = _with_added_link()
    state1 = win.links[0].view
    model1 = state1.cmd_model
    win.switch_link(0)
    assert win._session is first and win._cmd_proxy.sourceModel() is model1
    assert win.links[0].view is state1 and state1.cmd_model is model1   # nothing rebuilt
    assert win._link_combo.currentIndex() == 0 and win._link_actions[0].isChecked()


def test_switching_keeps_the_instant_the_cursor_marks():
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 3_000_000                 # Link 2's sample 0 is 3 µs in
    s2 = int(second.commands[20]["start_sample"])
    win.cursor.set_sample(s2)
    t = win.links[1].to_ps(s2)
    win.switch_link(0)
    assert win.cursor.sample == win.links[0].to_sample(t)


def test_the_cursor_is_clamped_into_the_new_links_capture():
    win, first, second = _with_added_link()
    win.links[1].offset_ps = -10**15                   # Link 2 lies far before Link 1
    win.cursor.set_sample(0)
    win.switch_link(0)
    assert win.cursor.sample == 0


def test_each_link_keeps_its_own_command_filter():
    win, first, second = _with_added_link()
    win.switch_link(0)
    win._errors_only_act.setChecked(True)
    win._dev_menu._actions[3].setChecked(True)
    win.switch_link(1)
    assert not win._errors_only_act.isChecked()        # Link 2 was never filtered
    assert not any(a.isChecked() for a in win._dev_menu._actions)
    win.switch_link(0)
    assert win._errors_only_act.isChecked() and win._dev_menu._actions[3].isChecked()
    assert win._cmd_proxy.describe_active()             # and the proxy really filters


def test_statistics_rows_follow_the_link():
    win, first, second = _with_added_link()
    win._refresh_measurements()                         # built for Link 2
    rows2 = win.links[1].view.meas_rows
    win.switch_link(0)
    win._refresh_measurements()
    rows1 = win.links[0].view.meas_rows
    assert rows1 is not None and rows1 != rows2
    shown = []
    win._meas_view.set_rows = shown.append              # observe what the pane is given
    win.switch_link(1)
    assert shown and shown[-1] is rows2                 # the cached rows, not a rebuild


def test_a_background_re_decode_refreshes_its_link_and_leaves_the_panes():
    win, first, second = _with_added_link()            # Link 2 on screen
    old_state_model = win.links[0].view.cmd_model
    shown_model = win._cmd_proxy.sourceModel()
    win.load_session(first)                             # Link 1 re-decoded behind the scenes
    assert win.links.active_index == 1 and win._cmd_proxy.sourceModel() is shown_model
    assert win.links[0].view.cmd_model is not old_state_model
    assert len(win.links) == 2


def test_opening_a_capture_still_replaces_every_link():
    win, first, second = _with_added_link()
    win._bookmarks.add(5, link=1)
    win.load_demo()
    assert len(win.links) == 1 and win._session not in (first, second)
    assert len(win._bookmarks) == 0 and win._link_combo.isHidden()


def test_rename_link_updates_every_place_it_is_shown(monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    win, first, second = _with_added_link()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("  Amp bus ", True))
    win.rename_link()
    assert win.links[1].name == "Amp bus"
    assert win._link_combo.itemText(1) == "Amp bus"
    assert win._link_actions[1].text() == "Amp bus"


def test_add_link_is_open_capture_on_add(monkeypatch):
    win = _win()
    seen = []
    monkeypatch.setattr(win, "open_capture", lambda **k: seen.append(k))
    win.add_link()
    assert seen and seen[0]["prefer_add"] is True and "caption" in seen[0]


def test_add_link_with_nothing_loaded_is_a_plain_open(monkeypatch):
    win = _win()
    win._session = None
    seen = []
    monkeypatch.setattr(win, "open_capture", lambda **k: seen.append(k))
    win.add_link()
    assert seen and seen[0]["prefer_add"] is False


def test_the_load_queue_supersedes_only_what_a_request_makes_pointless():
    """With a decode in flight: an added Link is never dropped by a re-decode, a re-decode
    replaces only an earlier re-decode of the SAME session, and an open replaces all."""
    win, first, second = _with_added_link()
    win._load_thread = object()                         # pretend a decode is running
    try:
        f = [lambda **kw: None for _ in range(6)]
        win._load_async(f[0], kind="add")
        win._load_async(f[1], kind="redecode")          # re-decode of Link 2 (active)
        win.links.set_active(0)
        win._load_async(f[2], kind="redecode")          # re-decode of Link 1
        win._load_async(f[3], kind="redecode")          # supersedes f[2] only
        assert [r[0] for r in win._load_pending] == [f[0], f[1], f[3]]
        win._load_async(f[4])                           # an open: supersedes everything
        assert [r[0] for r in win._load_pending] == [f[4]]
    finally:
        win._load_thread = None
        win._load_pending = []


def test_a_switch_re_measures_and_refits_nothing(monkeypatch):
    """What makes a switch cheap, pinned as COUNTS (a wall clock loose enough for a shared
    runner could not tell these apart): the Timing pane's whole-capture measurement and the
    command table's fit-to-content are done once per decode and reused per Link."""
    from swi3s_studio.ui import command_table, eye_view
    win, first, second = _with_added_link()
    for i in (0, 1):                                    # each Link shown and measured once
        win.switch_link(i)
        win._eye_view._render()
    calls = {"measure": 0, "fit": 0}
    real_measure = eye_view.measure_bus_timing
    real_fit = command_table.CommandTableView.fit_columns

    def measure(*a, **k):
        calls["measure"] += 1
        return real_measure(*a, **k)

    def fit(self):
        calls["fit"] += 1
        return real_fit(self)
    monkeypatch.setattr(eye_view, "measure_bus_timing", measure)
    monkeypatch.setattr(command_table.CommandTableView, "fit_columns", fit)
    for i in (0, 1, 0, 1):
        win.switch_link(i)
        win._eye_view._render()                         # as a visible Timing pane would
    assert calls == {"measure": 0, "fit": 0}
    win.load_session(win._session)                      # a re-decode IS a new measurement
    win._eye_view._render()
    assert calls == {"measure": 1, "fit": 1}


def test_each_link_keeps_the_column_widths_the_user_set():
    win, first, second = _with_added_link()
    win.switch_link(0)
    win._cmd_view.setColumnWidth(0, 321)
    win.switch_link(1)
    win.switch_link(0)
    assert win._cmd_view.columnWidth(0) == 321


# ---- phase 4: the stacked timeline and the All Links Commands scope ----

def test_the_timeline_stacks_a_band_per_link_on_one_time_scale():
    win, first, second = _with_added_link()
    assert len(win._ribbons) == 2
    assert all(not lbl.isHidden() for lbl in win._ribbon_labels)
    assert "<b>" in win._ribbon_labels[1].text()           # the Link on screen is marked
    assert all(r.maximumHeight() > 92 for r in win._ribbons)   # resizable in a stack
    # Each band draws its OWN Link, and both show the same instants.
    assert win._ribbons[0]._commands is first.commands
    assert win._ribbons[1]._commands is second.commands
    win.links[1].offset_ps = 5_000_000
    win._update_timeline_domain()
    win._timeline._set_view(1000.0, 400_000.0)             # the user zooms the active band
    a, b = win._ribbons
    for sample in (1000, 200_000):
        t = win.links[1].to_ps(sample)
        x_b = b._x_for(sample)
        x_a = a._x_for(win.links[0].to_sample(t))
        assert abs(x_a - x_b) < 1.0


def test_one_link_keeps_the_single_fixed_height_ribbon():
    win = _win()
    assert len(win._ribbons) == 1 and win._ribbon_labels[0].isHidden()
    assert win._ribbons[0].maximumHeight() == 92


def test_the_cursor_is_drawn_on_every_band():
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 2_000_000
    s = int(second.commands[10]["start_sample"])
    win.cursor.set_sample(s)
    assert win._ribbons[1]._cursor == s
    assert win._ribbons[0]._cursor == win.links.convert(s, 1, 0)


def test_a_click_on_another_links_band_makes_it_active_and_leaves_the_groups():
    """A band click makes that band's Link the active one, so the next bookmark goes on the
    band that was clicked, and puts the cursor on the clicked sample there. What each pane
    GROUP shows is still its picker's business."""
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 1_500_000
    s = int(first.commands[10]["start_sample"])
    win._on_ribbon_seek(win._ribbons[0], s)
    assert win.links.active_index == 0
    assert win.cursor.sample == s
    assert win._pane_link == {"commands": 1, "right": 1, "grid": 1, "bottom": 1}


def test_all_links_merges_every_command_in_global_time():
    win, first, second = _with_added_link()
    win.links[1].offset_ps = -1_000_000
    win.set_commands_scope_all(True)
    model = win._cmd_proxy.sourceModel()
    assert model is win._all_model
    assert model.rowCount() == win.links[0].view.cmd_model.rowCount() + \
        win.links[1].view.cmd_model.rowCount()
    assert model.headerData(0, Qt.Horizontal, Qt.DisplayRole) == "Link"
    assert {model.data(model.index(r, 0)) for r in range(model.rowCount())} == \
        {"Link 1", "Link 2"}
    pos = model.positions_ps
    assert all(pos[i] <= pos[i + 1] for i in range(len(pos) - 1))
    assert win._link_combo.currentText() == "All Links" and win._all_links_action.isChecked()


def test_selecting_an_all_links_row_shows_its_link_and_keeps_the_scope():
    win, first, second = _with_added_link()
    win.set_commands_scope_all(True)
    model = win._all_model
    row = next(r for r in range(model.rowCount()) if model.link_at(r) == 0)
    cmd = model.command_at(row)
    win._cmd_view.setCurrentIndex(win._cmd_proxy.mapFromSource(model.index(row, 1)))
    win._on_command_selected()
    assert win.links.active_index == 0
    assert win.cursor.sample == first.command_cursor_sample(cmd)
    assert win._cmd_all and win._cmd_proxy.sourceModel() is win._all_model


def test_the_all_links_filter_applies_across_links_and_leaving_restores_the_links_own():
    win, first, second = _with_added_link()
    win._errors_only_act.setChecked(False)
    win.set_commands_scope_all(True)
    win._dev_menu._actions[0].setChecked(True)             # Device 0, on every Link
    model = win._all_model
    shown = {model.link_at(win._cmd_proxy.mapToSource(win._cmd_proxy.index(r, 0)).row())
             for r in range(win._cmd_proxy.rowCount())}
    assert shown == {0, 1}
    win.set_commands_scope_all(False)
    assert win._cmd_proxy.sourceModel() is win._cmd_model
    assert not any(a.isChecked() for a in win._dev_menu._actions)   # Link 2 unfiltered


def test_opening_a_capture_leaves_all_links():
    win, first, second = _with_added_link()
    win.set_commands_scope_all(True)
    win.load_demo()
    assert not win._cmd_all and win._cmd_proxy.sourceModel() is win._cmd_model
    assert win._link_combo.isHidden()


def test_the_link_picker_is_wide_enough_for_its_names():
    win, first, second = _with_added_link()
    fm = win._link_combo.fontMetrics()
    need = max(fm.horizontalAdvance(t) for t in ("Link 1", "Link 2", "All Links"))
    assert win._link_combo.minimumWidth() > need + 30
    assert win._link_combo.view().minimumWidth() > need + 30


def test_all_links_is_sorted_by_global_time_and_leaving_restores_the_sort():
    win, first, second = _with_added_link()
    hdr = win._cmd_view.horizontalHeader()
    before = (hdr.sortIndicatorSection(), hdr.sortIndicatorOrder())
    win.links[1].offset_ps = 3_000_000
    win.set_commands_scope_all(True)
    model, proxy = win._all_model, win._cmd_proxy
    times = [model.data(model.index(proxy.mapToSource(proxy.index(r, 0)).row(), 2),
                        Qt.UserRole + 7) for r in range(proxy.rowCount())]
    assert times == sorted(times)
    assert model.headerData(hdr.sortIndicatorSection(), Qt.Horizontal) == "Time (µs)"
    win.set_commands_scope_all(False)
    assert (hdr.sortIndicatorSection(), hdr.sortIndicatorOrder()) == before


# ---- independent pane groups: Commands, right (Registers/Statistics), bottom ----

def _record_register_reads(win):
    """Which Link's Session the Registers pane last read, and at which sample."""
    seen = []
    for i, link in enumerate(win.links):
        real = link.session.register_files_at

        def spy(sample, i=i, real=real):
            seen.append((i, int(sample)))
            return real(sample)
        link.session.register_files_at = spy
    return seen


def test_every_group_dock_carries_a_picker_once_there_are_two_links():
    win = _win()
    assert not win._pane_pickers["right"]            # one Link: no pickers built at all
    win.load_session(_flow_control(win), add_link=True)
    for group, docks in win._group_docks.items():
        assert len(win._pane_pickers[group]) == len(docks)
        for picker in win._pane_pickers[group]:
            assert not picker.isHidden()
            # the bottom group also offers All Links (tests/test_bottom_all_links.py)
            extra = ["All Links"] if group == "bottom" else []
            assert [picker.itemText(i) for i in range(picker.count())] == \
                ["Link 1", "Link 2"] + extra
            assert picker.currentIndex() == 1


def test_the_right_group_can_show_another_link_than_the_bottom():
    win, first, second = _with_added_link()           # every group on Link 2
    bottom_store = win._audio_view._store
    commands_model = win._cmd_proxy.sourceModel()
    win.set_group_link("right", 0)
    assert win._pane_link == {"commands": 1, "right": 0, "grid": 1, "bottom": 1}
    assert win._audio_view._store is bottom_store      # the bottom group did not move
    assert win._cmd_proxy.sourceModel() is commands_model
    assert all(p.currentIndex() == 0 for p in win._pane_pickers["right"])
    assert all(p.currentIndex() == 1 for p in win._pane_pickers["bottom"])
    assert win.links.active_index == 0                 # the Link just asked for


def test_the_commands_picker_moves_only_commands():
    win, first, second = _with_added_link()
    win._on_link_combo(0)
    assert win._pane_link == {"commands": 0, "right": 1, "grid": 1, "bottom": 1}
    assert win._cmd_proxy.sourceModel() is win.links[0].view.cmd_model


def test_a_cursor_move_updates_each_group_at_the_same_instant_on_its_own_link():
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 4_000_000
    win.set_group_link("right", 0)
    win._reg_dock.show()
    seen = _record_register_reads(win)
    s = int(second.commands[30]["start_sample"])
    win.set_group_link("bottom", 1)                    # active Link is Link 2 now
    win.cursor.set_sample(s)
    assert seen and seen[-1] == (0, win.links.convert(s, 1, 0))


def test_touching_a_pane_makes_its_groups_link_the_active_one():
    win, first, second = _with_added_link()
    win.set_group_link("right", 0)                     # active: Link 1
    before = win.links[0].to_ps(win.cursor.sample)
    win._on_pane_touched(win._grid_view)               # a click in the bottom group
    assert win.links.active_index == 1
    assert abs(win.links[1].to_ps(win.cursor.sample) - before) < 5_000   # same instant
    win._on_pane_touched(win._reg_view)
    assert win.links.active_index == 0


def test_a_panes_menu_acts_on_the_link_its_group_shows():
    win, first, second = _with_added_link()
    win.set_group_link("right", 0)                     # active: Link 1, bottom on Link 2
    ran = []
    win._for_group("bottom", lambda: ran.append(win._session))()
    assert ran == [second] and win.links.active_index == 1


def test_bottom_panes_draw_their_own_links_bookmarks_and_take_seeks_in_it():
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 2_000_000
    win.set_group_link("bottom", 0)
    win.set_group_link("right", 1)                     # active: Link 2
    on_2 = win._bookmarks.add(1000, link=1)
    on_1 = win._bookmarks.add(3000, link=0)
    marks = []
    win._raw_view.set_bookmarks = marks.append
    win._push_bookmarks()
    assert marks[-1] == [(3000, on_1.display())]       # only Link 1's, the bottom group's
    assert on_2.display() not in [m[1] for m in marks[-1]]
    win._audio_view.seeked.emit(50_000)                # a waveform click, Link 1 samples
    assert win.cursor.sample == win.links.convert(50_000, 0, 1)


def test_a_drag_in_a_bottom_pane_counts_in_that_panes_link():
    """Audio and Capture count in the bottom group's Link. A drag there used to be read as
    the ACTIVE Link's samples, so with the bottom group on another Link the bookmark
    landed somewhere else."""
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 2_000_000
    win.set_group_link("bottom", 0)
    win.set_group_link("right", 1)                     # active: Link 2, bottom on Link 1
    bm = win._bookmarks.add(3000, link=0)
    win._raw_view.bookmarkMoved.emit(bm.display(), 7000, False)   # a drag in Capture
    assert bm.sample == 7000 and bm.link == 0


def test_the_links_menu_still_shows_a_link_everywhere():
    win, first, second = _with_added_link()
    win.set_group_link("right", 0)
    win.switch_link(0)
    assert win._pane_link == {"commands": 0, "right": 0, "grid": 0, "bottom": 0}


def test_a_re_decode_rebinds_only_the_groups_showing_that_link():
    win, first, second = _with_added_link()
    win.set_group_link("right", 0)                     # right on Link 1, the rest on Link 2
    bottom_store = win._audio_view._store
    seen = _record_register_reads(win)
    win.load_session(first)                            # Link 1 re-decoded
    assert any(i == 0 for i, _ in seen)                # the right group re-read Link 1
    assert win._audio_view._store is bottom_store      # the bottom group was left alone


def test_the_pane_touch_filter_exists_only_while_some_window_has_two_links():
    from swi3s_studio.ui.main_window import _PaneTouchFilter
    win, first, second = _with_added_link()
    assert _PaneTouchFilter._installed is not None
    win.load_demo()                                    # back to one Link
    assert id(win) not in _PaneTouchFilter._users


# ---- phase 4: offsets, Align on bookmark pair, other-Link marks, saved layout ----

def test_setting_an_offset_moves_the_links_bookmarks_and_all_links_times():
    win, first, second = _with_added_link()
    bm = win._bookmarks.add(1000, link=1)
    before = win._bm_ps(bm)
    win.set_commands_scope_all(True)
    model = win._all_model
    row = next(r for r in range(model.rowCount()) if model.link_at(r) == 1)
    t0 = model.data(model.index(row, model.TIME_COLUMN), Qt.UserRole + 7)
    win.set_link_offset(1, 7_000_000)                  # +7 µs
    assert win._bm_ps(bm) - before == 7_000_000        # it moved with its Link
    assert bm.sample == 1000                           # ... in its own samples, unchanged
    model = win._all_model                             # rebuilt
    row = next(r for r in range(model.rowCount()) if model.link_at(r) == 1)
    assert model.data(model.index(row, model.TIME_COLUMN), Qt.UserRole + 7) == \
        pytest.approx(t0 + 7.0)


def test_align_on_a_bookmark_pair_makes_its_members_coincide():
    win, first, second = _with_added_link()
    a1 = win._bookmarks.add(int(first.commands[8]["start_sample"]), link=0)
    a2 = win._bookmarks.add(int(second.commands[3]["start_sample"]), link=1)
    win.align_on_bookmark_pair("A")
    assert win._bm_ps(a1) == win._bm_ps(a2)
    delta = win._bookmark_measure_rows()[-1]
    assert delta[0] == "A1-A2" and delta[3] in ("0.0 ns", "-0.0 ns")


def test_align_without_a_cross_link_pair_explains_what_to_do(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win, first, second = _with_added_link()
    said = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: said.append(a[2]))
    win._bookmarks.add(10, link=0)
    win._bookmarks.add(20, link=0)                     # a pair, but on one Link
    win.align_on_bookmark_pair()
    assert said and "across two Links" in said[0]


def test_a_bookmark_is_drawn_only_on_its_own_links_band():
    win, first, second = _with_added_link()
    bm = win._bookmarks.add(5000, link=1)
    win._push_bookmarks()
    assert win._ribbons[1]._bookmarks == [(5000, "A1")]
    assert win._ribbons[0]._bookmarks == []
    win._on_ribbon_bookmark_moved(win._ribbons[1], "A1", 9000, False)    # dragged on it
    assert bm.sample == 9000


def test_placing_a_bookmark_on_each_link_by_clicking_its_band_and_timing_them():
    """The workflow: click Link 1's band, Ctrl+B; click Link 2's band, Ctrl+B. Each
    bookmark is on the band clicked, and drawn only there; the Bookmarks pane names each
    one's Link and times the pair across the two buses."""
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 1_000_000                 # Link 2 starts 1 µs later
    s1 = int(first.commands[10]["start_sample"])
    s2 = int(second.commands[20]["start_sample"])
    win._on_ribbon_seek(win._ribbons[0], s1)
    win.toggle_bookmark()
    win._on_ribbon_seek(win._ribbons[1], s2)
    win.toggle_bookmark()
    a1, a2 = sorted(win._bookmarks, key=lambda b: b.index)
    assert (a1.link, a1.sample, a2.link, a2.sample) == (0, s1, 1, s2)
    assert win._ribbons[0]._bookmarks == [(s1, "A1")]
    assert win._ribbons[1]._bookmarks == [(s2, "A2")]
    rows = win._bookmark_measure_rows()
    assert [r[5] for r in rows] == ["Link 1", "Link 2", "Link 1 → Link 2"]
    assert rows[-1][0] == "A1-A2"
    dt = (win.links[1].to_ps(s2) - win.links[0].to_ps(s1)) / 1e12
    from swi3s_studio.ui.main_window import _fmt_duration
    assert rows[-1][3] == _fmt_duration(dt)
    assert not win._pair_view.isColumnHidden(1)          # the Link column is shown
    assert win._pair_view.item(2, 1).text() == "Link 1 → Link 2"


def test_the_link_column_is_hidden_with_one_link():
    win = _win()
    win._bookmarks.add(100)
    win._push_bookmarks()
    assert win._pair_view.isColumnHidden(1)


def test_a_bookmarks_row_makes_its_link_active_and_seeks_it():
    win, first, second = _with_added_link()
    s = int(first.commands[12]["start_sample"])
    win._bookmarks.add(s, link=0)
    win._push_bookmarks()                                # Link 2 active
    win._pair_view._on_cell(0, 0)                        # the A1 row, clicked
    assert win.links.active_index == 0 and win.cursor.sample == s


def test_next_bookmark_stays_on_the_active_link():
    win, first, second = _with_added_link()             # Link 2 active
    win.cursor.set_sample(0)
    win._bookmarks.add(500, link=0)                      # only Link 1 draws this one
    win._bookmarks.add(900, link=1)
    win.next_bookmark()
    assert win.cursor.sample == 900


def test_the_multi_link_layout_is_saved_and_restored(tmp_path, monkeypatch):

    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.workspace import Workspace
    win, first, second = _with_added_link()
    win.set_group_link("right", 0)
    win.set_group_link("grid", 0)
    win.set_commands_scope_all(True)
    win._on_pane_touched(win._raw_view)                # active: the bottom group's Link 2
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    view = Workspace.load(path).view
    assert view["pane_links"] == {"commands": 1, "right": 0, "grid": 0, "bottom": 1}
    assert view["commands_all"] is True and view["timeline_height"] > 0

    win2 = MainWindow()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    win2.open_workspace()
    pump_loads(win2)
    assert win2._pane_link == {"commands": 1, "right": 0, "grid": 0, "bottom": 1}
    assert win2._cmd_all and win2.links.active_index == 1


def test_a_single_link_workspace_saves_no_multi_link_layout(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.workspace import Workspace
    win = _win()
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    assert "pane_links" not in Workspace.load(path).view


def test_links_at_different_row_rates_work_end_to_end():
    """Equal row rates are the expected case, not a requirement: with a PHY3 Link (3072 kHz
    rows) beside a PHY2 one (1536 kHz), everything works, and only row/UI deltas across the
    two are withheld. (PHY1 was used here once, but PHY1 and PHY2 run the SAME row rate —
    the test only believed otherwise because it compared measured rates for exact float
    equality, the very bug the tolerance fixes.)"""
    from swi3s_studio.session import Session
    win = _win()
    phy3 = Session.from_demo(300, cold_start=True, phy=3, register_map=win._rmap)
    r2, r3 = win._session.row_rate_khz, phy3.row_rate_khz
    assert abs(r3 - r2) / r2 > 0.5                                   # the premise, for real
    win.load_session(phy3, add_link=True)
    win._bookmarks.add(int(win.links[0].session.commands[6]["start_sample"]), link=0)
    win._bookmarks.add(int(phy3.commands[6]["start_sample"]), link=1)
    delta = win._bookmark_measure_rows()[-1]
    assert delta[1] == "—" and delta[2] == "—" and delta[3] != "—"  # time only
    win.set_commands_scope_all(True)
    assert win._all_model.rowCount() == len(win.links[0].view.starts) + len(phy3.commands) \
        + len(phy3.commit_point_rows())
    win.set_group_link("bottom", 0)
    win.switch_link(1)
    assert win._session is phy3


def test_measured_rates_that_agree_to_a_few_ppm_count_as_the_same_rate():
    """Two measured row rates never agree to the last digit. PHY2 in 16 columns and PHY1
    in 4 run the same row rate (1.536 MRows/s, measured a few ppm apart) at different UI
    rates (24.576 and 6.144 MHz): ΔRows is shown, ΔUI not. Both bookmarks sit in those
    regions (not at sample 0, inside the bring-up, where comparing the captures' FINAL rates
    would pass for the wrong reason)."""
    from swi3s_studio.session import Session
    win = _win()
    phy1 = Session.from_demo(300, cold_start=True, phy=1, register_map=win._rmap)
    win.load_session(phy1, add_link=True)
    a2, b2, _u2, r2 = win.links[0].session.rate_regions()[-1]     # PHY2, 16 columns
    a1, b1, _u1, r1 = phy1.rate_regions()[-1]                     # PHY1, 4 columns
    assert r1 != r2 and abs(r1 - r2) / r2 < 1e-5                    # measured, not equal
    lo, hi = max(a1, a2) + 1000, min(b1, b2) - 1000
    assert lo < hi
    win._bookmarks.add(lo, link=0)
    win._bookmarks.add(hi, link=1)
    dui, drows, dt = win._bookmark_measure_rows()[-1][1:4]
    assert drows != "—" and dui == "—" and dt != "—"


# ---- phase 5: Remove Link, per-Link export names ----

def _three_links():
    from swi3s_studio.session import Session
    win, first, second = _with_added_link()
    third = Session.from_demo(300, cold_start=True, phy=1, register_map=win._rmap)
    win.load_session(third, add_link=True)
    return win, first, second, third


def test_removing_a_link_renumbers_what_refers_to_links_by_index():
    win, first, second, third = _three_links()
    win.links.rename(2, "PHY1 bus")
    b0 = win._bookmarks.add(100, link=0)
    win._bookmarks.add(200, link=1)                    # goes with Link 2
    b2 = win._bookmarks.add(300, link=2)
    win.set_group_link("right", 1)
    win.set_group_link("bottom", 2)
    win.set_group_link("commands", 0)
    win.remove_link(1, confirm=False)
    assert [e.session for e in win.links] == [first, third]
    assert [(b.sample, b.link) for b in win._bookmarks] == [(100, 0), (300, 1)]
    assert b0.link == 0 and b2.link == 1
    # The right group showed the removed Link: it moves to the active one. The others keep
    # their Link, renumbered.
    assert win._pane_link["bottom"] == 1 and win._pane_link["commands"] == 0
    assert win._pane_link["right"] == win.links.active_index
    assert len(win._ribbons) == 2
    assert win._ribbons[1]._commands is third.commands     # the band follows its Link
    assert [p.itemText(i) for p in win._pane_pickers["right"][:1]
            for i in range(p.count())] == ["Link 1", "PHY1 bus"]


def test_removing_down_to_one_link_restores_the_single_link_window():
    win, first, second = _with_added_link()
    win.set_commands_scope_all(True)
    win.remove_link(0, confirm=False)
    assert len(win.links) == 1 and win._session is second
    assert not win._cmd_all and win._link_combo.isHidden()
    assert win._ribbon_labels[0].isHidden() and win._ribbons[0].maximumHeight() == 92
    assert all(p.isHidden() for ps in win._pane_pickers.values() for p in ps)


def test_the_last_link_cannot_be_removed():
    win = _win()
    win.remove_link(confirm=False)
    assert len(win.links) == 1


def test_remove_link_asks_first_and_names_the_bookmarks_it_takes(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win, first, second = _with_added_link()
    win._bookmarks.add(10, link=1)
    win._bookmarks.add(20, link=1)
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    win.remove_link(1)
    assert asked and "Link 2" in asked[0] and "2 bookmark(s)" in asked[0]
    assert len(win.links) == 2                          # declined: nothing removed


def test_exports_are_named_for_their_link_once_there_are_two():
    win = _win()
    assert win._link_stem() == ""
    win.load_session(_flow_control(win), add_link=True)
    win.links.rename(1, "Amp bus / L")
    assert win._link_stem() == "_Amp_bus_L"


def test_removing_a_link_while_commands_shows_all_links_keeps_the_merge_consistent():
    win, first, second, third = _three_links()
    win.set_commands_scope_all(True)
    win.remove_link(0, confirm=False)
    assert win._cmd_all and len(win.links) == 2
    model = win._all_model
    assert {model.link_at(r) for r in range(model.rowCount())} == {0, 1}
    assert model.rowCount() == sum(e.view.cmd_model.rowCount() for e in win.links)


# ---- removal, re-decode and layout edge cases ----

def test_a_re_decode_of_a_removed_link_is_discarded_not_loaded_as_a_new_capture():
    win, first, second = _with_added_link()
    win._load_pending = [(lambda **kw: second, None, "redecode", second, None)]
    win.remove_link(1, confirm=False)
    assert win._load_pending == []                       # its queued re-decode is purged
    win._load_kind, win._load_after = "redecode", None   # one already running lands now
    win._on_load_done(second, None, {})
    assert [e.session for e in win.links] == [first]     # not replaced by the removed Link


def test_a_multi_link_workspace_with_a_failing_middle_link_maps_the_rest_correctly(
        tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.workspace import LinkSpec, Workspace
    win, first, second = _with_added_link()
    bad = {"type": "sal", "path": str(tmp_path / "missing.sal"),
           "clock_channel": 0, "data_channel": 1}
    ws = Workspace(links=[LinkSpec(source=first.source, name="A"),
                          LinkSpec(source=bad, name="B", offset_ps=5),
                          LinkSpec(source=second.source, name="C", offset_ps=9)],
                   active_link=2,
                   bookmarks=[{"sample": 1, "group": "A", "index": 1, "label": "", "link": 0},
                              {"sample": 2, "group": "A", "index": 2, "label": "", "link": 1},
                              {"sample": 3, "group": "B", "index": 1, "label": "", "link": 2}])
    path = str(tmp_path / "ws.json")
    ws.save(path)
    win2 = MainWindow()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(win2, "_ask_missing_capture", lambda *a: "skip")   # B's file is gone
    win2.open_workspace()
    pump_loads(win2)
    assert [(e.name, e.offset_ps) for e in win2.links] == [("A", 0), ("C", 9)]
    # B's bookmark is gone; C's moved to where C landed (index 1), not onto a neighbour.
    assert sorted((b.sample, b.link) for b in win2._bookmarks) == [(1, 0), (3, 1)]
    assert win2.links.active_index == 1                  # the saved active Link, C


def test_a_workspace_whose_first_link_fails_adds_nothing_to_the_open_analysis(
        tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from swi3s_studio.workspace import LinkSpec, Workspace
    win = _win()
    before = win._session
    bad = {"type": "sal", "path": str(tmp_path / "missing.sal"),
           "clock_channel": 0, "data_channel": 1}
    path = str(tmp_path / "ws.json")
    Workspace(links=[LinkSpec(source=bad), LinkSpec(source=before.source)]).save(path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(win, "_ask_missing_capture", lambda *a: "skip")   # its file is gone
    win.open_workspace()
    pump_loads(win)
    assert len(win.links) == 1 and win._session is before


def test_a_re_decode_keeps_bookmarks_placed_while_it_ran():
    win, first, second = _with_added_link()
    win._redecode_preserving(lambda **kw: win._session)
    added = win._bookmarks.add(123, link=0)              # placed while the decode runs
    pump_loads(win)
    assert added in list(win._bookmarks)


def test_a_click_in_a_floating_dock_makes_its_groups_link_active():
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFocusEvent

    from swi3s_studio.ui.main_window import _PaneTouchFilter
    win, first, second = _with_added_link()
    win.set_group_link("right", 0)
    win.set_group_link("bottom", 1)                      # active: Link 2
    win._reg_dock.setFloating(True)
    assert win._reg_view.window() is not win             # the premise: its own window
    _PaneTouchFilter._installed.eventFilter(win._reg_view, QFocusEvent(QEvent.FocusIn))
    assert win.links.active_index == 0


def test_a_bookmarks_row_seeks_its_instant_on_the_link_active_when_clicked():
    win, first, second = _with_added_link()
    win.links[1].offset_ps = 2_000_000
    bm = win._bookmarks.add(int(first.commands[12]["start_sample"]), link=0)
    rows = win._bookmark_measure_rows()                  # built with Link 2 active
    win._activate_link(0)                                # ... then another pane touched
    win._on_pair_seek(rows[0][4])
    assert win.cursor.sample == bm.sample


def test_a_digital_csv_with_more_than_two_channels_offers_each_clock_with_a_data_line(
        tmp_path, monkeypatch):
    """Four channels, two busy clocks and two quiet lines: the dialog offers the busiest as
    the clock and a quiet line as its data (not the second clock), with the counts shown,
    and + Add a Link pairs the other two the same way."""
    from conftest import open_via_dialog
    path = tmp_path / "four.csv"
    lines = ["Time [s],CLK1,CLK2,D1,D2"]
    for i in range(400):                                 # two busy clocks, two quiet lines
        lines.append(f"{i * 1e-6:.9f},{i % 2},{(i // 1) % 2},{(i // 50) % 2},{(i // 80) % 2}")
    path.write_text("\n".join(lines) + "\n")
    win = _win()
    seen = []

    def look(dlg):
        dlg._add_row_from_free()
        seen.extend(r.pair() for r in dlg.rows())
        seen.append([r.clock.currentText() for r in dlg.rows()])
        return False                                     # cancel: nothing is opened
    open_via_dialog(monkeypatch, [str(path)], look)
    win.open_capture()
    names = ["CLK1", "CLK2", "D1", "D2"]
    (c1, d1), (c2, d2), texts = seen
    assert {names[c1], names[c2]} == {"CLK1", "CLK2"} and {names[d1], names[d2]} == {"D1", "D2"}
    assert all("edges)" in t for t in texts)


def test_renaming_a_link_renames_it_in_all_links():
    from PySide6.QtWidgets import QInputDialog
    win, first, second = _with_added_link()
    win.set_commands_scope_all(True)
    QInputDialog_getText = QInputDialog.getText
    try:
        QInputDialog.getText = staticmethod(lambda *a, **k: ("Amp", True))
        win.rename_link()                                # active: Link 2
    finally:
        QInputDialog.getText = QInputDialog_getText
    model = win._all_model
    names = {model.data(model.index(r, 0)) for r in range(model.rowCount())}
    assert names == {"Link 1", "Amp"}
    row = next(r for r in range(model.rowCount()) if model.link_at(r) == 1)
    assert "amp" in model.search_text(row)


def test_rename_renames_the_link_that_was_active_when_asked(monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    win, first, second = _with_added_link()             # active: Link 2

    def answer(*a, **k):
        win._activate_link(0)                           # focus returns to another group
        return "Renamed", True
    monkeypatch.setattr(QInputDialog, "getText", answer)
    win.rename_link()
    assert [e.name for e in win.links] == ["Link 1", "Renamed"]


def test_passing_through_a_short_link_does_not_lose_the_cursors_instant():
    win, first, second = _with_added_link()
    win.links[1].offset_ps = -10 ** 15                  # Link 2 ends long before this instant
    win._activate_link(0)
    s = int(first.commands[40]["start_sample"])
    win.cursor.set_sample(s)
    win._activate_link(1)                               # clamped into Link 2 ...
    win._activate_link(0)                               # ... and back
    assert win.cursor.sample == s


def test_sub_capture_matches_are_bookmarked_on_the_link_searched(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)    # the "found N" announcement
    win, first, second = _with_added_link()
    win._locate_link = 0                                # the search started on Link 1
    win._activate_link(1)                               # ... Link 2 is active when it lands
    win._finish_locate_subcapture("sub.sal", [{"start_sample": 77, "score": 1.0}], {})
    assert [(b.sample, b.link) for b in win._bookmarks] == [(77, 0)]


def test_all_links_keeps_its_column_widths_across_a_rebuild():
    win, first, second = _with_added_link()
    win.set_commands_scope_all(True)
    win._cmd_view.setColumnWidth(0, 211)
    win.set_link_offset(1, 3_000)                        # rebuilds the All Links model
    assert win._cmd_view.columnWidth(0) == 211


def test_a_window_destroyed_without_closing_releases_the_pane_filter():
    from swi3s_studio.ui.main_window import _PaneTouchFilter
    win, first, second = _with_added_link()
    key = id(win)
    assert key in _PaneTouchFilter._users
    win.join_worker_threads()
    win.deleteLater()
    QApplication.instance().processEvents()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert key not in _PaneTouchFilter._users


def test_a_newer_open_is_not_added_to_by_a_workspaces_other_links(tmp_path, monkeypatch):
    # The workspace's first Link decodes, a newer open is asked for meanwhile: the
    # workspace's remaining Links must not land on top of it.
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    win, _first, _second = _with_added_link()
    path = str(tmp_path / "ws.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    monkeypatch.setattr(MainWindow, "_event_loop_live", True)
    win2 = MainWindow()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    win2.open_workspace()
    win2.load_demo(phy=1)                                 # newer, while Link 1 decodes
    pump_loads(win2)
    assert len(win2.links) == 1 and win2.links[0].session.source.get("phy") == 1


def test_leaving_all_links_for_a_replace_does_no_cursor_work_on_unbound_panes(monkeypatch):
    from swi3s_studio.session import Session
    win, _first, _second = _with_added_link()
    win.set_bottom_scope_all(True)
    calls = []
    real = win._restore_cursor
    monkeypatch.setattr(win, "_restore_cursor", lambda s: (calls.append(win._bottom_all), real(s)))
    win.load_session(Session.from_demo(300, cold_start=True, register_map=win._rmap))
    assert calls == []                  # leaving All Links left the cursor to the bind
