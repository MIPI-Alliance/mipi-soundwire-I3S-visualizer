"""Full-corpus CSV round-trip — every example config survives save -> reload intact.

Each config is loaded, saved and reloaded through the CSVHandler save/load path the app
uses. The built bus model JSON, the Interface's registers and per-source lists, each data
port's and Flow Control Port's registers, the description and each data port's name, number,
enable, handover and display fields are compared with the original's. Every example must also
parse as strict CSV.

Run: PYTHONPATH=. python3 -m pytest tests/test_visualizer_roundtrip.py
"""
import glob
import os
import tempfile

from swi3s_studio.model import viz_engine
from swi3s_studio.swviz.io.csv_handler import CSVHandler
from swi3s_studio.swviz.models.interface import Interface
from swi3s_studio.swviz.viz import VizConfig

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLES = os.path.join(os.path.dirname(_HERE), "visualizer_examples")


def _all_example_csvs():
    return sorted(glob.glob(os.path.join(_EXAMPLES, "**", "*.csv"), recursive=True))


def _resave(src_csv: str, dst_csv: str) -> None:
    """Load `src_csv` through the app's CSV handler and write it back to `dst_csv`."""
    iface, viz = Interface(), VizConfig()
    CSVHandler.load_csv(src_csv, iface, viz)
    CSVHandler.save_csv(dst_csv, iface, viz)


def _authoring_fields(csv: str):
    """What a CSV loads into: the Interface's scalars and per-source lists, the description,
    each data port's name, number, enable, handover and label fields and its registers
    (`vars(dp.config)`), and each Flow Control Port's registers (zeroed where its data port's
    FlowMode_REG is 0 or 1, as the registers are then unused)."""
    iface, viz = Interface(), VizConfig()
    CSVHandler.load_csv(csv, iface, viz)
    scalars = {k: v for k, v in vars(iface).items() if k not in ("devices", "flow_control_ports")}
    ports = []
    for dp, drawn in zip(iface.data_ports, viz.data_ports):
        fcp = vars(iface.get_fcp(dp.dp_index).config)
        if dp.config.FlowMode_REG in (0, 1):
            fcp = dict.fromkeys(fcp, 0)
        ports.append((drawn.name, drawn.dp_number, drawn.enabled, drawn.enable_handover,
                      drawn.display_fields, dict(vars(dp.config)), dict(fcp)))
    return scalars, ports


def test_all_configs_csv_roundtrip():
    """load -> save -> reload leaves the built bus model byte-for-byte identical, and every
    register the CSV loads (the Interface's scalars and per-source lists, each data port's and
    Flow Control Port's registers) and the description and data ports' drawing fields equal,
    for every example config. Catches any of them dropped or reordered by the CSV writer."""
    csvs = _all_example_csvs()
    assert len(csvs) == 96, f"expected the example corpus of 96 configs, found {len(csvs)}"
    fails = []
    with tempfile.TemporaryDirectory() as d:
        for i, csv in enumerate(csvs):
            original = viz_engine.model_json(csv)
            resaved = os.path.join(d, f"{i}.csv")     # index-named: category basenames can collide
            _resave(csv, resaved)
            roundtripped = viz_engine.model_json(resaved)
            if _authoring_fields(resaved) != _authoring_fields(csv):
                fails.append(f"{os.path.relpath(csv, _EXAMPLES)}: a loaded register, the description "
                             f"or a data port's drawing field changed")
            if roundtripped != original:
                diff = [k for k in set(original) | set(roundtripped)
                        if original.get(k) != roundtripped.get(k)]
                fails.append(f"{os.path.relpath(csv, _EXAMPLES)}: differing keys {diff}")
    assert not fails, "CSV round-trip changed the bus model:\n  " + "\n  ".join(fails)


def test_every_example_csv_is_well_formed_csv():
    """Quotes inside a quoted field are doubled, so a description is read as written."""
    import csv
    bad = []
    for path in _all_example_csvs():
        try:
            with open(path, encoding="utf-8", newline="") as f:
                list(csv.reader(f, strict=True))
        except csv.Error as exc:
            bad.append(f"{os.path.relpath(path, _EXAMPLES)}: {exc}")
    assert not bad, "\n".join(bad)
