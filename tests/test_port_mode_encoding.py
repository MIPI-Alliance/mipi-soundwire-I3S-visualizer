"""DPn_PortMode encoding: 0 Normal, 1 Reserved, 2 Test0, 3 Test1 (spec Table 157, "Effect
of DPn_PortMode").

In Test0 every Channel, TxPresentCh and FCP DRQ bit carries raw 0 (before scrambling); in
Test1 every such bit carries raw 1. Pinned here, for each place the value is read:

- the register map (`data/registers.json`) names 2 Test0 and 3 Test1, and the example configs
  `test_mode_zeros.csv` and `test_mode_ones.csv` carry PortMode_REG 2 and 3;
- the swviz model: `PortMode.TEST0 == 2`, `PortMode.TEST1 == 3`, and `port_mode_bit`
  maps each mode to the raw bit value it forces;
- the native core: `grid_from_csv` carries the port's PortMode on each of its DATA cells.

The Bus Grid labels and the Port Test Mode dialog are in `test_port_mode_encoding_gui.py`.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_port_mode_encoding.py
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
from pathlib import Path

import pytest

from swi3s_studio.model.bus_config import BusConfig
from swi3s_studio.model.grid_slots import GridSlot

ROOT = Path(__file__).resolve().parent.parent
_RAW_BIT = {2: 0, 3: 1}                      # PortMode -> raw value of every channel bit


def _cfg(port_mode: int) -> BusConfig:
    """One source port in `port_mode`, two one-bit channels side by side in every row."""
    cfg = BusConfig(num_columns=6, row_rate_khz=3072.0)
    dp = cfg.dataports[0]
    dp.enabled = True
    dp.device_number = 0
    dp.port_direction = False
    dp.port_mode = port_mode
    dp.enable_ch = 0b11
    dp.sample_size = 0
    dp.interval = 1
    dp.offset = 0
    dp.horizontal_start = 2
    dp.horizontal_count = 2
    return cfg


def _native_cells(port_mode: int) -> list:
    import swi3score
    with tempfile.TemporaryDirectory() as d:
        return swi3score.grid_from_csv(_cfg(port_mode).to_csv_file(os.path.join(d, "c.csv")), 4)


def _swviz_cells(port_mode: int) -> list:
    from swi3s_studio.model import viz_engine
    with tempfile.TemporaryDirectory() as d:
        return viz_engine.render_payload(_cfg(port_mode).to_csv_file(os.path.join(d, "c.csv")),
                                         4)[0]


def _data(cells: list) -> list:
    return [c for c in cells if c["slot"] == int(GridSlot.DATA)]


def test_register_map_names_2_test0_and_3_test1():
    regs = json.loads((ROOT / "data" / "registers.json").read_text(encoding="utf-8"))
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("name") == "PortMode" and "valid" in node:
                found.append(node["valid"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(regs)
    assert found, "registers.json defines DPn_PortMode"
    for valid in found:
        assert "2=Test0" in valid and "3=Test1" in valid, valid


def test_the_test_mode_example_configs_carry_the_spec_values():
    examples = ROOT / "visualizer_examples" / "directed_tests"
    for name, mode in (("test_mode_zeros.csv", "2"), ("test_mode_ones.csv", "3")):
        rows = [r.split(",") for r in (examples / name).read_text(encoding="utf-8").splitlines()]
        [row] = [r for r in rows if r[0] == "PortMode_REG"]
        assert row[1:3] == [mode, mode], name


def test_swviz_port_mode_values():
    from swi3s_studio.swviz.models.enums import PortMode
    assert PortMode.NORMAL == 0
    assert PortMode.RESERVED == 1
    assert PortMode.TEST0 == 2
    assert PortMode.TEST1 == 3


@pytest.mark.parametrize("mode, bit", [(0, None), (1, None), (2, 0), (3, 1)])
def test_swviz_port_mode_bit(mode, bit):
    from swi3s_studio.swviz.models.enums import port_mode_bit
    assert port_mode_bit(mode) == bit


@pytest.mark.parametrize("port_mode", (2, 3))
def test_native_core_carries_port_mode_on_every_data_cell(port_mode):
    data = _data(_native_cells(port_mode))
    assert len({c["col"] for c in data}) == 2, "the fixture places two channel columns"
    assert {c["port_mode"] for c in data} == {port_mode}
