"""CSVHandler.load_csv row checks: repeated field rows, DisplayFields characters, a field
row without a value, a UTF-8 byte-order mark, and the VizConfig a load returns; and
CSVHandler.save_csv writes unix line endings."""
from pathlib import Path

import pytest

from swi3s_studio.swviz.io.csv_handler import CSVHandler
from swi3s_studio.swviz.models.interface import Interface
from swi3s_studio.swviz.viz import VizConfig

pytestmark = pytest.mark.platform     # line ends and the byte-order mark depend on the OS


def _saved(tmp_path: Path) -> Path:
    path = tmp_path / "cfg.csv"
    CSVHandler.save_csv(str(path), Interface(), VizConfig())
    return path


def _lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines()


def _load(path: Path, viz=None):
    return CSVHandler.load_csv(str(path), Interface(), viz)


def test_a_saved_file_loads_clean(tmp_path):
    res = _load(_saved(tmp_path))
    assert res.success and res.warnings == []


def test_a_repeated_field_row_is_reported(tmp_path):
    path = _saved(tmp_path)
    lines = _lines(path)
    row = next(i for i, ln in enumerate(lines) if ln.startswith("RowRate,"))
    path.write_text("\n".join(lines + ["RowRate,3072.0"]) + "\n", encoding="utf-8")
    res = _load(path)
    assert res.success
    assert any("RowRate" in w and f"repeats line {row + 1}" in w for w in res.notes())


def test_a_repeated_manager_dataport_row_decides_the_port_like_any_other(tmp_path):
    """The later of two ManagerDataport rows is the one applied, True then False or the
    reverse, and its DeviceNumber then places a port that is no longer the Manager's."""
    path = _saved(tmp_path)
    lines = _lines(path)
    i = next(i for i, ln in enumerate(lines) if ln.startswith("ManagerDataport,"))
    n = len(lines[i].split(",")) - 1
    on = ",".join(["ManagerDataport"] + ["True"] + ["False"] * (n - 1))
    off = ",".join(["ManagerDataport"] + ["False"] * n)
    lines[i] = on
    path.write_text("\n".join(lines + [off]) + "\n", encoding="utf-8")
    iface = Interface()
    res = CSVHandler.load_csv(str(path), iface)
    assert res.success and any("ManagerDataport" in w and "repeats line" in w for w in res.notes())
    device = int(next(ln for ln in lines if ln.startswith("DeviceNumber_REG,")).split(",")[1])
    assert not iface.is_dp_in_manager(0) and iface.get_dp_device(0) == device
    lines[i] = off
    path.write_text("\n".join(lines + [on]) + "\n", encoding="utf-8")
    CSVHandler.load_csv(str(path), iface)
    assert iface.is_dp_in_manager(0) and iface.get_dp_device(0) == -1


def test_a_display_fields_character_outside_scb_is_reported(tmp_path):
    path = _saved(tmp_path)
    lines = _lines(path)
    i = next(i for i, ln in enumerate(lines) if ln.startswith("DisplayFields"))
    cells = lines[i].split(",")
    cells[1] = "sxz"
    lines[i] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = _load(path)
    assert res.success
    assert any("DisplayFields" in w and "'xz'" in w for w in res.notes())


def test_a_field_row_without_a_value_names_the_row_and_field(tmp_path):
    path = _saved(tmp_path)
    lines = _lines(path)
    i = next(i for i, ln in enumerate(lines) if ln.startswith("RowRate,"))
    lines[i] = "RowRate"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = _load(path)
    assert not res.success
    assert res.error_message == f"Line {i + 1} (RowRate): missing value"


def test_a_byte_order_mark_does_not_hide_the_first_field(tmp_path):
    path = _saved(tmp_path)
    clean = _load(path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    res = _load(path)
    assert res.unrecognized_fields == clean.unrecognized_fields
    assert res.missing_fields == clean.missing_fields


def test_a_load_without_a_viz_config_returns_the_one_it_filled(tmp_path):
    res = _load(_saved(tmp_path))
    assert isinstance(res.viz_config, VizConfig)
    own = VizConfig()
    assert _load(_saved(tmp_path), own).viz_config is own


def test_a_saved_file_has_unix_line_endings(tmp_path):
    assert b"\r" not in _saved(tmp_path).read_bytes()


def test_stray_display_fields_characters_in_several_ports_are_one_message(tmp_path):
    """A DisplayFields row with strays in more than one cell gives one message for the
    row, naming each stray set and the data ports it is in."""
    path = _saved(tmp_path)
    lines = _lines(path)
    i = next(i for i, ln in enumerate(lines) if ln.startswith("DisplayFields"))
    cells = lines[i].split(",")
    cells[1], cells[2], cells[4] = "sx", "sx", "q"
    lines[i] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = _load(path)
    hits = [w for w in res.notes() if "DisplayFields" in w]
    assert len(hits) == 1
    assert hits[0].startswith(f"Line {i + 1} (DisplayFields)")
    assert "'x' (data ports 0, 1)" in hits[0] and "'q' (data port 3)" in hits[0]


def test_a_reported_line_is_the_physical_line_when_a_description_spans_lines(tmp_path):
    """A quoted Description over several lines moves every later record down; the number
    in a message is the line the record starts on."""
    iface = Interface()
    iface.description = "a\nb\nc"
    path = tmp_path / "multi.csv"
    CSVHandler.save_csv(str(path), iface, VizConfig())
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write("NumColumns_REG\n")
    physical = len(path.read_text(encoding="utf-8").splitlines())
    res = CSVHandler.load_csv(str(path), Interface())
    assert res.error_message == f"Line {physical} (NumColumns_REG): missing value"


def _with_cells(tmp_path: Path, field: str, cells):
    """The saved file with `field`'s row replaced by `cells`; returns the load result and
    the 1-based line of the row."""
    path = _saved(tmp_path)
    lines = _lines(path)
    i = next(i for i, ln in enumerate(lines) if ln.startswith(field + ","))
    lines[i] = ",".join([field, *cells])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return _load(path), i + 1


def _row_cells(tmp_path: Path, field: str):
    path = _saved(tmp_path)
    return next(ln for ln in _lines(path) if ln.startswith(field + ",")).split(",")[1:]


@pytest.mark.parametrize("field, cell", [
    ("RowRate", "abc"),
    ("PHY3Enabled", "maybe"),
    ("NumColumns_REG", "99"),
])
def test_an_interface_cell_that_is_refused_names_its_line_and_field(tmp_path, field, cell):
    res, line = _with_cells(tmp_path, field, [cell])
    assert not res.success
    assert res.error_message.startswith(f"Line {line} ({field}): ")


@pytest.mark.parametrize("field, bad", [
    ("DeviceNumber_REG", "q"),
    ("SampleSize_REG", "x"),
])
def test_a_per_port_cell_that_is_refused_names_its_line_field_and_port(tmp_path, field, bad):
    cells = _row_cells(tmp_path, field)
    cells[1] = bad
    res, line = _with_cells(tmp_path, field, cells)
    assert not res.success
    assert res.error_message.startswith(f"Line {line} ({field}): data port 1: ")


_CDS_ROWS = {"CDS_TailWidthPerSource": ("CDS_Tail_PerSource", 2),
             "CDS_DriveTypePerSource": ("CDS_DriveType_PerSource", 1),
             "CDS_EndDriveEarlyPerSource": ("CDS_EndDriveEarly_PerSource", 1)}


@pytest.mark.parametrize("field, cell, want", [
    ("CDS_DriveTypePerSource", "4", 0),          # the field's default is 1
    ("CDS_DriveTypePerSource", "6", 0),
    ("CDS_EndDriveEarlyPerSource", "3", 1),      # the field's default is 0
    ("CDS_TailWidthPerSource", "-5", 3),         # the field's default is 0
    ("CDS_TailWidthPerSource", "9", 1),
])
def test_a_per_source_cds_cell_wider_than_its_register_carries_its_low_bits(
        tmp_path, field, cell, want):
    """The load succeeds with the cell reduced to the register's width (`want`, which
    differs from the field's default), and one warning names the row's line, the source
    and the value."""
    path = _saved(tmp_path)
    lines = _lines(path)
    i = next(i for i, ln in enumerate(lines) if ln.startswith(field + ","))
    cells = lines[i].split(",")
    cells[3 + 1] = cell                                   # column 4 is Device 2
    lines[i] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    iface = Interface()
    res = CSVHandler.load_csv(str(path), iface)
    attr, bits = _CDS_ROWS[field]
    assert res.success
    assert getattr(iface, attr)[3] == want
    hits = [w for w in res.warnings if field in w]
    assert len(hits) == 1
    assert hits[0].startswith(f"Line {i + 1} ({field}): does not fit its {bits}-bit")
    assert f"Device 2 = {cell}" in hits[0]


def test_a_scalar_cds_drive_type_wider_than_its_register_carries_its_low_bit(tmp_path):
    path = _saved(tmp_path)
    keep = [ln for ln in _lines(path) if not ln.startswith("CDS_DriveTypePerSource,")]
    path.write_text("\n".join(keep + ["CDS_DriveType,2"]) + "\n", encoding="utf-8")
    iface = Interface()
    res = CSVHandler.load_csv(str(path), iface)
    assert res.success and iface.CDS_DriveType_PerSource == [0] * 13
    assert any("CDS_DriveType" in w and "low bit is carried" in w for w in res.warnings)


@pytest.mark.parametrize("prefix", [0, 40_000])
def test_a_byte_that_is_not_utf8_is_reported_without_a_line(tmp_path, prefix):
    """The reader decodes in buffered chunks, so a decode error past the first chunk
    surfaces while an earlier row is the last one parsed; the message names no line or
    field, wherever the byte is."""
    path = _saved(tmp_path)
    path.write_bytes(path.read_bytes() + b"Description," + b"a" * prefix + b"\xff\xfe\n")
    res = _load(path)
    assert not res.success
    assert "utf-8" in res.error_message and "Line" not in res.error_message


def test_a_failed_save_leaves_the_existing_file_and_no_temporary_file(tmp_path, monkeypatch):
    """The text is built whole and moved into place: a write that fails, or a setting that
    cannot be formatted, leaves the old file as it was with nothing else in the folder."""
    import os
    path = _saved(tmp_path)
    old = path.read_bytes()

    def fail(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        CSVHandler.save_csv(str(path), Interface(), VizConfig())
    monkeypatch.undo()
    assert path.read_bytes() == old and os.listdir(tmp_path) == ["cfg.csv"]

    iface = Interface()
    iface.data_ports[0].config.EnableCh_REG = "not a number"      # bin() refuses it
    with pytest.raises(TypeError):
        CSVHandler.save_csv(str(path), iface, VizConfig())
    assert path.read_bytes() == old and os.listdir(tmp_path) == ["cfg.csv"]


@pytest.mark.parametrize("tail, drive, ede", [(9, 5, 2), (-5, 0, 3)])
def test_a_config_with_over_range_cds_values_builds_the_grid_of_the_carried_values(
        tmp_path, tail, drive, ede):
    """End to end: BusConfig writes the values as given, the swviz engine reads them to
    their low bits, builds the same grid as the config holding those low bits, and the
    native core builds the same too."""
    import swi3score

    from swi3s_studio import import_notes
    from swi3s_studio.model import viz_engine
    from swi3s_studio.model.bus_config import demo_config

    def csv_for(t, d, e, name):
        cfg = demo_config()
        cfg.cds_tail[2], cfg.cds_drive_type[2], cfg.cds_end_drive_early[2] = t, d, e
        return cfg.to_csv_file(str(tmp_path / name))

    def key(cells):
        return sorted((c["row"], c["col"], c["slot"], c["dp"], bool(c.get("is_cds")))
                      for c in cells)

    raw = csv_for(tail, drive, ede, "raw.csv")
    low = csv_for(tail & 3, drive & 1, ede & 1, "low.csv")
    with import_notes.collect() as notes:
        cells = viz_engine.render_payload(raw, 8)[0]
    assert any("CDS_TailWidthPerSource" in n.message and "carried" in n.message
               for n in notes), [str(n) for n in notes]
    assert key(cells) == key(viz_engine.render_payload(low, 8)[0])
    assert key(swi3score.grid_from_csv(raw, 8)) == key(swi3score.grid_from_csv(low, 8))


# (scalar row, per-source row, Interface attribute, the attribute's default, a valid scalar
# value that differs from the default)
_SCALARS = [("CDS_TailWidth_REG", "CDS_TailWidthPerSource", "CDS_Tail_PerSource", [0] * 13, 2),
            ("CDS_DriveType", "CDS_DriveTypePerSource", "CDS_DriveType_PerSource", [1] * 13, 0)]


def _with_scalar(tmp_path: Path, name: str, per_source: str, scalar_rows, keep_per_source=False):
    """The saved file with its per-source row replaced by (or kept beside) scalar rows."""
    lines = [ln for ln in _lines(_saved(tmp_path))
             if keep_per_source or not ln.startswith(per_source + ",")]
    path = tmp_path / name
    path.write_text("\n".join(lines + scalar_rows) + "\n", encoding="utf-8")
    return path, len(lines) + 1


@pytest.mark.parametrize("key, per_source, attr, default, valid", _SCALARS)
@pytest.mark.parametrize("value", ["-1", "-2", "-5"])    # -2 has the low bit 0 of a drive type
def test_a_negative_legacy_scalar_cds_row_is_ignored_and_reported(tmp_path, key, per_source,
                                                                  attr, default, valid, value):
    """The file loads, the sources keep the defaults a file without the row has, and the
    row is reported with its line and field (BusConfig and the native loader ignore it too)."""
    path, line = _with_scalar(tmp_path, "neg.csv", per_source, [f"{key},{value}"])
    plain, _ = _with_scalar(tmp_path, "plain.csv", per_source, [])
    iface, base = Interface(), Interface()
    res = CSVHandler.load_csv(str(path), iface)
    CSVHandler.load_csv(str(plain), base)
    assert res.success, res.error_message
    assert getattr(iface, attr) == getattr(base, attr) == default
    assert res.warnings == [f"Line {line} ({key}): {value} is negative and is ignored; "
                            f"the CDS sources keep their defaults."]


@pytest.mark.parametrize("key, per_source, attr, default, valid", _SCALARS)
def test_a_negative_legacy_scalar_is_silent_beside_its_per_source_row(
        tmp_path, key, per_source, attr, default, valid):
    path, _ = _with_scalar(tmp_path, "both.csv", per_source, [f"{key},-1"],
                           keep_per_source=True)
    res = CSVHandler.load_csv(str(path), Interface())
    assert res.success and res.warnings == []


@pytest.mark.parametrize("key, per_source, attr, default, valid", _SCALARS)
def test_the_last_row_of_a_legacy_scalar_decides(tmp_path, key, per_source, attr, default,
                                                 valid):
    """A later non-negative row applies and nothing is reported; a later negative row
    after a valid one is ignored. The valid value differs from the default."""
    path, _ = _with_scalar(tmp_path, "later.csv", per_source,
                           [f"{key},-1", f"{key},{valid}"])
    iface = Interface()
    res = CSVHandler.load_csv(str(path), iface)
    assert res.success and getattr(iface, attr) == [valid] * 13 != default
    assert [w for w in res.warnings if "negative" in w] == []
    path, line = _with_scalar(tmp_path, "after.csv", per_source,
                              [f"{key},{valid}", f"{key},-1"])
    iface = Interface()
    res = CSVHandler.load_csv(str(path), iface)
    assert getattr(iface, attr) == default
    assert [w for w in res.warnings if "negative" in w] == [
        f"Line {line + 1} ({key}): -1 is negative and is ignored; the CDS sources keep "
        f"their defaults."]


# ---- one reading of a cell across the Visualizer engine, BusConfig and the native loader ----

def _three_readings(tmp_path: Path, field: str, cell: str):
    """``(engine result, interface, BusConfig, BusConfig warnings, native warnings)`` for the
    saved file with `field`'s only cell replaced by `cell`."""
    import swi3score

    from swi3s_studio import import_notes
    from swi3s_studio.model.bus_config import BusConfig
    path = _saved(tmp_path)
    lines = _lines(path)
    i = next(i for i, ln in enumerate(lines) if ln.startswith(field + ","))
    lines[i] = f"{field},{cell}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    iface = Interface()
    res = CSVHandler.load_csv(str(path), iface)
    with import_notes.collect() as notes:
        cfg = BusConfig.from_csv_text(path.read_text(encoding="utf-8"), "t.csv")
    return res, iface, cfg, [n.message for n in notes], list(swi3score.csv_warnings(str(path)))


@pytest.mark.parametrize("cell, want", [("yes", True), ("No", False), ("TRUE", True),
                                        ("0", False)])
def test_a_flag_cell_reads_the_same_in_the_engine_and_in_bus_config(tmp_path, cell, want):
    res, iface, cfg, notes, native = _three_readings(tmp_path, "PHY3Enabled", cell)
    assert res.success and iface.phy3_enabled is want and cfg.phy3_enabled is want
    assert notes == [] and not [w for w in native if "PHY3Enabled" in w]


@pytest.mark.parametrize("cell, want", [("0x0F", 15), ("0b101", 5), ("+7", 7), ("12", 12)])
def test_a_whole_number_cell_reads_the_same_in_the_engine_and_in_bus_config(tmp_path, cell,
                                                                           want):
    res, iface, cfg, notes, native = _three_readings(tmp_path, "NumColumns_REG", cell)
    assert res.success and iface.NumColumns_REG == want and cfg.num_columns == want
    assert notes == [] and not [w for w in native if "NumColumns_REG" in w]


@pytest.mark.parametrize("cell", ["1_6", "٣", "-0x1", "1.0", "0x", "1 6"])
def test_a_cell_that_is_not_a_whole_number_is_refused_by_the_engine_and_noted_elsewhere(
        tmp_path, cell):
    """The engine refuses the file naming the line; BusConfig and the native loader read
    the cell's leading digits and say so. No reader takes an underscore or another
    script's digit as part of a number."""
    res, _iface, _cfg, notes, native = _three_readings(tmp_path, "NumColumns_REG", cell)
    assert not res.success and "Cannot parse integer" in res.error_message
    assert res.error_message.startswith("Line ") and "(NumColumns_REG)" in res.error_message
    assert any("NumColumns_REG" in n for n in notes)
    assert any("NumColumns_REG" in w for w in native)


@pytest.mark.parametrize("cell, want", [("3072", 3072.0), ("+3072.5", 3072.5), ("3.072e3", 3072.0),
                                        ("2000.", 2000.0)])
def test_a_row_rate_cell_reads_the_same_in_the_engine_and_in_bus_config(tmp_path, cell, want):
    res, iface, cfg, _notes, native = _three_readings(tmp_path, "RowRate", cell)
    assert res.success and iface.row_rate == want and cfg.row_rate_khz == want
    assert not [w for w in native if "RowRate" in w]


@pytest.mark.parametrize("cell", ["3_072", "\u0663072", "nan", "inf", "3072kHz", "1e", "0x10"])
def test_a_row_rate_cell_that_is_not_a_number_is_refused_by_the_engine_and_noted_elsewhere(
        tmp_path, cell):
    """No reader takes an underscore, another script's digit, 'nan' or 'inf' as a number."""
    res, _iface, cfg, notes, native = _three_readings(tmp_path, "RowRate", cell)
    assert not res.success and "Cannot parse float" in res.error_message
    assert any("RowRate" in n for n in notes) and any("RowRate" in w for w in native)


def test_a_flag_cell_that_is_not_a_flag_is_refused_by_the_engine_and_noted_elsewhere(tmp_path):
    res, _iface, _cfg, notes, native = _three_readings(tmp_path, "PHY3Enabled", "maybe")
    assert not res.success and "Cannot parse boolean" in res.error_message
    assert any("PHY3Enabled" in n for n in notes) and any("PHY3Enabled" in w for w in native)


def test_a_refused_cell_is_quoted_cut_to_sixty_characters(tmp_path):
    from swi3s_studio.ingest.timebase import quoted
    cell = "x" * 100_000
    res, _iface, _cfg, _notes, _native = _three_readings(tmp_path, "NumColumns_REG", cell)
    assert not res.success and len(res.error_message) < 200
    assert res.error_message.endswith(f"Cannot parse integer from {quoted(cell)}")
    res, *_ = _three_readings(tmp_path, "PHY3Enabled", cell)
    assert res.error_message.endswith(f"Cannot parse boolean from {quoted(cell)}")
    res, *_ = _three_readings(tmp_path, "RowRate", cell)
    assert res.error_message.endswith(f"Cannot parse float from {quoted(cell)}")


def test_unrecognised_fields_are_listed_a_few_at_a_time_and_the_rest_counted(tmp_path):
    path = _saved(tmp_path)
    junk = [f"Junk{i}_{'x' * 200},1" for i in range(5000)]
    path.write_text("\n".join(_lines(path) + junk) + "\n", encoding="utf-8")
    res = _load(path)
    assert res.success and len(res.unrecognized_fields) == 5000
    [note] = [n for n in res.notes(missing=False) if "not recognised" in n]
    assert note.startswith("5000 fields are not recognised and were ignored: Junk0_")
    assert note.count(" (line ") == 3 and note.endswith("and 4997 more.")
    assert len(note) < 400


def test_a_legacy_scalar_cds_tail_width_above_its_register_carries_its_low_bits(tmp_path):
    """The load succeeds with every source holding the value's low two bits, and one warning
    names the line and the value, as the other readers do."""
    from swi3s_studio import import_notes
    from swi3s_studio.model.bus_config import BusConfig
    path, line = _with_scalar(tmp_path, "wide.csv", "CDS_TailWidthPerSource",
                              ["CDS_TailWidth_REG,6"])
    iface = Interface()
    res = CSVHandler.load_csv(str(path), iface)
    assert res.success, res.error_message
    assert iface.CDS_Tail_PerSource == [2] * 13
    assert res.warnings == [f"Line {line} (CDS_TailWidth_REG): 6 does not fit its 2-bit "
                            f"register field (0..3); the low 2 bits are carried."]
    with import_notes.collect() as notes:
        cfg = BusConfig.from_csv_text(path.read_text(encoding="utf-8"), "t.csv")
    assert cfg.cds_tail == [6] * 13 and any("does not fit its 2-bit" in n.message
                                            for n in notes)
