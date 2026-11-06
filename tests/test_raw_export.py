"""Round-trip tests for the workaround data-out paths (ingest.raw_export):
per-channel .bin and Logic-2 digital CSV must reconstruct the same Capture.

Run: python3 -m pytest tests/test_raw_export.py -q
"""
import os
import tempfile

import numpy as np
import pytest
import swi3score

from swi3s_studio.ingest import digital_csv, raw_export, saleae_binary
from swi3s_studio.ingest.transitions import build_capture_from_levels


def _assert_edges_equal(cap, cap2):
    np.testing.assert_array_equal(np.asarray(cap2.clock_edges, dtype=np.int64),
                                  np.asarray(cap.clock_edges, dtype=np.int64))
    np.testing.assert_array_equal(np.asarray(cap2.data_edges, dtype=np.int64),
                                  np.asarray(cap.data_edges, dtype=np.int64))
    assert bool(cap2.initial_clock) == bool(cap.initial_clock)
    assert bool(cap2.initial_data) == bool(cap.initial_data)


def test_bin_export_roundtrip():
    cap = build_capture_from_levels(swi3score.make_demo_levels(64))
    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, "capture.bin")
        written = raw_export.export_bin(cap, base, clock_channel=0, data_channel=1)
        assert [os.path.basename(p) for p in written] == \
            ["capture-digital-0.bin", "capture-digital-1.bin"]
        cap2 = saleae_binary.load_capture(written[0], written[1], cap.sample_rate_hz)
    _assert_edges_equal(cap, cap2)
    assert int(cap2.sample_rate_hz) == int(cap.sample_rate_hz)


def test_csv_export_roundtrip():
    cap = build_capture_from_levels(swi3score.make_demo_levels(64))
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "capture.csv")
        raw_export.export_csv(cap, p, clock_channel=0, data_channel=1)
        # Header + first data row sanity.
        with open(p, encoding="utf-8") as f:
            header = f.readline().strip()
        assert header == "Time [s],Channel 0,Channel 1"
        # column 0 = Time, 1 = clock, 2 = data (digital_csv is 1-based on channels)
        cap2 = digital_csv.load_capture(p, clock_col=1, data_col=2,
                                        sample_rate_hz=cap.sample_rate_hz)
    _assert_edges_equal(cap, cap2)


def test_bin_export_rejects_same_channel():
    cap = build_capture_from_levels(swi3score.make_demo_levels(8))
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError, match=r"clock_channel and data_channel must differ "
                                             r"\(both 1\)"):
            raw_export.export_bin(cap, os.path.join(d, "x.bin"),
                                  clock_channel=1, data_channel=1)


def test_csv_export_refuses_two_edges_of_one_line_at_one_sample(tmp_path):
    from swi3s_studio.ingest.capture import Capture
    cap = Capture(clock_edges=[10, 20, 20, 30, 40], data_edges=[15], initial_clock=False,
                  initial_data=False, sample_rate_hz=1_000_000)
    p = tmp_path / "dup.csv"
    with pytest.raises(ValueError, match="two edges at sample 20"):
        raw_export.export_csv(cap, str(p))
    assert not p.exists()
    raw_export.export_csv(cap, str(p), include_clock=False)        # data alone is clean
    assert p.read_text(encoding="utf-8").splitlines() == [
        "Time [s],Channel 1", "0,0", "1.5e-05,1"]                   # the data edge at sample 15



def test_a_failed_raw_export_keeps_the_previous_files(tmp_path, monkeypatch):
    cap = build_capture_from_levels(swi3score.make_demo_levels(64))
    csv_path = tmp_path / "capture.csv"
    bin_base = str(tmp_path / "capture.bin")
    bin_path = tmp_path / "capture-digital-0.bin"
    csv_path.write_bytes(b"previous csv")
    bin_path.write_bytes(b"previous bin")

    def fail(*args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="disk full"):
            raw_export.export_csv(cap, str(csv_path), clock_channel=0, data_channel=1)
        with pytest.raises(OSError, match="disk full"):
            raw_export.export_bin(cap, bin_base, clock_channel=0, data_channel=1)
    assert csv_path.read_bytes() == b"previous csv"
    assert bin_path.read_bytes() == b"previous bin"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["capture-digital-0.bin", "capture.csv"]
    raw_export.export_csv(cap, str(csv_path), clock_channel=0, data_channel=1)
    raw_export.export_bin(cap, bin_base, clock_channel=0, data_channel=1)
    assert csv_path.read_bytes().startswith(b"Time [s]")
    assert bin_path.read_bytes().startswith(b"<SALEAE>")
