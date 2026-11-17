"""The older spellings of the per-source CDS rows, read by all three config-CSV readers.

* Each per-source CDS row is written as CDS_<field>_REG_PerSource; a file giving it as
  CDS_<field>PerSource reads exactly as its twin with the current name, in BusConfig, the
  swviz CSVHandler and the native loader: the same values, registers and warnings.
* A warning about a row read under its older spelling names the current one.
* A file carrying a row in both spellings repeats the row: each reader reads it as it reads
  the current name given twice: each reports the repeat and uses the later row.

Every input is synthetic: a config the app writes, with row keys edited."""
import csv
import io

import pytest
import swi3score

from swi3s_studio import import_notes
from swi3s_studio.model.bus_config import BusConfig, demo_config
from swi3s_studio.swviz.io.csv_handler import CSVHandler
from swi3s_studio.swviz.models.interface import Interface
from swi3s_studio.swviz.viz import VizConfig

# The older spelling -> the current name. Stated here rather than imported, so the test
# pins the names a file may carry.
_OLDER = {
    "CDS_GuardEnabledPerSource": "CDS_GuardEnable_REG_PerSource",
    "CDS_GuardPolarityPerSource": "CDS_GuardPolarity_REG_PerSource",
    "CDS_TailWidthPerSource": "CDS_TailWidth_REG_PerSource",
    "CDS_DriveTypePerSource": "CDS_DriveType_REG_PerSource",
    "CDS_EndDriveEarlyPerSource": "CDS_EndDriveEarly_REG_PerSource",
}


def _config() -> BusConfig:
    """The demo config with every per-source CDS list off its default on Device 0, whose CDS
    registers the native loader's expected registers carry, and varying across sources."""
    cfg = demo_config()
    cfg.cds_guard_enable = [True, True, False] * 4 + [True]
    cfg.cds_guard_polarity = [0, 1, 1] * 4 + [0]
    cfg.cds_tail = [1, 2, 0] * 4 + [1]
    cfg.cds_drive_type = [1, 0] * 6 + [1]
    cfg.cds_end_drive_early = [0, 1] * 6 + [0]
    return cfg


def _rows():
    return list(csv.reader(io.StringIO(_config().to_csv())))


def _write(path, rows) -> str:
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _renamed(rows, names):
    """`rows` with each row named in `names` given its older spelling."""
    older = {new: old for old, new in _OLDER.items()}
    return [[older[r[0]]] + r[1:] if r and r[0] in names else list(r) for r in rows]


def _busconfig(path):
    with import_notes.collect() as notes:
        cfg = BusConfig.from_csv(path, source="cfg.csv")
    return ((cfg.cds_guard_enable, cfg.cds_guard_polarity, cfg.cds_tail, cfg.cds_drive_type,
             cfg.cds_end_drive_early),
            [n.message for n in notes])


def _handler(path):
    iface = Interface()
    res = CSVHandler.load_csv(path, iface, VizConfig())
    assert res.success, res.error_message
    return ((iface.CDS_GuardEnable_PerSource, iface.CDS_GuardPolarity_PerSource,
             iface.CDS_Tail_PerSource,
             iface.CDS_DriveType_PerSource, iface.CDS_EndDriveEarly_PerSource),
            res.notes())


def _native(path):
    return swi3score.registers_from_csv(path), list(swi3score.csv_warnings(path))


_READERS = {"BusConfig": _busconfig, "CSVHandler": _handler, "native": _native}


def test_both_writers_write_the_current_names_only(tmp_path):
    path = tmp_path / "h.csv"
    CSVHandler.save_csv(str(path), Interface(), VizConfig())
    with open(path, newline="", encoding="utf-8") as f:
        handler_keys = {r[0] for r in csv.reader(f) if r}
    for keys in ({r[0] for r in _rows() if r}, handler_keys):
        assert set(_OLDER.values()) <= keys and not set(_OLDER) & keys


@pytest.mark.parametrize("reader", sorted(_READERS))
@pytest.mark.parametrize("older", sorted(_OLDER))
def test_a_row_under_its_older_spelling_reads_as_its_twin(tmp_path, reader, older):
    """One row renamed: the same values, registers and warnings as the file as written; and
    the row takes effect (the file without it reads otherwise)."""
    read = _READERS[reader]
    new = _OLDER[older]
    rows = _rows()
    twin = read(_write(tmp_path / "new.csv", rows))
    assert read(_write(tmp_path / "old.csv", _renamed(rows, {new}))) == twin
    without = read(_write(tmp_path / "without.csv", [r for r in rows if r[0] != new]))
    assert without[0] != twin[0], f"{new} has no effect on {reader}; the check proves nothing"


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_a_file_with_every_older_spelling_reads_as_its_twin(tmp_path, reader):
    read = _READERS[reader]
    rows = _rows()
    assert read(_write(tmp_path / "old.csv", _renamed(rows, set(_OLDER.values())))) == \
        read(_write(tmp_path / "new.csv", rows))


def test_a_warning_on_an_older_spelling_names_the_current_one(tmp_path):
    """A drive type of 2 on Device 0 does not fit its 1-bit field, and a value past the last
    source column is ignored; each reader names CDS_DriveType_REG_PerSource."""
    rows = _renamed(_rows(), {"CDS_DriveType_REG_PerSource"})
    drive = next(r for r in rows if r[0] == "CDS_DriveTypePerSource")
    drive[2] = "2"
    path = _write(tmp_path / "old.csv", rows)
    for reader in ("BusConfig", "CSVHandler"):
        notes = _READERS[reader](path)[1]
        assert any("CDS_DriveType_REG_PerSource" in n for n in notes), (reader, notes)
        assert not any("CDS_DriveTypePerSource" in n for n in notes), (reader, notes)
    native = _native(path)[1]
    assert any(n.startswith("CDS_DriveType_REG_PerSource does not fit") for n in native), native
    drive.append("9")                     # CSVHandler refuses a 14-wide row; the others read it
    path = _write(tmp_path / "wide.csv", rows)
    assert any("CDS_DriveType_REG_PerSource: values past the last CDS source (13 columns)"
               in n for n in _native(path)[1])
    assert any("CDS_DriveType_REG_PerSource ('9')" in n for n in _busconfig(path)[1])


@pytest.mark.parametrize("older", sorted(_OLDER))
def test_a_row_in_both_spellings_is_a_repeated_row(tmp_path, older):
    """The older spelling first and the current name after, with other values: each reader
    reads the file as the current name given twice, and the later row is used."""
    new = _OLDER[older]
    rows = _rows()
    at = next(i for i, r in enumerate(rows) if r[0] == new)
    later = [new] + ["0" if c != "0" else "1" for c in rows[at][1:]]
    both = rows[:at] + [[older] + rows[at][1:]] + rows[at + 1:] + [later]
    twice = rows[:at] + [rows[at]] + rows[at + 1:] + [later]
    single = rows[:at] + rows[at + 1:] + [later]
    for reader, read in _READERS.items():
        got = read(_write(tmp_path / "both.csv", both))
        assert got == read(_write(tmp_path / "twice.csv", twice)), reader
        assert got[0] == read(_write(tmp_path / "single.csv", single))[0], reader
    assert f"{new} appears 2 times; the last row is used." in _native(str(tmp_path / "both.csv"))[1]
    assert f"{new} appears 2 times; the last row is used." in _busconfig(
        str(tmp_path / "both.csv"))[1]
    notes = _handler(str(tmp_path / "both.csv"))[1]
    assert f"Line {len(both)} ({new}) repeats line {at + 1}; the later row is used." in notes
