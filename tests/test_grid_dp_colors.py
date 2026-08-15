"""Bus-Visualizer data-port colours key on the config SLOT, not the DataPortNumber.

The model's rule is that a *device* may not reuse a data-port number, but the same
number across different devices is fine (BusConfig.duplicate_dp_numbers). A smart-amp
config exercises that: four devices each own a DP0 and a DP1, so eight distinct ports
carry only two distinct DataPortNumbers.

The grid used to colour by that number. Every one of those eight ports collapsed onto
two colours and the top key drew two swatches ("DP0", "DP1"), so the rendered colours
did not identify what the key named. Colouring by slot restores the reference
Visualizer's behaviour — there a cell's `dp` *was* the slot index, which is why
`palette[dp % 12]` was right in frame_renderer and wrong once Studio's cells started
carrying the logical number (see model/viz_engine.py).

Run: python3 -m pytest tests/test_grid_dp_colors.py
"""
import os
import tempfile

import pytest

from swi3s_studio.model import viz_engine
from swi3s_studio.model.bus_config import BusConfig


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# (device, dp_number, name) per slot — two numbers shared across four devices, the
# manager included (device -1), which is exactly the shape that used to collapse.
_PORTS = [(-1, 0, "IV"), (0, 0, "SB0"), (1, 0, "SB1"), (2, 0, "TWT"),
          (-1, 1, "DL"), (0, 1, "SB1"), (1, 1, "SB2"), (2, 1, "TWT")]


def _smart_amp_cfg() -> BusConfig:
    """Eight enabled ports sharing two DataPortNumbers across four devices. Offsets are
    staggered so each port owns its own rows and every one of them reaches the grid."""
    cfg = BusConfig(num_columns=15, row_rate_khz=3072.0)
    for slot, (dev, num, name) in enumerate(_PORTS):
        dp = cfg.dataports[slot]
        dp.enabled = True
        dp.device_number = dev
        dp.dp_number = num
        dp.name = name
        dp.port_direction = (slot % 2 == 0)      # alternate sink/source
        dp.enable_ch = 0b1
        dp.sample_size = 15
        dp.interval = 63
        dp.offset = slot * 2
        dp.horizontal_start = 2
        dp.horizontal_count = 7
    return cfg


def _cells(cfg):
    with tempfile.TemporaryDirectory() as d:
        return viz_engine.render_payload(cfg.to_csv_file(os.path.join(d, "c.csv")), 64)[0]


def test_cells_carry_both_the_slot_and_the_logical_number():
    """`dp` stays the logical DataPortNumber (labels, cross-engine identity); `dp_index`
    is the config slot. Both are needed — neither substitutes for the other here."""
    cells = _cells(_smart_amp_cfg())
    data = [c for c in cells if not c["is_cds"] and c["dp_index"] >= 0]
    assert data, "no data cells rendered"
    assert sorted({c["dp"] for c in data}) == [0, 1]                     # two numbers
    assert sorted({c["dp_index"] for c in data}) == list(range(8))       # eight slots
    # Every cell's logical number agrees with its slot's declared number.
    for c in data:
        assert c["dp"] == _PORTS[c["dp_index"]][1]
        assert c["device"] == _PORTS[c["dp_index"]][0]


def test_every_port_gets_its_own_colour_and_swatch(qapp):
    from swi3s_studio.ui.grid_view import GridView

    cfg = _smart_amp_cfg()
    cells = _cells(cfg)
    gv = GridView()
    gv.resize(1500, 400)
    gv.set_bus_model(cells, {}, cfg.column_count(), 64,
                     slot_names={i: p[2] for i, p in enumerate(_PORTS)})

    assert len(gv._stream_colors) == 8, "one colour entry per port, not per DP number"
    names = {c.name() for c in gv._stream_colors.values()}
    assert len(names) == 8, f"ports share a colour: {sorted(names)}"
    # Keyed by slot, so a port's colour does not depend on which number it was given.
    assert sorted(gv._stream_colors) == list(range(8))


def test_colouring_by_dp_number_would_collapse_these_ports(qapp):
    """The mutation this file exists to catch: drop `dp_index` and the fallback keys on
    the logical number again, reproducing the old two-colour render. If this stops
    collapsing, the fixture no longer reproduces the bug and the guard above is inert."""
    from swi3s_studio.ui.grid_view import GridView

    cfg = _smart_amp_cfg()
    cells = [{k: v for k, v in c.items() if k != "dp_index"} for c in _cells(cfg)]
    gv = GridView()
    gv.resize(1500, 400)
    gv.set_bus_model(cells, {}, cfg.column_count(), 64)
    assert len(gv._stream_colors) == 2, "fixture should collapse to 2 without dp_index"


def _scene_texts(gv) -> list:
    """Every rendered label. _text() draws QGraphicsSimpleTextItem (`.text()`), not
    addText's QGraphicsTextItem (`.toPlainText()`) — see GridView._text."""
    return [it.text() for it in gv._scene.items() if hasattr(it, "text")]


def test_key_labels_identify_the_port_not_the_slot(qapp):
    """A swatch is labelled with the port's name; without one it falls back to a
    device-qualified number ("Mgr·DP0", "D1·DP0") — never a bare "DP0", which four of
    these ports would share."""
    from swi3s_studio.ui.grid_view import GridView

    cfg = _smart_amp_cfg()
    cells = _cells(cfg)

    gv = GridView()
    gv.resize(1500, 400)
    gv.set_bus_model(cells, {}, cfg.column_count(), 64,
                     slot_names={i: p[2] for i, p in enumerate(_PORTS)})
    wanted = [p[2] for p in _PORTS]
    labels = [t for t in _scene_texts(gv) if t in set(wanted)]
    # Eight swatches, one per slot — including the two repeated names (SB1, TWT), which
    # are the config's own doing and still get distinct colours.
    assert sorted(labels) == sorted(wanted), f"key labels wrong: {sorted(labels)}"

    # No names supplied -> device-qualified numbers, still one per port.
    gv2 = GridView()
    gv2.resize(1500, 400)
    gv2.set_bus_model(cells, {}, cfg.column_count(), 64)
    texts = set(_scene_texts(gv2))
    for want in ("Mgr·DP0", "D0·DP0", "D1·DP0", "D2·DP0", "Mgr·DP1"):
        assert want in texts, f"missing key label {want} in {sorted(texts)}"


def test_engine_key_swatches_stay_non_clickable(qapp):
    """Colouring by slot means a swatch *could* now name a (device, dp) port, but the
    authoring panel owns label-field editing in this mode — the click target belongs to
    the Analyzer grid alone (see test_grid_label_fields)."""
    from swi3s_studio.ui.grid_view import GridView

    cfg = _smart_amp_cfg()
    gv = GridView()
    gv.resize(1500, 400)
    gv.set_bus_model(_cells(cfg), {}, cfg.column_count(), 64)
    assert gv._key_hits == []
