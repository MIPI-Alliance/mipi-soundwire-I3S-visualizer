"""File ▸ Open Capture's one-page dialog (ui/open_capture_dialog.py) and the open it drives.

It replaced a chain of modal questions (clock, data, "another Link?", a blind start and
length for a large .sal, Open Capture Time Window, the analog CSV dialog) with one page that
says what the file is first. These pin its defaults, what it refuses, what it returns, and
that Open Capture loads exactly what it asked for. Captures are synthetic (TESTING.md).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import open_via_dialog, pump_loads
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from swi3s_studio.ingest.probe import CaptureProbe, ProbedChannel
from swi3s_studio.ui.open_capture_dialog import OpenCaptureDialog


def _probe(n=4, edges=(9000, 900, 8800, 700), windowed=True, duration=2.0, cost=10**9,
           kind="sal", analog=False, rate=500e6, two_file=False):
    chans = [ProbedChannel(i, f"Ch {i}", edges[i] if edges else None, approx=True)
             for i in range(n)]
    return CaptureProbe(kind, ["/x/run.sal"], "Logic 2 project (.sal)", chans,
                        sample_rate_hz=rate, duration_s=duration, windowed=windowed,
                        analog=analog, two_file=two_file, cost_all_bytes=cost)


def _dlg(pr=None, names=None, prefer_add=False, budget=4 * 10**9):
    QApplication.instance() or QApplication([])
    return OpenCaptureDialog(pr or _probe(), names or [], prefer_add=prefer_add,
                             budget_bytes=budget)


def _name(row, text):
    row.name.setText(text)
    row.name.textEdited.emit(text)                       # as typing does


# ---- defaults ----------------------------------------------------------------------------

def test_one_link_named_link_1_on_the_busiest_clock_and_a_data_line():
    dlg = _dlg()
    (row,) = dlg.rows()
    assert row.name.text() == "Link 1"
    assert row.pair() == (0, 1)              # clock the busiest, data a quiet line (not ch 2)
    assert "(~9,000 edges)" in row.clock.currentText()
    assert dlg._all.isChecked() and not dlg._into.isVisibleTo(dlg)
    assert dlg._open_btn.isEnabled()


def test_building_the_dialog_raises_nothing_in_a_slot(monkeypatch):
    # PySide prints a slot's exception and carries on, so a build-order bug (the window's
    # first value validating before the Into radios existed) passed every other test here.
    import sys
    raised = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: raised.append(exc[1]))
    for kw in ({}, {"names": ["Link 1"], "prefer_add": True}, {"budget": 10**8}):
        _dlg(**kw)
    assert raised == []


def test_the_source_says_what_the_file_is():
    dlg = _dlg()
    text = dlg._source_line.text()
    assert "Length 2.000 s" in text and "1.0 GB" in text and "(fits)" in text


def test_adding_continues_the_open_links_names_and_takes_an_offset():
    dlg = _dlg(names=["Link 1", "Link 2"], prefer_add=True)
    assert dlg._add.isChecked() and dlg._into.isVisibleTo(dlg)
    assert dlg.rows()[0].name.text() == "Link 3"
    assert dlg._offset.isVisibleTo(dlg)
    dlg._replace.setChecked(True)                        # replacing: numbering starts over
    assert dlg.rows()[0].name.text() == "Link 1" and not dlg._offset.isVisibleTo(dlg)


def test_a_name_the_user_typed_is_kept():
    dlg = _dlg(names=["Link 1"], prefer_add=True)
    _name(dlg.rows()[0], "Speakers")
    dlg._replace.setChecked(True)
    assert dlg.rows()[0].name.text() == "Speakers"


# ---- rows --------------------------------------------------------------------------------

def test_add_a_link_pairs_the_free_channels_and_runs_out():
    dlg = _dlg()
    dlg._add_row_from_free()
    r1, r2 = dlg.rows()
    assert r2.pair() == (2, 3) and r2.name.text() == "Link 2"
    assert not dlg._add_row_btn.isEnabled()              # no two channels left
    assert r1.remove.isVisibleTo(dlg)
    dlg._remove_row(r2)
    assert len(dlg.rows()) == 1 and not dlg.rows()[0].remove.isVisibleTo(dlg)
    assert dlg._add_row_btn.isEnabled()


def test_two_channels_or_two_files_offer_no_second_link():
    assert not _dlg(_probe(n=2, edges=(10, 1)))._add_row_btn.isVisibleTo(None)
    assert not _dlg(_probe(n=2, edges=(10, 1), two_file=True))._add_row_btn.isVisibleTo(None)


# ---- what it refuses ---------------------------------------------------------------------

@pytest.mark.parametrize("break_it, says", [
    (lambda d: _name(d.rows()[0], ""), "needs a name"),
    (lambda d: d.rows()[0].data.setCurrentIndex(0), "different channels"),
    (lambda d: (d._add_row_from_free(), d.rows()[1].clock.setCurrentIndex(0)),
     "only one Link"),
    (lambda d: (d._add_row_from_free(), _name(d.rows()[0], "Bus"), _name(d.rows()[1], "Bus")),
     "already a Link"),                                   # both typed: a real clash
    (lambda d: (d._part.setChecked(True), d._t0.set_seconds(1.5), d._t1.set_seconds(1.0)),
     "end after it starts"),
])
def test_open_waits_for_a_valid_request(break_it, says):
    dlg = _dlg()
    break_it(dlg)
    assert not dlg._open_btn.isEnabled() and says in dlg._problem.text()


def test_an_added_link_cannot_take_an_open_links_name():
    dlg = _dlg(names=["Link 1"], prefer_add=True)
    _name(dlg.rows()[0], "Link 1")
    assert not dlg._open_btn.isEnabled() and "already a Link" in dlg._problem.text()


# ---- how much to decode ------------------------------------------------------------------

def test_a_window_shows_its_length_and_cost_live():
    dlg = _dlg()
    dlg._t0.set_seconds(0.5)
    dlg._t1.set_seconds(1.0)
    assert dlg._part.isChecked()                         # editing a bound selects From/to
    assert "500.000 ms" in dlg._window_line.text() and "250 MB" in dlg._window_line.text()


def test_when_all_does_not_fit_it_opens_on_the_largest_window_that_does():
    dlg = _dlg(budget=250 * 10**6)                       # a quarter of the 1 GB
    assert dlg._part.isChecked() and dlg._t0.seconds() == 0.0
    assert dlg._t1.seconds() == pytest.approx(0.5)         # 2 s x 1/4
    assert "does not fit" in dlg._source_line.text()


def test_a_format_read_whole_says_so():
    dlg = _dlg(_probe(windowed=False))
    assert "reads the whole file" in dlg._source_line.text()
    dlg._t1.set_seconds(1.0)
    assert "reads the whole file" in dlg._window_line.text()


def test_no_length_no_window():
    dlg = _dlg(_probe(duration=None))
    assert not dlg._part.isEnabled() and dlg._all.isChecked()


# ---- the request -------------------------------------------------------------------------

def test_the_request_carries_everything_asked_for():
    dlg = _dlg(names=["Link 1"], prefer_add=True)
    dlg._add_row_from_free()
    _name(dlg.rows()[1], "Second")
    dlg._t0.set_seconds(0.25)
    dlg._t1.set_seconds(0.75)
    dlg._offset.setValue(1.5)                            # ms
    req = dlg.result_request()
    assert [(ln.name, ln.clock, ln.data) for ln in req.links] == \
        [("Link 2", 0, 1), ("Second", 2, 3)]
    assert req.add and req.offset_s == pytest.approx(1.5e-3)
    assert req.window_s == (0.25, 0.75)
    assert not any(ln.auto_clock for ln in req.links)    # four channels: the user chose


def test_a_two_channel_default_lets_the_loader_orient_the_clock():
    dlg = _dlg(_probe(n=2, edges=None))
    assert dlg.result_request().links[0].auto_clock      # unknown counts: as before, auto
    dlg.rows()[0].clock.setCurrentIndex(1)
    dlg.rows()[0].data.setCurrentIndex(0)
    assert not dlg.result_request().links[0].auto_clock  # swapped by hand: as chosen


def test_analog_thresholds_are_both_or_neither():
    dlg = _dlg(_probe(n=3, edges=None, analog=True, kind="analog_csv"))
    row = dlg.rows()[0]
    ch, cl, dh, dl = row.thresh
    ch.setText("1.2")
    assert not dlg._open_btn.isEnabled() and "both hi and lo" in dlg._problem.text()
    cl.setText("0.4")
    dh.setText("0.3")
    dl.setText("0.9")
    assert "above lo" in dlg._problem.text()
    dh.setText("0.9")
    dl.setText("0.3")
    assert dlg._open_btn.isEnabled()
    link = dlg.result_request().links[0]
    assert link.clock_thresh == (1.2, 0.4) and link.data_thresh == (0.9, 0.3)


def test_a_bin_pair_without_a_readable_rate_asks_for_one():
    pr = _probe(n=2, edges=(10, 1), kind="saleae_binary", rate=None, two_file=True)
    dlg = _dlg(pr)
    assert dlg._rate_spin is not None
    dlg._rate_spin.setValue(250_000_000)
    assert dlg.result_request().sample_rate_hz == 250_000_000


# ---- Open Capture loads what the dialog asked for ----------------------------------------

@pytest.fixture
def sal(tmp_path):
    from swi3s_studio.session import Session
    demo = Session.from_demo(300, cold_start=True)
    path = str(tmp_path / "demo.sal")
    demo.export_sal(path)
    return path, demo


def _window():
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    return MainWindow()


def test_open_capture_loads_the_named_window(sal, monkeypatch):
    path, demo = sal
    win = _window()
    dur = demo.capture.duration_s

    asked = []

    def edit(dlg):
        _name(dlg.rows()[0], "Codec bus")
        dlg._t0.set_seconds(dur / 4)
        dlg._t1.set_seconds(dur / 2)
        asked.extend((dlg._t0.seconds(), dlg._t1.seconds()))   # as the spin boxes hold them
    open_via_dialog(monkeypatch, [path], edit)
    win.open_capture()
    pump_loads(win)
    (link,) = list(win.links)
    rate = demo.capture.sample_rate_hz
    assert link.name == "Codec bus"
    # The nearest samples, the end one included (the range is half-open).
    assert link.session.source["window"] == [round(asked[0] * rate), round(asked[1] * rate) + 1]
    assert abs(asked[0] - dur / 4) <= 0.5e-6                 # a ms capture's fields: to the µs


def test_adding_places_the_new_link_at_its_offset(sal, monkeypatch):
    path, demo = sal
    win = _window()
    open_via_dialog(monkeypatch, [path])
    win.open_capture()
    pump_loads(win)

    def edit(dlg):
        assert dlg._add.isChecked()
        dlg._offset.setValue(2.5)                         # ms
    open_via_dialog(monkeypatch, [path], edit)
    win.add_link()
    pump_loads(win)
    assert [e.name for e in win.links] == ["Link 1", "Link 2"]
    assert win.links[1].offset_ps == 2_500_000_000 and win.links[0].offset_ps == 0


def test_cancel_opens_nothing(sal, monkeypatch):
    path, _demo = sal
    win = _window()
    open_via_dialog(monkeypatch, [path], lambda dlg: False)
    win.open_capture()
    pump_loads(win)
    assert len(win.links) == 0


def test_a_file_that_is_not_a_capture_says_why(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    p = tmp_path / "config.csv"
    p.write_text("AppVersion,1.0\nNumColumns,16\n")
    win = _window()
    said = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: said.append(a[2]))
    shown = open_via_dialog(monkeypatch, [str(p)])
    win.open_capture()
    assert not shown and said and "Visualizer config CSV" in said[0]


def _analog_csv(tmp_path, channels):
    path = tmp_path / "scope.csv"
    head = "Model,MSO58\nSample Interval,1.6e-10\n\nTIME," + ",".join(channels) + "\n"
    rows = "".join(f"{i * 1e-9}," + ",".join("0.0" for _ in channels) + "\n"
                   for i in range(4))
    path.write_text(head + rows)
    return str(path)


def test_an_analog_csv_opens_on_the_picked_channels_and_thresholds(tmp_path, monkeypatch):
    from swi3s_studio.session import Session
    win = _window()
    path = _analog_csv(tmp_path, ["CH1", "CH2", "CH3"])

    def edit(dlg):
        row = dlg.rows()[0]
        row.clock.setCurrentIndex(2)
        row.data.setCurrentIndex(0)
        row.thresh[2].setText("0.9")
        row.thresh[3].setText("0.3")
    open_via_dialog(monkeypatch, [path], edit)
    calls = []
    monkeypatch.setattr(Session, "from_analog_csv", classmethod(
        lambda cls, p, **kw: calls.append((p, kw))))
    monkeypatch.setattr(win, "_load_async", lambda factory, after=None, **k: factory())
    win.open_capture()
    (p, kw), = calls
    assert p == path and (kw["clock"], kw["data"]) == ("CH3", "CH1")
    assert kw["auto_clock"] is False and kw["data_thresh"] == (0.9, 0.3)
    assert kw["clock_thresh"] is None and kw["window_s"] is None


def test_a_two_channel_analog_csv_left_as_offered_is_auto_oriented(tmp_path, monkeypatch):
    from swi3s_studio.session import Session
    win = _window()
    open_via_dialog(monkeypatch, [_analog_csv(tmp_path, ["CH1", "CH2"])])
    calls = []
    monkeypatch.setattr(Session, "from_analog_csv", classmethod(
        lambda cls, p, **kw: calls.append(kw)))
    monkeypatch.setattr(win, "_load_async", lambda factory, after=None, **k: factory())
    win.open_capture()
    assert calls and calls[0]["auto_clock"] is True


# ---- Locate Sub-Capture reads its reference through the same dialog ---------------------

def test_the_reference_form_has_no_name_no_second_link_and_no_into():
    dlg = OpenCaptureDialog(_probe(), ["Link 1"], reference=True)
    (row,) = dlg.rows()
    assert not row.name.isVisibleTo(dlg) and not dlg._add_row_btn.isVisibleTo(dlg)
    assert not dlg._into.isVisibleTo(dlg) and dlg._open_btn.text() == "Search"
    assert dlg._part.isEnabled()                         # a reference can be windowed too


def test_a_reference_window_is_cut_from_the_reference(sal):
    from swi3s_studio.ingest import probe as probe_mod
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.open_capture_dialog import LinkRequest, OpenRequest
    path, demo = sal
    pr = probe_mod.probe([path])
    rate = demo.capture.sample_rate_hz
    dur = demo.capture.duration_s
    req = OpenRequest(links=[LinkRequest("", 0, 1)], window_s=(dur / 3, dur / 2))
    cap = MainWindow._reference_capture(pr, req)
    want = demo.capture.subcapture(round(dur / 3 * rate), round(dur / 2 * rate) + 1)
    assert cap.clock_edges.size == want.clock_edges.size
    assert int(cap.clock_edges[0]) == int(want.clock_edges[0])


def test_locate_finds_a_slice_picked_in_the_dialog(tmp_path, monkeypatch):
    from swi3s_studio.ingest.sal_export import export_sal
    win = _window()
    win.load_demo()
    main = win._session.capture
    rate = main.sample_rate_hz
    s0 = int(main.duration_s * rate * 0.6)
    ref = str(tmp_path / "ref.sal")
    export_sal(main.subcapture(s0, s0 + rate // 2000), ref)        # 0.5 ms of it
    shown = open_via_dialog(monkeypatch, [ref])
    from PySide6.QtWidgets import QMessageBox
    said = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: said.append(box.text()) or 0)
    win.locate_subcapture()
    import time
    deadline = time.time() + 60
    while getattr(win, "_locate_thread", None) is not None:
        assert time.time() < deadline, "the search never finished"
        QApplication.instance().processEvents()
        time.sleep(0.01)
    assert shown and shown[0]._reference
    assert said and said[0].startswith("Found 1 match")
    starts = [b.sample for b in win._bookmarks]
    assert any(abs(s - s0) < rate // 100_000 for s in starts), (starts, s0)


# ---- a window, in every format, from the dialog to the workspace -------------------------

def _format_files(tmp_path):
    """One file (or pair) per format, each holding the same kind of bus, and how to make the
    whole capture with the loader Open Capture uses."""
    from test_analog_import import _square_wave_codes, _write_synthetic_wfm
    from test_vcd import _write_vcd

    from swi3s_studio.ingest import saleae_binary
    from swi3s_studio.session import Session
    demo = Session.from_demo(300, cold_start=True)
    out = {}
    out["sal"] = [str(tmp_path / "w.sal")]
    demo.export_sal(out["sal"][0])
    out["digital_csv"] = [str(tmp_path / "w.csv")]
    demo.export_csv(out["digital_csv"][0])
    out["vcd"] = [str(tmp_path / "w.vcd")]
    _write_vcd(out["vcd"][0], demo.capture, timescale="2ns")
    out["saleae_binary"] = [str(tmp_path / "w0.bin"), str(tmp_path / "w1.bin")]
    saleae_binary.write_capture(demo.capture, *out["saleae_binary"])
    out["wfm"] = [str(tmp_path / "ch1.wfm"), str(tmp_path / "ch2.wfm")]
    _write_synthetic_wfm(out["wfm"][0], _square_wave_codes(40000, 8), dt=1e-9)
    _write_synthetic_wfm(out["wfm"][1], _square_wave_codes(40000, 64), dt=1e-9)
    path = tmp_path / "scope.csv"
    rows = ["Model,MSO58", "Sample Interval,1e-09", "TIME,CH1,CH2"]
    rows += [f"{i * 1e-9:.12f},{1.0 if (i // 4) % 2 else 0.0},{1.0 if (i // 32) % 2 else 0.0}"
             for i in range(30000)]
    path.write_text("\n".join(rows) + "\n")
    out["analog_csv"] = [str(path)]
    return out


def _same(a, b):
    import numpy as np
    return (np.array_equal(a.clock_edges, b.clock_edges)
            and np.array_equal(a.data_edges, b.data_edges)
            and a.initial_clock == b.initial_clock and a.initial_data == b.initial_data)


@pytest.mark.parametrize("kind", ["sal", "digital_csv", "vcd", "saleae_binary", "wfm",
                                  "analog_csv"])
def test_every_format_opens_the_window_asked_for_and_a_workspace_reopens_it(
        tmp_path, monkeypatch, kind):
    from swi3s_studio.ingest import probe as probe_mod
    from swi3s_studio.session import Session
    paths = _format_files(tmp_path)[kind]
    dur = probe_mod.probe(paths).duration_s
    asked = []

    def whole(dlg):
        asked.append(None)
    open_via_dialog(monkeypatch, paths, whole)
    win = _window()
    win.open_capture()
    pump_loads(win)
    full = win.links[0].session.capture

    def part(dlg):
        dlg._t0.set_seconds(dur / 3)
        dlg._t1.set_seconds(dur / 2)
        asked.append((dlg._t0.seconds(), dlg._t1.seconds()))
    open_via_dialog(monkeypatch, paths, part)
    win.open_capture()
    pump_loads(win)
    (link,) = list(win.links)
    want = Session.window_samples(asked[-1], full.sample_rate_hz)
    assert link.session.source["window"] == list(want)
    assert _same(link.session.capture, full.subcapture(*want))
    again = Session.from_source(link.session.source)          # as a workspace reopens it
    assert _same(again.capture, link.session.capture)


def test_a_dlv_csv_window_is_taken_at_the_rate_it_loads_at(tmp_path, monkeypatch):
    # A complementary pair loads at its true DLV rate (the timestamp quantum), not at the
    # rate the finest edge spacing suggests, which is the probe's: converted with the
    # probe's, a window of half a millisecond decoded 25 µs, in the wrong place.
    import random

    from swi3s_studio.ingest import probe as probe_mod
    from swi3s_studio.session import Session
    random.seed(1)
    t, lv, rows = 0.0, 0, ["Time [s],DP,DN"]
    for _ in range(20000):
        rows.append(f"{t:.10f},{lv},{1 - lv}")
        t += 2e-9 * (20 * random.choice([1, 2, 3]) + random.choice([0, 1]))
        lv ^= 1
    path = tmp_path / "dlv.csv"
    path.write_text("\n".join(rows) + "\n")
    pr = probe_mod.probe([str(path)])
    asked = []

    def edit(dlg):
        dlg._t0.set_seconds(pr.duration_s / 4)
        dlg._t1.set_seconds(pr.duration_s / 2)
        asked.extend((dlg._t0.seconds(), dlg._t1.seconds()))
    open_via_dialog(monkeypatch, [str(path)], edit)
    win = _window()
    win.open_capture()
    pump_loads(win)
    cap = win.links[0].session.capture
    assert cap.sample_rate_hz != pr.sample_rate_hz            # the case: the rates differ
    s0, s1 = win.links[0].session.source["window"]
    assert (s0, s1) == Session.window_samples(asked, cap.sample_rate_hz)
    # the span asked for, to the sample (the end sample is kept: one more)
    assert (s1 - s0) / cap.sample_rate_hz == pytest.approx(asked[1] - asked[0],
                                                           abs=2 / cap.sample_rate_hz)


def test_an_added_window_sits_where_it_is_in_the_file(sal, monkeypatch):
    # The offset places the FILE's time zero; the window is rebased to its own start, so
    # the added Link is placed that much later, in line with the same file opened whole.
    path, demo = sal
    win = _window()
    open_via_dialog(monkeypatch, [path])
    win.open_capture()
    pump_loads(win)
    dur = demo.capture.duration_s

    def edit(dlg):
        dlg._t0.set_seconds(dur / 4)
        dlg._t1.set_seconds(dur / 2)
        dlg._offset.setValue(1.0)                         # ms
    open_via_dialog(monkeypatch, [path], edit)
    win.add_link()
    pump_loads(win)
    t0 = win.links[1].session.source["window"][0] / demo.capture.sample_rate_hz
    assert win.links[1].offset_ps == pytest.approx((1e-3 + t0) * 1e12, abs=1e3)


def test_a_typed_bin_rate_and_wfm_thresholds_reach_the_loader(tmp_path, monkeypatch):
    from swi3s_studio.ingest import probe as probe_mod
    from swi3s_studio.session import Session
    files = _format_files(tmp_path)
    real = probe_mod.probe

    def no_rate(paths):
        pr = real(paths)
        if pr.kind == "saleae_binary":
            pr.sample_rate_hz = None                      # the times do not give it away
        return pr
    monkeypatch.setattr(probe_mod, "probe", no_rate)
    calls = {}
    monkeypatch.setattr(Session, "from_saleae_binary", classmethod(
        lambda cls, *a, **kw: calls.setdefault("bin", (a, kw))))
    monkeypatch.setattr(Session, "from_wfm", classmethod(
        lambda cls, *a, **kw: calls.setdefault("wfm", (a, kw))))
    win = _window()
    monkeypatch.setattr(win, "_load_async", lambda factory, after=None, **k: factory())

    def bin_rate(dlg):
        dlg._rate_spin.setValue(123_000_000)
    open_via_dialog(monkeypatch, files["saleae_binary"], bin_rate)
    win.open_capture()
    assert calls["bin"][0][2] == 123_000_000

    def thresholds(dlg):
        row = dlg.rows()[0]
        for box, v in zip(row.thresh, ("1.2", "0.4", "0.9", "0.3")):
            box.setText(v)
    open_via_dialog(monkeypatch, files["wfm"], thresholds)
    win.open_capture()
    kw = calls["wfm"][1]
    assert kw["clock_thresh"] == (1.2, 0.4) and kw["data_thresh"] == (0.9, 0.3)


def test_locate_cuts_a_reference_read_whole_to_its_window(tmp_path):
    from swi3s_studio.ingest import probe as probe_mod
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.open_capture_dialog import LinkRequest, OpenRequest
    (path,) = _format_files(tmp_path)["digital_csv"]
    pr = probe_mod.probe([path])
    full = Session.from_digital_csv(path, 1, 2).capture
    asked = (pr.duration_s / 3, pr.duration_s / 2)
    cap = MainWindow._reference_capture(pr, OpenRequest(links=[LinkRequest("", 1, 2)],
                                                        window_s=asked))
    assert _same(cap, full.subcapture(*Session.window_samples(asked, full.sample_rate_hz)))


# ---- what is open when opens overlap ----------------------------------------------------

def test_a_newer_open_is_not_added_to_by_an_older_files_other_links(tmp_path, monkeypatch):
    # The decode dialog is not modal: a second Open (Replace) can be queued while the first
    # file's first Link decodes. That file's other Links, queued once its first has loaded,
    # must not land on the newer file's analysis.
    from conftest import export_sal_links, two_link_captures

    from swi3s_studio.ingest import probe as probe_mod
    from swi3s_studio.ingest.sal_export import export_sal
    a, b = two_link_captures()
    two, one = str(tmp_path / "two.sal"), str(tmp_path / "one.sal")
    export_sal_links([a, b], two)
    export_sal(a, one)
    win = _window()
    pr2 = probe_mod.probe([two])
    dlg = OpenCaptureDialog(pr2, [], win)
    dlg._add_row_from_free()
    win._open_request(pr2, dlg.result_request())             # Link 1 of `two` decodes…
    pr1 = probe_mod.probe([one])
    win._open_request(pr1, OpenCaptureDialog(pr1, [], win).result_request())   # …Open
    pump_loads(win)
    assert [e.session.source["path"] for e in win.links] == [one]


def test_a_link_added_while_commands_shows_all_links_is_listed(sal, monkeypatch):
    path, _demo = sal
    win = _window()
    open_via_dialog(monkeypatch, [path])
    win.open_capture()
    pump_loads(win)
    open_via_dialog(monkeypatch, [path])
    win.add_link()
    pump_loads(win)
    win.set_commands_scope_all(True)

    def edit(dlg):
        _name(dlg.rows()[0], "Speaker")
    open_via_dialog(monkeypatch, [path], edit)
    win.add_link()
    pump_loads(win)
    model = win._all_model
    assert win._cmd_all and model is win._cmd_proxy.sourceModel()
    assert {model.link_at(r) for r in range(model.rowCount())} == {0, 1, 2}
    col = next(c for c in range(model.columnCount())
               if model.headerData(c, Qt.Horizontal) == "Link")
    shown = {model.index(r, col).data() for r in range(model.rowCount())}
    assert "Speaker" in shown


def test_the_cost_counts_every_link_on_the_page():
    # Each Link from the file is loaded and kept: two Links cost two, and can tip it over.
    dlg = _dlg(_probe(cost=3 * 10**9), budget=4 * 10**9)
    assert "~3.0 GB to load all (fits)" in dlg._source_line.text()
    dlg._add_row_from_free()
    text = dlg._source_line.text()
    assert "~6.0 GB to load all for 2 Links (does not fit)" in text, text
    dlg._part.setChecked(True)
    dlg._t0.set_seconds(0.0)
    dlg._t1.set_seconds(1.0)                                 # half of 2 s, windowed: 1.5 GB each
    assert "~3.0 GB for 2 Links" in dlg._window_line.text()
    dlg.rows()[1].remove.click()
    assert "(fits)" in dlg._source_line.text() and "for 2" not in dlg._source_line.text()



def test_typing_another_rows_default_name_moves_that_row_on():
    dlg = _dlg()
    dlg._add_row_from_free()
    first, second = dlg.rows()
    assert (first.name.text(), second.name.text()) == ("Link 1", "Link 2")
    _name(first, "Link 2")
    assert second.name.text() == "Link 1" and dlg._open_btn.isEnabled()
    _name(second, "Link 2")                               # a clash the user DID make
    assert not dlg._open_btn.isEnabled()


def test_numbers_read_at_a_glance():
    from swi3s_studio.ui.open_capture_dialog import _edges, _TimeSpin
    assert (_edges(9_800_000, True), _edges(162_979, False), _edges(700, True),
            _edges(42, False)) == ("~9.8M", "~163k", "~700", "42")
    for span, suffix, shown in ((12.4, " s", "2.500 s"), (8.8e-3, " ms", "2.500 ms"),
                                (40e-6, " µs", "2.500 µs")):
        spin = _TimeSpin(span)
        spin.set_seconds(2.5 * {" s": 1, " ms": 1e-3, " µs": 1e-6}[suffix])
        assert spin.suffix() == suffix and spin.text() == shown
        assert spin.seconds() == pytest.approx(2.5 * {" s": 1, " ms": 1e-3, " µs": 1e-6}[suffix])
    dlg = _dlg(_probe(edges=(9_000_000, 900_000, 8_800, 700), rate=98.304e6))
    assert dlg.rows()[0].clock.currentText() == "Ch 0  (~9.0M edges)"
    assert "· 98.304 MHz" in dlg.findChildren(QLabel)[0].text()
    assert "· 500 MHz" in _dlg().findChildren(QLabel)[0].text()
    assert _dlg(names=["Link 1"], prefer_add=True)._offset.text() == "0.000 ms"


# ---- review: queued opens, the probe's busy wait, odd channels, the window's end ----------

def test_a_queued_open_keeps_its_other_links(monkeypatch):
    # An open asked for while another capture decodes is queued, then started from the queue;
    # it was counted twice, so its own follow-on Links looked superseded and were dropped.
    from swi3s_studio.ui.main_window import MainWindow
    monkeypatch.setattr(MainWindow, "_event_loop_live", True)
    win = _window()
    win.load_demo()                                       # a decode in flight
    assert win._load_thread is not None
    win.load_demo_links()                                 # queued behind it
    pump_loads(win)
    assert len(win.links) == 2


def test_the_probes_busy_dialog_holds_until_the_read_is_done(monkeypatch, tmp_path):
    import threading
    import time

    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QProgressDialog

    from swi3s_studio.ingest import probe as probe_mod
    from swi3s_studio.ui.main_window import MainWindow
    monkeypatch.setattr(MainWindow, "_event_loop_live", True)
    win = _window()
    gate = threading.Event()
    monkeypatch.setattr(probe_mod, "probe", lambda paths: (gate.wait(5), "probed")[1])
    seen = {}

    def poke():
        dlg = next(d for d in win.findChildren(QProgressDialog) if d.isVisible())
        QApplication.sendEvent(dlg, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        dlg.close()
        seen["still_up"] = dlg.isVisible()
        seen["reentry_refused"] = win._probing
        gate.set()
    from PySide6.QtCore import QTimer
    QTimer.singleShot(600, poke)
    t0 = time.time()
    assert win._probe_files([str(tmp_path / "x.csv")]) == "probed"
    assert seen == {"still_up": True, "reentry_refused": True}, seen
    assert time.time() - t0 < 5


def test_a_probe_finishing_after_the_window_began_closing_opens_nothing(monkeypatch, tmp_path):
    from swi3s_studio.ingest import probe as probe_mod
    from swi3s_studio.ui.main_window import MainWindow
    monkeypatch.setattr(MainWindow, "_event_loop_live", True)
    win = _window()

    def slow(paths):
        import time
        time.sleep(0.5)
        win._closing = True                               # as closeEvent sets it meanwhile
        return "probed"
    monkeypatch.setattr(probe_mod, "probe", slow)
    assert win._probe_files([str(tmp_path / "x.csv")]) is None


def test_the_window_can_reach_the_end_of_the_capture():
    from swi3s_studio.ui.open_capture_dialog import _TimeSpin
    for span in (2.0004, 0.0123454, 40.0e-6 + 4e-10):     # each rounds DOWN at 3 decimals
        spin = _TimeSpin(span)
        spin.set_seconds(span)
        assert spin.seconds() == span
