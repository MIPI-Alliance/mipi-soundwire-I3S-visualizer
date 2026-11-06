"""Rules of the native config CSV loader (swi3score.csv_warnings / registers_from_csv).

* A hex or binary cell wider than an int is reported and reads as 0 on every platform; a 0x
  or 0b prefix not followed by a digit of its base (a sign, a space) is reported and reads
  as 0, as BusConfig reads it.
* A line ends at LF, CR LF or a lone CR outside a quoted cell: the three copies of a file
  give the same registers and BusConfig's warnings.
* A config path is read as UTF-8, so a non-ASCII folder name loads.
* A field on more than one row is reported, and the last row is used.
* A blank SkippingDenominator_REG reads as its reset value 1, and is reported as blank.
* A RowRate that is not a number, non-finite or negative, or one outside the field's
  1..48000 kHz, is reported once and leaves the rate unset; a RowRate of 0 is the unset rate
  too, and is reported; a repeated RowRate row takes its last row, a blank one included.
  A RowRate reads with a '.' decimal point whatever the process's LC_NUMERIC, and its
  warning writes the value read the same way, as BusConfig writes it: an unreadable cell's
  value as the shortest decimal that reads back, an out-of-range rate in %g form.
* The scalar CDS_DriveType row is broadcast to every source when no per-source row is
  present, like the Python readers. A negative scalar CDS_TailWidth_REG or CDS_DriveType is
  ignored (the sources keep their defaults) and reported once, in BusConfig's words.
* A CDS_GuardEnabledPerSource or CDS_GuardPolarityPerSource cell other than 0 or 1 is
  reported, and reads as 1.
* An EnableCh_REG that does not fit its 16 bits is reported on every enabled port, also
  when its low 16 bits enable no channel, and not on a disabled one.
* S1TailWidth_REG that is negative is reported and places the CDS as a tail of 0; one that
  places the CDS beyond its 5-bit field (up to the largest int) is reported, and the
  expected registers carry the column's low 5 bits; a tail in range is not reported;
  SkippingDenominator_REG below 1 (0, negative, the smallest int) is reported and read as 1.
* A row the decode reads that the file does not carry is reported by name and takes its
  default; a CDS row given by its legacy scalar spelling counts as present, and
  Description and the visualizer-only rows are not required.
* Unrecognised keys are tracked in linear time, and a warning lists its first 3 items and
  counts the rest, as BusConfig's import warnings do.
* A quoted cell reads as its text without the quotes, a doubled quote inside as one, and a
  ',' inside as part of the cell, as Python's csv reader reads them; a hexadecimal RowRate
  is not a number.
* A key that is not a config field is reported (the visualizer-only ones, and every key the
  visualizer reader skips, are not); a
  leading UTF-8 byte-order mark is not part of the first key; a quoted cell may span lines;
  a '"' inside an unquoted cell is literal, as Python's csv reader reads it; a quoted cell
  still open at the end of the file is reported, and is read in linear time; a path that
  cannot be opened or read (a missing file, a directory) reports why and loads nothing.
* A warning quotes a cell or a key of up to 60 characters whole, and a longer one as its
  first 59 characters and an ellipsis. A 1-bit field over its width carries its low bit.
* The manual-SSP pattern period (pattern_period) is the LCM of the enabled ports' pattern
  periods, capped rather than overflowing.

Every input is synthetic: a config the app writes (demo_config) with cells edited."""
import csv
import io
import locale

import pytest
import swi3score

from swi3s_studio.model.bus_config import BusConfig, demo_config

_CDS_DRIVE = 0x1186     # CDS_DriveType, device 0's source block


def _rows():
    return list(csv.reader(io.StringIO(demo_config().to_csv())))


def _write(path, rows):
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _set(rows, key, col, value):
    for r in rows:
        if r and r[0] == key:
            r[col] = value
            return
    raise KeyError(key)


def _mentions(warnings, text):
    return [w for w in warnings if text in w]


def _registers(tmp_path, name, key, value):
    rows = _rows()
    _set(rows, key, 1, value)
    return swi3score.registers_from_csv(_write(tmp_path / name, rows))


def _audio_rates(path):
    settings = swi3score.DecoderSettings()
    settings.config_csv_path = path
    d = swi3score.Decoder(swi3score.MemorySampleSource([False] * 20_000, 98_304_000, 4),
                          settings)
    d.run()
    return dict(d.audio_sample_rates())


def test_a_hex_cell_wider_than_an_int_is_reported(tmp_path):
    rows = _rows()
    _set(rows, "NumColumns_REG", 1, "0x100000007")
    warnings = swi3score.csv_warnings(_write(tmp_path / "wide.csv", rows))
    assert _mentions(warnings, "NumColumns_REG: 1 cell is not a valid value: '0x100000007'")
    assert swi3score.registers_from_csv(str(tmp_path / "wide.csv")) == \
        _registers(tmp_path, "zero.csv", "NumColumns_REG", "0")


def test_a_binary_cell_wider_than_an_int_is_reported(tmp_path):
    rows = _rows()
    _set(rows, "NumColumns_REG", 1, "0b1" + "0" * 40)
    warnings = swi3score.csv_warnings(_write(tmp_path / "wide.csv", rows))
    assert _mentions(warnings, "NumColumns_REG: 1 cell is not a valid value")
    assert swi3score.registers_from_csv(str(tmp_path / "wide.csv")) == \
        _registers(tmp_path, "zero.csv", "NumColumns_REG", "0")


def test_a_non_ascii_path_loads(tmp_path):
    folder = tmp_path / "Müller"
    folder.mkdir()
    rows = _rows()
    _set(rows, "SkippingDenominator_REG", 1, "0")
    path = _write(folder / "cfg.csv", rows)
    assert _mentions(swi3score.csv_warnings(path), "SkippingDenominator_REG")
    assert swi3score.registers_from_csv(path)


def test_a_repeated_field_is_reported(tmp_path):
    rows = _rows()
    rows.insert(0, ["NumColumns_REG", "3"])
    warnings = swi3score.csv_warnings(_write(tmp_path / "dup.csv", rows))
    assert _mentions(warnings, "NumColumns_REG appears 2 times; the last row is used.")
    assert swi3score.registers_from_csv(str(tmp_path / "dup.csv")) == \
        swi3score.registers_from_csv(_write(tmp_path / "clean.csv", _rows()))


def test_a_clean_file_reports_no_repeat(tmp_path):
    assert not _mentions(swi3score.csv_warnings(_write(tmp_path / "ok.csv", _rows())),
                         "appears")


def test_a_non_finite_or_negative_row_rate_is_reported_and_left_unset(tmp_path):
    rows = _rows()
    _set(rows, "RowRate", 1, "")
    unset = _audio_rates(_write(tmp_path / "rate_blank.csv", rows))
    assert unset != _audio_rates(_write(tmp_path / "rate_given.csv", _rows()))
    for bad in ("nan", "inf", "-5"):
        rows = _rows()
        _set(rows, "RowRate", 1, bad)
        path = _write(tmp_path / f"rate_{bad}.csv", rows)
        warnings = swi3score.csv_warnings(path)
        assert _mentions(warnings, f"RowRate: 1 cell is not a valid value: '{bad}'"), bad
        assert _audio_rates(path) == unset, bad


def test_a_zero_row_rate_is_reported(tmp_path):
    rows = _rows()
    _set(rows, "RowRate", 1, "0")
    path = _write(tmp_path / "rate0.csv", rows)
    assert _mentions(swi3score.csv_warnings(path), "RowRate is 0")
    rows = _rows()
    _set(rows, "RowRate", 1, "")
    assert _audio_rates(path) == _audio_rates(_write(tmp_path / "rate_blank.csv", rows))
    assert not _mentions(swi3score.csv_warnings(_write(tmp_path / "ok.csv", _rows())),
                         "RowRate is 0")


def test_a_row_rate_that_is_not_a_number_is_reported_once(tmp_path):
    for bad in ("abc", "0abc"):
        rows = _rows()
        _set(rows, "RowRate", 1, bad)
        warnings = swi3score.csv_warnings(_write(tmp_path / "rate_text.csv", rows))
        assert _mentions(warnings, f"RowRate: 1 cell is not a valid value: '{bad}'"), bad
        assert not _mentions(warnings, "RowRate is 0"), bad


_RANGE_NOTE = "kHz is outside 1..48000 kHz; the decode uses the row rate it measures."


@pytest.mark.parametrize("cell, note", [
    ("abc", "RowRate: 1 cell is not a valid value: 'abc' read as 0.0."),
    ("0abc", "RowRate: 1 cell is not a valid value: '0abc' read as 0.0."),
    ("nan", "RowRate: 1 cell is not a valid value: 'nan' read as 0.0."),
    ("inf", "RowRate: 1 cell is not a valid value: 'inf' read as 0.0."),
    ("-5", "RowRate: 1 cell is not a valid value: '-5' read as 0.0."),
    ("0x10", "RowRate: 1 cell is not a valid value: '0x10' read as 0.0."),
    ("+0x1", "RowRate: 1 cell is not a valid value: '+0x1' read as 0.0."),
    ("3072kHz", "RowRate: 1 cell is not a valid value: '3072kHz' read as 3072.0."),
    ("1e12", f"RowRate 1e+12 {_RANGE_NOTE}"),
    ("0.5", f"RowRate 0.5 {_RANGE_NOTE}"),
    ("1e400", "RowRate: 1 cell is not a valid value: '1e400' read as 0.0."),
    ("0", "RowRate is 0; the decode uses the row rate it measures."),
    ("1e-400", "RowRate is 0; the decode uses the row rate it measures."),
])
def test_a_bad_row_rate_gives_exactly_one_row_rate_warning(tmp_path, cell, note):
    rows = _rows()
    _set(rows, "RowRate", 1, cell)
    warnings = swi3score.csv_warnings(_write(tmp_path / "rate.csv", rows))
    assert [w for w in warnings if "RowRate" in w] == [note]


def _comma_locale():
    """A locale name whose decimal point is ',', or None when none is installed."""
    saved = locale.setlocale(locale.LC_NUMERIC)
    try:
        for name in ("de_DE.UTF-8", "de_DE.utf8", "fr_FR.UTF-8", "fr_FR.utf8", "nl_NL.UTF-8",
                     "nl_NL.utf8", "German_Germany.1252", "de_DE", "fr_FR"):
            try:
                locale.setlocale(locale.LC_NUMERIC, name)
            except locale.Error:
                continue
            if locale.localeconv()["decimal_point"] == ",":
                return name
        return None
    finally:
        locale.setlocale(locale.LC_NUMERIC, saved)


def test_a_row_rate_reads_with_a_decimal_point_under_a_comma_locale(tmp_path):
    """Under an LC_NUMERIC whose decimal point is ',', RowRate 2822.4 reads as 2822.4 kHz
    with no warning, and a cell with trailing characters reports the value with a '.'."""
    name = _comma_locale()
    if name is None:
        pytest.skip("no comma-decimal locale is installed")
    rows = _rows()
    _set(rows, "RowRate", 1, "2822.4")
    fractional = _write(tmp_path / "rate_frac.csv", rows)
    rows = _rows()
    _set(rows, "RowRate", 1, "2822.4kHz")
    trailing = _write(tmp_path / "rate_trail.csv", rows)
    want = _audio_rates(fractional)
    saved = locale.setlocale(locale.LC_NUMERIC)
    locale.setlocale(locale.LC_NUMERIC, name)
    try:
        assert locale.localeconv()["decimal_point"] == ","
        assert not _mentions(swi3score.csv_warnings(fractional), "RowRate")
        assert _audio_rates(fractional) == want
        assert [w for w in swi3score.csv_warnings(trailing) if "RowRate" in w] == [
            "RowRate: 1 cell is not a valid value: '2822.4kHz' read as 2822.4."]
    finally:
        locale.setlocale(locale.LC_NUMERIC, saved)
    assert want != _audio_rates(_write(tmp_path / "rate_given.csv", _rows()))


def test_a_repeated_row_rate_takes_its_last_row_a_blank_one_included(tmp_path):
    rows = _rows()
    _set(rows, "RowRate", 1, "")
    unset = _audio_rates(_write(tmp_path / "rate_blank.csv", rows))
    for last in ("", "0", "nan", "1e12"):
        rows = _rows()
        rows.append(["RowRate", last])
        path = _write(tmp_path / "rate_twice.csv", rows)
        assert _mentions(swi3score.csv_warnings(path), "RowRate appears 2 times"), last
        assert _audio_rates(path) == unset, last
    rows = _rows()
    _set(rows, "RowRate", 1, "0")
    rows.append(["RowRate", "3072"])
    path = _write(tmp_path / "rate_zero_then_given.csv", rows)
    assert not _mentions(swi3score.csv_warnings(path), "RowRate is 0")
    assert _audio_rates(path) == _audio_rates(_write(tmp_path / "rate_given.csv", _rows()))


def test_a_cds_guard_cell_other_than_0_or_1_is_reported_and_read_as_1(tmp_path):
    for key in ("CDS_GuardEnabledPerSource", "CDS_GuardPolarityPerSource"):
        for value in ("5", "-1", "2"):
            rows = _rows()
            _set(rows, "CDS_GuardEnabledPerSource", 2, "1")
            _set(rows, key, 2, value)
            path = _write(tmp_path / "guard.csv", rows)
            assert _mentions(swi3score.csv_warnings(path),
                             f"{key} does not fit its 1-bit register field (0..1) for "
                             f"Device 0 = {value}; a value other than 0 reads as 1."), \
                (key, value)
            one = _rows()
            _set(one, "CDS_GuardEnabledPerSource", 2, "1")
            _set(one, key, 2, "1")
            assert swi3score.registers_from_csv(path) == \
                swi3score.registers_from_csv(_write(tmp_path / "guard1.csv", one)), (key, value)
    rows = _rows()
    _set(rows, "CDS_GuardEnabledPerSource", 2, "1")
    assert not _mentions(swi3score.csv_warnings(_write(tmp_path / "guard_ok.csv", rows)),
                         "CDS_Guard")


def test_an_enable_ch_that_wraps_to_no_channel_is_reported(tmp_path):
    for value in ("65536", "131072", "65537"):
        rows = _rows()
        _set(rows, "EnableCh_REG", 1, value)
        _set(rows, "Enabled", 4, "True")                 # a second enabled port, DP3
        _set(rows, "EnableCh_REG", 4, value)
        _set(rows, "EnableCh_REG", 6, value)             # DP5 stays disabled
        warnings = swi3score.csv_warnings(_write(tmp_path / "enable.csv", rows))
        assert _mentions(warnings, f"EnableCh_REG does not fit its 16 channel bits for "
                                   f"DP0 = {value}, DP3 = {value}; only"), value
        assert not _mentions(warnings, "DP5"), value
    rows = _rows()
    _set(rows, "Enabled", 1, "False")
    _set(rows, "EnableCh_REG", 1, "65536")
    assert not _mentions(swi3score.csv_warnings(_write(tmp_path / "off.csv", rows)),
                         "EnableCh_REG")


def test_the_scalar_drive_type_is_broadcast_like_the_per_source_row(tmp_path):
    scalar = _rows()
    scalar = [r for r in scalar if r and r[0] != "CDS_DriveTypePerSource"]
    scalar.append(["CDS_DriveType", "0"])
    per_source = _rows()
    for i in range(1, 14):
        _set(per_source, "CDS_DriveTypePerSource", i, "0")
    a = swi3score.registers_from_csv(_write(tmp_path / "scalar.csv", scalar))
    b = swi3score.registers_from_csv(_write(tmp_path / "per.csv", per_source))
    assert a == b
    assert {v for d, addr, v in a if d == 0 and addr == _CDS_DRIVE} == {0}


def test_a_blank_scalar_drive_type_keeps_the_default(tmp_path):
    blank = [r for r in _rows() if r and r[0] != "CDS_DriveTypePerSource"]
    blank.append(["CDS_DriveType", ""])
    default = [r for r in _rows() if r and r[0] != "CDS_DriveTypePerSource"]
    assert swi3score.registers_from_csv(_write(tmp_path / "blank.csv", blank)) == \
        swi3score.registers_from_csv(_write(tmp_path / "none.csv", default))


@pytest.mark.parametrize("key, per_source", [
    ("CDS_TailWidth_REG", "CDS_TailWidthPerSource"),
    ("CDS_DriveType", "CDS_DriveTypePerSource")])
def test_a_negative_legacy_scalar_cds_row_is_ignored_and_reported_once(tmp_path, key,
                                                                         per_source):
    """The scalar row is not applied (the registers equal the file without it) and the
    warning is the sentence BusConfig words, so a file read by both reports it once."""
    from swi3s_studio import import_notes
    from swi3s_studio.ingest import visualizer_csv
    plain = [r for r in _rows() if r and r[0] != per_source]
    negative = plain + [[key, "-1"]]
    neg_path = _write(tmp_path / "neg.csv", negative)
    want = f"{key} -1 is negative and is ignored; the CDS sources keep their defaults."
    assert swi3score.csv_warnings(neg_path) == [want]
    assert swi3score.registers_from_csv(neg_path) == \
        swi3score.registers_from_csv(_write(tmp_path / "plain.csv", plain))
    with import_notes.collect() as notes:
        BusConfig.from_csv(neg_path, source="neg.csv")
        visualizer_csv.report_csv_warnings(neg_path, "neg.csv")
    assert [n.message for n in notes] == [want]


def test_a_negative_legacy_scalar_under_a_per_source_row_is_not_reported(tmp_path):
    rows = _rows() + [["CDS_TailWidth_REG", "-1"], ["CDS_DriveType", "-1"]]
    assert swi3score.csv_warnings(_write(tmp_path / "both.csv", rows)) == []


def _s1_tail(tmp_path, tail):
    rows = _rows()
    _set(rows, "PHY3Enabled", 1, "True")
    _set(rows, "S1TailWidth_REG", 1, tail)
    return _write(tmp_path / f"tail_{tail}.csv", rows)


def test_an_s1_tail_width_outside_its_range_is_reported(tmp_path):
    for tail in ("255", "-9", "2147483647"):
        warnings = swi3score.csv_warnings(_s1_tail(tmp_path, tail))
        assert _mentions(warnings, f"S1TailWidth_REG {tail}"), tail
    # A negative tail places the CDS as a tail of 0, not as its low 5 bits (-9 & 31 = 23).
    assert swi3score.registers_from_csv(_s1_tail(tmp_path, "-9")) == \
        swi3score.registers_from_csv(_s1_tail(tmp_path, "0"))
    assert swi3score.registers_from_csv(_s1_tail(tmp_path, "-9")) != \
        swi3score.registers_from_csv(_s1_tail(tmp_path, "23"))
    # The expected registers carry the column's low 5 bits: tails 255, 31 and the largest
    # int (handover bit set) all place the CDS at column 1 mod 32.
    for tail in ("255", "2147483647"):
        assert swi3score.registers_from_csv(_s1_tail(tmp_path, tail)) == \
            swi3score.registers_from_csv(_s1_tail(tmp_path, "31")), tail
    assert not _mentions(swi3score.csv_warnings(_s1_tail(tmp_path, "5")), "S1TailWidth_REG")


def test_a_skipping_denominator_below_one_is_reported_and_read_as_one(tmp_path):
    one = _registers(tmp_path, "den1.csv", "SkippingDenominator_REG", "1")
    for value in ("0", "-3", "-2147483648"):
        rows = _rows()
        _set(rows, "SkippingDenominator_REG", 1, value)
        warnings = swi3score.csv_warnings(_write(tmp_path / "den.csv", rows))
        assert _mentions(warnings, f"SkippingDenominator_REG {value} is below 1; the decode "
                                   "and the expected registers use 1."), value
        assert swi3score.registers_from_csv(str(tmp_path / "den.csv")) == one, value


def test_a_blank_skipping_denominator_reads_as_its_reset_value(tmp_path):
    rows = _rows()
    _set(rows, "SkippingDenominator_REG", 1, "")
    warnings = swi3score.csv_warnings(_write(tmp_path / "den_blank.csv", rows))
    assert _mentions(warnings, "SkippingDenominator_REG is blank; the decode and the expected "
                               "registers use its reset value 1.")
    assert not _mentions(warnings, "below 1")
    assert swi3score.registers_from_csv(str(tmp_path / "den_blank.csv")) == \
        _registers(tmp_path, "den1.csv", "SkippingDenominator_REG", "1")


def test_a_row_rate_outside_its_range_is_reported_and_left_unset(tmp_path):
    rows = _rows()
    _set(rows, "RowRate", 1, "")
    unset = _audio_rates(_write(tmp_path / "rate_blank.csv", rows))
    for bad, shown in (("1e12", "1e+12"), ("1e308", "1e+308"), ("0.5", "0.5"),
                       ("1e-300", "1e-300"), ("48000.5", "48000.5"), (".5", "0.5")):
        rows = _rows()
        _set(rows, "RowRate", 1, bad)
        path = _write(tmp_path / f"rate_{bad}.csv", rows)
        assert _mentions(swi3score.csv_warnings(path),
                         f"RowRate {shown} kHz is outside 1..48000 kHz"), bad
        assert _audio_rates(path) == unset, bad
    for good in ("1", "48000", "3072.0"):
        rows = _rows()
        _set(rows, "RowRate", 1, good)
        assert not _mentions(swi3score.csv_warnings(_write(tmp_path / f"ok_{good}.csv", rows)),
                             "RowRate"), good


def test_the_fields_the_visualizer_reader_skips_are_silent(tmp_path):
    from swi3s_studio.ingest import visualizer_csv
    for key in sorted(visualizer_csv._SILENT_SKIP):
        rows = _rows()
        rows.insert(1, [key, "1"])
        assert not _mentions(swi3score.csv_warnings(_write(tmp_path / "skip.csv", rows)),
                             key), key


def test_an_unclosed_quote_reads_the_rest_of_a_long_file_in_linear_time(tmp_path):
    """The quote state is carried line to line: a quoted cell left open on line 1 of a
    100,000-line file is read once as the joined cell grows. The file is sized so that
    reading the cell again on each line would not finish; the test passes by finishing."""
    path = tmp_path / "long.csv"
    path.write_text('Description,"x\n' + "a,b,c,d,e,f\n" * 100_000, encoding="utf-8")
    warnings = swi3score.csv_warnings(str(path))
    assert _mentions(warnings, "Line 1 opens a quoted cell that is not closed")


@pytest.mark.parametrize("cell", ["\u00e9", "\u00c9", "TR\u00dcE", "\u00fcno"])
def test_a_non_ascii_boolean_cell_is_reported_once_and_read_as_false(tmp_path, cell):
    """A boolean cell is matched case-blind over ASCII letters only; a non-ASCII character
    makes it no boolean, one warning, read as false."""
    rows = _rows()
    _set(rows, "PortDirection_REG", 1, cell)
    path = _write(tmp_path / "bool.csv", rows)
    assert _mentions(swi3score.csv_warnings(path), "PortDirection_REG") == [
        f"PortDirection_REG: 1 cell is not a valid value: DP0 '{cell}' read as false."]
    assert swi3score.registers_from_csv(path) == _registers(tmp_path, "false.csv",
                                                            "PortDirection_REG", "False")
    for upper in ("TRUE", "Yes", "fAlSe", "NO"):
        _set(rows, "PortDirection_REG", 1, upper)
        assert not _mentions(swi3score.csv_warnings(_write(tmp_path / "ok.csv", rows)),
                             "PortDirection_REG"), upper


def test_an_unknown_field_is_reported(tmp_path):
    rows = _rows()
    rows.insert(1, ["Bogus_REG", "1"])
    warnings = swi3score.csv_warnings(_write(tmp_path / "bogus.csv", rows))
    assert _mentions(warnings, "Field Bogus_REG is not a config field and is ignored.")


def test_a_clean_file_reports_no_unknown_field(tmp_path):
    assert not _mentions(swi3score.csv_warnings(_write(tmp_path / "ok.csv", _rows())),
                         "not a config field")


def test_a_byte_order_mark_is_not_part_of_the_first_key(tmp_path):
    rows = _rows()
    first = next(r for r in rows if r and r[0] == "NumColumns_REG")
    rows.remove(first)
    rows.insert(0, ["NumColumns_REG", "0x100000007"])
    path = tmp_path / "bom.csv"
    _write(path, rows)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    warnings = swi3score.csv_warnings(str(path))
    assert _mentions(warnings, "NumColumns_REG: 1 cell is not a valid value")
    assert not _mentions(warnings, "not a config field")


def test_a_multi_line_description_is_one_cell(tmp_path):
    rows = [r for r in _rows() if r and r[0] != "Description"]
    path = tmp_path / "desc.csv"
    _write(path, rows)
    text = path.read_text(encoding="utf-8")
    path.write_text('Description,"first line\nsecond, line\nthird"\n' + text, encoding="utf-8")
    assert not _mentions(swi3score.csv_warnings(str(path)), "not a config field")


def test_a_quote_inside_an_unquoted_cell_is_literal(tmp_path):
    """A '"' that does not open a cell joins no lines: every later field still loads, as in
    Python's csv reader."""
    rows = [r for r in _rows() if r and r[0] != "Description"]
    path = tmp_path / "quote.csv"
    _write(path, rows)
    text = path.read_text(encoding="utf-8")
    path.write_text('Description,5" woofer\n' + text, encoding="utf-8")
    assert swi3score.registers_from_csv(str(path)) == \
        swi3score.registers_from_csv(_write(tmp_path / "plain.csv", rows))
    assert not _mentions(swi3score.csv_warnings(str(path)), "quoted cell")


def test_a_quoted_cell_open_at_the_end_of_the_file_is_reported(tmp_path):
    rows = _rows()
    path = tmp_path / "open.csv"
    _write(path, rows)
    path.write_text(path.read_text(encoding="utf-8") + 'Description,"never closed\n',
                    encoding="utf-8")
    line = len(rows) + 1
    assert _mentions(swi3score.csv_warnings(str(path)),
                     f"Line {line} opens a quoted cell that is not closed")


def test_a_file_that_cannot_be_opened_reports_why(tmp_path):
    missing = str(tmp_path / "missing.csv")
    assert swi3score.csv_warnings(missing) == [f"Cannot open config CSV: {missing}"]


def test_a_directory_is_not_read_as_an_empty_config(tmp_path):
    folder = tmp_path / "cfg.csv"
    folder.mkdir()
    assert swi3score.csv_warnings(str(folder)) == [f"Cannot open config CSV: {folder}"]
    assert swi3score.registers_from_csv(str(folder)) == []
    assert _audio_rates(str(folder)) == {}


_MICRO = "\u00b5"      # two bytes in UTF-8: the cut counts characters, not bytes


def test_a_long_cell_or_key_is_quoted_cut_to_60_characters(tmp_path):
    rows = _rows()
    _set(rows, "NumColumns_REG", 1, "9" * 3_000_000)
    _set(rows, "EnableCh_REG", 1, "x" * 60)
    _set(rows, "EnableCh_REG", 2, _MICRO * 200)
    rows.append(["Interval_REG"] + ["1"] * 12 + ["y" * 100])
    rows.append(["K" * 5000, "1"])
    rows.append(["RowRate", "1" * 100])
    warnings = swi3score.csv_warnings(_write(tmp_path / "long.csv", rows))
    assert max(len(w) for w in warnings) < 400
    assert _mentions(warnings, f"NumColumns_REG: 1 cell is not a valid value: "
                               f"'{'9' * 59}\u2026' read as 0.")
    assert _mentions(warnings, f"DP0 '{'x' * 60}' read as 0, DP1 '{_MICRO * 59}\u2026' read as 0")
    assert _mentions(warnings, f"Interval_REG: values past the last data port column (12) "
                               f"are ignored: '{'y' * 59}\u2026'.")
    assert _mentions(warnings, f"Field {'K' * 59}\u2026 is not a config field and is ignored.")
    assert _mentions(warnings, "RowRate 1.11111e+99 kHz is outside 1..48000 kHz")


# The rows the decode reads, as the app writes them; the rest of a written config is
# Description and the visualizer-only rows.
_READ_ROWS = (
    "NumColumns_REG", "SkippingDenominator_REG", "PHY3Enabled", "S1TailWidth_REG",
    "EnforceS1Handover", "CDS_BitWidth_REG", "RowRate", "CDS_GuardEnabledPerSource",
    "CDS_GuardPolarityPerSource", "CDS_TailWidthPerSource", "CDS_DriveTypePerSource",
    "CDS_EndDriveEarlyPerSource", "Enabled", "DeviceNumber_REG", "PortDirection_REG",
    "EnableCh_REG", "SampleSize_REG", "SampleGrouping_REG", "ChannelGrouping_REG",
    "Spacing_REG", "Interval_REG", "Offset_REG", "HorizontalStart_REG",
    "HorizontalCount_REG", "BitWidth_REG", "TailWidth_REG", "SkippingNumerator_REG",
    "SubRowInterval_REG", "FlowMode_REG", "PortMode_REG", "ScramblerEn_REG",
    "GuardEnable_REG", "GuardPolarity_REG", "FCP_HorizontalStart_REG", "FCP_BitWidth_REG",
    "FCP_TailWidth_REG", "FCP_Offset_REG", "FCP_GuardEnable_REG", "FCP_GuardPolarity_REG",
    "DataPortNumber", "ManagerDataport")


def test_each_missing_row_the_decode_reads_is_reported(tmp_path):
    written = [r[0] for r in _rows() if r]
    assert set(_READ_ROWS) <= set(written)
    for key in written:
        rows = [r for r in _rows() if r and r[0] != key]
        missing = _mentions(swi3score.csv_warnings(_write(tmp_path / "cut.csv", rows)),
                            "missing")
        if key in _READ_ROWS:
            assert missing == [f"1 row is missing and takes its default: {key}."], key
        else:
            assert not missing, key


def test_a_file_cut_at_a_row_boundary_names_every_missing_row(tmp_path):
    rows = _rows()
    cut = next(i for i, r in enumerate(rows) if r and r[0] == "Enabled")
    warnings = swi3score.csv_warnings(_write(tmp_path / "cut.csv", rows[:cut]))
    gone = [k for k in _READ_ROWS if k not in {r[0] for r in rows[:cut] if r}]
    assert len(gone) == 29
    assert _mentions(warnings, f"{len(gone)} rows are missing and take their defaults: "
                               + ", ".join(gone[:3]) + f" and {len(gone) - 3} more.")


def test_a_legacy_cds_scalar_stands_in_for_its_per_source_row(tmp_path):
    for per_source, legacy, value in (
            ("CDS_GuardEnabledPerSource", "CDS_GuardEnabled_REG", "False"),
            ("CDS_GuardPolarityPerSource", "CDS_GuardPolarity_REG", "False"),
            ("CDS_TailWidthPerSource", "CDS_TailWidth_REG", "0"),
            ("CDS_DriveTypePerSource", "CDS_DriveType", "1")):
        rows = [r for r in _rows() if r and r[0] != per_source] + [[legacy, value]]
        assert not _mentions(swi3score.csv_warnings(_write(tmp_path / "legacy.csv", rows)),
                             "missing"), per_source


def test_many_distinct_unknown_fields_are_read_in_linear_time(tmp_path):
    """160,000 distinct unrecognised keys (1.6 MB) load, and the warning lists the first 3 by
    name. The file is sized so that a scan of the keys seen so far for each key would not
    finish; the test passes by finishing."""
    path = tmp_path / "unknown.csv"
    text = "\n".join(",".join(r) for r in _rows()) + "\n"
    path.write_text(text + "".join(f"Foo{i},1\n" for i in range(160_000)), encoding="utf-8")
    warnings = swi3score.csv_warnings(str(path))
    assert _mentions(warnings, "Fields Foo0, Foo1, Foo2 and 159997 more are not a config "
                               "field and are ignored.")


def test_a_quoted_cell_reads_as_its_text(tmp_path):
    plain = swi3score.registers_from_csv(_write(tmp_path / "plain.csv", _rows()))
    quoted = [[r[0]] + [f'"{c}"' if c else c for c in r[1:]] for r in _rows()]
    path = _write(tmp_path / "quoted.csv", quoted)
    assert swi3score.registers_from_csv(path) == plain
    assert not _mentions(swi3score.csv_warnings(path), "not a valid value")


def test_a_comma_or_doubled_quote_inside_a_quoted_cell_is_part_of_it(tmp_path):
    rows = [r for r in _rows() if r and r[0] != "Description"]
    path = tmp_path / "desc.csv"
    _write(path, rows)
    path.write_text('Description,"a, ""b"", c"\n' + path.read_text(encoding="utf-8"),
                    encoding="utf-8")
    assert swi3score.csv_warnings(str(path)) == []


def test_a_hexadecimal_row_rate_is_not_a_number(tmp_path):
    rows = _rows()
    _set(rows, "RowRate", 1, "")
    unset = _audio_rates(_write(tmp_path / "rate_blank.csv", rows))
    for bad in ("0x10", "-0X1p4", "+0x1"):
        rows = _rows()
        _set(rows, "RowRate", 1, bad)
        path = _write(tmp_path / "rate_hex.csv", rows)
        assert _mentions(swi3score.csv_warnings(path),
                         f"RowRate: 1 cell is not a valid value: '{bad}'"), bad
        assert _audio_rates(path) == unset, bad


def _python_notes(rows):
    from swi3s_studio import import_notes
    from swi3s_studio.model.bus_config import BusConfig
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    with import_notes.collect() as notes:
        BusConfig.from_csv_text(buf.getvalue(), "t.csv")
    return [n.message for n in notes]


def test_a_warning_listing_more_than_three_items_reads_as_the_python_importers(tmp_path):
    """Both engines list three items and count the rest, so one file gives one warning per
    field: five ports over Interval_REG's width and thirteen sources with a bad
    CDS_DriveTypePerSource cell, each word for word."""
    rows = _rows()
    for i in range(1, 5):
        _set(rows, "Enabled", i + 1, "True")
        _set(rows, "EnableCh_REG", i + 1, "1")
    for i in range(1, 6):
        _set(rows, "Interval_REG", i, "99999")
    for i in range(1, 14):
        _set(rows, "CDS_DriveTypePerSource", i, "2")
    native = swi3score.csv_warnings(_write(tmp_path / "many.csv", rows))
    python = _python_notes(rows)
    for needle in ("Interval_REG does not fit", "CDS_DriveTypePerSource"):
        n, p = _mentions(native, needle), _mentions(python, needle)
        assert n and n == p, (needle, n, p)
    assert " and 2 more;" in _mentions(native, "Interval_REG does not fit")[0]
    assert " and 10 more" in _mentions(native, "CDS_DriveTypePerSource")[0]


def test_a_cell_or_key_that_is_not_utf8_is_echoed_with_replacement_characters(tmp_path):
    """The warnings convert to Python strings whatever bytes the file holds: a byte that is
    not part of a well-formed UTF-8 sequence (a lone continuation or lead byte, an overlong
    or surrogate encoding, a cut sequence) is quoted as U+FFFD."""
    body = "\n".join(",".join(r) for r in _rows()).encode("utf-8") + b"\n"
    for n, (tail, want) in enumerate((
            (b"RowRate,\xff\n", "'\ufffd' read as"),
            (b"Gr\xf6\xdfe,1\n", "Gr\ufffd\ufffde"),
            (b"SampleSize_REG,15,\xe9\n", "'\ufffd'"),
            (b"Bad\xc0\x80,1\n", "Bad\ufffd\ufffd"),
            (b"Sur\xed\xa0\x80,1\n", "Sur\ufffd\ufffd\ufffd"),
            (b"Cut\xe2\x82,1\n", "Cut\ufffd\ufffd"),
            (b"Cut\xf0\x9f\x98,1\n", "Cut\ufffd\ufffd\ufffd"))):
        path = tmp_path / f"bytes{n}.csv"
        path.write_bytes(body + tail)
        warnings = swi3score.csv_warnings(str(path))
        assert any(want in w for w in warnings), (tail, warnings)
        assert swi3score.registers_from_csv(str(path))


def _write_text(path, text):
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def _python_text_notes(text):
    from swi3s_studio import import_notes
    with import_notes.collect() as notes:
        BusConfig.from_csv_text(text, "t.csv")
    return [n.message for n in notes]


def test_a_config_csv_reads_alike_with_lf_cr_lf_or_lone_cr_line_endings(tmp_path):
    """A line ends at LF, CR LF or a lone CR outside a quoted cell, as Python's csv reader
    with newline='' reads it: the three copies of one file give the LF copy's registers
    and BusConfig's warnings, word for word, and a quoted cell spanning lines stays one
    cell."""
    rows = _rows()
    _set(rows, "Offset_REG", 1, "12abc")
    _set(rows, "RowRate", 1, "3072kHz")
    _set(rows, "Interval_REG", 2, "0x+5")
    rows = [r for r in rows if not r or r[0] != "Description"]
    rows.append(["Description", "two\nlines, one cell"])
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    lf = buf.getvalue()
    want_registers = swi3score.registers_from_csv(_write_text(tmp_path / "lf.csv", lf))
    assert len(want_registers) > 0
    for name, eol in (("lf", "\n"), ("crlf", "\r\n"), ("cr", "\r")):
        text = lf.replace("\n", eol)
        path = _write_text(tmp_path / f"{name}.csv", text)
        native = swi3score.csv_warnings(path)
        assert native and native == _python_text_notes(text), (name, native)
        assert swi3score.registers_from_csv(path) == want_registers, name


@pytest.mark.parametrize("cell", ["0x+5", "0x 5", "0x\t5", "0x-1", "0b+1", "0b 1", "0X+F"])
def test_a_radix_prefix_without_a_digit_of_its_base_is_not_a_number(tmp_path, cell):
    """After 0x or 0b the next character is a digit of that base, as in the Python readers'
    0[xX][0-9a-fA-F]+ and 0[bB][01]+: a sign or a space there is reported and reads as 0,
    worded as BusConfig words it."""
    rows = _rows()
    _set(rows, "Interval_REG", 1, cell)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    path = _write_text(tmp_path / "radix.csv", buf.getvalue())
    native = swi3score.csv_warnings(path)
    assert native == [f"Interval_REG: 1 cell is not a valid value: DP0 '{cell}' read as 0."]
    assert native == _python_text_notes(buf.getvalue())


@pytest.mark.parametrize("cell", ["1_000", "3072kHz", "2822.4kHz", "abc", "-5", "1e400",
                                  "0x10", "1e12", ".5", "48001", "1e-300"])
def test_a_row_rate_warning_reads_as_busconfigs(tmp_path, cell):
    """A RowRate warning writes the value read as BusConfig does: an unreadable cell as the
    shortest decimal that reads back ('1.0', '2822.4'), an out-of-range rate in %g form
    ('1e+12', '0.5'), so one file read by both engines gives one warning."""
    rows = _rows()
    _set(rows, "RowRate", 1, cell)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    path = _write_text(tmp_path / "rate.csv", buf.getvalue())
    native = [w for w in swi3score.csv_warnings(path) if "RowRate" in w]
    python = [n for n in _python_text_notes(buf.getvalue()) if "RowRate" in n]
    assert native and native == python, (native, python)


def test_a_one_bit_field_that_does_not_fit_names_its_low_bit(tmp_path):
    """A 1-bit CDS field over its width is reported as carrying its low bit."""
    rows = _rows()
    _set(rows, "CDS_DriveTypePerSource", 2, "2")
    _set(rows, "CDS_EndDriveEarlyPerSource", 1, "3")
    warnings = swi3score.csv_warnings(_write(tmp_path / "bit.csv", rows))
    assert warnings == [
        "CDS_DriveTypePerSource does not fit its 1-bit register field (0..1) for Device 0 = 2; "
        "the expected registers carry its low bit.",
        "CDS_EndDriveEarlyPerSource does not fit its 1-bit register field (0..1) for "
        "Manager = 3; the expected registers carry its low bit."]


def _ports_with_intervals(tmp_path, lengths):
    """A config whose first len(lengths) ports are enabled with one channel and an interval
    of lengths[i] Rows (Interval_REG lengths[i] - 1), without skipping."""
    rows = _rows()
    for i, n in enumerate(lengths):
        _set(rows, "Enabled", i + 1, "True")
        _set(rows, "EnableCh_REG", i + 1, "1")
        _set(rows, "Interval_REG", i + 1, str(n - 1))
        _set(rows, "SkippingNumerator_REG", i + 1, "0")
    return _write(tmp_path / "periods.csv", rows)


def test_the_pattern_period_is_capped_rather_than_overflowing(tmp_path):
    """The manual-SSP period is the LCM of the enabled ports' pattern periods; an LCM past
    the cap returns the cap, including one past the 64-bit range (six coprime 12-bit
    intervals), so the SSP row reduces by a period above it and stays as written."""
    lengths = [4093, 4091, 4089, 4087, 4085, 4083]
    path = _ports_with_intervals(tmp_path, lengths)
    assert swi3score.pattern_period(path, 2**63 - 1) == 2**63 - 1
    assert swi3score.pattern_period(path, 10**9) == 10**9
    two = _ports_with_intervals(tmp_path, lengths[:2])
    assert swi3score.pattern_period(two, 2**63 - 1) == 4093 * 4091
    settings = swi3score.DecoderSettings()
    settings.config_csv_path = path
    settings.ssp_row = 12_345
    d = swi3score.Decoder(swi3score.MemorySampleSource([False] * 20_000, 98_304_000, 4),
                          settings)
    d.run()
