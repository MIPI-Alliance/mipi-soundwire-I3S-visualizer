"""BusConfig reading: what a short or blank config-CSV cell takes, what is reported (including
rows missing from the file and values past a register's width, in a CSV and in a saved config),
the RowRate range, the per-source lists of a saved config, and the device encoding the writer
uses."""
import csv
import io
import typing

import pytest
import swi3score

from swi3s_studio import import_notes
from swi3s_studio.model.bus_config import (
    _CDS_REGISTER_WIDTHS,
    _DP_REGISTER_WIDTHS,
    CDS_NUM_SOURCES,
    ROW_RATE_MAX_KHZ,
    ROW_RATE_MIN_KHZ,
    BusConfig,
    DataPortConfig,
    _low_bits,
    csv_device_identity,
    demo_config,
)


def _read(rows):
    """BusConfig and the warning messages from CSV `rows` (lists of cells)."""
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    with import_notes.collect() as notes:
        cfg = BusConfig.from_csv_text(buf.getvalue(), "t.csv")
    return cfg, [n.message for n in notes]


def _rows():
    return [r for r in csv.reader(io.StringIO(demo_config().to_csv()))]


def _row(rows, key):
    return next(r for r in rows if r[0] == key)


def test_a_short_port_row_takes_the_field_defaults():
    rows = _rows()
    for key in ("ScramblerEn_REG", "EnforceHandover", "DataPortNumber"):
        del _row(rows, key)[3:]
    cfg, notes = _read(rows)
    dflt = DataPortConfig()
    assert cfg.dataports[5].scrambler_en is dflt.scrambler_en is True
    assert cfg.dataports[5].enforce_handover is True
    assert cfg.dataports[5].dp_number == dflt.dp_number == -1
    assert len(notes) == 1 and "ScramblerEn_REG (2 of 12)" in notes[0]


def test_a_blank_port_cell_takes_the_field_default_and_is_reported():
    rows = _rows()
    _row(rows, "SampleSize_REG")[1] = ""             # DP0's demo value is 3; the default is 0
    cfg, notes = _read(rows)
    assert cfg.dataports[0].sample_size == DataPortConfig().sample_size == 0
    assert len(notes) == 1 and "SampleSize_REG (11 of 12)" in notes[0]


def test_a_short_display_fields_row_keeps_the_default_and_is_reported():
    rows = _rows()
    del _row(rows, "DisplayFields")[2:]
    cfg, notes = _read(rows)
    assert cfg.dataports[0].display_fields == 6 and cfg.dataports[7].display_fields == 6
    assert any("DisplayFields (1 of 12)" in n for n in notes)


def test_a_blank_interface_value_takes_the_default_and_is_reported():
    rows = _rows()
    _row(rows, "SkippingDenominator_REG")[1] = ""
    _row(rows, "RowRate")[1] = ""
    cfg, notes = _read(rows)
    assert cfg.row_rate_khz == 0.0               # blank reads as 0, the measured rate
    assert cfg.skipping_denominator == 1
    assert any("RowRate (0 of 1)" in n and "SkippingDenominator_REG (0 of 1)" in n
               for n in notes)


def test_an_absent_skipping_denominator_reads_as_the_register_reset():
    """The C++ core and the Visualizer's Interface both take 1 for a file with no
    SkippingDenominator_REG row; the 16 seeding a new document applies only outside a file."""
    from swi3s_studio.swviz.models.interface import Interface
    rows = [r for r in _rows() if r[0] != "SkippingDenominator_REG"]
    cfg, notes = _read(rows)
    assert cfg.skipping_denominator == Interface().SkippingDenominator_REG == 1
    assert notes == ["1 row is missing and takes its default: SkippingDenominator_REG."]
    assert BusConfig().skipping_denominator == 16


def test_a_non_finite_row_rate_is_reported_and_not_kept():
    for text in ("nan", "inf", "-inf"):
        rows = _rows()
        _row(rows, "RowRate")[1] = text
        cfg, notes = _read(rows)
        assert cfg.row_rate_khz == 0.0
        assert notes == [f"RowRate: 1 cell is not a valid value: '{text}' read as 0.0."]


def test_an_interface_row_with_extra_cells_is_reported():
    rows = _rows()
    _row(rows, "NumColumns_REG").append("9")
    _cfg, notes = _read(rows)
    assert notes == ["values past the last data port or CDS source column are ignored: "
                     "NumColumns_REG ('9')."]


def test_display_fields_letters_other_than_scb_are_reported():
    rows = _rows()
    _row(rows, "DisplayFields")[1] = "sx"
    cfg, notes = _read(rows)
    assert cfg.dataports[0].display_fields == 1
    assert notes == ["DisplayFields: 1 cell is not a valid value: DP0 'sx' read as 's'."]
    _row(rows, "DisplayFields")[1] = "xyz"
    cfg, notes = _read(rows)
    assert cfg.dataports[0].display_fields == 0
    assert notes == ["DisplayFields: 1 cell is not a valid value: DP0 'xyz' read as no fields."]


def test_a_saved_config_with_a_wrong_length_source_list_is_padded_and_reported():
    d = demo_config().to_dict()
    d["cds_tail"] = [2, 1]
    d["cds_drive_type"] = [1] * (CDS_NUM_SOURCES + 2)
    with import_notes.collect() as notes:
        cfg = BusConfig.from_dict(d, "ws.swi3s")
    assert cfg.cds_tail == [2, 1] + [0] * (CDS_NUM_SOURCES - 2)
    assert cfg.cds_drive_type == [1] * CDS_NUM_SOURCES
    assert {n.source for n in notes} == {"ws.swi3s"}
    assert [n.message.split(" has ")[0] for n in notes] == ["cds_tail", "cds_drive_type"]
    assert len(BusConfig.from_csv_text(cfg.to_csv()).cds_tail) == CDS_NUM_SOURCES


def test_a_saved_config_with_unknown_keys_or_extra_data_ports_reports_them():
    d = demo_config().to_dict()
    d["future_setting"] = 1
    d["dataports"][0]["future_field"] = 2
    d["dataports"] += [dict(d["dataports"][0]) for _ in range(2)]
    with import_notes.collect() as notes:
        BusConfig.from_dict(d, "ws.swi3s")
    msgs = [n.message for n in notes]
    assert len(msgs) == 2
    assert "ignored: future_setting, data port future_field" in msgs[0]
    assert "14 data ports saved; the 2 past the 12th are not read" in msgs[1]


def test_a_whole_length_source_list_and_a_scalar_read_without_warnings():
    d = demo_config().to_dict()
    d["cds_tail"] = 2
    with import_notes.collect() as notes:
        cfg = BusConfig.from_dict(d)
    assert notes == [] and cfg.cds_tail == [2] * CDS_NUM_SOURCES


def test_the_csv_encodes_a_device_number_the_way_csv_device_identity_says():
    assert csv_device_identity(-1) == (0, True)
    assert csv_device_identity(-5) == (0, False)
    assert csv_device_identity(15) == (15, False)
    cfg = demo_config()
    cfg.dataports[0].device_number = -1
    cfg.dataports[1].device_number = -5
    rows = _rows_of(cfg)
    assert _row(rows, "ManagerDataport")[1:3] == ["True", "False"]
    assert _row(rows, "DeviceNumber_REG")[1:3] == ["0", "0"]


def _rows_of(cfg):
    return list(csv.reader(io.StringIO(cfg.to_csv())))


def test_from_decoder_config_returns_the_config_and_the_enabled_count():
    hints = typing.get_type_hints(BusConfig.from_decoder_config)
    assert hints["return"] == typing.Tuple[BusConfig, int]
    cfg, n = BusConfig.from_decoder_config({"dataports": [{"enabled": True}] * 14})
    assert isinstance(cfg, BusConfig) and n == 14


def test_a_short_source_list_is_padded_with_the_fields_own_default():
    """cds_drive_type defaults to Normal (1), not 0: the padding is the default."""
    d = demo_config().to_dict()
    d["cds_drive_type"] = [0, 0]
    d["cds_end_drive_early"] = [1]
    with import_notes.collect():
        cfg = BusConfig.from_dict(d)
    assert cfg.cds_drive_type == [0, 0] + [1] * (CDS_NUM_SOURCES - 2)
    assert cfg.cds_end_drive_early == [1] + [0] * (CDS_NUM_SOURCES - 1)


def test_the_cds_bit_width_register_range_is_zero_to_seven():
    rows = _rows()
    for value, expected, warned in (("7", 7, False), ("0", 0, False), ("8", 7, True),
                                    ("9", 7, True), ("-2", 0, True)):
        _row(rows, "CDS_BitWidth_REG")[1] = value
        cfg, notes = _read(rows)
        assert cfg.cds_bit_width == expected and bool(notes) is warned, (value, notes)
        assert not warned or any(f"CDS_BitWidth_REG {value}" in n for n in notes), notes
    for value, expected, warned in ((7, 7, False), (8, 7, True), (-2, 0, True)):
        d = demo_config().to_dict()
        d["cds_bit_width"] = value
        with import_notes.collect() as notes:
            assert BusConfig.from_dict(d, "ws").cds_bit_width == expected
        assert bool(notes) is warned
        assert not warned or any(f"CDS_BitWidth_REG {value}" in n.message for n in notes)


_RANGED = (("NumColumns_REG", "num_columns", 1, 31), ("S0Width", "s0_width", 1, 8),
           ("S1TailWidth_REG", "s1_tail_width", 0, 2),
           ("SkippingDenominator_REG", "skipping_denominator", 1, 4095),
           ("RowsToDraw", "rows_to_draw", 1, 10240))


def test_the_interface_fields_are_held_in_their_ranges_and_the_clamp_is_reported():
    for csv_key, attr, lo, hi in _RANGED:
        for value, expected, warned in ((lo, lo, False), (hi, hi, False), (lo - 1, lo, True),
                                        (hi + 1, hi, True)):
            rows = _rows()
            _row(rows, csv_key)[1] = str(value)
            cfg, notes = _read(rows)
            assert getattr(cfg, attr) == expected, (csv_key, value)
            assert bool(notes) is warned, (csv_key, value, notes)
            if warned:
                assert any(f"{csv_key} {value} is outside {lo}..{hi}" in n for n in notes), notes
            d = demo_config().to_dict()
            d[attr] = value
            with import_notes.collect() as collected:
                assert getattr(BusConfig.from_dict(d, "ws"), attr) == expected, (attr, value)
            assert bool(collected) is warned, (attr, value)


def test_a_saved_config_with_wrong_typed_values_reads_the_rest_and_reports_them():
    d = demo_config().to_dict()
    d["num_columns"] = "x"
    d["rows_to_draw"] = None
    d["row_rate_khz"] = float("inf")
    d["cds_drive_type"][3] = "x"
    d["cds_guard"] = {}
    d["description"] = 5
    d["dataports"][0]["enabled"] = "x"
    d["dataports"][0]["horizontal_start"] = 1.5
    d["dataports"][1]["interval"] = None
    d["dataports"][2]["name"] = ["DP2"]
    d["dataports"][3] = "not a port"
    d["dataports"][4]["sample_size"] = 5.0           # a whole number, read as written
    d["dataports"][4]["guard_enable"] = 1            # 0/1, read as a bool
    with import_notes.collect() as notes:
        cfg = BusConfig.from_dict(d, "ws.swi3s")
    defaults, port = BusConfig(), DataPortConfig()
    assert cfg.num_columns == defaults.num_columns
    assert cfg.rows_to_draw == defaults.rows_to_draw
    assert cfg.row_rate_khz == defaults.row_rate_khz
    assert cfg.cds_drive_type == defaults.cds_drive_type
    assert cfg.cds_guard == defaults.cds_guard
    assert cfg.description == ""
    assert cfg.dataports[0].enabled is False and cfg.dataports[0].horizontal_start == 0
    assert cfg.dataports[1].interval == port.interval
    assert cfg.dataports[2].name == port.name
    assert cfg.dataports[3] == port
    assert cfg.dataports[4].sample_size == 5 and cfg.dataports[4].guard_enable is True
    assert cfg.dataports[5] == DataPortConfig(**demo_config().to_dict()["dataports"][5])
    assert len(notes) == 1 and notes[0].source == "ws.swi3s"
    for fragment in ("num_columns 'x'", "rows_to_draw None", "cds_drive_type[3] 'x'",
                     "dataports[0].enabled 'x'", "dataports[0].horizontal_start 1.5",
                     "dataports[3] 'not a port'"):
        assert fragment in notes[0].message, fragment


def test_a_saved_config_whose_dataports_are_not_a_list_reads_as_the_defaults():
    d = demo_config().to_dict()
    d["dataports"] = "x"
    with import_notes.collect() as notes:
        cfg = BusConfig.from_dict(d, "ws.swi3s")
    assert cfg.dataports == BusConfig().dataports and "dataports 'x'" in notes[0].message


def test_an_int_cell_with_trailing_characters_reads_as_its_leading_digits():
    """The C++ decode reads '5.7' as 5, '0x1G' as 1 and '6ab' as 6, with a warning; the
    authoring reader takes the same width."""
    for text, value in (("5.7", 5), ("6ab", 6), ("0x1G", 1), ("2x", 2), ("abc", 0)):
        rows = _rows()
        _row(rows, "CDS_BitWidth_REG")[1] = text
        cfg, notes = _read(rows)
        assert cfg.cds_bit_width == value, text
        assert notes == [f"CDS_BitWidth_REG: 1 cell is not a valid value: '{text}' read as "
                         f"{value}."], text


def test_a_decimal_cell_past_the_digit_limit_reads_as_zero_with_a_warning():
    """Python refuses to convert a decimal run of more than 4300 digits; the cell is one
    more invalid value, not a failed load."""
    for text in ("1" * 5000, "1" * 5000 + "x"):
        rows = _rows()
        _row(rows, "CDS_BitWidth_REG")[1] = text
        cfg, notes = _read(rows)
        assert cfg.cds_bit_width == 0
        assert len(notes) == 1 and notes[0].startswith("CDS_BitWidth_REG: 1 cell is not a valid "
                                                       "value: '1111"), notes[0][:80]
        assert notes[0].endswith("read as 0.")


def test_a_row_rate_of_zero_is_reported_once():
    rows = _rows()
    _row(rows, "RowRate")[1] = "0"
    cfg, notes = _read(rows)
    assert cfg.row_rate_khz == 0.0
    assert notes == ["RowRate is 0; the decode uses the row rate it measures."]


_RATE_RANGE = f"{ROW_RATE_MIN_KHZ:g}..{ROW_RATE_MAX_KHZ:g}"


def _rate_note(text):
    return (f"RowRate {text} kHz is outside {_RATE_RANGE} kHz; the decode uses the row rate it "
            "measures.")


@pytest.mark.parametrize("text, rate, csv_note, dict_note", [
    ("1", 1.0, None, None), ("48000", 48000.0, None, None),
    ("0.5", 0.0, _rate_note("0.5"), _rate_note("0.5")),
    ("48001", 0.0, _rate_note("48001"), _rate_note("48001")),
    ("1e300", 0.0, _rate_note("1e+300"), _rate_note("1e+300")),
    ("-1", 0.0, "RowRate: 1 cell is not a valid value: '-1' read as 0.0.", _rate_note("-1")),
])
def test_the_row_rate_is_held_in_range_on_the_csv_and_the_authoring_dict(
        text, rate, csv_note, dict_note):
    """RowRate reads as written inside 1..48000 kHz and as 0 (the decode measures the rate)
    outside it, reported once; a negative CSV cell is an invalid value."""
    rows = _rows()
    _row(rows, "RowRate")[1] = text
    cfg, notes = _read(rows)
    assert cfg.row_rate_khz == rate
    assert notes == ([csv_note] if csv_note else [])
    d = demo_config().to_dict()
    d["row_rate_khz"] = float(text)
    with import_notes.collect() as collected:
        assert BusConfig.from_dict(d, "ws").row_rate_khz == rate
    assert [n.message for n in collected] == ([dict_note] if dict_note else [])


def test_a_per_port_field_wider_than_its_register_is_reported_on_both_paths(tmp_path):
    """Each per-port field is checked against its register width, in a CSV and in an
    authoring dict, and the value is kept as written. The notes are the native loader's."""
    for attr, name, bits in _DP_REGISTER_WIDTHS:
        value = 1 << bits
        rows = _rows()
        _row(rows, name)[1] = str(value)
        cfg, notes = _read(rows)
        assert getattr(cfg.dataports[0], attr) == value
        want = (f"{name} does not fit its {bits}-bit register field (0..{value - 1}) for "
                f"DP0 = {value}; the expected registers carry its {_low_bits(bits)} and the "
                "decode uses the value as written.")
        assert want in notes, (name, notes)
        path = tmp_path / f"{name}.csv"
        path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
        assert want in swi3score.csv_warnings(str(path)), name
        d = demo_config().to_dict()
        d["dataports"][0][attr] = value
        with import_notes.collect() as collected:
            assert getattr(BusConfig.from_dict(d, "ws").dataports[0], attr) == value
        assert [n.message for n in collected] == [want], name


def test_a_sample_size_channel_mask_or_port_number_out_of_range_is_reported(tmp_path):
    for key, value, want in (
            ("SampleSize_REG", "35", "SampleSize_REG does not fit its 5-bit register field "
             "(0..31) for DP0 = 35 (read as 3); the audio decode, the grid and the registers "
             "read its low 5 bits."),
            ("EnableCh_REG", "65539", "EnableCh_REG does not fit its 16 channel bits for "
             "DP0 = 65539; only channels 0..15 are read."),
            ("DataPortNumber", "32", "DataPortNumber is outside 0..31 for DP0 = 32; a negative "
             "number is ignored (the port keeps its column's number) and a larger one "
             "addresses no data port register block.")):
        rows = _rows()
        _row(rows, key)[1] = value
        _cfg, notes = _read(rows)
        assert want in notes, (key, notes)
        path = tmp_path / f"{key}.csv"
        path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
        assert want in swi3score.csv_warnings(str(path)), key


def test_a_wide_cds_source_value_is_reported_by_source():
    for attr, name, bits in _CDS_REGISTER_WIDTHS:
        d = demo_config().to_dict()
        d[attr] = [0] * CDS_NUM_SOURCES
        d[attr][2] = 1 << bits
        with import_notes.collect() as collected:
            BusConfig.from_dict(d, "ws")
        assert [n.message for n in collected] == [
            f"{name} does not fit its {bits}-bit register field (0..{(1 << bits) - 1}) for "
            f"Device 1 = {1 << bits}; the expected registers carry its {_low_bits(bits)}."]


def test_a_disabled_or_channelless_port_is_not_checked_against_the_register_widths():
    d = demo_config().to_dict()
    d["dataports"][1]["horizontal_start"] = 999            # DP1 is disabled
    d["dataports"][2].update(enabled=True, enable_ch=0, horizontal_start=999)
    with import_notes.collect() as collected:
        BusConfig.from_dict(d, "ws")
    assert [n.message for n in collected] == []


def test_a_row_missing_from_the_file_is_reported_by_name():
    """Every row the writer always writes is reported when the file lacks it, except
    AppVersion, and Description, which is written only when it is not empty; a CDS row whose
    legacy scalar is present is not missing."""
    rows = _rows()
    keys = [r[0] for r in rows if r[0] not in ("AppVersion",)]
    assert _read(rows)[1] == []
    for key in keys:
        cfg, notes = _read([r for r in rows if r[0] != key])
        assert notes == [f"1 row is missing and takes its default: {key}."], key
    cfg, notes = _read([r for r in rows if r[0] not in ("DisplayFields", "ManagerDataport")])
    assert notes == ["2 rows are missing and take their defaults: ManagerDataport, "
                     "DisplayFields."]
    legacy = [r for r in rows if r[0] != "CDS_TailWidthPerSource"] + [["CDS_TailWidth_REG", "1"]]
    assert _read(legacy)[1] == []


def test_a_truncated_file_names_the_first_missing_rows_and_counts_the_rest():
    rows = _rows()[:3]
    _cfg, notes = _read(rows)
    assert notes == ["45 rows are missing and take their defaults: PHY3Enabled, S0Width, "
                     "S1TailWidth_REG and 42 more."]


def test_a_warning_lists_a_few_names_and_counts_the_rest():
    rows = _rows() + [[f"Bogus{i}", "1"] for i in range(5000)]
    _cfg, notes = _read(rows)
    unknown = [n for n in notes if "not recognised" in n]
    assert len(unknown) == 1 and len(unknown[0]) < 300
    assert unknown[0].startswith("5000 fields are not recognised and were ignored: Bogus0 ")
    assert unknown[0].endswith("and 4997 more.")
    rows = _rows()
    _row(rows, "SampleSize_REG")[1:] = ["x"] * 12
    _cfg, notes = _read(rows)
    assert notes[0].count("'x'") == 3 and notes[0].endswith("and 9 more.")


@pytest.mark.parametrize("word, value", [("no", False), ("No", False), ("false", False),
                                         ("0", False), ("yes", True), ("TRUE", True), ("1", True)])
def test_every_boolean_spelling_reads_without_a_warning(word, value):
    rows = _rows()
    _row(rows, "Enabled")[2] = word
    cfg, notes = _read(rows)
    assert cfg.dataports[1].enabled is value
    assert notes == []
    _row(rows, "Enabled")[2] = "maybe"
    assert _read(rows)[1] == ["Enabled: 1 cell is not a valid value: DP1 'maybe' read as false."]


def _native_notes(rows, tmp_path, name="n.csv"):
    path = tmp_path / name
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    return swi3score.csv_warnings(str(path))


@pytest.mark.parametrize("key,cell", [
    ("PHY3Enabled", "on"), ("PHY3Enabled", "x" * 70), ("Enabled", "maybe"),
    ("SkippingDenominator_REG", "q" * 70), ("SkippingDenominator_REG", "q" * 60),
    ("SkippingDenominator_REG", "q" * 61)])
def test_a_bad_cell_is_worded_and_cut_as_the_native_loader_words_it(key, cell, tmp_path):
    """A flag reads as `false`, and file text is quoted whole up to 60 characters, else cut to
    59 and an ellipsis."""
    rows = _rows()
    _row(rows, key)[1 if key != "Enabled" else 2] = cell
    _cfg, notes = _read(rows)
    native = [n for n in _native_notes(rows, tmp_path) if n.startswith(f"{key}: 1 cell")]
    assert native and [n for n in notes if n.startswith(f"{key}: 1 cell")] == native


@pytest.mark.parametrize("key,cell", [
    ("NumColumns_REG", "1_5"), ("NumColumns_REG", "\u0663"), ("NumColumns_REG", "0x"),
    ("NumColumns_REG", "0b12"), ("NumColumns_REG", "+7"), ("NumColumns_REG", "15.7"),
    ("SkippingDenominator_REG", "1_5"), ("S1TailWidth_REG", "2\u0663"),
    ("CDS_BitWidth_REG", "1_0")])
def test_a_whole_number_cell_reads_the_way_the_native_loader_reads_it(key, cell, tmp_path):
    """ASCII digits only: an underscore or another script's digit ends the number."""
    rows = _rows()
    _row(rows, key)[1] = cell
    _cfg, notes = _read(rows)
    native = [n for n in _native_notes(rows, tmp_path) if n.startswith(f"{key}: 1 cell")]
    assert [n for n in notes if n.startswith(f"{key}: 1 cell")] == native


@pytest.mark.parametrize("cell,value", [
    ("3072 kHz", 3072.0), ("1_000", 1.0), ("1.5e3abc", 1500.0), ("1e", 1.0),
    ("\u0663", 0.0), ("  48000 ", 48000.0)])
def test_a_row_rate_cell_reads_its_leading_number_as_the_native_loader_does(
        cell, value, tmp_path):
    rows = _rows()
    _row(rows, "RowRate")[1] = cell
    cfg, notes = _read(rows)
    assert cfg.row_rate_khz == value
    native = [n for n in _native_notes(rows, tmp_path) if n.startswith("RowRate: 1 cell")]
    if cell.strip() == "48000":
        assert notes == [] and native == []
        return
    assert len(native) == 1 and len(notes) >= 1
    quoted = native[0].split("'")[1]
    assert f"'{quoted}' read as {value}" in notes[0]
    assert float(native[0].rsplit("read as ", 1)[1].rstrip(".")) == value


def test_a_blank_or_absent_row_rate_reads_as_the_measured_rate_the_native_loader_takes(
        tmp_path):
    rows = _rows()
    _row(rows, "RowRate")[1] = ""
    blank_cfg, blank_notes = _read(rows)
    absent_cfg, absent_notes = _read([r for r in rows if r[0] != "RowRate"])
    assert blank_cfg.row_rate_khz == absent_cfg.row_rate_khz == 0.0
    assert any("RowRate (0 of 1)" in n for n in blank_notes)
    assert absent_notes == ["1 row is missing and takes its default: RowRate."]
    assert not any("RowRate is 0" in n for n in blank_notes + absent_notes)
    assert _native_notes([r for r in rows if r[0] != "RowRate"], tmp_path) == [
        "1 row is missing and takes its default: RowRate."]


def test_a_cds_guard_outside_off_g0_g1_in_a_saved_config_reads_as_the_default():
    d = demo_config().to_dict()
    d["cds_guard"] = [7, -1, 3, 0, 1, 2] + [0] * (CDS_NUM_SOURCES - 6)
    with import_notes.collect() as collected:
        cfg = BusConfig.from_dict(d, "ws")
    assert cfg.cds_guard[:6] == [BusConfig().cds_guard[0]] * 3 + [0, 1, 2]
    msgs = [n.message for n in collected]
    assert len(msgs) == 1
    assert all(f"cds_guard[{i}]" in msgs[0] for i in range(3))
    assert BusConfig.from_csv_text(cfg.to_csv()).cds_guard == cfg.cds_guard


def test_a_saved_config_with_hostile_text_gives_short_warnings():
    d = demo_config().to_dict()
    d["cds_guard"] = ["x" * 1_000_000] * CDS_NUM_SOURCES
    for i in range(3000):
        d[f"unknown_{i}_" + "k" * 1000] = 1
    d["dataports"][0]["num_wide"] = 1
    with import_notes.collect() as collected:
        BusConfig.from_dict(d, "ws")
    assert sum(len(n.message) for n in collected) < 5000
    csv_text = ("RowRate," + "9" * 100_000 + "x\nNumColumns_REG," + "z" * 100_000 + "\n"
                + "".join(f"junk{i}_" + "j" * 1000 + ",1\n" for i in range(300)))
    with import_notes.collect() as collected:
        BusConfig.from_csv_text(csv_text, "t.csv")
    assert sum(len(n.message) for n in collected) < 5000


def _csv_rows(extra, drop=()):
    rows = [r for r in _rows() if r and r[0] not in drop]
    buf = io.StringIO()
    csv.writer(buf).writerows(rows + extra)
    return buf.getvalue()


def test_a_blank_scalar_cds_drive_type_leaves_the_default():
    text = _csv_rows([["CDS_DriveType", ""]], drop=("CDS_DriveTypePerSource",))
    with import_notes.collect() as notes:
        cfg = BusConfig.from_csv_text(text, "t.csv")
    assert cfg.cds_drive_type == [1] * 13
    assert any("CDS_DriveType" in n.message for n in notes)


def test_a_scalar_cds_drive_type_broadcasts_to_every_source():
    text = _csv_rows([["CDS_DriveType", "0"]], drop=("CDS_DriveTypePerSource",))
    assert BusConfig.from_csv_text(text, "t.csv").cds_drive_type == [0] * 13


def test_a_config_csv_with_a_bom_reads_its_first_field(tmp_path):
    text = "NumColumns_REG,9\nCDS_BitWidth_REG,3\n"
    p = tmp_path / "bom.csv"
    p.write_text(text, encoding="utf-8-sig")
    assert BusConfig.from_csv(str(p)).num_columns == 9
    assert BusConfig.from_csv_text("\ufeff" + text).num_columns == 9


@pytest.mark.parametrize("key, per_source, value, default", [
    ("CDS_TailWidth_REG", "CDS_TailWidthPerSource", "-2", [0] * 13),
    ("CDS_DriveType", "CDS_DriveTypePerSource", "-2", [1] * 13)])    # -2 has low bits 2 and 0
def test_a_negative_legacy_scalar_cds_row_is_ignored_as_the_native_loader_ignores_it(
        tmp_path, key, per_source, value, default):
    rows = [r for r in _rows() if r[0] != per_source] + [[key, value]]
    plain = [r for r in _rows() if r[0] != per_source]
    cfg, notes = _read(rows)
    baseline, _ = _read(plain)
    attr = "cds_tail" if key == "CDS_TailWidth_REG" else "cds_drive_type"
    assert getattr(cfg, attr) == getattr(baseline, attr) == default
    assert notes == [f"{key} {value} is negative and is ignored; the CDS sources keep their "
                     f"defaults."]
    paths = []
    for name, content in (("neg.csv", rows), ("plain.csv", plain)):
        path = tmp_path / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(content)
        paths.append(str(path))
    assert swi3score.registers_from_csv(paths[0]) == swi3score.registers_from_csv(paths[1])


def test_saving_a_bus_config_over_a_file_keeps_the_file_when_the_write_fails(
        tmp_path, monkeypatch):
    path = tmp_path / "bus.csv"
    path.write_text("old config\n", encoding="utf-8")
    cfg = demo_config()
    monkeypatch.setattr(BusConfig, "to_csv",
                        lambda self: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        cfg.to_csv_file(str(path))
    assert path.read_text(encoding="utf-8") == "old config\n"
    assert [p.name for p in tmp_path.iterdir()] == ["bus.csv"]
    monkeypatch.undo()
    assert cfg.to_csv_file(str(path)) == str(path)
    assert BusConfig.from_csv(str(path)).to_csv() == cfg.to_csv()


def test_a_config_csv_with_lone_cr_line_endings_reads_as_one_with_lf_endings():
    """A lone CR ends a line, as the native loader reads it."""
    text = demo_config().to_csv().replace("\r\n", "\n")
    with import_notes.collect() as notes:
        lf = BusConfig.from_csv_text(text, "t.csv")
        cr = BusConfig.from_csv_text(text.replace("\n", "\r"), "t.csv")
    assert cr.to_csv() == lf.to_csv()
    assert [n.message for n in notes] == []


def test_config_csv_text_the_csv_module_rejects_raises_value_error_naming_the_source():
    saved = csv.field_size_limit(100)           # process-global: set here, restored below
    try:
        with pytest.raises(ValueError, match=r"^t\.csv: line 2: .*field larger than"):
            BusConfig.from_csv_text("AppVersion,1\nDescription," + "x" * 200 + "\n", "t.csv")
    finally:
        csv.field_size_limit(saved)
