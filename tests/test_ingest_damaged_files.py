"""Truncated, partly written or damaged capture files are refused by the loaders' own
errors, and priced by what a load of them actually reads.

Each test builds a synthetic file in a temporary directory and damages it the way an
interrupted export or a partial copy does: a lost tail, a cut last row, a block chain
that no longer tiles its blob.
"""
import io
import json
import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest

from swi3s_studio import import_notes
from swi3s_studio.ingest import (
    analog_csv,
    digital_csv,
    probe,
    sal_export,
    saleae_binary,
    saleae_sal,
)
from swi3s_studio.ingest.capture import Capture

# ---- .sal: a channel whose block chain does not walk -------------------------------------


def _capture(n_clock=200_000, n_data=30_000, seed=3):
    rng = np.random.default_rng(seed)
    ce = np.cumsum(rng.integers(10, 13, n_clock)).astype(np.uint64)
    de = np.sort(rng.choice(ce + 1, size=n_data, replace=False)).astype(np.uint64)
    return Capture(ce, de, False, False, 500_000_000)


def _rewrite(path, member, change):
    """Rewrite the zip at `path` with `member`'s bytes replaced by `change(bytes)`."""
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            z.writestr(info.filename, change(data) if info.filename == member else data)


@pytest.fixture
def sal_with_a_damaged_channel(tmp_path):
    path = str(tmp_path / "damaged.sal")
    sal_export.export_sal(_capture(), path)
    _rewrite(path, "digital-1.bin", lambda b: b + b"\x01\x02\x03")
    return path


def test_a_channel_that_does_not_walk_is_priced_whole_in_a_window(
        sal_with_a_damaged_channel, monkeypatch):
    path = sal_with_a_damaged_channel
    chains = saleae_sal._v3_chains(path, saleae_sal.read_info(path), [0, 1])
    assert chains[0][1] >= 0 and chains[1][1] < 0
    with zipfile.ZipFile(path) as z:
        blob = z.getinfo("digital-1.bin").file_size
    model = saleae_sal.window_model(path, [0, 1])
    assert model.whole_channels == {1: blob}
    w = (0, 1_000)
    streamed_only = saleae_sal.window_peak_bytes(model.edge_bytes(*w), model.max_block_bytes)
    assert model.peak_bytes(*w) == streamed_only + saleae_sal.whole_channel_peak_bytes(blob)

    reads = []
    real_read = zipfile.ZipFile.read
    monkeypatch.setattr(zipfile.ZipFile, "read",
                        lambda self, name, *a, **k: reads.append(name) or
                        real_read(self, name, *a, **k))
    # A budget the streamed channel alone fits in, and the damaged one read whole does not.
    budget = streamed_only + saleae_sal.whole_channel_peak_bytes(blob) // 2
    with pytest.raises(saleae_sal.SalTooLargeError) as caught:
        saleae_sal.load_capture(path, 0, 1, window=w, max_bytes=budget)
    err = caught.value
    assert err.whole_channels == (1,) and err.streamed
    assert err.peak == model.peak_bytes(*w)
    assert "Channel 1 is read whole" in str(err)
    assert "digital-1.bin" not in reads, "the damaged channel was inflated before the guard"


def test_the_probe_prices_a_window_as_the_loader_does_when_a_channel_does_not_walk(
        sal_with_a_damaged_channel):
    path = sal_with_a_damaged_channel
    pr = probe.probe([path])
    model = saleae_sal.window_model(path, [0, 1])
    rate = pr.sample_rate_hz
    t0, t1 = 0.0, 999 / rate
    with zipfile.ZipFile(path) as z:
        blob = z.getinfo("digital-1.bin").file_size
    assert pr.cost_bytes(t0, t1) == model.peak_bytes(0, 1_000)
    assert pr.cost_bytes(t0, t1) > saleae_sal.whole_channel_peak_bytes(blob)


def test_a_damaged_channel_is_refused_as_malformed_once_admitted(sal_with_a_damaged_channel):
    with pytest.raises(saleae_binary.MalformedSaleaeV3):
        saleae_sal.load_capture(sal_with_a_damaged_channel, 0, 1, window=(0, 1_000),
                                max_bytes=0)


# ---- v3: the chain start is searched for in one bounded prefix by every reader -----------


def test_every_v3_reader_looks_for_the_chain_start_in_the_same_prefix():
    edges = np.arange(5, 50_000, 7, dtype=np.uint64)
    blob = saleae_binary.build_channel_v3(False, edges)
    head = 16                                       # magic, version, type
    pad = b"\xff" * (saleae_binary.V3_CHAIN_START_SCAN + 100)
    late = blob[:head] + pad + blob[head:]          # chain starts past the prefix
    for reader in (saleae_binary.parse_channel_v3,
                   saleae_binary._find_v3_chain_start,
                   lambda d: saleae_binary.parse_channel_v3_window(d, 0, 1_000)):
        with pytest.raises(saleae_binary.MalformedSaleaeV3):
            reader(late)
    assert saleae_sal._v3_span_streaming(io.BytesIO(late), len(late)) == 0

    early = blob[:head] + pad[:1_000] + blob[head:]  # chain starts inside it
    got = saleae_binary.parse_channel_v3(early)
    assert np.array_equal(got.transition_samples, edges)
    assert saleae_binary._find_v3_chain_start(early)[0] == head + 1_000
    assert saleae_sal._v3_span_streaming(io.BytesIO(early), len(early), want_start=True)[1] \
        == head + 1_000


# ---- v0 .bin: a lost tail or a damaged count ----------------------------------------------


def _bin(path, n=20_000, rate=500e6):
    e = np.arange(1, n + 1, dtype=np.uint64) * 10
    saleae_binary.write_channel(str(path), False, e / rate)
    return str(path)


def _bin_pair(tmp_path):
    return _bin(tmp_path / "clk.bin"), _bin(tmp_path / "dat.bin", n=5_000)


def test_a_bin_that_lost_its_tail_is_refused_as_truncated(tmp_path):
    clk, dat = _bin_pair(tmp_path)
    raw = Path(dat).read_bytes()
    Path(dat).write_bytes(raw[:len(raw) // 2])
    with pytest.raises(ValueError, match="truncated") as caught:
        saleae_binary.load_capture(clk, dat, 500_000_000)
    assert dat in str(caught.value)
    held = (len(raw) // 2 - saleae_binary.V0_HEADER.size) // 8
    assert saleae_binary.transition_count(dat) == held


def test_a_bin_with_a_damaged_count_is_refused_as_truncated_not_as_too_large(tmp_path):
    clk, dat = _bin_pair(tmp_path)
    raw = bytearray(Path(dat).read_bytes())
    struct.pack_into("<Q", raw, saleae_binary.V0_HEADER.size - 8, 10_000_000_000)
    Path(dat).write_bytes(bytes(raw))
    assert saleae_binary.transition_count(dat) == 5_000
    assert saleae_binary.predict_peak_bytes(clk, dat) < 10_000_000
    with pytest.raises(ValueError, match="truncated") as caught:
        saleae_binary.load_capture(clk, dat, 500_000_000)
    assert "10,000,000,000" in str(caught.value)


def test_the_rate_of_a_bin_cut_mid_value_is_still_inferred(tmp_path):
    clk, dat = _bin_pair(tmp_path)
    raw = Path(dat).read_bytes()
    Path(dat).write_bytes(raw[:saleae_binary.V0_HEADER.size + 8 * 1_000 + 3])
    assert saleae_binary.infer_sample_rate([dat]) > 0


def test_a_sal_v0_channel_that_lost_its_tail_is_refused_as_truncated(tmp_path):
    clk, dat = _bin_pair(tmp_path)
    path = str(tmp_path / "v0.sal")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("meta.json", '{"data": {"legacySettings": {"sampleRate": '
                                '{"digital": 500000000}}}}')
        z.writestr("digital-0.bin", Path(clk).read_bytes())
        z.writestr("digital-1.bin", Path(dat).read_bytes()[:2_000])
    with pytest.raises(ValueError, match="digital ch1: truncated"):
        saleae_sal.load_capture(path, 0, 1, max_bytes=0)


# ---- analog CSV: a partly written last row ------------------------------------------------


def _scope_csv(path, n, tail=""):
    t = np.arange(n) * 1e-9 - 1e-6
    clk = np.where((np.arange(n) // 4) % 2, 1.0, -1.0)
    with open(path, "w") as f:
        f.write("Model,SCOPE1\nSample Interval,1e-09\n\nTIME,CH1,CH2\n")
        f.write("\n".join(f"{t[i]:.6e},{clk[i]:.4e},{-clk[i]:.4e}" for i in range(n)) + "\n")
        f.write(tail)
    return str(path)


@pytest.mark.parametrize("tail", ["2.000000e-06,0.0,-", "2.000000e-06,0.0,1.2e",
                                  "2.000000e-06,0.0,-\n", "2.000000e-06,0.0,-\n\n"])
@pytest.mark.parametrize("batch", [7, 2_000, 50_000])
def test_a_partly_written_last_row_is_dropped(tmp_path, monkeypatch, tail, batch):
    monkeypatch.setattr(analog_csv, "_BATCH_ROWS", batch)
    whole = analog_csv.read_analog_csv(_scope_csv(tmp_path / "a.csv", 2_000), max_bytes=0)
    got = analog_csv.read_analog_csv(_scope_csv(tmp_path / "b.csv", 2_000, tail),
                                     max_bytes=0)
    assert np.array_equal(got["time"], whole["time"])
    assert all(np.array_equal(got["channels"][k], whole["channels"][k])
               for k in whole["channels"])


@pytest.mark.parametrize("batch", [7, 2_000, 50_000])
@pytest.mark.parametrize("at", [0, 6, 1_000, 1_998])
def test_an_unreadable_row_before_the_last_is_refused_by_name(tmp_path, monkeypatch,
                                                              batch, at):
    monkeypatch.setattr(analog_csv, "_BATCH_ROWS", batch)
    path = _scope_csv(tmp_path / "a.csv", 2_000)
    lines = Path(path).read_text().split("\n")
    first = lines.index("TIME,CH1,CH2") + 1
    lines[first + at] = "1.0e-06,oops,0.0"
    Path(path).write_text("\n".join(lines))
    with pytest.raises(ValueError, match=f"data row {at + 1:,} after the TIME header") \
            as caught:
        analog_csv.read_analog_csv(path, max_bytes=0)
    assert path in str(caught.value) and "oops" in str(caught.value)


def test_a_multi_channel_preamble_is_keyed_per_channel_block():
    rows = [["Model", "SCOPE1", "", "Model", "SCOPE1"],
            ["Sample Interval", "1.6e-10", "", "Sample Interval", "3.2e-10"],
            ["Channel", "CH1", "", "Channel", "CH2"],
            ["Vertical Units", "", "", "Vertical Units", "V"],
            ["Label"]]
    assert analog_csv._parse_preamble(rows) == {
        "Model": ["SCOPE1", "SCOPE1"],
        "Sample Interval": ["1.6e-10", "3.2e-10"],
        "Channel": ["CH1", "CH2"],
        "Vertical Units": ["", "V"],
        "Label": [""],
    }
    assert analog_csv._parse_preamble([["Model", "SCOPE1"], [], [""]]) == {"Model": ["SCOPE1"]}


# ---- digital CSV: a time that is no sample index ----------------------------------------


@pytest.mark.parametrize("bad_row", [0, -1])
@pytest.mark.parametrize("cell", ["-1e30", "1e30"])
def test_a_digital_csv_time_beyond_the_sample_range_is_refused(tmp_path, bad_row, cell):
    rows = [f"{i * 1e-6:.9f},{i % 2},{(i // 3) % 2}" for i in range(20)]
    rows[bad_row] = f"{cell},0,1"
    p = tmp_path / "far.csv"
    p.write_text("Time,CLK,DATA\n" + "\n".join(rows) + "\n")
    with pytest.raises(ValueError, match=r"far\.csv: the time .* too far from the capture start"):
        digital_csv.load_capture(str(p), 1, 2)


# ---- a time or begin time that is no sample index, in every importer ----------------------

_RATE = 1_000_000


def _v0_sal(tmp_path, clock_times, data_times, clock_begin=-1e-6, data_begin=-1e-6,
            name="v0.sal"):
    """A v0 .sal called `name` whose channels are `clock_times` and `data_times`
    (seconds), written at 1 MHz."""
    paths = []
    for blob, times, begin in (("c.bin", clock_times, clock_begin),
                               ("d.bin", data_times, data_begin)):
        p = tmp_path / blob
        saleae_binary.write_channel(str(p), False, times, begin, 2e-4)
        paths.append(p)
    out = str(tmp_path / name)
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("meta.json", json.dumps(
            {"data": {"legacySettings": {"sampleRate": {"digital": _RATE}}}}))
        z.writestr("digital-0.bin", paths[0].read_bytes())
        z.writestr("digital-1.bin", paths[1].read_bytes())
    return out


def _times():
    return np.arange(1, 200) / _RATE


@pytest.mark.parametrize("begin", [float("nan"), float("inf"), float("-inf")])
def test_a_bin_header_begin_time_that_is_not_finite_is_refused_by_file(tmp_path, begin):
    t = _times()
    c, d = str(tmp_path / "clk.bin"), str(tmp_path / "dat.bin")
    saleae_binary.write_channel(c, False, t, -1e-6, 2e-4)
    saleae_binary.write_channel(d, False, t[::7], begin, 2e-4)
    with pytest.raises(ValueError, match=r"dat\.bin: the header's begin time is"):
        saleae_binary.load_capture(c, d, _RATE)


@pytest.mark.parametrize("which, named", [("begin", "clk"), ("first", "clk"), ("last", "dat")])
def test_a_bin_time_beyond_the_sample_range_is_refused_by_file(tmp_path, which, named):
    """The file named is the first channel whose times fall too far from the capture start:
    a data channel that starts or begins long before puts the origin far from the clock's."""
    t = _times()
    bad_t = t.copy()
    begin = -1e-6
    if which == "begin":
        begin = -1e30
    elif which == "first":
        bad_t[0] = -1e30
    else:
        bad_t[-1] = 1e30
    c, d = str(tmp_path / "clk.bin"), str(tmp_path / "dat.bin")
    saleae_binary.write_channel(c, False, t, -1e-6, 2e-4)
    saleae_binary.write_channel(d, False, bad_t, begin, 2e-4)
    with pytest.raises(ValueError, match=named + r"\.bin: .*too far from the capture start"):
        saleae_binary.load_capture(c, d, _RATE)


@pytest.mark.parametrize("begin", [float("nan"), float("inf"), float("-inf")])
def test_a_sal_v0_begin_time_that_is_not_finite_is_refused_by_channel(tmp_path, begin):
    t = _times()
    p = _v0_sal(tmp_path, t, t[::7], data_begin=begin)
    with pytest.raises(ValueError, match=r"v0\.sal: digital ch1: the header's begin time is"):
        saleae_sal.load_capture(p, 0, 1, max_bytes=0)


@pytest.mark.parametrize("which", ["begin", "first", "last"])
def test_a_sal_v0_time_beyond_the_sample_range_is_refused_by_file(tmp_path, which):
    t = _times()
    bad_t = t.copy()
    begin = -1e-6
    if which == "begin":
        begin = -1e30
    elif which == "first":
        bad_t[0] = -1e30
    else:
        bad_t[-1] = 1e30
    p = _v0_sal(tmp_path, t, bad_t, data_begin=begin)
    with pytest.raises(ValueError, match=r"v0\.sal: the time .* too far from the capture start"):
        saleae_sal.load_capture(p, 0, 1, max_bytes=0)


@pytest.mark.parametrize("which", ["first", "last"])
def test_an_analog_time_beyond_the_sample_range_is_refused_by_source(which):
    from swi3s_studio.ingest.analog import capture_from_analog
    n = 401                                    # the last row is a level change
    t = np.arange(n) * 1e-6
    t[0 if which == "first" else -1] = -1e30 if which == "first" else 1e30
    clock = np.where((np.arange(n) // 5) % 2 == 0, 0.0, 1.0)
    data = np.where((np.arange(n) // 11) % 2 == 0, 0.0, 1.0)
    with pytest.raises(ValueError, match=r"scope\.wfm: the time .* too far from the capture "
                                         r"start"):
        capture_from_analog(t, clock, data, sample_rate_hz=_RATE, source="scope.wfm")


def test_a_config_csv_cell_over_the_csv_field_limit_is_refused_by_file_and_line(tmp_path):
    from swi3s_studio.ingest import visualizer_csv
    p = tmp_path / "cfg.csv"
    p.write_text("NumColumns_REG,8\nNotes," + "x" * 200_000 + "\n")
    with pytest.raises(ValueError, match=r"cfg\.csv: line 2: field larger than field limit"):
        visualizer_csv.normalized_path(str(p))


def test_a_wfm_curve_offset_below_zero_is_refused_by_file(tmp_path):
    from test_analog_import import _write_synthetic_wfm

    from swi3s_studio.ingest import wfm
    p = tmp_path / "neg.wfm"
    _write_synthetic_wfm(str(p), [0, 1, 0, 1])
    raw = bytearray(p.read_bytes())
    struct.pack_into("<i", raw, 0x010, -(1 << 20))          # curve_buffer_start
    p.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match=r"neg\.wfm: \.wfm curve offset -\d+ is negative"):
        wfm.read_wfm(str(p))


# ---- an analog CSV that is not text, and a time axis too coarse for its interval ----------

_SCOPE_HEAD = "Model,SCOPE1\nSample Interval,1e-09\n\nTIME,CH1,CH2\n"


def _scope_rows(n=50, t0=0.0):
    return "".join(f"{t0 + i * 1e-9:.17e},{(i % 2) * 3.3:.3f},{(i // 2 % 2) * 3.3:.3f}\n"
                   for i in range(n))


def test_an_analog_csv_with_a_byte_that_is_not_utf8_is_refused_by_file(tmp_path):
    p = tmp_path / "latin.csv"
    p.write_bytes((_SCOPE_HEAD + _scope_rows()).encode() + b"1e-6,\x80,0.1\n")
    with pytest.raises(ValueError, match=r"latin\.csv: not a text CSV"):
        analog_csv.read_analog_csv(str(p))


def test_an_analog_csv_cell_over_the_csv_field_limit_is_refused_by_file(tmp_path):
    p = tmp_path / "wide.csv"
    p.write_text(_SCOPE_HEAD + _scope_rows() + "1e-6," + "9" * 200_000 + ",0.1\n")
    with pytest.raises(ValueError, match=r"wide\.csv: not a text CSV"):
        analog_csv.read_analog_csv(str(p))


def test_the_probe_names_a_file_of_random_bytes_that_is_called_csv(tmp_path):
    rng = np.random.default_rng(1)
    for i in range(40):
        p = tmp_path / "r.csv"
        p.write_bytes(rng.integers(0, 256, int(rng.integers(1, 3000)),
                                   dtype=np.uint8).tobytes())
        with pytest.raises(ValueError, match=r"r\.csv"):
            probe.probe([str(p)])


def test_a_byte_flip_anywhere_in_an_analog_csv_loads_or_raises_a_value_error(tmp_path):
    raw = (_SCOPE_HEAD + _scope_rows(60)).encode()
    p = tmp_path / "flip.csv"
    for pos in range(len(raw)):
        mutated = bytearray(raw)
        mutated[pos] ^= 0xFF
        p.write_bytes(bytes(mutated))
        try:
            analog_csv.read_analog_csv(str(p))
        except ValueError:
            pass
        try:
            probe.probe([str(p)])
        except ValueError:
            pass


def test_a_long_absolute_time_axis_takes_the_preambles_interval_and_says_so(tmp_path):
    p = tmp_path / "epoch.csv"
    p.write_text(_SCOPE_HEAD + _scope_rows(400, t0=1_700_000_000.0))
    with import_notes.collect() as notes:
        got = analog_csv.read_analog_csv(str(p))
    assert got["sample_rate_hz"] == 1_000_000_000
    [msg] = [str(n) for n in notes]
    assert msg.startswith("epoch.csv: the TIME column's spacing gives ")
    assert msg.endswith("the TIME values are too coarse to resolve that interval, so the "
                        "preamble's is used")


def test_a_time_axis_that_resolves_its_interval_keeps_the_time_columns_rate(tmp_path):
    p = tmp_path / "decimated.csv"
    rows = "".join(f"{i * 4e-9:.17e},{(i % 2) * 3.3:.3f},0.5\n" for i in range(200))
    p.write_text(_SCOPE_HEAD + rows)
    with import_notes.collect() as notes:
        got = analog_csv.read_analog_csv(str(p))
    assert got["sample_rate_hz"] == 250_000_000
    assert [str(n) for n in notes][0].endswith("the TIME column's is used")


# ---- a header tag or identifier that is not one names the file ----------------------------


@pytest.mark.parametrize("head, text", [
    (b"\x00\x00", r"two\.wfm: \.wfm unrecognised byte-order tag 0x0000"),
    (b"\x0f\x0f" + b"\x00" * 0x3FE, r"two\.wfm: \.wfm unrecognised version tag"),
], ids=["byte_order", "version"])
def test_a_wfm_with_a_bad_byte_order_or_version_tag_is_refused_by_file(tmp_path, head, text):
    from swi3s_studio.ingest import wfm
    p = tmp_path / "two.wfm"
    p.write_bytes(head + b"\x00" * (0x400 - len(head)))
    for read in (wfm.read_wfm, wfm.read_header):
        with pytest.raises(ValueError, match=text):
            read(str(p))


def test_a_sal_channel_that_is_not_a_saleae_binary_is_refused_by_channel(tmp_path):
    path = str(tmp_path / "junk.sal")
    sal_export.export_sal(_capture(2000, 300), path)
    _rewrite(path, "digital-1.bin", lambda b: b"")
    with pytest.raises(ValueError, match=r"not a Saleae binary capture \(bad identifier\)"):
        saleae_sal.load_capture(path, 0, 1, max_bytes=0)
    with pytest.raises(ValueError, match=r"digital.*1.*: not a Saleae binary capture"):
        saleae_sal.load_capture(path, 0, 1, max_bytes=0)


# ---- digital CSV: a stray quote, no header row, a Time unit, a nameless column ----------


def _digital_lines(n=300, header="Time [s],Channel 0,Channel 1", scale=1.0):
    """A Logic-style export on a 50 MHz grid: the clock toggles every row, the data every
    second row; times are written in the unit `scale` seconds-per-unit's reciprocal."""
    rows = [f"{i * 2e-8 / scale:.9f},{i & 1},{(i >> 1) & 1}" for i in range(n)]
    return ([header] if header else []) + rows


def _write_lines(path, lines):
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _csv_notes(path, *cols, **kw):
    with import_notes.collect() as notes:
        cap = digital_csv.load_capture(str(path), *(cols or (1, 2)), **kw)
    return cap, [str(n) for n in notes]


def test_a_stray_double_quote_costs_its_own_row_not_the_rows_after_it(tmp_path):
    clean, notes = _csv_notes(_write_lines(tmp_path / "c.csv", _digital_lines()))
    assert notes == []
    lines = _digital_lines()
    lines[100] = lines[100].replace(",", ',"', 1)          # line 101: an opening quote only
    got, notes = _csv_notes(_write_lines(tmp_path / "q.csv", lines))
    assert np.array_equal(got.clock_edges, clean.clock_edges)
    assert np.array_equal(got.data_edges, clean.data_edges)
    assert notes == ["q.csv: 1 line with an unmatched double quote was read with the quote "
                     "ignored (first at line 101)"]


def test_every_line_with_an_unmatched_quote_is_counted_and_the_first_named(tmp_path):
    clean, _ = _csv_notes(_write_lines(tmp_path / "c.csv", _digital_lines()))
    lines = _digital_lines()
    for k in (60, 150, 250):
        lines[k] += '"'
    path = _write_lines(tmp_path / "q.csv", lines)
    got, notes = _csv_notes(path)
    assert np.array_equal(got.clock_edges, clean.clock_edges)
    assert notes == ["q.csv: 3 lines with an unmatched double quote were read with the "
                     "quote ignored (first at line 61)"]
    counts, _rate = digital_csv.scan(path)
    assert counts == [299, 149]


def test_a_line_with_an_even_number_of_quotes_cannot_swallow_the_lines_after_it(tmp_path):
    """A quote inside an unquoted cell is literal; a later cell that begins with a quote
    would open a field running to the end of the file."""
    clean, _ = _csv_notes(_write_lines(tmp_path / "c.csv", _digital_lines(20)))
    lines = _digital_lines(20)
    lines[5] = lines[5] + '"x,"'                          # line 6: two quotes, one cell left open
    path = _write_lines(tmp_path / "q.csv", lines)
    got, notes = _csv_notes(path)
    assert np.array_equal(got.clock_edges, clean.clock_edges)
    assert np.array_equal(got.data_edges, clean.data_edges)
    assert notes[0] == ("q.csv: 1 line with an unmatched double quote was read with the "
                        "quote ignored (first at line 6)")
    counts, _rate = digital_csv.scan(path)
    assert counts == [19, 9]


def test_a_fully_quoted_row_is_still_read(tmp_path):
    clean, _ = _csv_notes(_write_lines(tmp_path / "c.csv", _digital_lines()))
    lines = _digital_lines()
    t, a, b = lines[10].split(",")
    lines[10] = f'"{t}","{a}","{b}"'
    got, notes = _csv_notes(_write_lines(tmp_path / "q.csv", lines))
    assert notes == [] and np.array_equal(got.clock_edges, clean.clock_edges)


def test_a_digital_csv_without_a_header_row_is_refused_by_name(tmp_path):
    path = _write_lines(tmp_path / "bare.csv", _digital_lines(header=""))
    for call in (lambda: digital_csv.load_capture(path, 1, 2),
                 lambda: digital_csv.read_header(path),
                 lambda: digital_csv.scan(path),
                 lambda: probe.probe([path])):
        with pytest.raises(ValueError, match=r"bare\.csv: not a capture CSV \(the first row "
                                            r"starts with the number '0\.000000000'"):
            call()


@pytest.mark.parametrize("unit,scale", [("s", 1.0), ("ms", 1e-3), ("us", 1e-6),
                                        ("\u00b5s", 1e-6), ("ns", 1e-9), ("ps", 1e-12)])
def test_the_time_header_unit_scales_the_time_column_to_seconds(tmp_path, unit, scale):
    want, _ = _csv_notes(_write_lines(tmp_path / "s.csv", _digital_lines()))
    path = _write_lines(tmp_path / "u.csv",
                        _digital_lines(header=f"Time [{unit}],Channel 0,Channel 1", scale=scale))
    got, notes = _csv_notes(path)
    assert notes == [] and got.sample_rate_hz == want.sample_rate_hz == 50_000_000
    assert np.array_equal(got.clock_edges, want.clock_edges)
    assert digital_csv.scan(path)[1] == 50_000_000                # the probe's rate agrees


def test_a_time_header_unit_that_is_not_recognised_is_reported_and_read_as_seconds(tmp_path):
    path = _write_lines(tmp_path / "u.csv", _digital_lines(header="Time [min],A,B"))
    got, notes = _csv_notes(path)
    assert got.sample_rate_hz == 50_000_000
    assert notes == ["u.csv: the Time column's unit [min] is not recognised; the times are "
                     "read as seconds"]
    _got, notes = _csv_notes(_write_lines(tmp_path / "n.csv", _digital_lines(header="Time,A,B")))
    assert notes == []


def test_a_nameless_trailing_column_is_not_a_column_to_choose(tmp_path):
    path = _write_lines(tmp_path / "t.csv", [ln + ",0" if i else ln + ","
                                              for i, ln in enumerate(_digital_lines())])
    assert digital_csv.read_header(path) == ["Time [s]", "Channel 0", "Channel 1"]
    _cap, notes = _csv_notes(path)
    assert notes == ["t.csv: 300 rows with cells past the 3 columns of the header: those "
                     "cells are ignored (first at line 2)"]
    with pytest.raises(ValueError, match=r"data column 3 is out of range \(file has 3 "
                                         r"columns"):
        digital_csv.load_capture(path, 1, 3)


def test_the_probe_reads_a_row_shorter_than_the_header_as_the_loader_does(tmp_path):
    rows = _digital_lines(header="Time [s],A,B,C")
    path = _write_lines(tmp_path / "short.csv", rows)               # rows hold three cells
    counts, rate = digital_csv.scan(path)
    assert counts[:2] == [299, 149] and counts[2] == 0
    cap = digital_csv.load_capture(path, 1, 2)
    assert rate == cap.sample_rate_hz == 50_000_000
    assert cap.clock_edges.size == counts[0]


def test_a_digital_csv_row_with_cells_past_the_header_is_reported(tmp_path):
    lines = _digital_lines()
    lines[5] += ",9,9"                                    # line 6: two cells past the header
    lines[7] += ","                                       # a blank cell past it is nothing
    clean, _ = _csv_notes(_write_lines(tmp_path / "c.csv", _digital_lines()))
    got, notes = _csv_notes(_write_lines(tmp_path / "x.csv", lines))
    assert np.array_equal(got.clock_edges, clean.clock_edges)
    assert notes == ["x.csv: 1 row with cells past the 3 columns of the header: those cells "
                     "are ignored (first at line 6)"]


def test_a_digital_csv_whose_rows_are_all_too_short_says_so(tmp_path):
    lines = ["Time [s],A,B"] + [f"{i * 1e-9:.9f}" for i in range(50)]
    path = _write_lines(tmp_path / "short.csv", lines)
    with pytest.raises(ValueError, match=r"short\.csv: not enough rows to form a capture "
                                         r"\(50 rows too short for the chosen columns were "
                                         r"skipped, the first at line 2\)"):
        digital_csv.load_capture(path, 1, 2)


def test_an_empty_digital_csv_is_refused_as_empty_not_as_a_column_out_of_range(tmp_path):
    path = _write_lines(tmp_path / "e.csv", [])
    Path(path).write_bytes(b"")
    for call in (lambda: digital_csv.load_capture(path, 1, 2),
                 lambda: digital_csv.read_header(path)):
        with pytest.raises(ValueError, match=r"e\.csv: the file is empty"):
            call()


def test_a_digital_csv_with_a_byte_order_mark_reads_as_one_without(tmp_path):
    """A spreadsheet's BOM is not part of the first cell: the Time label is clean, and a
    file with no header row is still refused as one."""
    plain = _write_lines(tmp_path / "plain.csv", _digital_lines())
    bom = tmp_path / "bom.csv"
    bom.write_text("\n".join(_digital_lines()) + "\n", encoding="utf-8-sig")
    assert bom.read_bytes()[:3] == b"\xef\xbb\xbf"
    assert digital_csv.read_header(str(bom)) == digital_csv.read_header(plain)
    assert digital_csv.read_header(str(bom))[0] == "Time [s]"
    want, _ = _csv_notes(plain)
    got, notes = _csv_notes(bom)
    assert notes == [] and np.array_equal(got.clock_edges, want.clock_edges)
    assert digital_csv.scan(str(bom)) == digital_csv.scan(plain)
    assert digital_csv.looks_complementary(str(bom), 1, 2) is False
    bare = tmp_path / "bare.csv"
    bare.write_text("\n".join(_digital_lines(header="")) + "\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match=r"bare\.csv: not a capture CSV \(the first row "
                                         r"starts with the number '0\.000000000'"):
        digital_csv.load_capture(str(bare), 1, 2)
