"""Every loader's memory guard prices the PEAK it reaches, measured.

Each test builds a synthetic file in a temporary directory, loads it under tracemalloc,
and checks the peak against the figure the loader's own guard compares with the budget
(and the Open Capture probe shows). A guard below the real peak admits a load it exists
to refuse. Each loader also refuses, before reading, when that figure is over budget.

tracemalloc sees NumPy's allocations and Python objects alike, so these are exact
figures, not RSS samples.
"""
import json
import os
import pathlib
import tracemalloc
import zipfile

import numpy as np
import pytest
from test_analog_import import _write_synthetic_wfm
from test_vcd import _write_vcd

from swi3s_studio.ingest import (
    analog,
    analog_csv,
    digital_csv,
    probe,
    raw_export,
    saleae_binary,
    saleae_sal,
    vcd,
    wfm,
)
from swi3s_studio.ingest.capture import Capture


def _peak(fn):
    tracemalloc.start()
    try:
        out = fn()
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return out, peak


def _edge_capture(n, seed=0):
    rng = np.random.default_rng(seed)
    ce = np.cumsum(rng.integers(10, 13, n)).astype(np.uint64)
    de = np.sort(rng.choice(ce + 1, size=n // 2, replace=False)).astype(np.uint64)
    return Capture(ce, de, False, False, 500_000_000)


# ---- digital CSV ------------------------------------------------------------------------

@pytest.mark.parametrize("unsorted", [False, True])
def test_a_digital_csv_peaks_within_its_prediction(tmp_path, monkeypatch, unsorted):
    # A small batch, so the per-row term (what grows with the file) dominates the peak.
    monkeypatch.setattr(digital_csv, "_ROW_CHUNK", 5_000)
    cap = _edge_capture(200_000)
    path = tmp_path / "c.csv"
    raw_export.export_csv(cap, str(path))
    if unsorted:
        lines = path.read_text().splitlines()
        lines[5], lines[6] = lines[6], lines[5]
        path.write_text("\n".join(lines) + "\n")
    with open(path, "rb") as f:
        sample = f.read(64 << 10)
    rows = digital_csv._predict_rows(str(path), 3, sample)
    predicted = digital_csv.predict_peak_bytes(rows, 3, sample, ascending=not unsorted)
    got, peak = _peak(lambda: digital_csv.load_capture(str(path), 1, 2, max_bytes=0))
    assert np.array_equal(got.clock_edges, cap.clock_edges)
    assert peak <= predicted, f"peak {peak / 1e6:.1f} MB over the {predicted / 1e6:.1f} MB guard"


def test_the_digital_csv_probe_shows_the_loaders_figure(tmp_path):
    path = str(tmp_path / "c.csv")
    raw_export.export_csv(_edge_capture(5_000), path)
    with open(path, "rb") as f:
        sample = f.read(64 << 10)
    rows = digital_csv._predict_rows(path, 3, sample)
    assert probe.probe([path]).cost_all_bytes == digital_csv.predict_peak_bytes(rows, 3, sample)
    with pytest.raises(digital_csv.CsvTooLargeError):
        digital_csv.load_capture(path, 1, 2, max_bytes=probe.probe([path]).cost_all_bytes - 1)
    assert digital_csv.load_capture(path, 1, 2,
                                    max_bytes=probe.probe([path]).cost_all_bytes).clock_edges.size


# ---- analog CSV and .wfm ----------------------------------------------------------------

def _scope_csv(path, n):
    t = np.arange(n) * 1e-9 - 1e-6
    clk = np.where((np.arange(n) // 4) % 2, 1.0, -1.0)
    dat = np.where((np.arange(n) // 40) % 2, 0.9, -0.9)
    with open(path, "w") as f:
        f.write("Model,MSO58,,Model,MSO58\nSample Interval,1e-09,,Sample Interval,1e-09\n"
                "\nTIME,CH1,CH2\n")
        f.write("\n".join(f"{t[i]:.6e},{clk[i]:.4e},{dat[i]:.4e}" for i in range(n)) + "\n")


def test_an_analog_csv_peaks_within_its_prediction(tmp_path, monkeypatch):
    monkeypatch.setattr(analog_csv, "_BATCH_ROWS", 5_000)
    path = str(tmp_path / "scope.csv")
    _scope_csv(path, 200_000)
    predicted = analog_csv.predict_peak_bytes(path)

    def load():
        parsed = analog_csv.read_analog_csv(path, max_bytes=0)
        ch = parsed["channels"]
        return analog.capture_from_analog(parsed["time"], ch["CH1"], ch["CH2"],
                                          parsed["sample_rate_hz"] or None)
    cap, peak = _peak(load)
    assert cap.clock_edges.size > 10_000
    assert peak <= predicted, f"peak {peak / 1e6:.1f} MB over the {predicted / 1e6:.1f} MB guard"
    assert probe.probe([path]).cost_all_bytes == predicted


def test_an_analog_csv_over_budget_is_refused_before_it_is_read(tmp_path):
    path = str(tmp_path / "scope.csv")
    _scope_csv(path, 2_000)
    with pytest.raises(analog.AnalogTooLargeError) as caught:
        analog_csv.read_analog_csv(path, max_bytes=1)
    assert caught.value.points > 1_000 and "GB" in str(caught.value)
    assert analog_csv.read_analog_csv(path, max_bytes=0)["time"].size == 2_000


def test_a_parsed_analog_csv_is_unchanged_by_its_batches(tmp_path, monkeypatch):
    path = str(tmp_path / "scope.csv")
    _scope_csv(path, 3_001)
    whole = analog_csv.read_analog_csv(path, max_bytes=0)
    for rows in (1, 7, 1_000, 3_001):
        monkeypatch.setattr(analog_csv, "_BATCH_ROWS", rows)
        got = analog_csv.read_analog_csv(path, max_bytes=0)
        assert np.array_equal(got["time"], whole["time"]), rows
        assert all(np.array_equal(got["channels"][k], whole["channels"][k])
                   for k in whole["channels"]), rows
        assert got["sample_rate_hz"] == whole["sample_rate_hz"]


def _wfm_pair(tmp_path, n):
    a, b = str(tmp_path / "ch1.wfm"), str(tmp_path / "ch2.wfm")
    _write_synthetic_wfm(a, np.where((np.arange(n) // 4) % 2, 1000, -1000), v_scale=1e-3)
    _write_synthetic_wfm(b, np.where((np.arange(n) // 40) % 2, 900, -900), v_scale=1e-3)
    return a, b


def test_a_wfm_pair_peaks_within_its_prediction(tmp_path):
    n = 2_000_000
    a, b = _wfm_pair(tmp_path, n)

    def load():
        clk, dat = wfm.read_wfm(a, max_bytes=0), wfm.read_wfm(b, max_bytes=0)
        return analog.capture_from_analog(clk["time"], clk["volts"], dat["volts"], None,
                                          auto_clock=True)
    cap, peak = _peak(load)
    assert cap.clock_edges.size == n // 4 - 1
    predicted = wfm.predict_pair_peak_bytes(n)
    assert peak <= predicted, f"peak {peak / 1e6:.1f} MB over the {predicted / 1e6:.1f} MB guard"
    assert probe.probe([a, b]).cost_all_bytes == predicted


def test_a_wfm_over_budget_is_refused_before_its_curve_is_read(tmp_path):
    a, _b = _wfm_pair(tmp_path, 4_000)
    with pytest.raises(analog.AnalogTooLargeError):
        wfm.read_wfm(a, max_bytes=1)


def test_a_wfm_pair_is_priced_once_before_either_curve_is_read(tmp_path, monkeypatch):
    # Free memory falls by what the process holds: after the first file, its time axis
    # and volts are resident, and the pair's peak already counts them.
    n = 200_000
    a, b = _wfm_pair(tmp_path, n)
    predicted = wfm.predict_pair_peak_bytes(n)
    free = int(predicted / saleae_sal._MEM_HEADROOM) + 8 * n
    monkeypatch.setattr(saleae_sal, "available_memory_bytes",
                        lambda: free - tracemalloc.get_traced_memory()[0])
    tracemalloc.start()
    try:
        assert probe.probe([a, b]).cost_all_bytes == predicted <= saleae_sal.memory_budget()
        clk, dat = wfm.read_wfm_pair(a, b)                 # the pair the dialog called fitting
        assert clk["volts"].size == dat["volts"].size == n
        del clk, dat
        first = wfm.read_wfm(a)
        with pytest.raises(analog.AnalogTooLargeError):    # the second file, priced alone
            wfm.read_wfm(b)
        del first
    finally:
        tracemalloc.stop()


def test_a_wfm_pair_over_budget_is_refused_before_either_curve_is_read(tmp_path, monkeypatch):
    a, b = _wfm_pair(tmp_path, 4_000)
    monkeypatch.setattr(wfm, "read_wfm", lambda *a, **k: pytest.fail("a curve was read"))
    with pytest.raises(analog.AnalogTooLargeError) as caught:
        wfm.read_wfm_pair(a, b, max_bytes=wfm.predict_pair_peak_bytes(4_000) - 1)
    assert a in str(caught.value) and b in str(caught.value)


def test_the_two_files_of_one_acquisition_share_a_read_only_time_axis(tmp_path):
    a, b = _wfm_pair(tmp_path, 4_000)
    clk, dat = wfm.read_wfm(a), wfm.read_wfm(b)
    assert clk["time"] is dat["time"] and not clk["time"].flags.writeable
    assert np.array_equal(clk["time"], np.arange(4_000, dtype=np.float64) * 1e-9 + 0.0)


def test_the_analog_front_end_matches_the_whole_array_rules():
    # The chunked Schmitt fill, time-order check and median step against the
    # whole-array forms they replace, across chunk seams.
    rng = np.random.default_rng(3)
    v = np.repeat(rng.choice([0.0, 0.5, 1.0], 50_000), rng.integers(1, 4, 50_000))
    hi, lo = 0.6, 0.4
    decided = np.full(v.shape, -1, dtype=np.int8)
    decided[v > hi], decided[v < lo] = 1, 0
    if decided[0] < 0:
        decided[0] = 1 if v[0] > 0.5 else 0
    idx = np.where(decided >= 0, np.arange(v.size), 0)
    np.maximum.accumulate(idx, out=idx)
    expect = decided[idx].astype(np.uint8)
    old_chunk = analog._CHUNK
    try:
        for chunk in (7, 1000, old_chunk):
            analog._CHUNK = chunk
            assert np.array_equal(analog.schmitt_levels(v, hi, lo), expect), chunk
            t = np.cumsum(rng.choice([0.0, 1e-9, 2e-9], 10_001))
            d = np.diff(t)
            assert analog._median_positive_step(t) == float(np.median(d[d > 0]))
            assert analog._ascending(t) and not analog._ascending(t[::-1])
    finally:
        analog._CHUNK = old_chunk


# ---- Saleae .bin pair -------------------------------------------------------------------

def _bin_pair(tmp_path, n):
    e = np.cumsum(np.full(n, 11, dtype=np.uint64)).astype(np.uint64)
    a, b = str(tmp_path / "clk.bin"), str(tmp_path / "dat.bin")
    saleae_binary.write_channel(a, False, e / 500e6)
    saleae_binary.write_channel(b, False, e[::3] / 500e6)
    return a, b, e


@pytest.mark.parametrize("n", [10_000, 1_500_000])
def test_a_bin_pair_peaks_within_its_prediction(tmp_path, n):
    # Above and below one _TIMES_CHUNK of the conversion.
    a, b, e = _bin_pair(tmp_path, n)
    predicted = saleae_binary.predict_peak_bytes(a, b)
    cap, peak = _peak(lambda: saleae_binary.load_capture(a, b, 500_000_000, max_bytes=0))
    assert np.array_equal(cap.clock_edges, e)
    assert peak <= predicted, f"peak {peak / 1e6:.1f} MB over the {predicted / 1e6:.1f} MB guard"
    assert probe.probe([a, b]).cost_all_bytes == predicted


@pytest.mark.parametrize("count", [0, 1_000])
def test_a_bin_with_a_low_header_count_is_priced_by_what_is_read(tmp_path, count):
    """read_channel reads the whole file whatever its header counts, so a low or zero count
    does not lower the price below the bytes read."""
    import struct
    a, b, _e = _bin_pair(tmp_path, 1_500_000)
    for path in (a, b):
        raw = bytearray(pathlib.Path(path).read_bytes())
        struct.pack_into("<Q", raw, saleae_binary._HEADER.size - 8, count)
        pathlib.Path(path).write_bytes(bytes(raw))
    predicted = saleae_binary.predict_peak_bytes(a, b)

    def load():
        try:
            saleae_binary.load_capture(a, b, 500_000_000, max_bytes=0)
        except ValueError:
            pass                                       # refused after reading: still a peak
    _cap, peak = _peak(load)
    assert peak <= predicted, f"peak {peak / 1e6:.1f} MB over the {predicted / 1e6:.1f} MB guard"
    assert predicted >= os.path.getsize(a) + os.path.getsize(b)


def test_a_bin_pair_over_budget_is_refused_before_it_is_read(tmp_path, monkeypatch):
    a, b, e = _bin_pair(tmp_path, 4_000)
    predicted = saleae_binary.predict_peak_bytes(a, b)
    assert saleae_binary.load_capture(a, b, 500_000_000,
                                      max_bytes=predicted).clock_edges.size == e.size
    monkeypatch.setattr(saleae_binary, "read_channel",
                        lambda p: pytest.fail("a file was read"))
    with pytest.raises(saleae_binary.BinTooLargeError) as caught:
        saleae_binary.load_capture(a, b, 500_000_000, max_bytes=predicted - 1)
    assert isinstance(caught.value, MemoryError) and caught.value.predicted == predicted
    with pytest.raises(saleae_binary.BinTooLargeError):    # the default: free memory
        monkeypatch.setattr(saleae_sal, "available_memory_bytes", lambda: predicted)
        saleae_binary.load_capture(a, b, 500_000_000)


def test_a_bin_file_that_is_not_v0_is_charged_its_size(tmp_path):
    a, _b, _e = _bin_pair(tmp_path, 1_000)
    v3 = tmp_path / "v3.bin"
    v3.write_bytes(saleae_binary.build_channel_v3(False, np.arange(1, 5_000, 3,
                                                                   dtype=np.uint64)))
    both = saleae_binary.predict_peak_bytes(a, str(v3))
    assert both - saleae_binary.predict_peak_bytes(a, a) >= v3.stat().st_size - 1_000 * 16


# ---- VCD --------------------------------------------------------------------------------

def test_a_vcd_peaks_within_its_bytes_per_edge(tmp_path):
    cap = _edge_capture(300_000)
    path = str(tmp_path / "c.vcd")
    _write_vcd(path, cap, timescale="2ns")
    got, peak = _peak(lambda: vcd.load_capture(path, "!", "%", max_bytes=0))
    edges = got.clock_edges.size + got.data_edges.size
    assert np.array_equal(got.clock_edges, cap.clock_edges)
    assert peak <= edges * vcd.BYTES_PER_EDGE + (1 << 20), f"{peak / edges:.1f} B/edge"


def test_a_vcd_past_its_budget_is_stopped_while_it_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(vcd, "_CHECK_EVERY", 100)
    path = str(tmp_path / "c.vcd")
    _write_vcd(path, _edge_capture(5_000), timescale="2ns")
    with pytest.raises(vcd.VcdTooLargeError) as caught:
        vcd.load_capture(path, "!", "%", max_bytes=1_000)
    assert 0 < caught.value.edges < 7_500 and caught.value.held > 1_000


def test_a_long_vcd_probe_scales_its_counted_prefix(tmp_path, monkeypatch):
    path = str(tmp_path / "c.vcd")
    cap = _edge_capture(20_000)
    _write_vcd(path, cap, timescale="2ns")
    real = vcd.transition_counts_until
    monkeypatch.setattr(vcd, "transition_counts_until",
                        lambda p, ids, n: real(p, ids, 5_000))     # a prefix of the dump
    pr = probe.probe([path])
    assert pr.channels[0].approx
    edges = cap.clock_edges.size + cap.data_edges.size
    assert pr.cost_all_bytes == pytest.approx(edges * vcd.BYTES_PER_EDGE, rel=0.1)


# ---- .sal windows -----------------------------------------------------------------------

def _write_sal(path, blobs, rate=500_000_000, compress=False):
    meta = {"data": {"legacySettings": {"sampleRate": {"digital": rate}}},
            "binData": [{"type": "Digital", "file": f"digital-{i}.bin", "deviceChannel": i}
                        for i in range(len(blobs))]}
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", mode) as z:
        z.writestr("meta.json", json.dumps(meta))
        for i, b in enumerate(blobs):
            z.writestr(f"digital-{i}.bin", b)
    return str(path)


def test_a_window_of_a_one_block_channel_peaks_within_the_guard(tmp_path):
    # One block holds the whole channel: the window decode must not read it whole.
    rng = np.random.default_rng(5)
    ce = np.cumsum(rng.integers(10, 12, 1_000_000)).astype(np.uint64)
    de = np.cumsum(rng.integers(30, 36, 300_000)).astype(np.uint64)
    path = _write_sal(tmp_path / "c.sal", [saleae_binary.build_channel_v3(False, ce),
                                           saleae_binary.build_channel_v3(False, de)])
    model = saleae_sal.window_model(path, [0, 1])
    span = model.span_samples
    w = (span // 10, span // 10 + span // 100)
    predicted = saleae_sal.estimate_cost(path, [0, 1]).window_peak_bytes(model, w)
    cap, peak = _peak(lambda: saleae_sal.load_capture(path, 0, 1, window=w, max_bytes=0))
    sel = ce[(ce > w[0]) & (ce < w[1])] - w[0]
    assert np.array_equal(cap.clock_edges, sel)
    assert peak <= predicted, f"peak {peak / 1e6:.1f} MB over the {predicted / 1e6:.1f} MB guard"
    assert saleae_sal.load_capture(path, 0, 1, window=w, max_bytes=predicted).clock_edges.size


@pytest.fixture
def demo_sal(tmp_path):
    from swi3s_studio.session import Session
    path = str(tmp_path / "demo.sal")
    Session.from_demo(600, cold_start=True).export_sal(path)
    return path


def test_the_probes_window_cost_is_the_loaders_guard(demo_sal):
    from swi3s_studio.session import Session
    pr = probe.probe([demo_sal])
    assert pr.windowed and pr.window_model is not None
    rate = pr.sample_rate_hz
    for frac in (0.01, 0.3, 0.9):
        t1 = pr.duration_s * frac
        w = Session.window_samples((0.0, t1), rate)
        cost = pr.cost_bytes(0.0, t1)
        saleae_sal.load_capture(demo_sal, 0, 1, window=w, max_bytes=cost)    # admitted
        with pytest.raises(saleae_sal.SalTooLargeError):
            saleae_sal.load_capture(demo_sal, 0, 1, window=w, max_bytes=cost - 1)


def test_the_largest_window_the_probe_offers_is_admitted(demo_sal):
    from swi3s_studio.session import Session
    pr = probe.probe([demo_sal])
    budget = pr.cost_all_bytes // 2
    longest = pr.largest_window_s(budget)
    assert longest is not None and 0 < longest < pr.duration_s
    w = Session.window_samples((0.0, longest), pr.sample_rate_hz)
    saleae_sal.load_capture(demo_sal, 0, 1, window=w, max_bytes=budget)
    assert pr.cost_bytes(0.0, longest) <= budget
    assert pr.cost_bytes(0.0, longest * 1.01) > budget


def test_a_window_refused_says_what_the_window_needs(demo_sal):
    model = saleae_sal.window_model(demo_sal, [0, 1])
    w = (0, model.span_samples // 2)
    with pytest.raises(saleae_sal.SalTooLargeError) as caught:
        saleae_sal.load_capture(demo_sal, 0, 1, window=w, max_bytes=1)
    err = caught.value
    assert err.window == w
    assert err.peak == err.cost.window_peak_bytes(model, w) == model.peak_bytes(*w)
    assert "shorter window" in str(err) and "choose a window" not in str(err).lower()
    with pytest.raises(saleae_sal.SalTooLargeError) as whole:
        saleae_sal.load_capture(demo_sal, 0, 1, max_bytes=1)
    assert whole.value.window is None and whole.value.peak == whole.value.cost.est_peak_bytes


def test_a_small_window_of_a_compressed_capture_inflates_it_about_once(demo_sal, monkeypatch):
    with zipfile.ZipFile(demo_sal) as z:
        assert {i.compress_type for i in z.infolist()} == {zipfile.ZIP_DEFLATED}
        blobs = sum(i.file_size for i in z.infolist() if i.filename.startswith("digital"))
    inflated = [0]
    real = zipfile.ZipExtFile._read1

    def counting(self, n):
        data = real(self, n)
        inflated[0] += len(data)
        return data
    monkeypatch.setattr(zipfile.ZipExtFile, "_read1", counting)
    for max_bytes in (0, None):
        inflated[0] = 0
        saleae_sal.load_capture(demo_sal, 0, 1, window=(10, 1000), max_bytes=max_bytes)
        assert inflated[0] <= 1.2 * blobs, (
            f"max_bytes={max_bytes}: inflated {inflated[0] / blobs:.2f}x the channels")


def test_a_v0_channel_is_priced_whole_whatever_the_window(tmp_path):
    e = np.arange(100, 20_000, 7, dtype=np.uint64)
    v0 = tmp_path / "v0.bin"
    saleae_binary.write_channel(str(v0), False, e / 500e6)
    path = _write_sal(tmp_path / "v0.sal", [v0.read_bytes(), v0.read_bytes()])
    cost = saleae_sal.estimate_cost(path, [0, 1])
    assert not cost.streamable
    assert cost.window_peak_bytes(saleae_sal.window_model(path, [0, 1]), (0, 100)) \
        == cost.est_peak_bytes
    assert not probe.probe([path]).windowed
    with pytest.raises(saleae_sal.SalTooLargeError) as caught:
        saleae_sal.load_capture(path, 0, 1, window=(0, 100), max_bytes=1)
    assert not caught.value.streamed and "read whole" in str(caught.value)


def _idle_then_busy_sal(tmp_path, n_clock=400_000, idle=9_000_000_000):
    # Nothing for the first `idle` samples, then a clock every 20 samples and data on
    # every fourth clock: almost every edge sits in the last 0.1% of the capture.
    ce = (idle + 20 * np.arange(1, n_clock + 1, dtype=np.uint64)).astype(np.uint64)
    de = ce[::4] + np.uint64(1)
    end = int(ce[-1]) + 100

    def blob(edges):
        return saleae_binary.build_logic2_channel_v3(
            False, edges, sample_rate_hz=500e6, unix_ms=0, frac_ms=0.0, capture_end=end)
    return _write_sal(tmp_path / "busy.sal", [blob(ce), blob(de)]), ce, de, (idle, end)


def test_a_busy_window_of_a_mostly_idle_capture_is_priced_by_its_blocks(tmp_path):
    path, ce, de, w = _idle_then_busy_sal(tmp_path)
    model = saleae_sal.window_model(path, [0, 1])
    predicted = saleae_sal.estimate_cost(path, [0, 1]).window_peak_bytes(model, w)
    cap, peak = _peak(lambda: saleae_sal.load_capture(path, 0, 1, window=w, max_bytes=0))
    assert cap.clock_edges.size == ce.size and cap.data_edges.size == de.size
    assert peak <= predicted, f"peak {peak / 1e6:.1f} MB over the {predicted / 1e6:.1f} MB guard"
    # The window's share of the samples is 0.09%; its share of the edges is all of them.
    assert predicted >= 8 * (ce.size + de.size)
    with pytest.raises(saleae_sal.SalTooLargeError):
        saleae_sal.load_capture(path, 0, 1, window=w, max_bytes=peak // 2)
    # The idle stretch is charged only the first block of each channel, which reaches
    # into the burst.
    assert model.edge_bytes(0, w[0] // 2) < model.edge_bytes(*w) / 20


def test_the_probe_prices_a_busy_window_as_the_loader_does(tmp_path):
    from swi3s_studio.session import Session
    path, _ce, _de, w = _idle_then_busy_sal(tmp_path)
    pr = probe.probe([path])
    rate = pr.sample_rate_hz
    t0, t1 = w[0] / rate, pr.duration_s
    cost = pr.cost_bytes(t0, t1)
    win = Session.window_samples((t0, t1), rate)
    assert cost == pr.window_model.peak_bytes(*win)
    saleae_sal.load_capture(path, 0, 1, window=win, max_bytes=cost)          # admitted
    with pytest.raises(saleae_sal.SalTooLargeError):
        saleae_sal.load_capture(path, 0, 1, window=win, max_bytes=cost - 1)
    longest = pr.largest_window_s(cost - 1)        # from the start: stops short of the burst
    assert longest is not None and longest < pr.duration_s
    assert pr.cost_bytes(0.0, longest) <= cost - 1


def test_a_block_index_charges_at_least_the_blocks_a_window_overlaps(monkeypatch):
    monkeypatch.setattr(saleae_sal, "_INDEX_BUCKETS", 4)    # merged many times over
    rng = np.random.default_rng(11)
    spans = rng.integers(0, 50, 1_000)                      # zero-length blocks too
    ends = np.cumsum(spans)
    nbytes = rng.integers(0, 300, 1_000)
    starts = np.concatenate(([0], ends[:-1]))
    idx = saleae_sal._BlockIndex()
    exact = saleae_sal._BlockIndex()
    for e, c in zip(ends, nbytes):
        idx.add(int(e), int(c))
    idx.finish()
    monkeypatch.setattr(saleae_sal, "_INDEX_BUCKETS", 1 << 12)
    for e, c in zip(ends, nbytes):
        exact.add(int(e), int(c))
    exact.finish()
    assert idx.ends.size < 8 and exact.ends.size == 1_000
    assert idx.cum[-1] == exact.cum[-1] == nbytes.sum()
    for _ in range(2_000):
        s0 = int(rng.integers(-5, int(ends[-1]) + 5))
        s1 = s0 + int(rng.integers(0, 3_000))
        # What the window decode reads: every block ending after s0 and starting before s1.
        want = int(nbytes[(ends > s0) & (starts < s1)].sum()) if s1 > s0 else 0
        assert exact.bytes_overlapping(s0, s1) == want, (s0, s1)
        assert idx.bytes_overlapping(s0, s1) >= want, (s0, s1)
