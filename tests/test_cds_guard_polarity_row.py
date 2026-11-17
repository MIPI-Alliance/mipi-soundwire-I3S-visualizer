"""The per-source CDS guard is two independent fields, enable and polarity, in all three config
loaders (BusConfig.from_csv, the native swi3score.config_from_csv and the swviz CSVHandler):

- the three read the same enables and polarities for the polarity row alone, the scalar
  enable, the per-source enable, both polarity forms (the per-source row and the scalar
  CDS_GuardPolarity_REG) and a legacy file, with no note about the polarity;
- both CSV writers write the polarity row back as read, a polarity of 1 on a disabled
  guard included;
- a disabled guard's polarity is bit 2 of its expected CDS register;
- a saved workspace holding the packed `cds_guard` loads, with no note;
- a guard cell is drawn only for a source whose guard is enabled, with its polarity.

Every input is synthetic: the demo config with its guard rows replaced."""
import csv
import io

import pytest
import swi3score

from swi3s_studio import import_notes
from swi3s_studio.ingest import visualizer_csv
from swi3s_studio.model import viz_engine
from swi3s_studio.model.bus_config import BusConfig, demo_config
from swi3s_studio.model.grid_slots import GridSlot
from swi3s_studio.model.viz_engine import build_bus_model
from swi3s_studio.swviz.io.csv_handler import CSVHandler

_GUARD_ROWS = ("CDS_GuardEnable_REG_PerSource", "CDS_GuardPolarity_REG_PerSource",
               "CDS_GuardEnabled_REG", "CDS_GuardPolarity_REG")
# Device 0, the demo's only port-owning device, is source 1: polarity 1.
_POL = [0, 1] * 6 + [0]
_PS_EN = [1] * 7 + [0] * 6
_CDS_GUARD_REG = 0x1187               # bit 3 GuardEnable, bit 2 GuardPolarity


def _row(key, values):
    return [key] + [str(v) for v in values]


_CASES = {
    # extra rows -> (enable, polarity)
    "polarity row alone": (
        [_row("CDS_GuardPolarity_REG_PerSource", _POL)], [0] * 13, _POL),
    "with the scalar enable": (
        [["CDS_GuardEnabled_REG", "True"], _row("CDS_GuardPolarity_REG_PerSource", _POL)],
        [1] * 13, _POL),
    "with the per-source enable": (
        [_row("CDS_GuardEnable_REG_PerSource", _PS_EN),
         _row("CDS_GuardPolarity_REG_PerSource", _POL)], _PS_EN, _POL),
    "the per-source row over a scalar polarity": (
        [["CDS_GuardEnabled_REG", "True"], ["CDS_GuardPolarity_REG", "True"],
         _row("CDS_GuardPolarity_REG_PerSource", _POL)], [1] * 13, _POL),
    "a scalar polarity with the guard off": (
        [["CDS_GuardEnabled_REG", "False"], ["CDS_GuardPolarity_REG", "True"]],
        [0] * 13, [1] * 13),
    "a scalar polarity with the per-source enable": (
        [_row("CDS_GuardEnable_REG_PerSource", _PS_EN), ["CDS_GuardPolarity_REG", "True"]],
        _PS_EN, [1] * 13),
}


def _write(tmp_path, extra):
    rows = [r for r in csv.reader(io.StringIO(demo_config().to_csv()))
            if r and r[0] not in _GUARD_ROWS]
    path = tmp_path / "guard.csv"
    path.write_text("\n".join(",".join(r) for r in rows + extra) + "\n", encoding="utf-8")
    return str(path)


def _legacy(tmp_path, enabled, polarity):
    """A legacy (v1.73) file upgraded to the current format."""
    path = tmp_path / "old.csv"
    path.write_text("Columns per Row (2-32),8\nRow Rate [kHz],3072\n"
                    f"CDS Guard Enabled,{enabled}\nCDS Guard Polarity,{polarity}\n",
                    encoding="utf-8")
    with import_notes.collect() as notes:
        out = visualizer_csv.normalized_path(str(path))
    assert [n.message for n in notes] == []
    return out


def _polarity_row(path):
    with open(path, encoding="utf-8") as f:
        rows = {r[0]: r[1:] for r in csv.reader(f) if r}
    return [int(v) for v in rows["CDS_GuardPolarity_REG_PerSource"]]


def _loaders(path):
    """(enable, polarity) as BusConfig, the native loader and the CSVHandler read `path`,
    and the BusConfig notes."""
    with import_notes.collect() as bus_notes:
        cfg = BusConfig.from_csv(path)
    native = swi3score.config_from_csv(path)
    with import_notes.collect():
        _model, iface, _viz = build_bus_model(path, 4)
    got = [
        ([int(e) for e in cfg.cds_guard_enable], list(cfg.cds_guard_polarity)),
        (list(native["cds_guard_enable"]), list(native["cds_guard_polarity"])),
        ([int(e) for e in iface.CDS_GuardEnable_PerSource],
         list(iface.CDS_GuardPolarity_PerSource)),
    ]
    return got, [n.message for n in bus_notes]


@pytest.mark.parametrize("case", list(_CASES))
def test_the_three_loaders_read_enable_and_polarity_alike(tmp_path, case):
    extra, enable, polarity = _CASES[case]
    path = _write(tmp_path, extra)
    got, notes = _loaders(path)
    assert got == [(enable, polarity)] * 3
    # The polarity is never reported, whatever the enables.
    assert not any("CDS_GuardPolarity" in m for m in notes)
    assert not any("CDS_GuardPolarity" in w for w in swi3score.csv_warnings(path))


@pytest.mark.parametrize("enabled,polarity,en,pol", [
    ("True", "True", 1, 1), ("False", "True", 0, 1), ("True", "False", 1, 0)])
def test_the_three_loaders_read_a_legacy_guard_alike(tmp_path, enabled, polarity, en, pol):
    got, _notes = _loaders(_legacy(tmp_path, enabled, polarity))
    assert got == [([en] * 13, [pol] * 13)] * 3


@pytest.mark.parametrize("case", list(_CASES))
def test_both_writers_write_the_polarity_row_back_as_read(tmp_path, case):
    extra, _enable, polarity = _CASES[case]
    path = _write(tmp_path, extra)
    with import_notes.collect():
        cfg = BusConfig.from_csv(path)
        _model, iface, viz = build_bus_model(path, 4)
    bus_out = cfg.to_csv_file(str(tmp_path / "bus.csv"))
    viz_out = str(tmp_path / "viz.csv")
    CSVHandler.save_csv(viz_out, iface, viz)
    assert _polarity_row(bus_out) == _polarity_row(viz_out) == polarity


def test_a_csv_round_trip_keeps_polarity_1_with_the_guard_off(tmp_path):
    cfg = demo_config()
    cfg.cds_guard_enable = [False] * 13
    cfg.cds_guard_polarity = [1] * 13
    path = cfg.to_csv_file(str(tmp_path / "off.csv"))
    got, _notes = _loaders(path)
    assert got == [([0] * 13, [1] * 13)] * 3
    with import_notes.collect():
        _model, iface, viz = build_bus_model(path, 4)
    CSVHandler.save_csv(str(tmp_path / "viz.csv"), iface, viz)
    assert _polarity_row(str(tmp_path / "viz.csv")) == [1] * 13
    assert BusConfig.from_csv(str(tmp_path / "viz.csv")).cds_guard_polarity == [1] * 13


def test_a_disabled_guard_s_polarity_reaches_the_expected_register(tmp_path):
    """Device 0's guard is off and its polarity row reads 1: its expected CDS register
    carries GuardPolarity and not GuardEnable."""
    regs = {(d, a): v for d, a, v in swi3score.registers_from_csv(
        _write(tmp_path, [_row("CDS_GuardPolarity_REG_PerSource", _POL)]))}
    assert (regs[(0, _CDS_GUARD_REG)] >> 2) & 0x3 == 0b01


@pytest.mark.parametrize("packed,enable,polarity", [
    (0, False, 0), (1, True, 0), (2, True, 1)])
def test_a_saved_workspace_with_the_packed_guard_loads(packed, enable, polarity):
    """0 is off with polarity 0, 1 on with polarity 0, 2 on with polarity 1, with no note;
    a save writes the two fields and not the packed one."""
    d = demo_config().to_dict()
    del d["cds_guard_enable"], d["cds_guard_polarity"]
    d["cds_guard"] = [packed] * 13
    with import_notes.collect() as notes:
        cfg = BusConfig.from_dict(d, "ws")
    assert [n.message for n in notes] == []
    assert cfg.cds_guard_enable == [enable] * 13
    assert cfg.cds_guard_polarity == [polarity] * 13
    saved = cfg.to_dict()
    assert "cds_guard" not in saved
    assert saved["cds_guard_enable"] == [enable] * 13
    assert saved["cds_guard_polarity"] == [polarity] * 13


def test_a_saved_workspace_with_the_scalar_guard_keys_loads():
    """The older scalar keys: the enable broadcast, the polarity kept with the guard off."""
    d = demo_config().to_dict()
    del d["cds_guard_enable"], d["cds_guard_polarity"]
    with import_notes.collect() as notes:
        on = BusConfig.from_dict({**d, "cds_guard_enabled": True, "cds_guard_polarity": True})
        off = BusConfig.from_dict({**d, "cds_guard_enabled": False, "cds_guard_polarity": True})
    assert [n.message for n in notes] == []
    assert (on.cds_guard_enable, on.cds_guard_polarity) == ([True] * 13, [1] * 13)
    assert (off.cds_guard_enable, off.cds_guard_polarity) == ([False] * 13, [1] * 13)


def test_a_guard_cell_is_drawn_only_where_the_guard_is_enabled(tmp_path):
    """Device 0 (source 1) on at polarity 0, Device 1 on at polarity 1, every other source off
    at polarity 1: the guard column holds a G0 cell for Device 0, a G1 cell for Device 1 and
    nothing for anyone else."""
    cfg = demo_config()
    cfg.cds_guard_enable = [False, True, True] + [False] * 10
    cfg.cds_guard_polarity = [1, 0, 1] + [1] * 10
    path = cfg.to_csv_file(str(tmp_path / "g.csv"))
    with import_notes.collect():
        cells, _clashes, _issues, _cols, _rows = viz_engine.render_payload(path)
    guards = {(c["device"], c["slot"]) for c in cells
              if c["row"] == 0 and c["slot"] in (GridSlot.GUARD_0, GridSlot.GUARD_1)}
    assert guards == {(0, GridSlot.GUARD_0), (1, GridSlot.GUARD_1)}
    # With every guard off, whatever the polarities, no guard cell is drawn.
    cfg.cds_guard_enable = [False] * 13
    path = cfg.to_csv_file(str(tmp_path / "off.csv"))
    with import_notes.collect():
        cells, _clashes, _issues, _cols, _rows = viz_engine.render_payload(path)
    assert not [c for c in cells if c["slot"] in (GridSlot.GUARD_0, GridSlot.GUARD_1)]
