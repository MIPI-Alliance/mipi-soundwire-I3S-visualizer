"""Full-corpus CSV round-trip — every example config survives save -> reload intact.

Each config is loaded, saved and reloaded through the CSVHandler save/load path the app
uses. The built bus model JSON, the description and each data port's name, number, enable,
handover and display fields are compared with the original's. Every example must also
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
    """The fields a CSV carries that the bus model does not: the description and each data
    port's name, number, enable, handover and label fields."""
    iface, viz = Interface(), VizConfig()
    CSVHandler.load_csv(csv, iface, viz)
    return iface.description, [(d.name, d.dp_number, d.enabled, d.enable_handover,
                                d.display_fields) for d in viz.data_ports]


def test_all_configs_csv_roundtrip():
    """load -> save -> reload leaves the built bus model byte-for-byte identical, and the
    description and each data port's drawing fields equal, for every example config. Catches
    any authoring field dropped/reordered by the CSV writer."""
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
                fails.append(f"{os.path.relpath(csv, _EXAMPLES)}: description or "
                             f"data-port drawing fields changed")
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
