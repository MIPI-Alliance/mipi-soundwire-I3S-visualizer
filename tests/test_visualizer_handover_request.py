"""A handover is a device's request to hand the bus over at a slot.

Only another device's write, guard or tail in that slot clashes with the request. The
requesting device's own data, guard or tail in its handover column is not a clash, and its
handover marker stays.

Layout of the configs below (row 0): column 0 CDS, column 1 CDS guard, column 2 the CDS
handover; the Manager port's data, guard or tail lands in that handover column.
"""
import csv
import io

from swi3s_studio.model import viz_engine
from swi3s_studio.model.bus_config import demo_config


def _model(tmp_path, **overrides):
    """The demo config with each override written into the first value of its row; an
    override naming no row of the config is an error."""
    rows = list(csv.reader(io.StringIO(demo_config().to_csv())))
    applied = set()
    for r in rows:
        if not r:
            continue
        if r[0] == "CDS_GuardEnabledPerSource":
            r[1:] = ["1"] * 13
        elif r[0] in overrides:
            r[1] = str(overrides[r[0]])
            applied.add(r[0])
        elif r[0] == "ManagerDataport":
            r[1] = "True"
    assert applied == set(overrides), f"no such config row: {set(overrides) - applied}"
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    path = tmp_path / "cfg.csv"
    path.write_text(buf.getvalue(), encoding="utf-8", newline="")
    model, _iface, _viz = viz_engine.build_bus_model(str(path))
    return model


def _slots(model, row, column):
    return [(b.slot.name, b.device, b.clash.name)
            for b in model.get_bits_at(model.bit_index(row, column))]


def _is_a_bus_clash(model, row, column):
    return model.bit_index(row, column) in model.bus_clashes


def test_a_uniform_guard_leaves_the_handover_where_no_port_drives(tmp_path):
    m = _model(tmp_path, HorizontalStart_REG=3)
    assert _slots(m, 0, 2) == [("HANDOVER", -1, "NONE")]        # the port's data starts at 3
    assert _slots(m, 1, 2) == [("HANDOVER", -1, "NONE")]        # no data on row 1


def test_a_data_write_keeps_its_own_devices_handover_request(tmp_path):
    m = _model(tmp_path, HorizontalStart_REG=2)
    assert _slots(m, 0, 2) == [("HANDOVER", -1, "NONE"), ("DATA", -1, "NONE")]
    assert not _is_a_bus_clash(m, 0, 2)
    assert _slots(m, 1, 2) == [("HANDOVER", -1, "NONE")]


def test_a_port_guard_keeps_its_own_devices_handover_request(tmp_path):
    m = _model(tmp_path, HorizontalStart_REG=1, GuardEnable_REG=True,
               HorizontalCount_REG=0, EnableCh_REG=1, SampleSize_REG=0)
    assert _slots(m, 0, 2) == [("HANDOVER", -1, "NONE"), ("GUARD_0", -1, "NONE")]
    assert not _is_a_bus_clash(m, 0, 2)
    assert _slots(m, 1, 2) == [("HANDOVER", -1, "NONE")]


def test_a_port_tail_keeps_its_own_devices_handover_request(tmp_path):
    m = _model(tmp_path, HorizontalStart_REG=1, TailWidth_REG=1,
               HorizontalCount_REG=0, EnableCh_REG=1)
    assert _slots(m, 0, 2) == [("HANDOVER", -1, "NONE"), ("TAIL", -1, "NONE")]
    assert not _is_a_bus_clash(m, 0, 2)
    assert _slots(m, 1, 2) == _slots(m, 0, 2)                   # a tail follows every row
    assert not _is_a_bus_clash(m, 1, 2)


def test_another_devices_write_clashes_with_the_handover_request(tmp_path):
    m = _model(tmp_path, ManagerDataport=False, HorizontalStart_REG=2)
    slots = _slots(m, 0, 2)
    assert ("HANDOVER", -1, "DIFFERENT_DEVICE") in slots
    assert ("DATA", 0, "DIFFERENT_DEVICE") in slots
    assert _is_a_bus_clash(m, 0, 2)


def test_another_devices_guard_clashes_with_the_handover_request(tmp_path):
    m = _model(tmp_path, ManagerDataport=False, HorizontalStart_REG=1, GuardEnable_REG=True,
               HorizontalCount_REG=0, EnableCh_REG=1, SampleSize_REG=0)
    slots = _slots(m, 0, 2)
    assert ("HANDOVER", -1, "DIFFERENT_DEVICE") in slots
    assert _is_a_bus_clash(m, 0, 2)
    assert _slots(m, 1, 2) == [("HANDOVER", -1, "NONE")]
    assert not _is_a_bus_clash(_model(tmp_path, ManagerDataport=False, HorizontalStart_REG=1,
                                      GuardEnable_REG=False, HorizontalCount_REG=0,
                                      EnableCh_REG=1, SampleSize_REG=0), 0, 2)


def test_another_devices_tail_clashes_with_the_handover_request(tmp_path):
    m = _model(tmp_path, ManagerDataport=False, HorizontalStart_REG=1, TailWidth_REG=1,
               HorizontalCount_REG=0, EnableCh_REG=1)
    slots = _slots(m, 0, 2)
    assert ("HANDOVER", -1, "DIFFERENT_DEVICE") in slots
    assert _is_a_bus_clash(m, 0, 2)
    assert _slots(m, 1, 2) == _slots(m, 0, 2) and _is_a_bus_clash(m, 1, 2)
    assert not _is_a_bus_clash(_model(tmp_path, ManagerDataport=False, HorizontalStart_REG=1,
                                      TailWidth_REG=0, HorizontalCount_REG=0, EnableCh_REG=1), 0, 2)
