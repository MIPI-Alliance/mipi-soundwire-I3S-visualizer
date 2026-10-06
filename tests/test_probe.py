"""The probe behind the Open Capture dialog, and a decode window for every format.

The probe tells the dialog what a file is BEFORE it is decoded (channels and their edge
counts, the sample rate, the length, whether a window is read on its own, roughly what
loading costs), from cheap reads only. Every format is built here from the demo capture
with the writers the suite already has: no real captures (TESTING.md).

Every Session factory takes a `window`, recorded in `source` so a workspace reopens the same slice.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from test_analog_import import _square_wave_codes, _write_synthetic_wfm
from test_vcd import _write_vcd

from swi3s_studio.ingest import probe as probe_mod
from swi3s_studio.ingest import saleae_binary
from swi3s_studio.session import Session


@pytest.fixture(scope="module")
def demo():
    return Session.from_demo(300, cold_start=True)


def _same_capture(a, b):
    return (np.array_equal(a.clock_edges, b.clock_edges)
            and np.array_equal(a.data_edges, b.data_edges)
            and a.initial_clock == b.initial_clock and a.initial_data == b.initial_data)


def test_sal(demo, tmp_path):
    path = str(tmp_path / "c.sal")
    demo.export_sal(path)
    pr = probe_mod.probe([path])
    cap = demo.capture
    assert pr.kind == "sal" and pr.windowed and not pr.two_file
    assert [c.value for c in pr.channels] == [0, 1]
    assert pr.sample_rate_hz == cap.sample_rate_hz
    assert pr.duration_s == pytest.approx(cap.duration_s, rel=1e-3)
    clk = pr.channels[0]
    assert clk.approx and clk.edges >= cap.clock_edges.size    # an upper bound, as documented
    assert pr.busiest_pair() == (0, 1)
    assert pr.cost_all_bytes and pr.cost_bytes(0, pr.duration_s / 4) < pr.cost_all_bytes


def test_saleae_binary_pair(demo, tmp_path):
    clk, dat = str(tmp_path / "a.bin"), str(tmp_path / "b.bin")
    saleae_binary.write_capture(demo.capture, clk, dat)
    pr = probe_mod.probe([dat, clk])                          # order as the user picked
    cap = demo.capture
    assert pr.kind == "saleae_binary" and pr.two_file and not pr.windowed
    by = {c.value: c.edges for c in pr.channels}
    assert by[clk] == cap.clock_edges.size and by[dat] == cap.data_edges.size
    assert pr.channels[pr.busiest_pair()[0]].value == clk     # the clock is the busier
    # A .bin stores times, not a rate: the probe infers it exactly as the loader will (the
    # finest edge spacing, so 1/10 of the demo's 500 MHz), and both must agree.
    loaded = Session.from_saleae_binary(clk, dat, int(pr.sample_rate_hz)).capture
    assert pr.sample_rate_hz == saleae_binary.infer_sample_rate([clk, dat])
    assert loaded.clock_edges.size == cap.clock_edges.size
    assert pr.duration_s == pytest.approx(cap.duration_s, rel=1e-2)
    assert pr.cost_bytes(0, 0.001) == pr.cost_all_bytes       # read whole, whatever the window


def test_digital_csv(demo, tmp_path):
    path = str(tmp_path / "c.csv")
    demo.export_csv(path)
    pr = probe_mod.probe([path])
    cap = demo.capture
    assert pr.kind == "digital_csv" and not pr.windowed and len(pr.channels) == 2
    assert pr.channels[0].edges == cap.clock_edges.size and not pr.channels[0].approx
    assert pr.duration_s == pytest.approx(cap.duration_s, rel=1e-3)
    # As for .bin: the rate the loader will infer, not the demo's own.
    assert pr.sample_rate_hz == Session.from_digital_csv(path, 1, 2).capture.sample_rate_hz


def test_vcd(demo, tmp_path):
    path = str(tmp_path / "c.vcd")
    _write_vcd(path, demo.capture, timescale="2ns")
    pr = probe_mod.probe([path])
    cap = demo.capture
    assert pr.kind == "vcd"
    assert [c.label.split(".")[-1] for c in pr.channels] == ["clk", "data"]   # scope-qualified
    assert pr.channels[0].edges == cap.clock_edges.size
    assert pr.sample_rate_hz == pytest.approx(500e6)
    assert pr.duration_s == pytest.approx(cap.duration_s * cap.sample_rate_hz * 2e-9, rel=1e-3)


def test_wfm_pair(tmp_path):
    a, b = str(tmp_path / "ch1.wfm"), str(tmp_path / "ch2.wfm")
    _write_synthetic_wfm(a, _square_wave_codes(4000, 8), dt=1e-9)
    _write_synthetic_wfm(b, _square_wave_codes(4000, 64), dt=1e-9)
    pr = probe_mod.probe([a, b])
    assert pr.kind == "wfm" and pr.analog and pr.two_file
    assert pr.sample_rate_hz == pytest.approx(1e9)
    assert pr.duration_s == pytest.approx(4000e-9)


def test_analog_csv(tmp_path):
    path = tmp_path / "scope.csv"
    rows = ["Model,MSO58", "Sample Interval,1e-09", "TIME,CH1,CH2"]
    rows += [f"{i * 1e-9:.12f},{1.0 if (i // 4) % 2 else 0.0},{1.0 if (i // 32) % 2 else 0.0}"
             for i in range(3000)]
    path.write_text("\n".join(rows) + "\n")
    pr = probe_mod.probe([str(path)])
    assert pr.kind == "analog_csv" and pr.analog
    assert [c.label for c in pr.channels] == ["CH1", "CH2"]
    assert pr.sample_rate_hz == pytest.approx(1e9, rel=1e-3)
    assert pr.duration_s == pytest.approx(2999e-9, rel=1e-3)


@pytest.mark.parametrize("names, match", [
    (["one.wfm"], "BOTH files"),
    (["a.sal", "b.sal"], "one capture"),
    (["x.txt"], "not a capture format"),
    (["a.bin", "b.bin", "c.sal"], "and nothing else"),       # not quietly the .bin pair
    (["a.wfm", "b.bin"], "and nothing else"),
])
def test_a_pick_that_is_not_one_capture_says_so(tmp_path, names, match):
    paths = []
    for n in names:
        (tmp_path / n).write_bytes(b"")
        paths.append(str(tmp_path / n))
    with pytest.raises(ValueError, match=match):
        probe_mod.probe(paths)


def test_a_visualizer_config_csv_is_not_a_capture(tmp_path):
    p = tmp_path / "config.csv"
    p.write_text("AppVersion,1.0\nNumColumns,16\n")
    with pytest.raises(ValueError, match="Visualizer config CSV"):
        probe_mod.probe([str(p)])


# ---- a window, in every format -------------------------------------------------------------

def _files(demo, tmp_path):
    sal = str(tmp_path / "w.sal")
    demo.export_sal(sal)
    csv = str(tmp_path / "w.csv")
    demo.export_csv(csv)
    vcd = str(tmp_path / "w.vcd")
    _write_vcd(vcd, demo.capture, timescale="2ns")
    b0, b1 = str(tmp_path / "w0.bin"), str(tmp_path / "w1.bin")
    saleae_binary.write_capture(demo.capture, b0, b1)
    rate = demo.capture.sample_rate_hz
    return {
        "sal": lambda w: Session.from_sal(sal, 0, 1, window=w),
        "digital_csv": lambda w: Session.from_digital_csv(csv, 1, 2, window=w),
        "vcd": lambda w: Session.from_vcd(vcd, "!", "%", window=w),
        "saleae_binary": lambda w: Session.from_saleae_binary(b0, b1, rate, window=w),
    }


@pytest.mark.parametrize("kind", ["sal", "digital_csv", "vcd", "saleae_binary"])
def test_every_format_decodes_a_window_and_reopens_it(demo, tmp_path, kind):
    make = _files(demo, tmp_path)[kind]
    whole = make(None)
    last = int(whole.capture.duration_s * whole.capture.sample_rate_hz)
    win = (last // 3, 2 * last // 3)
    part = make(win)
    assert part.source["window"] == list(win)
    assert _same_capture(part.capture, whole.capture.subcapture(*win))
    again = Session.from_source(part.source)                 # as a workspace reopens it
    assert _same_capture(again.capture, part.capture)


# ---- the pair offered ------------------------------------------------------------------

def _counts(*edges):
    return probe_mod.CaptureProbe("sal", ["/x.sal"], "x",
                                  [probe_mod.ProbedChannel(i, f"Ch {i}", e)
                                   for i, e in enumerate(edges)])


def test_two_links_at_different_clock_rates_pair_by_their_wiring():
    # Link 2's clock runs at twice Link 1's: no ratio of counts separates Link 1's clock
    # from a data line, but the adjacent pairs read as Links (each busier than any quiet).
    pr = _counts(162_979, 48_237, 326_444, 68_712)          # the two-Link fixture's counts
    assert pr.busiest_pair() == (0, 1)
    assert pr.busiest_pair(among=[2, 3]) == (2, 3)
    assert _counts(48_237, 162_979).busiest_pair() == (1, 0)   # data wired first


def test_clocks_wired_side_by_side_fall_back_to_the_counts():
    # CLK1, CLK2, D1, D2: the adjacent pairs are not Links (two clocks, two quiet lines).
    pr = _counts(400, 400, 8, 5)
    clock, data = pr.busiest_pair()
    assert pr.channels[clock].edges == 400 and pr.channels[data].edges in (8, 5)


def test_a_window_in_seconds_takes_the_nearest_samples_and_keeps_its_last():
    # A .wfm's rate is 1/dt, here 999999999.9999999 Hz: truncating 1 µs gave sample 999.
    rate = 1 / 1e-9
    assert Session.window_samples((0.0, 1e-6), rate) == (0, 1001)
    assert Session.window_samples((2.5e-7, 5e-7), 1e9) == (250, 501)
    # The edge AT the window's end is in it (Capture.subcapture's range is half-open).
    from swi3s_studio.ingest.capture import Capture
    cap = Capture(clock_edges=np.array([100, 200, 300], dtype=np.uint64),
                  data_edges=np.array([150], dtype=np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000)
    part = cap.subcapture(*Session.window_samples((50e-6, 300e-6), 1e6))
    assert part.clock_edges.tolist() == [50, 150, 250]


# ---- a probe is bounded, and runs off the GUI thread -----------------------------------------

def test_a_csv_scan_counts_the_same_in_any_batch_size(demo, tmp_path, monkeypatch):
    from swi3s_studio.ingest import digital_csv
    path = str(tmp_path / "c.csv")
    demo.export_csv(path)
    whole = digital_csv.scan(path)
    for chunk in (7, 1000, 4096):                        # transitions across every seam
        monkeypatch.setattr(digital_csv, "_SCAN_CHUNK", chunk)
        assert digital_csv.scan(path) == whole
    cap = Session.from_digital_csv(path, 1, 2).capture
    assert whole[0] == [cap.clock_edges.size, cap.data_edges.size]
    assert digital_csv.scan(path, max_rows=10)[0] != whole[0]   # a prefix, as documented


def test_the_window_probes_off_the_gui_thread_behind_a_busy_dialog(monkeypatch, tmp_path):
    import threading
    import time

    from PySide6.QtWidgets import QApplication, QProgressDialog

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    monkeypatch.setattr(MainWindow, "_event_loop_live", True)
    seen = {}

    def slow(paths):
        seen["thread"] = threading.current_thread() is not threading.main_thread()
        time.sleep(0.6)
        return "probed"
    monkeypatch.setattr(probe_mod, "probe", slow)
    shown = []
    real_show = QProgressDialog.show
    monkeypatch.setattr(QProgressDialog, "show", lambda d: (shown.append(d.labelText()),
                                                            real_show(d)))
    assert win._probe_files([str(tmp_path / "big.csv")]) == "probed"
    assert seen["thread"] and shown and "big.csv" in shown[0]

    def broken(paths):
        raise ValueError("not a capture")
    monkeypatch.setattr(probe_mod, "probe", broken)
    with pytest.raises(ValueError, match="not a capture"):
        win._probe_files([str(tmp_path / "x.csv")])


def test_a_csv_scan_finds_the_finest_spacing_across_a_batch_seam(tmp_path, monkeypatch):
    from swi3s_studio.ingest import digital_csv
    path = tmp_path / "seam.csv"
    rows = ["Time [s],CLK,DAT", "0.0,0,0", "1e-6,1,0", "2e-6,0,1", "2.001e-6,1,1"]
    path.write_text("\n".join(rows) + "\n")
    monkeypatch.setattr(digital_csv, "_SCAN_CHUNK", 3)   # the 1 ns step spans two batches
    assert digital_csv.scan(str(path)) == ([3, 1], 1_000_000_000)



def test_a_capture_with_a_channel_named_like_a_register_is_a_capture(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text("Time [s],CLK_REG,DATA\n0.0,0,0\n1e-6,1,0\n2e-6,0,1\n3e-6,1,1\n")
    pr = probe_mod.probe([str(path)])
    assert pr.kind == "digital_csv" and [c.label for c in pr.channels] == ["CLK_REG", "DATA"]


def test_an_odd_channel_out_means_the_file_is_not_wired_in_pairs():
    # TRIG, CLK, DATA: pairing 0/1 would offer the trigger as data.
    pr = _counts(4, 20_000, 3_000)
    assert pr.busiest_pair() == (1, 2)
    assert _counts(9_000, 900, 8_800, 700, 5).busiest_pair() in ((0, 1),)
