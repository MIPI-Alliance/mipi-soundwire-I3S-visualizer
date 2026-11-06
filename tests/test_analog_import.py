"""Tests for the analog-import / sub-capture / .sal-export Session wiring:
Session.from_wfm, Session.from_analog_csv, Session.locate_subcapture,
Session.export_sal, and the decoder_ready progress hook.

Everything here is SYNTHETIC — `.wfm` files are written by `_write_synthetic_wfm`
and analog CSVs by `_write_csv`, so the whole module runs on any machine.
auto_clock orientation is covered by test_from_wfm_auto_clock_is_order_independent,
built from a synthetic pair.

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
    """capture_from_analog takes time, clock and data arrays of one length; any other
    combination (data or clock shorter or longer than time) raises a clear ValueError up
    front rather than misaligning timestamps or crashing deep in a closure."""
    from swi3s_studio.ingest.analog import capture_from_analog
    time = np.linspace(0.0, 1.0, 100)
    for n_clock, n_data in ((100, 50), (100, 150), (50, 100), (150, 100)):
        with pytest.raises(ValueError, match=r"length mismatch"):
            capture_from_analog(time, np.zeros(n_clock), np.zeros(n_data))


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
    still decode normally — the from_wfm validation must not false-positive on
    the common case."""
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
    """A .wfm pair's source uses the keys "clock_path"/"data_path"; source_label()
    names both files, like the two-file .bin/.sal labeling, not the generic
    'Capture'."""
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
    analog = _write_csv("Model,SCOPE1,,Model,SCOPE1\nSample Interval,1e-7\n\n"
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
    an extra column versus the header. It is truncated to the header's column
    count and parses cleanly, rather than leaving `data_rows` ragged for
    np.asarray(..., dtype=float64)."""
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


@pytest.mark.platform
def test_an_analog_csv_with_a_byte_order_mark_reads_as_one_without():
    """A spreadsheet's BOM is not part of the first cell: with the BOM on the TIME row
    itself, TIME is still the header row."""
    from swi3s_studio.ingest import analog_csv
    body = "TIME,CH1,CH2\n-1e-6,0.023,-0.5\n-9e-7,1.02,0.98\n"
    plain, bom = _write_csv(body), _write_csv(body)
    with open(bom, "w", encoding="utf-8-sig") as f:
        f.write(body)
    try:
        with open(bom, "rb") as f:
            assert f.read(3) == b"\xef\xbb\xbf"
        want = analog_csv.read_analog_csv(plain)
        got = analog_csv.read_analog_csv(bom)
        assert list(got["channels"]) == list(want["channels"]) == ["CH1", "CH2"]
        assert list(got["time"]) == list(want["time"])
        assert analog_csv.channel_names(bom) == ["CH1", "CH2"]
    finally:
        os.remove(plain)
        os.remove(bom)


def test_a_first_line_over_the_line_limit_is_not_an_analog_csv():
    """A first line longer than the limit (a file with no line breaks) is not a row of a
    scope export: the analog row after it does not make the file an analog CSV."""
    from swi3s_studio.ingest import analog_csv, timebase
    long_line = "x" * (timebase.MAX_LINE_CHARS + 10)
    plain = _write_csv("0,0.5,0.7\n")
    long_first = _write_csv(long_line + "\n0,0.5,0.7\n")
    no_breaks = _write_csv("0," + ",".join(["0.5"] * (timebase.MAX_LINE_CHARS // 3)))
    try:
        assert analog_csv.is_analog_csv(plain) is True
        assert analog_csv.is_analog_csv(long_first) is False
        assert analog_csv.is_analog_csv(no_breaks) is False
    finally:
        os.remove(plain)
        os.remove(long_first)
        os.remove(no_breaks)


def test_a_preamble_cell_over_the_csv_field_limit_is_no_header_row():
    """The preamble is bounded by its byte count and by the csv module's field limit: a
    cell under the limit is read, a cell over it leaves no TIME header row."""
    import csv

    from swi3s_studio.ingest import analog_csv
    saved = csv.field_size_limit(1000)               # process-global: set here, restored below
    try:
        under = _write_csv("Note," + "y" * 999 + "\nTIME,CH1\n0,0.5\n")
        over = _write_csv("Note," + "y" * 1001 + "\nTIME,CH1\n0,0.5\n")
        try:
            assert analog_csv.channel_names(under) == ["CH1"]
            with pytest.raises(ValueError, match="no TIME header row"):
                analog_csv.channel_names(over)
        finally:
            os.remove(under)
            os.remove(over)
    finally:
        csv.field_size_limit(saved)


def test_the_no_header_error_of_every_analog_reader_names_the_file():
    """A CSV with no TIME header row is refused by name, whichever reader meets it."""
    from swi3s_studio.ingest import analog_csv
    path = _write_csv("a,b\n1,2\n")
    try:
        for call in (lambda: analog_csv.channel_names(path), lambda: analog_csv.scan_header(path),
                     lambda: analog_csv.predict_peak_bytes(path),
                     lambda: analog_csv.read_analog_csv(path),
                     lambda: analog_csv.read_analog_csv(path, max_bytes=0)):
            with pytest.raises(ValueError, match="no TIME header row") as err:
                call()
            assert str(err.value).startswith(f"{path}: ")
    finally:
        os.remove(path)


def test_is_analog_csv_detects_analog_past_a_long_railed_lead_in():
    """A capture with a long railed/saturated lead-in — many rows all exactly
    0/1 — before the real analog data begins must still classify as analog.
    A small max_scan_rows keeps the fixture file a reasonable size while still
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
    path = _write_csv("Model,SCOPE1\nSample Interval,1e-7\n\n"
                      "TIME,CH1,CH2\n-1e-6,0.023,-0.5\n-9e-7,1.02,0.98\n")
    try:
        assert analog_csv.channel_names(path) == list(
            analog_csv.read_analog_csv(path)["channels"].keys()
        )
    finally:
        os.remove(path)


def test_duplicate_and_blank_header_names_keep_every_column_under_unique_names(tmp_path):
    """Two columns named CH1 are two channels (the later one ``CH1 (2)``) and a blank
    header cell between named ones is called ``Column N``; the import notes say so, and
    channel_names agrees with the parse."""
    from swi3s_studio import import_notes
    from swi3s_studio.ingest import analog_csv
    p = tmp_path / "dup.csv"
    p.write_text("TIME,CH1,CH1,,CH1 (2),CH1\n0,1,2,3,4,5\n1e-9,6,7,8,9,10\n")
    with import_notes.collect() as notes:
        parsed = analog_csv.read_analog_csv(str(p))
    names = ["CH1", "CH1 (3)", "Column 4", "CH1 (2)", "CH1 (4)"]
    assert list(parsed["channels"]) == names == analog_csv.channel_names(str(p))
    assert [list(parsed["channels"][n]) for n in names] == [
        [1, 6], [2, 7], [3, 8], [4, 9], [5, 10]]
    msgs = [str(n) for n in notes]
    assert any("no name for column 4; read as 'Column 4'" in m for m in msgs), msgs
    assert any("repeats the name 'CH1'" in m for m in msgs), msgs


def test_a_header_of_unique_names_makes_no_header_note(tmp_path):
    from swi3s_studio import import_notes
    from swi3s_studio.ingest import analog_csv
    p = tmp_path / "ok.csv"
    p.write_text("TIME,CH1,CH2,\n0,1,2\n1e-9,3,4\n")
    with import_notes.collect() as notes:
        parsed = analog_csv.read_analog_csv(str(p))
    assert list(parsed["channels"]) == ["CH1", "CH2"] and not notes


def test_from_analog_csv_selects_a_repeated_name_by_position(tmp_path):
    """With two columns named CH1, clock=0 and data=1 read the first and the second."""
    n = 400
    k = np.arange(n)
    fast, slow = (k // 5) % 2, (k // 50) % 2
    p = tmp_path / "two.csv"
    p.write_text("TIME,CH1,CH1\n" + "".join(
        f"{i * 1e-9:.9g},{float(fast[i])},{float(slow[i])}\n" for i in range(n)))
    from swi3s_studio import import_notes
    with import_notes.collect() as notes:
        s = Session.from_analog_csv(str(p), clock=0, data=1, auto_clock=False)
        swapped = Session.from_analog_csv(str(p), clock="CH1 (2)", data="CH1",
                                          auto_clock=False)
    assert notes and all("repeats the name 'CH1'" in str(m) for m in notes)
    assert (s.source["clock"], s.source["data"]) == ("CH1", "CH1 (2)")
    assert s.capture.clock_edges.size > s.capture.data_edges.size > 0
    assert swapped.capture.clock_edges.size < swapped.capture.data_edges.size


def test_a_threshold_pair_with_high_equal_to_low_is_accepted():
    from swi3s_studio.ingest.analog import capture_from_analog
    t, clk, dat = _square_pair()
    want = capture_from_analog(t, clk, dat, clock_thresh=(0.6, 0.4))
    got = capture_from_analog(t, clk, dat, clock_thresh=(0.5, 0.5))
    assert got.clock_edges.size > 0
    assert np.array_equal(got.clock_edges, want.clock_edges)


def test_from_wfm_auto_clock_is_order_independent():
    """auto_clock (default) assigns the forwarded clock by transition count, so the two
    file arguments can be given in either order and yield the same capture; the busier
    line becomes the clock. auto_clock=False honours the given order instead.

    Synthetic pair: the property is about transition COUNTS, so a square-wave pair with
    a known busier line exercises it exactly, and runs everywhere."""
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


def _square_pair(n: int = 2000):
    t = np.arange(n) * 1e-9
    clk = np.where((np.arange(n) // 5) % 2 == 1, 1.0, 0.0)
    dat = np.where((np.arange(n) // 50) % 2 == 1, 1.0, 0.0)
    return t, clk, dat


@pytest.mark.parametrize("bad", [(5, 6), (float("nan"), float("nan")), (float("inf"), 0.0),
                                 ("a", "b"), (1,), (1, 2, 3), 1e300, "hi"])
@pytest.mark.parametrize("field", ["clock_thresh", "data_thresh"])
def test_a_threshold_pair_that_is_not_two_finite_numbers_high_over_low_is_refused(bad, field):
    """A given pair is checked before use: ValueError naming the field, never TypeError
    or IndexError, and never an empty channel."""
    from swi3s_studio.ingest.analog import capture_from_analog
    t, clk, dat = _square_pair()
    with pytest.raises(ValueError, match=field):
        capture_from_analog(t, clk, dat, **{field: bad})


def test_a_threshold_pair_that_no_sample_reaches_is_reported():
    """A finite, ordered pair above every sample gives a channel with no transitions; the
    import warning names the channel and its span."""
    from swi3s_studio import import_notes
    from swi3s_studio.ingest.analog import capture_from_analog
    t, clk, dat = _square_pair()
    with import_notes.collect() as notes:
        cap = capture_from_analog(t, clk, dat, clock_thresh=(6.0, 5.0), source="s.csv")
    assert cap.clock_edges.size == 0 and cap.data_edges.size > 0
    msgs = [str(n) for n in notes]
    assert any("s.csv: the clock channel never crosses its thresholds 6 V / 5 V "
               "(its samples span 0 V to 1 V)" in m for m in msgs), msgs
    assert not any("data channel never crosses" in m for m in msgs)


def test_a_flat_channel_is_not_reported_as_never_crossing():
    from swi3s_studio import import_notes
    from swi3s_studio.ingest.analog import capture_from_analog
    t, clk, _dat = _square_pair()
    with import_notes.collect() as notes:
        capture_from_analog(t, clk, np.full(t.size, 0.5), source="s.csv")
    assert not any("never crosses" in str(n) for n in notes)


def test_a_channel_that_holds_only_noise_is_flat_not_thresholded_on_its_own_band():
    """A swing under 1 microvolt is noise: the channel takes the fallback thresholds,
    gives no edges and is reported as undetectable."""
    from swi3s_studio import import_notes
    from swi3s_studio.ingest.analog import capture_from_analog
    t, clk, _dat = _square_pair()
    noise = 0.5 + np.random.default_rng(1).normal(0.0, 1e-9, t.size)
    with import_notes.collect() as notes:
        cap = capture_from_analog(t, clk, noise, source="s.csv")
    assert cap.data_edges.size == 0 and cap.clock_edges.size > 0
    assert any("data channel's thresholds could not be detected" in str(n) for n in notes)
