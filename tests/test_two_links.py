"""Two Links in one capture, end to end: the multi-channel .sal export, each Link decoding
exactly as it does alone, one file-wide timeline, and File ▸ Open taking both Links from the
same file through the real async load queue. Offscreen Qt for the GUI half."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from conftest import TWO_LINK_SHIFT_SAMPLES, export_sal_links, pump_loads, two_link_captures
from PySide6.QtWidgets import QApplication

from swi3s_studio.ingest import saleae_sal
from swi3s_studio.session import Session

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def two_link_sal(tmp_path_factory):
    a, b = two_link_captures()
    path = str(tmp_path_factory.mktemp("links") / "two_links.sal")
    export_sal_links([a, b], path)
    return path, a, b


def test_each_pair_round_trips_through_one_sal(two_link_sal):
    path, a, b = two_link_sal
    for n, cap in enumerate((a, b)):
        back = saleae_sal.load_capture(path, 2 * n, 2 * n + 1)
        assert np.array_equal(back.clock_edges, cap.clock_edges)
        assert np.array_equal(back.data_edges, cap.data_edges)
        assert (back.initial_clock, back.initial_data) == (cap.initial_clock, cap.initial_data)


def test_each_link_decodes_exactly_as_it_does_alone(two_link_sal):
    """Sharing a file must change nothing about a Link's own decode."""
    path, a, b = two_link_sal
    for n, cap in enumerate((a, b)):
        from_file = Session.from_sal(path, 2 * n, 2 * n + 1)
        alone = Session(cap, source={"type": "fixture"})
        assert [c["start_sample"] for c in from_file.commands] == \
            [c["start_sample"] for c in alone.commands]
        assert from_file.column_count == alone.column_count


def test_links_from_one_file_share_its_timeline(two_link_sal):
    """Link 2 was recorded TWO_LINK_SHIFT_SAMPLES later; loaded from the same file, its
    samples must still say so — no per-pair rebase — so offset 0 is the right offset."""
    path, _a, b = two_link_sal
    link2 = Session.from_sal(path, 2, 3)
    assert int(link2.capture.clock_edges[0]) == int(b.clock_edges[0])
    assert int(link2.capture.clock_edges[0]) >= TWO_LINK_SHIFT_SAMPLES


def test_the_multi_link_export_refuses_mixed_rates(tmp_path):
    from dataclasses import replace
    a, b = two_link_captures()
    path = tmp_path / "mixed.sal"
    with pytest.raises(ValueError, match=r"one \.sal has one sample rate"):
        export_sal_links([a, replace(b, sample_rate_hz=a.sample_rate_hz // 2)], str(path))
    assert not path.exists()


def test_open_takes_both_links_from_one_sal(two_link_sal, monkeypatch):
    """Two Links from one .sal, named in the Open Capture dialog: they load aligned (one
    file is one timeline), with the names given."""
    from conftest import open_via_dialog

    from swi3s_studio.ui.main_window import MainWindow
    path, a, b = two_link_sal
    QApplication.instance() or QApplication([])
    win = MainWindow()

    def edit(dlg):
        dlg._add_row_from_free()
        (r1, r2) = dlg.rows()
        r1.clock.setCurrentIndex(0), r1.data.setCurrentIndex(1)
        r2.clock.setCurrentIndex(2), r2.data.setCurrentIndex(3)
        r2.name.setText("Speaker bus")
        r2.name.textEdited.emit("Speaker bus")
    open_via_dialog(monkeypatch, [path], edit)
    win.open_capture()
    pump_loads(win)
    assert len(win.links) == 2
    assert [(e.session.source["clock_channel"], e.session.source["data_channel"])
            for e in win.links] == [(0, 1), (2, 3)]
    assert [e.name for e in win.links] == ["Link 1", "Speaker bus"]
    assert all(e.offset_ps == 0 for e in win.links)
    assert win.links.active_index == 1                  # the last Link added is shown
    # Aligned: the same instant is the same sample number on both, since one file = one
    # timeline at one rate.
    assert win.links.convert(TWO_LINK_SHIFT_SAMPLES, 1, 0) == TWO_LINK_SHIFT_SAMPLES


def test_one_row_opens_one_link(two_link_sal, monkeypatch):
    from conftest import open_via_dialog

    from swi3s_studio.ui.main_window import MainWindow
    path, _a, _b = two_link_sal
    QApplication.instance() or QApplication([])
    win = MainWindow()
    shown = open_via_dialog(monkeypatch, [path])
    win.open_capture()
    pump_loads(win)
    assert len(win.links) == 1
    # The four-channel file holds two Links; the default pairs a clock with a data line,
    # not the two busiest (both clocks).
    (row,) = shown[0].rows()
    c, d = row.pair()
    edges = [ch.edges for ch in shown[0].probe.channels]
    assert edges[d] * 2 < edges[c]


def test_add_link_from_another_file_keeps_the_first(two_link_sal, tmp_path, monkeypatch):
    from conftest import open_via_dialog

    from swi3s_studio.ingest.sal_export import export_sal
    from swi3s_studio.ui.main_window import MainWindow
    _path, a, b = two_link_sal
    p1, p2 = str(tmp_path / "one.sal"), str(tmp_path / "two.sal")
    export_sal(a, p1)
    export_sal(b, p2)
    QApplication.instance() or QApplication([])
    win = MainWindow()
    open_via_dialog(monkeypatch, [p1])
    win.open_capture()
    pump_loads(win)
    first = win._session
    shown = open_via_dialog(monkeypatch, [p2])
    win.add_link()                                     # the dialog opens on Add
    pump_loads(win)
    assert shown[0]._add.isChecked()
    assert [e.session.source["path"] for e in win.links] == [p1, p2]
    assert [e.name for e in win.links] == ["Link 1", "Link 2"]
    assert win.links[0].session is first and win.links.active_index == 1


# The channel edge counts and the busiest-as-clock default are tested in
# tests/test_probe.py and tests/test_open_capture_dialog.py; test_one_row_opens_one_link
# above pins the clock-with-a-data-line default on this four-channel file.


def test_align_recovers_a_known_offset_between_two_files(tmp_path, monkeypatch):
    """The two-file path end to end: Link 2 was recorded TWO_LINK_SHIFT_SAMPLES after
    Link 1 but its file starts at its own sample 0. Bookmarking one instant on each and
    aligning must recover that offset, to within one sample."""
    from dataclasses import replace

    from conftest import open_via_dialog

    from swi3s_studio.ingest.sal_export import export_sal
    from swi3s_studio.ui.main_window import MainWindow
    a, b = two_link_captures()
    shift = np.uint64(TWO_LINK_SHIFT_SAMPLES)
    b_own = replace(b, clock_edges=b.clock_edges - shift, data_edges=b.data_edges - shift)
    p1, p2 = str(tmp_path / "one.sal"), str(tmp_path / "two.sal")
    export_sal(a, p1)
    export_sal(b_own, p2)
    QApplication.instance() or QApplication([])
    win = MainWindow()
    open_via_dialog(monkeypatch, [p1])
    win.open_capture()
    pump_loads(win)
    open_via_dialog(monkeypatch, [p2])
    win.add_link()
    pump_loads(win)
    assert win.links[1].offset_ps == 0                  # nothing known yet
    s1 = TWO_LINK_SHIFT_SAMPLES + 250_000               # one instant, on Link 1 ...
    win._bookmarks.add(s1, link=0)
    win._bookmarks.add(s1 - TWO_LINK_SHIFT_SAMPLES, link=1)   # ... and the same on Link 2
    win.align_on_bookmark_pair("A")
    period_ps = 1e12 / a.sample_rate_hz
    assert abs(win.links[1].offset_ps - TWO_LINK_SHIFT_SAMPLES * period_ps) <= period_ps


# ---- the in-app two-Link demo (Open Demo Capture ▸ PHY2) ----

def _demo_menu_actions(win):
    from PySide6.QtWidgets import QMenu
    for m in win.findChildren(QMenu):
        if m.title() == "Open &Demo Capture":
            return [a.text() for a in m.actions() if a.text()]
    raise AssertionError("no Open Demo Capture menu")


def test_the_demo_menu_offers_two_links(monkeypatch):
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    assert "PHY2" in _demo_menu_actions(win)
    opened = []
    monkeypatch.setattr(win, "load_session", lambda s, **kw: opened.append(s.source))
    monkeypatch.setattr(win, "apply_workspace", lambda *a, **kw: None)
    win.open_demo("phy2")
    assert [(s["variant"], s.get("delay_samples", 0)) for s in opened] == [
        ("", 0), ("flow_control", TWO_LINK_SHIFT_SAMPLES)]


def test_the_two_link_demo_is_the_test_fixture():
    """The app's demo and the tests' fixture are one definition: each Link's capture is
    the fixture's, edge for edge, so what the suite pins is what a user opens."""
    from swi3s_studio.ui.main_window import MainWindow, _demo_samples
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    assert len(win.links) == 2 and [e.name for e in win.links] == ["Link 1", "Link 2"]
    assert all(e.offset_ps == 0 for e in win.links)          # one analyzer: one sample 0
    for entry, cap in zip(win.links, two_link_captures(_demo_samples())):
        assert np.array_equal(entry.session.capture.clock_edges, cap.clock_edges)
        assert np.array_equal(entry.session.capture.data_edges, cap.data_edges)
    assert win.links[1].session.source["delay_samples"] == TWO_LINK_SHIFT_SAMPLES
    assert "delay_samples" not in win.links[0].session.source   # a plain demo's source
    assert win.links[1].session.source["variant"] == "flow_control"
    # Two different buses, so a mix-up between them cannot pass as agreement.
    assert len(win.links[0].session.commands) != len(win.links[1].session.commands)


def test_the_two_link_demo_loads_through_the_async_queue(monkeypatch):
    """With the event loop live (the real app), Link 2 is queued behind Link 1."""
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    monkeypatch.setattr(MainWindow, "_event_loop_live", True)
    win.load_demo_links()
    assert win._load_thread is not None or win._load_pending   # queued, not loaded inline
    assert len(win.links) == 0
    pump_loads(win)
    assert [e.session.source.get("variant") for e in win.links] == ["", "flow_control"]


def test_the_two_link_demo_reopens_from_a_workspace(tmp_path, monkeypatch):
    """The delay is a decode input, so it lives in the Link's source and a saved
    workspace rebuilds the same second Link rather than one at sample 0."""
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    path = str(tmp_path / "demo.json")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    win2 = MainWindow()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    win2.open_workspace()
    pump_loads(win2)
    assert len(win2.links) == 2
    for a, b in zip(win.links, win2.links):
        assert a.session.source == b.session.source
        assert np.array_equal(a.session.capture.clock_edges, b.session.capture.clock_edges)


def _menu_item(menu, text):
    return next(a for a in menu.actions() if a.text().replace("&", "") == text)


def test_the_device_dialogs_name_the_link_they_edit_and_mark_its_devices(monkeypatch):
    """Each Link has its own 12 devices: the Devices dialogs edit the Link the Registers
    pane shows, name it in the title, and mark the devices that answer its Pings; a name
    given there is that Link's only."""
    from swi3s_studio.analysis.measurements import devices_on_bus
    from swi3s_studio.ui import devices_dialog
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    opened = []

    def exec_(dlg):
        opened.append(dlg)
        if dlg._name_edits:
            dlg._name_edits[0].setText("Codec")
            dlg.names = {d: e.text() for d, e in dlg._name_edits.items() if e.text()}
        return 1

    monkeypatch.setattr(devices_dialog.DevicesDialog, "exec", exec_)
    win.set_group_link("right", 1)
    _menu_item(win._devices_menu, "Device Names…").trigger()
    dlg = opened[-1]
    assert dlg.windowTitle() == "Device Names — Link 2"
    seen = devices_on_bus(win.links[1].session.commands)
    assert 0 in seen and dlg._seen == set(seen)
    assert {d for d, lab in dlg._on_bus.items() if lab.text() == "✓"} == set(seen)
    assert win.links[1].session.device_names == {0: "Codec"}
    assert win.links[0].session.device_names == {}
    win.set_group_link("right", 0)
    _menu_item(win._devices_menu, "Hub Depths…").trigger()
    assert opened[-1].windowTitle() == "Device Hub Depths — Link 1"


def test_the_all_links_device_filter_says_it_matches_every_link():
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    assert win._dev_menu.title() == "&Devices"
    win.set_commands_scope_all(True)
    assert win._dev_menu.title() == "&Devices (every Link)"
    win.set_commands_scope_all(False)
    assert win._dev_menu.title() == "&Devices"


def test_the_bus_grid_shows_its_own_links_instant_when_another_link_is_active(monkeypatch):
    """With Link 2 offset in time, the Bus Grid showing Link 2 while Link 1 is active is
    drawn at Link 2's sample for the playhead's instant."""
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1400, 900)
    win.show()
    win.load_demo_links()
    win._grid_dock.raise_()
    QApplication.processEvents()
    win.links[1].offset_ps = 3_000_000                       # Link 2 starts 3 µs later
    win.set_group_link("grid", 1)
    win.set_group_link("right", 0)                           # Link 1 active
    assert win.links.active_index == 0
    drawn = []
    monkeypatch.setattr(win, "_update_grid_for_cursor", lambda s: drawn.append(s))
    win.cursor.set_sample(200_000)
    QApplication.processEvents()
    win._apply_cursor_heavy()
    want = win.links.convert(200_000, 0, 1)
    assert want != 200_000 and drawn and drawn[-1] == want, (drawn, want)


def test_remove_region_csv_is_named_for_the_bus_grids_link(tmp_path):
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    second = win.links[1].session
    sample = int(second.segments[-1]["start_sample"]) + 1000
    second.csv_sections = {len(second.segments) - 1: str(tmp_path / "grid.csv")}
    win.set_group_link("grid", 1)
    win.set_group_link("right", 0)
    win.cursor.set_sample(win.links.convert(sample, 1, 0))
    win._update_remove_region_csv()
    act = win._remove_region_csv_action
    assert act.isEnabled() and "grid.csv" in act.text()


def test_the_rename_prompts_name_the_link(monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    asked = []
    monkeypatch.setattr(QInputDialog, "getText",
                        lambda _p, _t, label, **_k: asked.append(label) or ("", False))
    win.rename_data_port(0, 1, link=1)
    win.rename_region(0, link=0)
    assert "Device 0 · DP1 on Link 2 " in asked[0] and " on Link 1 " in asked[1], asked


def test_import_visualizer_csv_asks_which_link_and_compares_against_it(monkeypatch):
    """With two Links the dialog offers both, the Bus Grid's Link first; the compare is made
    against the Link chosen, which the Bus Grid and the Registers then show."""
    from PySide6.QtWidgets import QMessageBox

    from swi3s_studio.ui import open_config_dialog
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    win.set_group_link("grid", 0)
    seen = {}

    def exec_(dlg):
        seen["links"] = [dlg._link.itemText(i) for i in range(dlg._link.count())]
        seen["default"] = dlg._link.currentIndex()
        dlg._from_auth.setChecked(True)               # compare the Visualizer's config
        dlg._link.setCurrentIndex(1)
        dlg._accept()
        return 1

    monkeypatch.setattr(open_config_dialog.OpenVisualizerConfigDialog, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "exec", lambda *_a: 0)
    monkeypatch.setattr(QMessageBox, "information", lambda *_a, **_k: 0)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a, **_k: 0)
    compared = []
    monkeypatch.setattr(win, "_compare_with_active_link",
                        lambda _p: compared.append(win._session) and None)
    _menu_item(win._decode_menu, "Import Visualizer CSV…").trigger()
    assert seen == {"links": ["Link 1", "Link 2"], "default": 0}
    assert win._pane_link["grid"] == 1 and win._pane_link["right"] == 1
    assert compared == [win.links[1].session]


def test_next_sscr_in_all_links_steps_through_every_links_commits_in_shared_time():
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    win.links[1].offset_ps = 2_000_000                  # Link 2's commits fall later
    want = []
    for k, link in enumerate(win.links):
        s = link.session
        want += sorted((link.to_ps(s.command_cursor_sample(c)), k, s.command_cursor_sample(c))
                       for c in s.commands if c.get("command") in ("SSCR", "DSCR"))
    want.sort()
    assert {k for _t, k, _s in want} == {0, 1}
    win.set_commands_scope_all(True)
    win._activate_link(0)
    win.cursor.set_sample(0)
    got = []
    for _ in want:
        win.next_sscr()
        got.append((win.links.active_index, win.cursor.sample))
    assert got == [(k, s) for _t, k, s in want]
    win.next_sscr()                                     # the last commit: it stays there
    assert got[-1] == (win.links.active_index, win.cursor.sample)
    for k, s in reversed(got[:-1]):
        win.prev_sscr()
        assert (win.links.active_index, win.cursor.sample) == (k, s)
    _t, k, s = want[-1]                                 # past the last commit: Next stays
    win._activate_link(k)
    past = s + 1000
    win.cursor.set_sample(past)
    win.next_sscr()
    assert (win.links.active_index, win.cursor.sample) == (k, past)
    _t, k, s = want[0]                                  # before the first: Previous stays
    win._activate_link(k)
    win.cursor.set_sample(max(0, s - 1))
    win.prev_sscr()
    assert (win.links.active_index, win.cursor.sample) == (k, max(0, s - 1))


def test_next_and_previous_sscr_beyond_the_end_commits_leave_the_playhead():
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    pts = win._sscr_samples()
    assert pts and pts[0] > 0
    past = pts[-1] + 1000
    win.cursor.set_sample(past)
    win.next_sscr()
    assert win.cursor.sample == past
    win.cursor.set_sample(pts[0] - 1)
    win.prev_sscr()
    assert win.cursor.sample == pts[0] - 1
    win.next_sscr()
    assert win.cursor.sample == pts[0]


def test_the_export_and_ssp_text_names_the_link(monkeypatch):
    from swi3s_studio.ui import capture_export_dialog
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    titles = []
    monkeypatch.setattr(capture_export_dialog.CaptureExportDialog, "exec",
                        lambda dlg: titles.append(dlg.windowTitle()) or 0)
    win.set_group_link("bottom", 1)
    _menu_item(win._file_menu, "Export Capture…").trigger()
    assert titles == ["Export Capture — Link 2"]
    monkeypatch.setattr(win, "_redecode_preserving", lambda apply, after, **_kw: after())
    win.set_ssp_row_async(-1)
    assert win._status.text().startswith("Link 2: Manual SSP cleared")


def test_locate_sub_capture_names_the_link_it_marked(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_links()
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()) or 0)
    win._locate_session = win.links[1].session
    win._finish_locate_subcapture("/x/ref.sal", [{"start_sample": 1000, "score": 0.99}], {})
    assert shown == ["Found 1 match(es) on Link 2; cursors placed at each start sample."]
    assert [b.link for b in win._bookmarks] == [1]
    assert "on Link 2" in win._status.text()
