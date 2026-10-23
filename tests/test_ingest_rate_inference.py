"""The sample rate of a capture stored as transition TIMES: digital CSV and v0 .bin.

Neither format stores the rate, and an export with one row (or one time) per transition
has no two timestamps closer than the shortest gap between edges, which on the demo
capture is 10 samples. The rate is the grid the timestamps sit on (timebase), so an
export reopened without a rate gives back the same rate and the same edges.

Everything here is synthetic: the demo capture and generated timestamps.
"""
import numpy as np
import pytest

from swi3s_studio.ingest import digital_csv, probe, raw_export, saleae_binary, timebase
from swi3s_studio.ingest.capture import Capture
from swi3s_studio.ingest.transitions import demo_capture


@pytest.fixture(scope="module")
def cap():
    c = demo_capture(300)
    assert c.sample_rate_hz == 500_000_000
    gaps = np.diff(np.unique(np.concatenate([c.clock_edges, c.data_edges])))
    assert int(gaps.min()) > 1, "the premise: no two edges one sample apart"
    return c


def _same_edges(a, b):
    return (np.array_equal(a.clock_edges, b.clock_edges)
            and np.array_equal(a.data_edges, b.data_edges)
            and a.initial_clock == b.initial_clock and a.initial_data == b.initial_data)


def test_a_csv_export_reopens_at_its_own_rate_with_the_same_edges(cap, tmp_path):
    path = str(tmp_path / "c.csv")
    raw_export.export_csv(cap, path)
    got = digital_csv.load_capture(path, 1, 2)
    assert got.sample_rate_hz == cap.sample_rate_hz
    assert _same_edges(got, cap)
    assert digital_csv.infer_sample_rate(path) == cap.sample_rate_hz
    pr = probe.probe([path])
    assert pr.sample_rate_hz == cap.sample_rate_hz          # what the dialog shows


def test_a_bin_export_reports_its_own_rate_and_reloads_the_same_edges(cap, tmp_path):
    written = raw_export.export_bin(cap, str(tmp_path / "c.bin"))
    rate = saleae_binary.infer_sample_rate(written)
    assert rate == cap.sample_rate_hz
    assert probe.probe(written).sample_rate_hz == cap.sample_rate_hz
    assert _same_edges(saleae_binary.load_capture(written[0], written[1], rate), cap)


def test_a_pretrigger_csv_printed_to_the_nanosecond_keeps_its_rate(tmp_path):
    # A Logic-style export: times from a negative pre-trigger origin, nine decimals.
    rate = 500_000_000
    e = np.cumsum(np.random.default_rng(4).integers(9, 31, 20_000)).astype(np.uint64)
    path = tmp_path / "logic.csv"
    lines = ["Time [s],Channel 0,Channel 1"]
    lvl = 0
    for s in e.tolist():
        lvl ^= 1
        lines.append(f"{s / rate - 0.0012:.9f},{lvl},0")
    path.write_text("\n".join(lines) + "\n")
    got = digital_csv.load_capture(str(path), 1, 2)
    assert got.sample_rate_hz == rate
    assert np.array_equal(got.clock_edges, (e[1:] - e[0]).astype(np.uint64))


@pytest.mark.parametrize("rate, gaps, digits", [
    (500_000_000, (10, 13), None),        # an edge export, exact float times
    (16_000_000, (5, 40), 9),             # 62.5 ns period, times printed to 1 ns
    (24_000_000, (1, 2), 9),              # one row per sample, 41.67 ns, printed to 1 ns
    (781_250, (3, 9), None),              # a rate that is not a whole kHz
    (500_000_000, (5000, 9000), None),    # a slow line: thousands of samples per gap
])
def test_the_rate_is_the_grid_every_time_sits_on(rate, gaps, digits):
    e = np.cumsum(np.random.default_rng(1).integers(*gaps, 50_000))
    t = e / rate
    if digits is not None:
        t = np.round(t, digits)
    assert timebase.rate_from_times(t) == rate


def test_times_on_a_coarser_grid_give_that_grid():
    # Every edge on an even sample: 250 MHz describes the capture exactly, edge for edge.
    e = np.cumsum(np.random.default_rng(2).integers(5, 20, 10_000)) * 2
    assert timebase.rate_from_times(e / 500e6) == 250_000_000


def test_too_few_times_give_no_rate():
    assert timebase.rate_from_times(np.array([1.0])) == 0
    assert timebase.rate_from_times(np.array([1.0, 1.0, np.nan])) == 0


def test_the_csv_scan_and_the_loader_agree_on_a_long_file(cap, tmp_path, monkeypatch):
    # Both read the first _RATE_ROWS timestamps; a small one puts the rest past it.
    monkeypatch.setattr(digital_csv, "_RATE_ROWS", 1000)
    path = str(tmp_path / "c.csv")
    raw_export.export_csv(cap, path)
    assert digital_csv.scan(path)[1] == digital_csv.load_capture(path, 1, 2).sample_rate_hz \
        == cap.sample_rate_hz


def test_an_unsorted_csv_reloads_the_same_as_its_sorted_rows(cap, tmp_path):
    path = tmp_path / "c.csv"
    raw_export.export_csv(cap, str(path))
    lines = path.read_text().splitlines()
    lines[5], lines[6] = lines[6], lines[5]               # one pair out of time order
    shuffled = tmp_path / "s.csv"
    shuffled.write_text("\n".join(lines) + "\n")
    got = digital_csv.load_capture(str(shuffled), 1, 2)
    assert got.sample_rate_hz == cap.sample_rate_hz
    assert _same_edges(got, cap)


def test_a_capture_with_one_sample_gaps_is_unchanged(tmp_path):
    # Edges one sample apart: the finest spacing IS the period.
    ce = np.arange(10, 4010, 2, dtype=np.uint64)
    de = ce + 1
    c = Capture(ce, de, False, True, 250_000_000)
    path = str(tmp_path / "c.csv")
    raw_export.export_csv(c, path)
    got = digital_csv.load_capture(path, 1, 2)
    assert got.sample_rate_hz == 250_000_000 and _same_edges(got, c)
