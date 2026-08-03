"""Tests for the analog-import / sub-capture / .sal-export Session wiring:
Session.from_wfm, Session.from_analog_csv, Session.locate_subcapture,
Session.export_sal, and the decoder_ready progress hook.

Everything here is SYNTHETIC — `.wfm` files are written by `_write_synthetic_wfm`
and analog CSVs by `_write_csv`, so the whole module runs on any machine. The four
tests that needed a locally-synced scope export (a WFM pair + the analog CSV of
the same acquisition) were dropped in 3.0.11: they skipped everywhere, so they never
actually guarded anything, and three of the four duplicated synthetic coverage above.
The one behaviour only they exercised — auto_clock orientation — is now
test_from_wfm_auto_clock_is_order_independent, built from a synthetic pair.

Run: PYTHONPATH="$PWD" python3 -m pytest tests/test_analog_import.py -q
"""
from __future__ import annotations

import os
import struct
import tempfile

import numpy as np
import pytest

from swi3s_studio.ingest.capture import Capture
from swi3s_studio.session import Session


def _write_csv(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def _write_synthetic_wfm(path: str, codes, dt: float = 1e-9, t0: float = 0.0,
                         v_scale: float = 1.0, v_offset: float = 0.0) -> None:
    """Minimal, valid Tektronix .wfm (version 3) file — just enough of the header
    fields wfm.read_wfm actually parses (see swi3s_studio/ingest/wfm.py's layout
    reference) to exercise Session.from_wfm without a real scope export."""
    codes = np.asarray(codes, dtype="<i2")
    bytes_per_point = 2
    curve_buffer_start = 0x346          # arbitrary, past every fixed-offset field used
    data_start = 0
    postcharge_start = codes.size * bytes_per_point
    buf = bytearray(curve_buffer_start + postcharge_start)
    struct.pack_into("<H", buf, 0x000, 0x0F0F)                  # little-endian BOM
    buf[0x002:0x002 + 8] = b":WFM#003"
    struct.pack_into("<B", buf, 0x00F, bytes_per_point)
    struct.pack_into("<i", buf, 0x010, curve_buffer_start)
    struct.pack_into("<i", buf, 0x07A, 2)                       # data_type: single-valued
    struct.pack_into("<d", buf, 0x0A8, v_scale)
    struct.pack_into("<d", buf, 0x0B0, v_offset)
    struct.pack_into("<i", buf, 0x0F0, 0)                       # v_format: int16
    struct.pack_into("<d", buf, 0x1E8, dt)
    struct.pack_into("<d", buf, 0x1F0, t0)
    struct.pack_into("<I", buf, 0x336, data_start)
    struct.pack_into("<I", buf, 0x33A, postcharge_start)
    buf[curve_buffer_start + data_start:curve_buffer_start + postcharge_start] = codes.tobytes()
    with open(path, "wb") as f:
        f.write(buf)


def _square_wave_codes(n: int, period: int, hi: int = 1000, lo: int = -1000):
    codes = np.full(n, lo, dtype="<i2")
    codes[(np.arange(n) // (period // 2)) % 2 == 1] = hi
    return codes


def test_capture_from_analog_rejects_mismatched_lengths():
    """Session.from_wfm reads the clock/data .wfm files independently, so a
    record-length mismatch is a real input error; it must raise a clear
    ValueError up front rather than silently misalign timestamps (data
    shorter than time) or crash deep in a closure (data longer)."""
    from swi3s_studio.ingest.analog import capture_from_analog
    time = np.linspace(0.0, 1.0, 100)
    clock_v = np.zeros(100)
    data_v = np.zeros(50)                            # deliberately short
    with pytest.raises(ValueError, match=r"length mismatch"):
        capture_from_analog(time, clock_v, data_v)


def test_from_wfm_rejects_mismatched_record_length():
    """from_wfm reads the two .wfm files independently and reuses ONLY the clock
    file's time axis for both channels (see Session.from_wfm) — so a mismatched
    pair (different record length) must be rejected up front with a WFM-specific
    error, not silently misalign the data edges against the wrong time base."""
    n = 4000
    codes = _square_wave_codes(n, period=20)
    short_codes = _square_wave_codes(n // 2, period=20)
    with tempfile.TemporaryDirectory() as d:
        clock_path = os.path.join(d, "clock.wfm")
        data_path = os.path.join(d, "data.wfm")
        _write_synthetic_wfm(clock_path, codes, dt=1e-9)
        _write_synthetic_wfm(data_path, short_codes, dt=1e-9)      # different record length
        with pytest.raises(ValueError, match=r"from_wfm.*disagree"):
            Session.from_wfm(clock_path, data_path)


def test_from_wfm_rejects_mismatched_time_base():
    """Same pair length, but a different dt (time base) — also a disagreement
    from_wfm must catch, since reusing the clock file's time axis for the data
    channel would silently mis-time every data edge."""
    n = 4000
    codes = _square_wave_codes(n, period=20)
    with tempfile.TemporaryDirectory() as d:
        clock_path = os.path.join(d, "clock.wfm")
        data_path = os.path.join(d, "data.wfm")
        _write_synthetic_wfm(clock_path, codes, dt=1e-9)
        _write_synthetic_wfm(data_path, codes, dt=2e-9)            # different sample rate
        with pytest.raises(ValueError, match=r"from_wfm.*disagree"):
            Session.from_wfm(clock_path, data_path)


def test_from_wfm_accepts_matching_pair():
    """A genuine same-acquisition pair (matching record length + time base) must
    still decode normally — the new from_wfm validation must not false-positive
    on the common case."""
    n = 4000
    clk_codes = _square_wave_codes(n, period=20)       # busier -> auto-picked as clock
    dat_codes = _square_wave_codes(n, period=200)
    with tempfile.TemporaryDirectory() as d:
        clock_path = os.path.join(d, "clock.wfm")
        data_path = os.path.join(d, "data.wfm")
        _write_synthetic_wfm(clock_path, clk_codes, dt=1e-9, v_scale=1.0 / 1000.0)
        _write_synthetic_wfm(data_path, dat_codes, dt=1e-9, v_scale=1.0 / 1000.0)
        s = Session.from_wfm(clock_path, data_path)
    assert s.capture.clock_edges.size > 0
    assert s.capture.data_edges.size > 0


def test_source_label_names_wfm_pair():
    """source_label() only checked source keys "clock"/"data"/"path"; a .wfm pair's
    source uses "clock_path"/"data_path" instead, so its window title used to fall
    through to the useless generic 'Capture'. Must name both files, like the
    two-file .bin/.sal labeling."""
    n = 4000
    codes = _square_wave_codes(n, period=20)
    with tempfile.TemporaryDirectory() as d:
        clock_path = os.path.join(d, "ch1_clock.wfm")
        data_path = os.path.join(d, "ch2_data.wfm")
        _write_synthetic_wfm(clock_path, codes, dt=1e-9)
        _write_synthetic_wfm(data_path, codes, dt=1e-9)
        s = Session.from_wfm(clock_path, data_path)
    label = s.source_label()
    assert "ch1_clock.wfm" in label and "ch2_data.wfm" in label


def test_auto_threshold_detects_low_duty_cycle_minority_level():
    """A channel idle at 3.0V with a brief dip to 2.4V has both the 10th and
    90th percentile land on the idle rail (the dip is far under 10% duty
    cycle), so `swing` computed from percentiles alone looks degenerate even
    though the line genuinely toggles. auto_threshold must fall back to the
    true min/max range for the degeneracy check, and schmitt_levels must then
    report the dip as real (non-empty) edges rather than none."""
    from swi3s_studio.ingest.analog import auto_threshold, schmitt_levels
    n = 4000
    v = np.full(n, 3.0)
    v[2000:2010] = 2.4                               # ~0.25% duty cycle dip
    hi, lo = auto_threshold(v)
    levels = schmitt_levels(v, hi, lo)
    edges = np.flatnonzero(np.diff(levels) != 0)
    assert edges.size > 0, (hi, lo)


def test_is_analog_csv_decides_by_values_not_header():
    """Digital vs analog is decided by the DATA VALUES (channels strictly 0/1 =
    digital; any other value = analog), not by the header/preamble text."""
    from swi3s_studio.ingest import analog_csv
    # Digital: 0/1 channels — even with a scientific-notation time column and a
    # non-'Time' header, it must read as digital.
    digital = _write_csv("Time,D0,D1\n-1.0e-06,0,1\n-9.0e-07,1,0\n-8.0e-07,1,1\n")
    # Analog: Tek preamble + TIME header + float volts.
    analog = _write_csv("Model,MSO58,,Model,MSO58\nSample Interval,1.6e-10\n\n"
                        "TIME,CH1,CH2\n-1e-6,0.023,-0.5\n-9e-7,1.02,0.98\n")
    # A digital-looking header but analog values -> analog (values win).
    disguised = _write_csv("Time [s], Channel 0, Channel 1\n0.0,0.5,0.25\n1e-6,0.9,0.1\n")
    try:
        assert analog_csv.is_analog_csv(digital) is False
        assert analog_csv.is_analog_csv(analog) is True
        assert analog_csv.is_analog_csv(disguised) is True
    finally:
        for p in (digital, analog, disguised):
            os.remove(p)


def test_read_analog_csv_truncates_ragged_trailing_comma_row():
    """A stray trailing comma (e.g. from an Excel re-save) gives one data row
    an extra column versus the header. Before the fix this made `data_rows`
    ragged and crashed np.asarray(..., dtype=float64) with a cryptic
    'inhomogeneous shape' ValueError; it must instead be truncated to the
    header's column count and parse cleanly."""
    from swi3s_studio.ingest import analog_csv
    path = _write_csv(
        "TIME,CH1,CH2\n"
        "-1e-6,0.023,-0.5\n"
        "-9e-7,1.02,0.98,\n"          # stray trailing comma -> 4 fields
        "-8e-7,0.5,0.1\n"
    )
    try:
        result = analog_csv.read_analog_csv(path)
        assert result["time"].size == 3
        assert list(result["channels"]["CH2"]) == [-0.5, 0.98, 0.1]
    finally:
        os.remove(path)


def test_is_analog_csv_detects_analog_past_a_long_railed_lead_in():
    """A capture with a long railed/saturated lead-in — many rows all exactly
    0/1 — before the real analog data begins must still classify as analog.
    The old hard cap at max_scan_rows DATA rows stopped scanning before
    reaching the real data and misclassified it as digital; use a small
    max_scan_rows here to keep the fixture file a reasonable size while still
    exercising the past-the-cap thinning path."""
    from swi3s_studio.ingest import analog_csv
    lines = ["TIME,CH1,CH2"]
    for i in range(300):                              # railed lead-in, all 0/1
        lines.append(f"{i * 1e-9},{i % 2},{(i + 1) % 2}")
    for i in range(300, 400):                         # real analog data
        lines.append(f"{i * 1e-9},0.023,-0.5")
    path = _write_csv("\n".join(lines) + "\n")
    try:
        assert analog_csv.is_analog_csv(path, max_scan_rows=100) is True
    finally:
        os.remove(path)


def test_channel_names_matches_read_analog_csv_without_full_parse():
    """channel_names(path) is a cheap header-only read; it must agree with the
    keys read_analog_csv derives from a full parse of the same file."""
    from swi3s_studio.ingest import analog_csv
    path = _write_csv("Model,MSO58\nSample Interval,1.6e-10\n\n"
                      "TIME,CH1,CH2\n-1e-6,0.023,-0.5\n-9e-7,1.02,0.98\n")
    try:
        assert analog_csv.channel_names(path) == list(
            analog_csv.read_analog_csv(path)["channels"].keys()
        )
    finally:
        os.remove(path)


def test_from_wfm_auto_clock_is_order_independent():
    """auto_clock (default) assigns the forwarded clock by transition count, so the two
    file arguments can be given in either order and yield the same capture; the busier
    line becomes the clock. auto_clock=False honours the given order instead.

    Synthetic pair (was gated on a local Box scope export — see the module docstring):
    the property is about transition COUNTS, so a square-wave pair with a known busier
    line exercises it exactly, and runs everywhere."""
    n = 4000
    busy = _square_wave_codes(n, period=20)            # many transitions -> the clock
    quiet = _square_wave_codes(n, period=200)
    with tempfile.TemporaryDirectory() as d:
        busy_path = os.path.join(d, "busy.wfm")
        quiet_path = os.path.join(d, "quiet.wfm")
        _write_synthetic_wfm(busy_path, busy, dt=1e-9, v_scale=1.0 / 1000.0)
        _write_synthetic_wfm(quiet_path, quiet, dt=1e-9, v_scale=1.0 / 1000.0)

        a = Session.from_wfm(busy_path, quiet_path)
        b = Session.from_wfm(quiet_path, busy_path)     # swapped
        assert np.array_equal(a.capture.clock_edges, b.capture.clock_edges)
        assert np.array_equal(a.capture.data_edges, b.capture.data_edges)
        assert a.capture.clock_edges.size >= a.capture.data_edges.size

        # Explicit order preserved when auto-detection is off: naming the QUIET line
        # as the clock must keep it there, even though it isn't the busier one.
        c = Session.from_wfm(quiet_path, busy_path, auto_clock=False)
        assert c.capture.clock_edges.size <= c.capture.data_edges.size


def _level_before(edges: np.ndarray, initial: bool, sample: int) -> bool:
    """Logic level of a line just before `sample` (parity of prior edges)."""
    n = int(np.searchsorted(edges, np.uint64(sample), side="left"))
    return bool(initial) ^ bool(n & 1)


def _slice_capture(main: Capture, clock_lo_idx: int, clock_hi_idx: int) -> tuple:
    """Extract a contiguous SUB capture spanning main clock edges
    [clock_lo_idx, clock_hi_idx), re-based to sample 0 (mirrors
    tests/test_subcapture.py's helper). Returns (sub_capture, origin_sample)."""
    clk = main.clock_edges
    dat = main.data_edges
    origin = int(clk[clock_lo_idx])
    hi_sample = int(clk[clock_hi_idx]) if clock_hi_idx < clk.size else int(clk[-1]) + 1

    sub_clk = clk[clock_lo_idx:clock_hi_idx].astype(np.int64) - origin
    lo_mask = (dat.astype(np.int64) >= origin) & (dat.astype(np.int64) < hi_sample)
    sub_dat = dat[lo_mask].astype(np.int64) - origin

    initial_clock = _level_before(clk, main.initial_clock, origin)
    initial_data = _level_before(dat, main.initial_data, origin)

    sub = Capture(
        clock_edges=sub_clk.astype(np.uint64),
        data_edges=sub_dat.astype(np.uint64),
        initial_clock=initial_clock,
        initial_data=initial_data,
        sample_rate_hz=main.sample_rate_hz,
    )
    return sub, origin


def test_locate_subcapture_finds_mid_window_slice():
    s = Session.from_demo(300)
    n_edges = s.capture.clock_edges.size
    lo, hi = n_edges // 3, n_edges // 3 + 4000     # a mid window, a few thousand UIs
    sub, origin = _slice_capture(s.capture, lo, hi)

    matches = s.locate_subcapture(sub)
    assert matches, "expected at least one match"
    best = max(matches, key=lambda m: m["score"])
    assert abs(best["start_sample"] - origin) <= 4, best
    assert best["score"] >= 0.9, best


def test_export_sal_roundtrip():
    s = Session.from_demo(64)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "export.sal")
        s.export_sal(p, clock_channel=0, data_channel=1)

        s2 = Session.from_sal(p, 0, 1)

    np.testing.assert_array_equal(s2.capture.clock_edges, s.capture.clock_edges)
    np.testing.assert_array_equal(s2.capture.data_edges, s.capture.data_edges)
    assert bool(s2.capture.initial_clock) == bool(s.capture.initial_clock)
    assert bool(s2.capture.initial_data) == bool(s.capture.initial_data)


def test_decoder_ready_hook_fires_before_construction_returns():
    captured = []
    s = Session.from_demo(64, decoder_ready=lambda d: captured.append(d))
    assert len(captured) == 1
    assert captured[0] is s.decoder


def test_decoder_ready_defaults_to_none_without_error():
    # Backward-compatible: omitting decoder_ready must not change behaviour.
    s = Session.from_demo(64)
    assert s.decoder is not None
