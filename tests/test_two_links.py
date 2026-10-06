"""Two Links in one capture, end to end: the multi-channel .sal export, each Link decoding
exactly as it does alone, one file-wide timeline, and File ▸ Open taking both Links from the
same file through the real async load queue. Offscreen Qt for the GUI half."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from conftest import TWO_LINK_SHIFT_SAMPLES, export_sal_links, two_link_captures
from PySide6.QtWidgets import QApplication

from swi3s_studio.ingest import saleae_sal
from swi3s_studio.session import Session


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


def test_the_multi_link_export_refuses_mixed_rates():
    from dataclasses import replace
    a, b = two_link_captures()
    with pytest.raises(ValueError):
        export_sal_links([a, replace(b, sample_rate_hz=a.sample_rate_hz // 2)], "/dev/null")


def _pump(win, seconds=120.0):
    app = QApplication.instance()
    deadline = time.time() + seconds
    while (getattr(win, "_load_thread", None) is not None or win._load_pending) \
            and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert getattr(win, "_load_thread", None) is None, "the loads never finished"


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
    _pump(win)
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
    _pump(win)
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
    _pump(win)
    first = win._session
    shown = open_via_dialog(monkeypatch, [p2])
    win.add_link()                                     # the dialog opens on Add
    _pump(win)
    assert shown[0]._add.isChecked()
    assert [e.session.source["path"] for e in win.links] == [p1, p2]
    assert [e.name for e in win.links] == ["Link 1", "Link 2"]
    assert win.links[0].session is first and win.links.active_index == 1


# The channel picker and its edge-count helpers went with the Open Capture dialog: the
# counts and the busiest-as-clock default are tests/test_probe.py's and
# tests/test_open_capture_dialog.py's now, and test_one_row_opens_one_link above pins the
# clock-with-a-data-line default on this four-channel file.


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
    _pump(win)
    open_via_dialog(monkeypatch, [p2])
    win.add_link()
    _pump(win)
    assert win.links[1].offset_ps == 0                  # nothing known yet
    s1 = TWO_LINK_SHIFT_SAMPLES + 250_000               # one instant, on Link 1 ...
    win._bookmarks.add(s1, link=0)
    win._bookmarks.add(s1 - TWO_LINK_SHIFT_SAMPLES, link=1)   # ... and the same on Link 2
    win.align_on_bookmark_pair("A")
    period_ps = 1e12 / a.sample_rate_hz
    assert abs(win.links[1].offset_ps - TWO_LINK_SHIFT_SAMPLES * period_ps) <= period_ps


# ---- the in-app two-Link demo (Open Demo Capture ▸ PHY2 (Two Links)) ----

def _demo_menu_actions(win):
    from PySide6.QtWidgets import QMenu
    for m in win.findChildren(QMenu):
        if m.title() == "Open &Demo Capture":
            return [a.text() for a in m.actions() if a.text()]
    raise AssertionError("no Open Demo Capture menu")


def test_the_demo_menu_offers_two_links():
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    assert "PHY2 (Two Links)" in _demo_menu_actions(MainWindow())


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
    _pump(win)
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
    _pump(win2)
    assert len(win2.links) == 2
    for a, b in zip(win.links, win2.links):
        assert a.session.source == b.session.source
        assert np.array_equal(a.session.capture.clock_edges, b.session.capture.clock_edges)
