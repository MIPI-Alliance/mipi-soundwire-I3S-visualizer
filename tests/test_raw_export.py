"""Round-trip tests for the workaround data-out paths (ingest.raw_export):
per-channel .bin and Logic-2 digital CSV must reconstruct the same Capture.

Run: python3 -m pytest tests/test_raw_export.py -q
"""
import os
import tempfile

import numpy as np
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
        try:
            raw_export.export_bin(cap, os.path.join(d, "x.bin"),
                                  clock_channel=1, data_channel=1)
            assert False, "expected ValueError"
        except ValueError:
            pass
