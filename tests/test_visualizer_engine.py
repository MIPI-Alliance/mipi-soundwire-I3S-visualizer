"""SWI3S Visualizer parity — the Visualizer's testsuite, run against Studio.

Bus-Visualizer mode is driven by the swviz engine (`swi3s_studio/swviz/`), reached through
the `swi3s_studio.model.viz_engine` adapter, so Studio's bus model must reproduce the
Visualizer's golden output exactly — not just placement, but **bits + bus_clashes +
device_clashes + read_overlaps + warnings**. The test builds the model from each
config, dumps JSON and compares it to the vendored golden, as the Visualizer's own
`test/testsuite.py` does.

Fixtures:
- `visualizer_examples/<category>/<name>.csv` (repo root) — the Visualizer's 96 example
  configs, copied from the Visualizer with the intentional-error configs moved and the quotes
  in the Description field of six spec_figures CSVs doubled; the single source of test inputs.
- `tests/visualizer/golden_json/<category>/<name>.json` — the Visualizer's golden
  serialized BusModel for each (vendored from its `test/test_json_outputs/`).

Run: PYTHONPATH=. python3 -m pytest tests/test_visualizer_engine.py
"""
import glob
import json
import os
import pathlib

import pytest

from swi3s_studio.model import viz_engine

_HERE = os.path.dirname(os.path.abspath(__file__))
_GOLD = os.path.join(_HERE, "visualizer", "golden_json")
# Example configs are the repo-root visualizer_examples/ folder; the goldens stay
# vendored under tests/visualizer.
_EXAMPLES = os.path.join(os.path.dirname(_HERE), "visualizer_examples")


def _cases():
    for gold in sorted(glob.glob(os.path.join(_GOLD, "**", "*.json"), recursive=True)):
        base = os.path.relpath(gold, _GOLD)[:-5]              # category/name
        csv = os.path.join(_EXAMPLES, base + ".csv")
        if os.path.exists(csv):
            yield base, csv, gold


def test_examples_present():
    cases = list(_cases())
    assert len(cases) == 96, f"expected 96 vendored configs, found {len(cases)}"


def test_bus_model_matches_golden():
    """Studio's bus model (via the swviz engine) == the Visualizer golden for
    every config — bits, clashes, and warnings included."""
    mismatches = []
    for base, csv, gold in _cases():
        want = json.loads(pathlib.Path(gold).read_text())
        got = viz_engine.model_json(csv)
        if got != want:
            diff = [k for k in set(want) | set(got) if want.get(k) != got.get(k)]
            mismatches.append(f"{base}: differing keys {diff}")
    assert not mismatches, "bus-model mismatches:\n  " + "\n  ".join(mismatches)


def test_an_enable_ch_register_wider_than_16_bits_builds_and_reports(tmp_path):
    """EnableCh_REG bits above 15 do not make the build raise: the placement counts the 16
    bits the register holds and the port's validation reports the wider value."""
    import csv
    import io

    from swi3s_studio.model.bus_config import demo_config
    rows = list(csv.reader(io.StringIO(demo_config().to_csv())))
    for r in rows:
        if r and r[0] == "EnableCh_REG":
            r[1] = "0b10000000000000011"
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    path = tmp_path / "wide.csv"
    path.write_text(buf.getvalue(), encoding="utf-8", newline="")
    model, _iface, _viz = viz_engine.build_bus_model(str(path))
    issues = {where: res for where, res in model.warnings.validation_issues}
    assert "DP0" in issues and any("EnableCh_REG" in e.message for e in issues["DP0"].errors)
    assert {b.channel for b in model.bits if b.dp == 0 and b.channel is not None} == {0, 1}


def _one_port(columns=15, **regs):
    """An Interface and VizConfig with DP0 enabled on device 0 and a 12-column window."""
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig
    iface, viz = Interface(), VizConfig()
    iface.NumColumns_REG = columns
    cfg = iface.data_ports[0].config
    cfg.EnableCh_REG, cfg.HorizontalStart_REG, cfg.HorizontalCount_REG = 1, 2, 11
    for name, value in regs.items():
        setattr(cfg, name, value)
    viz.data_ports[0].enabled = True
    iface.set_dp_device(0, 0)
    return iface, viz


def _build(iface, viz, rows=4):
    from swi3s_studio.swviz.core.engine import BusModelBuilder
    return BusModelBuilder(iface, rows, viz).build()


def _registers(iface, viz):
    """Every register, scalar setting and port enable a build reads, as plain values."""
    scalars = {k: v for k, v in vars(iface).items() if isinstance(v, (bool, int, float, str))}
    return (scalars,
            [dict(vars(dp.config)) for dp in iface.data_ports],
            [dict(vars(fcp.config)) for fcp in iface.flow_control_ports],
            [dp.enabled for dp in viz.data_ports])


def test_a_build_leaves_the_interface_as_given_and_reports_the_wide_register_each_time():
    """Every build reports the wide EnableCh_REG and leaves every register, setting and
    port enable as it was given."""
    iface, viz = _one_port(EnableCh_REG=0x1FFFF, HorizontalCount_REG=12)
    before = _registers(iface, viz)
    assert before[0] and before[1] and before[2]
    for _ in range(2):
        issues = dict(_build(iface, viz).warnings.validation_issues)
        assert any(e.field == "EnableCh_REG" for e in issues["DP0"].errors)
        assert _registers(iface, viz) == before
    assert iface.data_ports[0].config.EnableCh_REG == 0x1FFFF


def test_a_port_on_device_11_is_drawn_and_not_reported_unplaced():
    iface, viz = _one_port()
    iface.set_dp_device(0, 11)
    model = _build(iface, viz)
    assert any(b.dp == 0 for b in model.bits)
    assert "DP0" not in dict(model.warnings.validation_issues)


@pytest.mark.parametrize("settings,need", [
    # S0 (1) + CDS (1) + S1 (1) + CDS handover (1) + S1 handover (1)
    ({}, 5),
    # Both handovers off leaves S0, the CDS and S1.
    ({"cds_handover_enabled": False, "s1_handover_enabled": False}, 3),
    # A CDS_BitWidth_REG of 2 is a 3-column CDS.
    ({"cds_handover_enabled": False, "s1_handover_enabled": False, "CDS_BitWidth_REG": 2}, 5),
    # The CDS guard, one S1 tail column and one CDS tail column add three.
    ({"CDS_GuardEnabled_REG": True, "tail_width": 1, "CDS_TailWidth_REG": 1}, 8),
    # A wider S0 adds its width.
    ({"cds_handover_enabled": False, "s1_handover_enabled": False, "s0_width": 2}, 4),
])
def test_the_phy3_slot_layout_error_starts_below_the_columns_the_slots_need(settings, need):
    """The error is raised for a frame one column short of `need` and not for `need`."""
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig

    def layout_issue(columns):
        iface = Interface()
        iface.phy3_enabled = True
        for name, value in settings.items():
            setattr(iface, name, value)
        iface.NumColumns_REG = columns - 1
        issues = _build(iface, VizConfig(), rows=2).warnings.validation_issues
        return any(where == "System slot layout" for where, _ in issues)

    assert layout_issue(need) is False
    assert layout_issue(need - 1) is True


def test_a_tx_controlled_port_expects_its_txp_bit_in_every_channel_sample():
    from swi3s_studio.swviz.models import FlowMode
    # One 4-bit sample plus its TxPresent bit is 5 bits; a 4-column window places 4.
    iface, viz = _one_port(FlowMode_REG=FlowMode.TX_CONTROLLED, SampleSize_REG=3,
                           Interval_REG=0, HorizontalCount_REG=3)
    assert _build(iface, viz).warnings.interval_overflow_warnings == [("DP0", 5, 4)]


def test_a_wide_drq_past_the_row_end_is_reported_only_for_a_drawn_port():
    from swi3s_studio.swviz.models import FlowMode

    def truncations(drawn):
        iface, viz = _one_port(FlowMode_REG=FlowMode.RX_CONTROLLED, PortDirection_REG=True)
        fcp = iface.flow_control_ports[0].config
        fcp.FCP_HorizontalStart_REG, fcp.FCP_BitWidth_REG = 15, 2
        viz.data_ports[0].enabled = drawn
        return _build(iface, viz).warnings.drq_truncation_warnings

    assert truncations(True) == [("DP0", 17, 16)]
    assert truncations(False) == []
