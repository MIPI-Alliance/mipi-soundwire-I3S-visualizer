"""Import warnings from the capture readers: what each skips, sorts, rounds, defaults or
ignores in a file is reported through import_notes, naming the file, and a well-formed
file of each format reports nothing. Every input is synthetic, written to tmp_path."""
from __future__ import annotations

import json
import os
import struct
import zipfile

import numpy as np
import swi3score

from swi3s_studio import import_notes
from swi3s_studio.analysis import dlv_detect
from swi3s_studio.ingest import (
    analog,
    analog_csv,
    digital_csv,
    sal_export,
    saleae_binary,
    saleae_sal,
    vcd,
)
from swi3s_studio.ingest.transitions import build_capture_from_levels
from swi3s_studio.session import Session


def _messages(notes, source):
    assert all(n.source == source for n in notes), notes
    return [n.message for n in notes]


# ---------------------------------------------------------------------------- VCD

_VCD_HEAD = """$timescale {ts} $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 1 " dat $end
$var wire 1 # idle $end
$upscope $end
$enddefinitions $end
$dumpvars
x!
x"
$end
"""


def _vcd(tmp_path, body, ts="1 ns", name="cap.vcd"):
    p = tmp_path / name
    p.write_text(_VCD_HEAD.format(ts=ts) + body)
    return str(p)


def test_a_well_formed_vcd_reports_nothing(tmp_path):
    p = _vcd(tmp_path, "#0\n0!\n0\"\n#10\n1!\n#20\n0!\n1\"\n#30\n1!\n")
    with import_notes.collect() as notes:
        vcd.load_capture(p, "!", '"')
    assert notes == []


def test_a_vcd_reports_rounding_xz_backwards_time_and_a_bare_hash(tmp_path):
    p = _vcd(tmp_path, "#0\n0!\n0\"\n#10\n1!\n#20\n0!\nx\"\n#15\n1!\n1\"\n#\n", ts="3 ns")
    with import_notes.collect() as notes:
        vcd.load_capture(p, "!", '"')
    assert _messages(notes, "cap.vcd") == [
        "the $timescale of 3 ns is not a whole number of hertz; the sample rate is "
        "rounded to 333,333,333 Hz",
        "1 x/z value on signal 'dat' (first at #20) was read as the level before it",
        "time goes backwards at 1 place (first to #15); the edges are sorted into time "
        "order",
        "1 \"#\" with no time was ignored (the dump may be truncated)",
    ]


def test_a_vcd_signal_with_no_value_is_reported_as_constant(tmp_path):
    p = _vcd(tmp_path, "#0\n0!\n#10\n1!\n")
    with import_notes.collect() as notes:
        vcd.load_capture(p, "!", "#")
    assert _messages(notes, "cap.vcd") == [
        "signal 'idle' has no 0/1 value change; it is read as a constant 0"]


def test_vcd_x_fills_inside_dumpoff_and_before_a_first_value_are_not_reported(tmp_path):
    p = _vcd(tmp_path, "#0\n0!\n0\"\n#10\n1!\n$dumpoff\nx!\nx\"\n$end\n#20\n$dumpon\n0!\n"
                       "0\"\n$end\n")
    with import_notes.collect() as notes:
        vcd.load_capture(p, "!", '"')
    assert notes == []


# ------------------------------------------------------------------- digital CSV

def _digital_rows(n=40):
    return ["Time [s],Channel 0,Channel 1"] + [
        f"{i * 1e-6:.9f},{i % 2},{(i // 3) % 2}" for i in range(n)]


def test_a_well_formed_digital_csv_reports_nothing(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("\n".join(_digital_rows()) + "\n\n")      # a blank line at the end too
    with import_notes.collect() as notes:
        digital_csv.load_capture(str(p), 1, 2)
    assert notes == []


def test_a_digital_csv_reports_short_rows_bad_times_and_bad_levels(tmp_path):
    rows = _digital_rows()
    rows[5] = "0.000004,1"
    rows[8] = "abc,1,0"
    rows[10] = rows[10].rsplit(",", 1)[0] + ",x"
    rows[12] = rows[12].rsplit(",", 1)[0] + ",true"
    p = tmp_path / "d.csv"
    p.write_text("\n".join(rows) + "\n")
    with import_notes.collect() as notes:
        digital_csv.load_capture(str(p), 1, 2)
    assert _messages(notes, "d.csv") == [
        "1 row too short for the chosen columns was skipped (first at line 6)",
        "1 row with a time that is not a number was skipped (first at line 9: 'abc')",
        "2 data cells not 0 or 1 were read as 0 (first at line 11: 'x')",
    ]


def test_a_digital_csv_out_of_time_order_is_reported_sorted(tmp_path):
    rows = _digital_rows()
    rows[3], rows[4] = rows[4], rows[3]
    p = tmp_path / "d.csv"
    p.write_text("\n".join(rows) + "\n")
    with import_notes.collect() as notes:
        cap = digital_csv.load_capture(str(p), 1, 2)
    assert _messages(notes, "d.csv") == [
        "the rows are not in time order; they are sorted by time"]
    assert np.all(np.diff(cap.clock_edges.astype(np.int64)) > 0)


def test_a_digital_csv_with_no_rate_in_its_timestamps_reports_the_default(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("Time,a,b\n0,0,0\n0,1,1\n")
    with import_notes.collect() as notes:
        cap = digital_csv.load_capture(str(p), 1, 2)
    assert cap.sample_rate_hz == 1_000_000
    assert _messages(notes, "d.csv") == [
        "the timestamps do not give a sample rate; 1 MHz is assumed"]


def test_a_complementary_csv_whose_rate_is_snapped_reports_it(tmp_path):
    # Edges on a 1.01 µs grid: 990,099 Hz from the spacing, snapped to 1 MHz.
    rows = ["Time [s],Channel 0,Channel 1"] + [
        f"{i * 1.01e-6:.9f},{i % 2},{1 - i % 2}" for i in range(20)]
    p = tmp_path / "dlv.csv"
    p.write_text("\n".join(rows) + "\n")
    with import_notes.collect() as notes:
        assert dlv_detect.true_sample_rate_csv(str(p)) == 1_000_000
    assert _messages(notes, "dlv.csv") == [
        "the timestamps' spacing gives a sample rate of 990,099 Hz; it is snapped to the "
        "standard rate of 1,000,000 Hz"]


def test_a_complementary_csv_on_a_standard_rate_reports_nothing(tmp_path):
    rows = ["Time [s],Channel 0,Channel 1"] + [
        f"{i * 1e-6:.9f},{i % 2},{1 - i % 2}" for i in range(20)]
    p = tmp_path / "dlv.csv"
    p.write_text("\n".join(rows) + "\n")
    with import_notes.collect() as notes:
        dlv_detect.true_sample_rate_csv(str(p))
    assert notes == []


# --------------------------------------------------------- analog CSV, front end

def _analog_rows(n=200):
    rows = ["Model,SCOPE,,", "Sample Interval,1e-9", "", "TIME,CH1,CH2"]
    rows += [f"{i * 1e-9:.6e},{3.3 * (i % 2):.3f},{3.3 * ((i // 4) % 2):.3f}"
             for i in range(n)]
    return rows


def test_a_well_formed_analog_csv_reports_nothing(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("\n".join(_analog_rows()) + "\n\n")
    with import_notes.collect() as notes:
        parsed = analog_csv.read_analog_csv(str(p))
        analog.capture_from_analog(parsed["time"], parsed["channels"]["CH1"],
                                   parsed["channels"]["CH2"], None, source="a.csv")
    assert notes == []


def test_an_analog_csv_reports_dropped_rows_extra_cells_and_a_rate_mismatch(tmp_path):
    rows = _analog_rows()
    rows[0:2] = ["Model,SCOPE,,", "Sample Interval,2e-9"]
    rows[10] = "1e-9,0.1"
    rows[20] = rows[20] + ",7"
    rows.append("2.5e-7,1.0,")
    p = tmp_path / "a.csv"
    p.write_text("\n".join(rows) + "\n")
    with import_notes.collect() as notes:
        analog_csv.read_analog_csv(str(p))
    assert _messages(notes, "a.csv") == [
        "the last data row (200 after the TIME header) is not all numbers and was dropped "
        "(the file may be partly written): '2.5e-7,1.0,'",
        "1 row with fewer cells than the 3 columns of the TIME header was skipped (first: "
        "row 7 after the TIME header)",
        "1 row with cells past the 3 columns of the TIME header: those cells are ignored "
        "(first: row 17 after the TIME header)",
        "the TIME column's spacing gives 1000 MHz and the preamble's Sample Interval 500 "
        "MHz; the TIME column's is used",
    ]


def test_the_analog_front_end_reports_sorting_default_rate_flat_and_nan():
    t = np.arange(100, dtype=np.float64) * 1e-9
    clk = 3.3 * (np.arange(100) % 2).astype(np.float64)
    with import_notes.collect() as notes:
        analog.capture_from_analog(t[::-1].copy(), clk[::-1].copy(), np.zeros(100),
                                   None, source="s.csv")
    assert _messages(notes, "s.csv") == [
        "the samples are not in time order; they are sorted by time",
        "the data channel's thresholds could not be detected (it is flat or holds "
        "non-numbers); 0.6 V / 0.4 V are used",
    ]
    dat = clk.copy()
    dat[5] = np.nan
    with import_notes.collect() as notes:
        analog.capture_from_analog(np.zeros(100), clk, dat, None, source="s.csv")
    assert _messages(notes, "s.csv") == [
        "the time axis does not give a sample rate; 1 MHz is assumed",
        "the data channel's thresholds could not be detected (it is flat or holds "
        "non-numbers); 0.6 V / 0.4 V are used",
        "1 data sample is not a number; each holds the level before it",
    ]


# ------------------------------------------------------------------- .wfm pair

def _write_wfm(path, codes, dt=1e-9):
    """A minimal version-3 .wfm: the header fields wfm.read_wfm parses, then int16 codes."""
    codes = np.asarray(codes, dtype="<i2")
    start = 0x346
    buf = bytearray(start + codes.size * 2)
    struct.pack_into("<H", buf, 0x000, 0x0F0F)
    buf[0x002:0x00A] = b":WFM#003"
    struct.pack_into("<B", buf, 0x00F, 2)
    struct.pack_into("<i", buf, 0x010, start)
    struct.pack_into("<i", buf, 0x07A, 2)
    struct.pack_into("<d", buf, 0x0A8, 1.0 / 1000.0)
    struct.pack_into("<d", buf, 0x0B0, 0.0)
    struct.pack_into("<i", buf, 0x0F0, 0)
    struct.pack_into("<d", buf, 0x1E8, dt)
    struct.pack_into("<d", buf, 0x1F0, 0.0)
    struct.pack_into("<I", buf, 0x336, 0)
    struct.pack_into("<I", buf, 0x33A, codes.size * 2)
    buf[start:] = codes.tobytes()
    with open(path, "wb") as f:
        f.write(buf)


def _square(n, period):
    return np.where((np.arange(n) // (period // 2)) % 2 == 1, 1000, -1000)


def test_a_wfm_pair_with_a_flat_channel_reports_the_default_thresholds(tmp_path):
    c, d = str(tmp_path / "clk.wfm"), str(tmp_path / "dat.wfm")
    _write_wfm(c, _square(4000, 20))
    _write_wfm(d, np.full(4000, 500))
    with import_notes.collect() as notes:
        Session.from_wfm(c, d, auto_clock=False)
    assert [str(n) for n in notes] == [
        "clk.wfm + dat.wfm: the data channel's thresholds could not be detected (it is "
        "flat or holds non-numbers); 0.6 V / 0.4 V are used"]


def test_a_well_formed_wfm_pair_reports_nothing(tmp_path):
    c, d = str(tmp_path / "clk.wfm"), str(tmp_path / "dat.wfm")
    _write_wfm(c, _square(4000, 20))
    _write_wfm(d, _square(4000, 200))
    with import_notes.collect() as notes:
        Session.from_wfm(c, d)
    assert notes == []


# -------------------------------------------------------------- binary .bin pair

_RATE = 1_000_000


def _bin_pair(tmp_path, clock_times, data_times, clock_begin=-1e-6, data_begin=-1e-6):
    c, d = str(tmp_path / "clk.bin"), str(tmp_path / "dat.bin")
    saleae_binary.write_channel(c, False, clock_times, clock_begin, 2e-4)
    saleae_binary.write_channel(d, False, data_times, data_begin, 2e-4)
    return c, d


def test_a_well_formed_bin_pair_reports_nothing(tmp_path):
    t = np.arange(1, 200) / _RATE
    c, d = _bin_pair(tmp_path, t, t[::7])
    with import_notes.collect() as notes:
        saleae_binary.load_capture(c, d, _RATE)
    assert notes == []


def test_a_bin_pair_reports_extra_bytes_unsorted_and_off_grid_times_and_its_starts(
        tmp_path):
    t = np.arange(1, 200) / _RATE
    bad = t.copy()
    bad[5], bad[6] = bad[6], bad[5]
    bad[50] += 0.4 / _RATE
    c, d = _bin_pair(tmp_path, bad, t[::7], data_begin=-5e-6)
    with open(c, "ab") as f:
        f.write(b"xyz")
    with import_notes.collect() as notes:
        cap = saleae_binary.load_capture(c, d, _RATE)
    assert [str(n) for n in notes] == [
        "clk.bin: 3 bytes past the 199 transitions the header counts are ignored",
        "clk.bin: the transition times step back at 1 place; they are sorted into time "
        "order",
        "clk.bin: the transition times do not sit on the 1,000,000 Hz sample grid (up to "
        "0.40 of a sample off in the first 199); each is rounded to the nearest sample",
        "clk.bin + dat.bin: the two files start at different times (-1e-06 s and -5e-06 "
        "s); both are read on one timeline from the earlier start",
    ]
    assert np.all(np.diff(cap.clock_edges.astype(np.int64)) > 0)


def test_a_bin_pair_read_at_too_coarse_a_rate_reports_merged_transitions(tmp_path):
    t = np.arange(1, 200) / _RATE
    c, d = _bin_pair(tmp_path, t, t[::7])
    with import_notes.collect() as notes:
        saleae_binary.load_capture(c, d, _RATE // 4)
    msgs = [str(n) for n in notes]
    assert "clk.bin: 148 transitions land on the same sample as the one before at " \
           "250,000 Hz (the rate is coarser than the transitions)" in msgs
    assert any(m.startswith("clk.bin: the transition times do not sit on the 250,000 Hz")
               for m in msgs)


# -------------------------------------------------------------------- .sal archive

def _sal(tmp_path, name="cap.sal"):
    cap = build_capture_from_levels(swi3score.make_demo_levels(16))
    p = str(tmp_path / name)
    sal_export.export_sal(cap, p, clock_channel=0, data_channel=1)
    return p, cap


def test_a_well_formed_sal_reports_nothing_whole_or_windowed(tmp_path):
    p, cap = _sal(tmp_path)
    span = int(max(cap.clock_edges[-1], cap.data_edges[-1]))
    with import_notes.collect() as notes:
        saleae_sal.load_capture(p, 0, 1)
        saleae_sal.load_capture(p, 0, 1, window=(10, span // 2))
    assert notes == []


def test_a_sal_window_past_the_capture_end_is_reported(tmp_path):
    p, cap = _sal(tmp_path)
    span = saleae_sal.capture_span_samples(p, [0, 1])
    rate = cap.sample_rate_hz
    with import_notes.collect() as notes:
        saleae_sal.load_capture(p, 0, 1, window=(10, 2 * span))
    assert _messages(notes, "cap.sal") == [
        f"the window ends at {(2 * span - 1) / rate:.9g} s, past the capture's end at "
        f"{span / rate:.9g} s; it is read to the end of the capture"]


def test_a_sal_meta_entry_naming_a_missing_file_is_reported(tmp_path):
    p, _cap = _sal(tmp_path)
    out = str(tmp_path / "extra.sal")
    with zipfile.ZipFile(p) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "meta.json":
                meta = json.loads(data)
                meta.setdefault("binData", []).append(
                    {"type": "Digital", "file": "digital-9.bin", "deviceChannel": 9})
                data = json.dumps(meta).encode()
            zout.writestr(item, data)
    with import_notes.collect() as notes:
        saleae_sal.load_capture(out, 0, 1)
    assert _messages(notes, "extra.sal") == [
        "1 digital channel entry in meta.json names no file in the archive or no device "
        "channel and is skipped: digital-9.bin"]


# --------------------------------------------------- a window cut after the read

def test_a_window_starting_before_a_whole_read_capture_is_reported(tmp_path):
    p = _vcd(tmp_path, "#0\n0!\n0\"\n" + "".join(
        f"#{10 * k}\n{k % 2}!\n" for k in range(1, 400)))
    with import_notes.collect() as notes:
        Session.from_vcd(p, "!", '"', window=(-1000, 3000))
    assert _messages(notes, "cap.vcd") == [
        "the window starts at -1e-06 s, before the capture; it is read from 0 s"]


# ------------------------------------------------------------------- end to end

def test_opening_a_malformed_capture_shows_the_import_warnings_window(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from conftest import pump_loads
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    rows = _digital_rows(4000)
    rows[100] = "not-a-time,1,0"
    p = tmp_path / "broken.csv"
    p.write_text("\n".join(rows) + "\n")
    win = MainWindow()
    win._load_async(lambda: Session.from_digital_csv(str(p), 1, 2))
    pump_loads(win)
    dlgs = win.import_warning_dialogs()
    assert len(dlgs) == 1
    assert dlgs[0].windowTitle() == "Import warnings — broken.csv"
    assert dlgs[0].lines() == [
        "broken.csv: 1 row with a time that is not a number was skipped (first at line "
        "101: 'not-a-time')"]

