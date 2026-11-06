"""Edge cases of the analog front end, the digital CSV reader and the visualizer engine.

* An analog channel's thresholds come from its numeric samples; a NaN sample does not
  send the channel to the fallback thresholds.
* The analog sample rate is at least 1 Hz: a time axis or a given rate below it is
  replaced by 1 MHz with a warning.
* The analog warnings name each channel by its role after the auto-clock orientation.
* A digital CSV with an empty, over-long or non-text first row, or bytes that are not
  UTF-8, raises ValueError naming the file.
* The engine reports a drawn data port on an unplaceable device, system slots that do
  not fit the frame, and a wide-DRQ truncation only for a drawn port; its interval check
  counts the TxP bit in every flow mode that has one (not NORMAL); its mismatch lists are
  built without duplicates.
* The demo capture builder centres each data edge in its UI and sets the cold-start
  PHY-number bits on DN before the clock edge that carries them.
* The analog sample rate is at most the maximum; the analog front end reports transitions
  that land on one sample; the legacy visualizer CSV converter reports values it changes
  and names the field of a cell that is not a number.
* The legacy converter also reports an excess-one channel count as the count the file
  means, names the row and cell of a legacy value the model refuses, clips a cell an import
  message quotes to sixty characters and caps the list of unrecognised legacy fields.
* A config CSV file that is not UTF-8, is over the size limit, is a device read no further
  than the limit, or has no content is refused or reported by file.
"""
import os
import sys

import numpy as np
import pytest

from swi3s_studio import import_notes
from swi3s_studio.ingest import analog, digital_csv, timebase, transitions, visualizer_csv
from swi3s_studio.swviz.core.engine import BusModelBuilder
from swi3s_studio.swviz.models import FlowMode
from swi3s_studio.swviz.models.interface import Interface
from swi3s_studio.swviz.viz import VizConfig


def _messages(notes):
    return [n.message if hasattr(n, "message") else str(n) for n in notes]


# ------------------------------------------------------------------ analog front end

def test_a_nan_sample_leaves_the_channels_detected_thresholds():
    t = np.arange(2000) * 1e-9
    v = np.where(np.arange(2000) % 2, 5.3, 2.0)
    clean = analog.auto_threshold(v)
    v[500] = np.nan
    assert analog.auto_threshold(v) == pytest.approx(clean, abs=0.05)
    with import_notes.collect() as notes:
        cap = analog.capture_from_analog(t, v, np.where(np.arange(2000) % 8 < 4, 5.3, 2.0),
                                         source="x.csv")
    assert len(cap.clock_edges) > 1900
    assert _messages(notes) == ["1 clock sample is not a number; each holds the level "
                                "before it"]


def test_a_channel_of_only_nan_uses_the_fallback_thresholds():
    assert analog.auto_threshold(np.full(10, np.nan)) == (0.6, 0.4)


def test_the_median_step_of_an_even_count_is_the_mean_of_the_middle_two():
    t = np.cumsum([0.0, 1.0, 1.0, 2.0, 4.0])      # steps 1, 1, 2, 4
    assert analog._median_positive_step(t) == 1.5


def test_a_time_axis_below_one_hertz_assumes_one_megahertz_and_says_so():
    t = np.arange(40, dtype=np.float64) * 100.0
    clk = np.tile([0.0, 3.3], 20)
    with import_notes.collect() as notes:
        cap = analog.capture_from_analog(t, clk, clk, source="s.csv")
    assert cap.sample_rate_hz == 1_000_000
    assert _messages(notes) == ["the time axis gives a sample rate of 0.01 Hz, below 1 Hz; "
                                "1 MHz is assumed"]


def test_a_time_axis_with_no_step_does_not_give_a_sample_rate():
    t = np.zeros(40)
    clk = np.tile([0.0, 3.3], 20)
    with import_notes.collect() as notes:
        cap = analog.capture_from_analog(t, clk, clk, source="s.csv")
    assert cap.sample_rate_hz == 1_000_000
    assert "the time axis does not give a sample rate; 1 MHz is assumed" in _messages(notes)


def test_a_time_axis_too_far_from_its_start_for_the_assumed_rate_is_refused_by_name():
    t = np.arange(40, dtype=np.float64) * 1e300
    clk = np.tile([0.0, 3.3], 20)
    with import_notes.collect() as notes:
        with pytest.raises(ValueError, match="s.csv: .*too far from the capture start"):
            analog.capture_from_analog(t, clk, clk, source="s.csv")
    assert any("gives a sample rate of" in m and "below 1 Hz" in m for m in _messages(notes))


def test_a_time_axis_with_subnormal_steps_is_refused_by_name():
    t = np.arange(10) * 1e-320
    clk = np.tile([0.0, 3.3], 5)
    with pytest.raises(ValueError, match=r"s\.csv: the time axis steps by .* above"):
        analog.capture_from_analog(t, clk, clk, source="s.csv")


@pytest.mark.parametrize("step, ok", [(1e-15, True), (1e-16, False)])
def test_the_finest_time_axis_step_is_a_femtosecond(step, ok):
    t = np.arange(40, dtype=np.float64) * step
    clk = np.tile([0.0, 3.3], 20)
    if ok:
        assert analog.capture_from_analog(t, clk, clk).sample_rate_hz == 10 ** 15
    else:
        with pytest.raises(ValueError, match="above"):
            analog.capture_from_analog(t, clk, clk)


@pytest.mark.parametrize("rate, problem", [
    (2.0 ** 64, "is above 1e+15 Hz"), (1e300, "is above 1e+15 Hz"),
    (float("inf"), "is above 1e+15 Hz"), (float("nan"), "is not a number")])
def test_a_given_rate_beyond_the_maximum_is_replaced_by_the_time_axis_rate(rate, problem):
    t = np.arange(40) * 1e-6
    clk = np.tile([0.0, 3.3], 20)
    with import_notes.collect() as notes:
        cap = analog.capture_from_analog(t, clk, clk, sample_rate_hz=rate, source="s.csv")
    assert cap.sample_rate_hz == 1_000_000
    assert _messages(notes) == [f"the sample rate {rate!r} Hz {problem}; it is taken from "
                                f"the time axis instead"]


@pytest.mark.parametrize("rate, problem", [("abc", "is not a number"),
                                           (10 ** 400, "is above 1e+15 Hz")])
def test_a_given_rate_that_is_not_a_float_is_warned_about_and_replaced(rate, problem):
    t = np.arange(40) * 1e-6
    clk = np.tile([0.0, 3.3], 20)
    with import_notes.collect() as notes:
        cap = analog.capture_from_analog(t, clk, clk, sample_rate_hz=rate, source="s.csv")
    assert cap.sample_rate_hz == 1_000_000
    assert _messages(notes) == [f"the sample rate {rate!r} Hz {problem}; it is taken from "
                                f"the time axis instead"]


def test_analog_transitions_that_land_on_one_sample_are_counted_for_both_channels():
    n = 400
    t = np.arange(n) * 1e-6
    clk = np.where((np.arange(n) // 2) % 2 == 0, 0.0, 3.3)
    dat = np.where((np.arange(n) // 4) % 2 == 0, 0.0, 3.3)
    with import_notes.collect() as notes:
        cap = analog.capture_from_analog(t, clk, dat, sample_rate_hz=1000, source="s.csv")
    merged = [m for m in _messages(notes) if "land on the same sample" in m]
    want = (timebase.repeats(cap.clock_edges) + timebase.repeats(cap.data_edges))
    assert want > 100
    assert merged == [f"{want:,} transitions land on the same sample as the one before at "
                      f"1,000 Hz (the rate is coarser than the transitions)"]
    with import_notes.collect() as notes:
        analog.capture_from_analog(t, clk, dat, source="s.csv")
    assert not any("land on the same sample" in m for m in _messages(notes))


def test_a_given_rate_below_one_hertz_is_replaced_by_the_time_axis_rate():
    t = np.arange(40) * 1e-6
    clk = np.tile([0.0, 3.3], 20)
    with import_notes.collect() as notes:
        cap = analog.capture_from_analog(t, clk, clk, sample_rate_hz=0.01, source="s.csv")
    assert cap.sample_rate_hz == 1_000_000
    assert _messages(notes) == ["the sample rate 0.01 Hz is below 1 Hz; it is taken from the "
                                "time axis instead"]


def test_the_analog_warnings_name_the_channel_by_its_role_after_the_swap():
    t = np.arange(200) * 1e-9
    busy = 3.3 * (np.arange(200) % 2)
    flat = np.zeros(200)
    with import_notes.collect() as notes:
        # the busy channel is given as `data`: after the swap it is the clock
        analog.capture_from_analog(t, flat, busy, auto_clock=True, source="s.csv")
    msgs = _messages(notes)
    assert any(m.startswith("the data channel's thresholds could not be detected")
               for m in msgs)
    assert not any(m.startswith("the clock channel's thresholds") for m in msgs)


def test_the_never_crosses_warning_pairs_each_channel_with_its_own_span_after_the_swap():
    t = np.arange(200) * 1e-9
    quiet = 1.0 + 1e-4 * (np.arange(200) % 2)             # between the thresholds
    busy = 3.3 * (np.arange(200) % 2)
    for first, second, quiet_role in ((quiet, busy, "data"), (busy, quiet, "data")):
        with import_notes.collect() as notes:
            analog.capture_from_analog(t, first, second, auto_clock=True, source="s.csv",
                                       clock_thresh=(2.0, 1.0), data_thresh=(2.0, 1.0))
        never = [m for m in _messages(notes) if "never crosses" in m]
        assert never == [f"the {quiet_role} channel never crosses its thresholds 2 V / 1 V "
                         f"(its samples span 1 V to 1.0001 V); it has no transitions"]


# ------------------------------------------------------------------- digital CSV

def _write(path, data: bytes):
    path.write_bytes(data)
    return str(path)


def test_an_empty_digital_csv_raises_a_value_error_naming_the_file(tmp_path):
    p = _write(tmp_path / "empty.csv", b"")
    with pytest.raises(ValueError, match="empty.csv"):
        digital_csv.read_header(p)


def test_a_first_row_with_no_line_break_is_refused_without_being_split(tmp_path):
    p = _write(tmp_path / "flat.csv", b"0," * (timebase.MAX_LINE_CHARS))
    for call in (digital_csv.read_header, digital_csv.scan,
                 lambda q: digital_csv.load_capture(q, 1, 2)):
        with pytest.raises(ValueError, match="flat.csv"):
            call(p)


def test_a_first_row_with_too_many_columns_is_refused(tmp_path):
    n = digital_csv._MAX_HEADER_CELLS + 1
    p = _write(tmp_path / "wide.csv", ("Time," + ",".join(["c"] * n) + "\n0,1\n").encode())
    with pytest.raises(ValueError, match="wide.csv"):
        digital_csv.read_header(p)


@pytest.mark.parametrize("data", [bytes(range(128, 256)) * 32,
                                  "Time,A,B\n0,1,0\n".encode("utf-16")],
                         ids=["high_bytes", "utf16"])
def test_bytes_that_are_not_utf8_text_raise_a_value_error_naming_the_file(tmp_path, data):
    p = _write(tmp_path / "bin.csv", data)
    with pytest.raises(ValueError, match="bin.csv"):
        digital_csv.load_capture(p, 1, 2)
    with pytest.raises(ValueError, match="bin.csv"):
        digital_csv.read_header(p)


# ------------------------------------------------------------------ visualizer engine

def _build(iface, viz=None, rows=4):
    viz = viz or VizConfig()
    return BusModelBuilder(iface, rows, viz).build()


def _issues(bm):
    return [name for name, _ in bm.warnings.validation_issues]


@pytest.mark.parametrize("device", [12, -2])
def test_a_drawn_port_on_an_unplaceable_device_is_reported(device):
    iface = Interface()
    viz = VizConfig()
    iface.set_dp_device(0, device)
    iface.data_ports[0].config.EnableCh_REG = 1
    viz.data_ports[0].enabled = True
    bm = _build(iface, viz)
    assert "DP0" in _issues(bm)
    result = dict(bm.warnings.validation_issues)["DP0"]
    assert any(e.field == "DeviceNumber" for e in result.errors)


def test_a_port_on_an_unplaceable_device_that_is_not_drawn_is_not_reported():
    iface = Interface()
    iface.set_dp_device(0, 12)
    assert "DP0" not in _issues(_build(iface))


@pytest.mark.parametrize("num_columns_reg, reported", [(3, True), (4, True), (5, False),
                                                       (6, False), (15, False)])
def test_phy3_system_slots_are_reported_only_when_they_overflow_the_frame(
        num_columns_reg, reported):
    """NumColumns_REG is the column count minus one (REG 5 is 6 columns). The default
    interface with a 2-column S0 needs 6 columns: REG 5 and above fit, REG 4 overflows."""
    iface = Interface()
    iface.phy3_enabled = True
    iface.s0_width = 2
    iface.NumColumns_REG = num_columns_reg
    assert ("System slot layout" in _issues(_build(iface))) is reported


def test_a_legacy_flag_that_is_not_true_or_false_reads_false_and_is_reported(tmp_path):
    lines = ["S0 S1 Enabled,x", "CDS Guard Enabled,maybe", "Draw S0 Handover,2",
             "CDS Guard Polarity,"]
    _text, notes = _upgrade(_legacy(tmp_path, lines))
    [note] = notes
    assert note.startswith("4 legacy values did not fit and were changed: ")
    for part in ("S0 S1 Enabled is 'x', not true or false; false is used",
                 "CDS Guard Enabled is 'maybe', not true or false; false is used",
                 "Draw S0 Handover is '2', not true or false; false is used",
                 "CDS Guard Polarity is '', not true or false; false is used"):
        assert part in note


def test_a_legacy_per_port_flag_that_is_not_true_or_false_names_its_port(tmp_path):
    row = "Data Port Enabled,yes,x," + ",".join(["no"] * 10)
    text, notes = _upgrade(_legacy(tmp_path, [row]))
    assert notes == ["1 legacy value did not fit and was changed: Data Port Enabled for "
                     "data port 1 is 'x', not true or false; false is used."]


def test_a_legacy_flag_in_any_case_of_the_closed_set_is_not_reported(tmp_path):
    lines = ["S0 S1 Enabled,TRUE", "CDS Guard Enabled,yes", "Draw S0 Handover,1",
             "CDS Guard Polarity,No", "Data Port Enabled,FALSE," + ",".join(["0"] * 11)]
    _text, notes = _upgrade(_legacy(tmp_path, lines))
    assert notes == []


def test_a_truncated_system_slot_is_named_for_the_slot_not_for_phy3():
    iface = Interface()
    iface.phy3_enabled = False
    iface.NumColumns_REG = 1
    iface.CDS_BitWidth_REG = 7
    names = _issues(_build(iface))
    assert "System slot truncation" in names
    assert "PHY3 Truncation" not in names


def _drq_sink(iface, viz, enabled):
    iface.set_dp_device(3, 0)
    cfg = iface.data_ports[3].config
    cfg.EnableCh_REG = 1
    cfg.PortDirection_REG = True
    cfg.FlowMode_REG = FlowMode.RX_CONTROLLED
    fcp = iface.flow_control_ports[3].config
    fcp.FCP_HorizontalStart_REG = 15
    fcp.FCP_BitWidth_REG = 2
    viz.data_ports[3].enabled = enabled


def test_a_wide_drq_truncation_is_reported_only_for_a_drawn_port():
    for enabled, expect in ((True, True), (False, False)):
        iface, viz = Interface(), VizConfig()
        iface.NumColumns_REG = 15
        _drq_sink(iface, viz, enabled)
        bm = _build(iface, viz)
        assert bool(bm.warnings.drq_truncation_warnings) is expect


def _interval_overflow(mode):
    iface, viz = Interface(), VizConfig()
    iface.NumColumns_REG = 15
    iface.set_dp_device(0, 0)
    cfg = iface.data_ports[0].config
    cfg.EnableCh_REG = 1
    cfg.SampleSize_REG = 13
    cfg.HorizontalStart_REG = 2
    cfg.HorizontalCount_REG = 13
    cfg.FlowMode_REG = mode
    viz.data_ports[0].enabled = True
    return _build(iface, viz).warnings.interval_overflow_warnings


@pytest.mark.parametrize("mode, counts_txp", [
    (FlowMode.NORMAL, False), (FlowMode.TX_CONTROLLED, True),
    (FlowMode.RX_CONTROLLED, True), (FlowMode.ASYNC, True)])
def test_the_interval_check_counts_the_txp_bit_in_every_mode_that_has_one(mode, counts_txp):
    assert _interval_overflow(mode) == ([("DP0", 15, 14)] if counts_txp else [])


def test_the_mismatch_lists_hold_each_mismatch_once():
    """A source and a sink over the same window disagree at every bit of every row; the
    lists hold each mismatch once."""
    iface, viz = Interface(), VizConfig()
    iface.NumColumns_REG = 31
    for dp in (0, 1):
        sink = dp == 1
        iface.set_dp_device(dp, dp)
        cfg = iface.data_ports[dp].config
        cfg.EnableCh_REG = 3
        cfg.SampleSize_REG = 13 if sink else 14
        cfg.ScramblerEn_REG = not sink
        cfg.PortDirection_REG = sink
        cfg.HorizontalStart_REG = 0
        cfg.HorizontalCount_REG = 31
        viz.data_ports[dp].enabled = True
    bm = _build(iface, viz, 1024)
    for found in (bm.warnings.scrambler_mismatches, bm.warnings.sample_bit_mismatches):
        assert len(found) == 28 * 1024
        assert len(found) == len(set(found))


# ------------------------------------------------------- legacy visualizer CSV converter

def _legacy(tmp_path, rows, name="old.csv"):
    p = tmp_path / name
    p.write_text("Columns per Row (2-32),8\nRow Rate [kHz],3072\n" + "\n".join(rows) + "\n",
                 encoding="utf-8")
    return str(p)


def _upgrade(path):
    with import_notes.collect() as notes:
        out = visualizer_csv.normalized_path(path)
    try:
        with open(out, encoding="utf-8") as f:
            text = f.read()
    finally:
        if out != path:
            os.remove(out)
    return text, [n.message for n in notes]


def _enable_ch(text, port):
    for line in text.splitlines():
        if line.startswith("EnableCh_REG"):
            return line.split(",")[port + 1]
    raise KeyError("EnableCh_REG")


def test_a_channel_count_beyond_the_register_is_clamped_without_building_a_wide_mask(tmp_path):
    counts = ",".join(["2147483648"] + ["3"] * 11)
    text, notes = _upgrade(_legacy(tmp_path, [f"Data Port Channels,{counts}",
                                              "Data Port Enabled," + ",".join(["True"] * 12)]))
    assert len(text) < 100_000
    assert int(_enable_ch(text, 0), 0) == 0xFFFF and int(_enable_ch(text, 1), 0) == 0b111
    assert notes == ["1 legacy value did not fit and was changed: Data Port Channels "
                     "2147483648 for data port 0 is above 16; 16 channels are enabled."]


def test_a_channel_count_of_exactly_the_register_width_is_not_changed(tmp_path):
    text, notes = _upgrade(_legacy(tmp_path, ["Data Port Channels," + ",".join(["16"] * 12)]))
    assert int(_enable_ch(text, 5), 0) == 0xFFFF and notes == []


def test_a_count_below_one_is_reported_once_per_value(tmp_path):
    text, notes = _upgrade(_legacy(tmp_path, [
        "Data Port Channels,-3," + ",".join(["1"] * 11),
        "Data Port Sample Width,-1," + ",".join(["8"] * 11),
        "Data Port Interval,0," + ",".join(["2"] * 11)]))
    assert int(_enable_ch(text, 0), 0) == 0
    assert len(notes) == 1 and notes[0].startswith("3 legacy values did not fit and were "
                                                   "changed: ")
    for part in ("Data Port Channels -3 for data port 0 is below 1; no channel is enabled",
                 "Data Port Sample Width -1 for data port 0 is below 1; 1 is used",
                 "Data Port Interval 0 for data port 0 is below 1; 1 is used"):
        assert part in notes[0]


def test_rows_to_draw_outside_its_range_is_clamped_and_reported(tmp_path):
    for value, used in (("-5", 1), ("0", 1), ("1", 1), ("10240", 10240), ("10241", 10240)):
        _text, notes = _upgrade(_legacy(tmp_path, [f"Rows to Draw,{value}"]))
        if value in ("1", "10240"):
            assert notes == [], value
        else:
            assert notes == [f"1 legacy value did not fit and was changed: Rows to Draw "
                             f"{value} is outside 1 to 10240; {used} is used."], value


@pytest.mark.parametrize("value, used", [("0", "1"), ("-5", "1"), ("61440", "48000"),
                                         ("48000.5", "48000"), ("0.5", "1")])
def test_row_rate_outside_its_range_is_clamped_and_reported(tmp_path, value, used):
    text, notes = _upgrade(_legacy(tmp_path, [f"Row Rate [kHz],{value}"]))
    assert notes == [f"1 legacy value did not fit and was changed: Row Rate [kHz] {value} is "
                     f"outside 1 to 48000; {used} is used."]
    assert f"RowRate,{used}.0" in text.splitlines()


@pytest.mark.parametrize("value", ["1", "48000", "768", "3072.5"])
def test_row_rate_inside_its_range_is_not_changed(tmp_path, value):
    text, notes = _upgrade(_legacy(tmp_path, [f"Row Rate [kHz],{value}"]))
    assert notes == []
    assert f"RowRate,{float(value)}" in text.splitlines()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_a_row_rate_that_is_not_finite_is_refused_by_field(tmp_path, value):
    with pytest.raises(ValueError, match=r"Row Rate \[kHz\] is '%s', not a finite number"
                                        % value):
        _upgrade(_legacy(tmp_path, [f"Row Rate [kHz],{value}"]))


def test_a_cell_that_is_not_a_whole_number_names_its_field_and_port(tmp_path):
    with pytest.raises(ValueError, match=r"old\.csv: could not convert legacy visualizer CSV: "
                                        r"Data Port Channels for data port 0 is 'nan'"):
        _upgrade(_legacy(tmp_path, ["Data Port Channels,nan"]))
    with pytest.raises(ValueError, match="Rows to Draw is 'x', not a whole number"):
        _upgrade(_legacy(tmp_path, ["Rows to Draw,x"]))
    with pytest.raises(ValueError, match=r"Row Rate \[kHz\] is 'fast', not a number"):
        _upgrade(_legacy(tmp_path, ["Row Rate [kHz],fast"]))


@pytest.mark.parametrize("text", ["", "\n\n", "   \n \t \n", "\ufeff"])
def test_a_config_csv_with_no_content_is_reported_as_having_no_field(text):
    with import_notes.collect() as notes:
        assert visualizer_csv.to_current_text(text, "empty.csv") == text
    assert [str(n) for n in notes] == [
        "empty.csv: no config field is recognised (no _REG row and no legacy field); every "
        "setting takes its default."]


def test_a_zero_byte_config_csv_file_is_reported_when_it_is_opened(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_bytes(b"")
    with import_notes.collect() as notes:
        assert visualizer_csv.normalized_path(str(p)) == str(p)
    assert len(notes) == 1 and "no config field is recognised" in str(notes[0])


# ------------------------------------------------------------- the synthetic capture builder

@pytest.mark.parametrize("spu", [32, 20.345, 7.5])
def test_each_data_edge_lies_in_its_ui_between_the_two_clock_edges(spu):
    rng = np.random.default_rng(4)
    levels = rng.integers(0, 2, 400).astype(bool)
    cap = transitions.build_capture_from_levels(levels, samples_per_ui=spu)
    clk = cap.clock_edges.astype(np.int64)
    dat = cap.data_edges.astype(np.int64)
    before = np.searchsorted(clk, dat, side="right")          # clock edges at or before each
    assert (before >= 1).all() and (before < clk.size).all()
    assert (clk[before - 1] < dat).all() and (dat < clk[before]).all()
    if spu == int(spu):
        # centred: setup equals hold, half a UI each side
        assert ((dat - clk[before - 1]) == spu // 2).all()
        assert ((clk[before] - dat) == spu // 2).all()
    else:
        assert np.abs((dat - clk[before - 1]) - (clk[before] - dat)).max() <= 1


def test_the_demo_capture_centres_every_data_edge_in_its_ui():
    """Setup and hold of the demo's data edges are each half a UI, within a sample."""
    cap = transitions.demo_capture(300)
    clk = cap.clock_edges.astype(np.int64)
    dat = cap.data_edges.astype(np.int64)
    before = np.searchsorted(clk, dat, side="right")
    assert dat.size > 0 and (before >= 1).all() and (before < clk.size).all()
    setup, hold = dat - clk[before - 1], clk[before] - dat
    assert (setup > 0).all() and (hold > 0).all()
    assert np.abs(setup - hold).max() <= 1


def test_cold_start_sets_dn_two_microseconds_before_the_rising_edge_that_carries_it():
    rate = 500_000_000
    us = lambda x: int(round(x * rate / 1e6))        # noqa: E731
    for phy in (0, 2, 5, 15):
        clk, dat, end, level = transitions.cold_start_edges(phy, rate)
        assert level is False
        assert len(dat) == len(set(dat.tolist()))
        rising = {int(c) for c in clk[::2]}           # the clock alternates rise / fall
        assert all(d + us(2) in rising for d in dat.tolist())
        # DN at each rising edge of the four PHY-number clocks is that bit, MSb first
        level_at = lambda t: int(np.searchsorted(dat, t, side="right")) & 1   # noqa: E731
        edges = sorted(rising)[2:6]
        assert [level_at(e) for e in edges] == [(phy >> i) & 1 for i in (3, 2, 1, 0)]


# -------------------------------------------------------------------- the config CSV file

def test_a_config_csv_that_is_not_utf8_is_refused_by_file(tmp_path):
    p = tmp_path / "latin.csv"
    p.write_bytes(b"NumColumns_REG,8\nNotes,caf\xe9\n")
    with pytest.raises(ValueError, match=r"latin\.csv: not a text config CSV"):
        visualizer_csv.normalized_path(str(p))
    with pytest.raises(ValueError, match=r"latin\.csv: not a text config CSV"):
        visualizer_csv.read_legacy_csv(str(p))


def test_a_config_csv_over_the_size_limit_is_refused_by_file_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(visualizer_csv, "MAX_CONFIG_CHARS", 100)
    p = tmp_path / "big.csv"
    p.write_text("NumColumns_REG,8\n" + "Notes," + "x" * 200 + "\n")
    with pytest.raises(ValueError, match=r"big\.csv: too large for a config CSV \(over 100 "):
        visualizer_csv.normalized_path(str(p))
    p.write_text("NumColumns_REG,8\n")
    with import_notes.collect():
        assert visualizer_csv.normalized_path(str(p)) == str(p)


@pytest.mark.platform
@pytest.mark.skipif(sys.platform == "win32" or not os.path.exists("/dev/zero"),
                    reason="needs /dev/zero, an endless stream")
def test_a_device_as_the_config_csv_is_read_no_further_than_the_limit():
    with pytest.raises(ValueError, match=r"zero: too large for a config CSV"):
        visualizer_csv.read_config_text("/dev/zero")


def test_an_excess_one_channel_count_is_reported_as_the_count_the_file_means(tmp_path):
    rows = ["Save file using excess one,True", "Data Port Channels,16," + ",".join(["0"] * 11)]
    _text, notes = _upgrade(_legacy(tmp_path, rows))
    assert notes == ["1 legacy value did not fit and was changed: Data Port Channels 16 "
                     "(17 channels) for data port 0 is above 16; 16 channels are enabled."]


def test_a_legacy_value_the_model_refuses_names_its_row_and_cell(tmp_path):
    with pytest.raises(ValueError, match=r"CDS Tail Width is '5', which the model refuses"):
        _upgrade(_legacy(tmp_path, ["CDS Tail Width,5"]))
    with pytest.raises(ValueError, match=r"Skipping Denominator is '9999', which the model "
                                         r"refuses \(Skipping denominator must be <= 4095"):
        _upgrade(_legacy(tmp_path, ["Skipping Denominator,9999"]))


def test_a_legacy_cell_an_import_message_quotes_is_clipped_to_sixty_characters(tmp_path):
    long = "9" * 100_000
    with pytest.raises(ValueError) as err:
        _upgrade(_legacy(tmp_path, [f"Rows to Draw,{long}x"]))
    assert len(str(err.value)) < 200
    _text, notes = _upgrade(_legacy(tmp_path, [f"Row Rate [kHz],0.{long}", "S0 S1 Enabled,"
                                               + long]))
    assert len(notes) == 1 and len(notes[0]) < 600 and "S0 S1 Enabled is '" in notes[0]


def test_the_unrecognised_legacy_field_list_is_capped(tmp_path):
    rows = [f"Bogus{i},1" for i in range(30)] + ["Z" * 5000 + ",1"]
    _text, notes = _upgrade(_legacy(tmp_path, rows))
    [note] = notes
    assert "31 legacy fields are not recognised and were skipped: Bogus0, Bogus1" in note
    assert note.endswith(" and 23 more.") and len(note) < 600
