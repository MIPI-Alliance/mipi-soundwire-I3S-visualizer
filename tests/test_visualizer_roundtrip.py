"""Full-corpus CSV round-trip — every example config survives save -> reload intact.

The upstream visualizer suite round-tripped every config (load CSV -> save CSV ->
reload -> compare JSON) to prove no authoring data is lost across a save/load cycle.
Studio previously had only a couple of targeted BusConfig round-trip cases
(test_config_csv); this restores whole-corpus coverage using the same CSVHandler
save/load path the app uses.

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


def test_all_configs_csv_roundtrip():
    """load -> save -> reload leaves the built bus model byte-for-byte identical, for
    every example config. Catches any authoring field dropped/reordered by the CSV
    writer across the whole corpus, not just the hand-picked cases in test_config_csv."""
    csvs = _all_example_csvs()
    assert len(csvs) >= 89, f"expected the vendored example corpus, found {len(csvs)}"
    fails = []
    with tempfile.TemporaryDirectory() as d:
        for i, csv in enumerate(csvs):
            original = viz_engine.model_json(csv)
            resaved = os.path.join(d, f"{i}.csv")     # index-named: category basenames can collide
            _resave(csv, resaved)
            roundtripped = viz_engine.model_json(resaved)
            if roundtripped != original:
                diff = [k for k in set(original) | set(roundtripped)
                        if original.get(k) != roundtripped.get(k)]
                fails.append(f"{os.path.relpath(csv, _EXAMPLES)}: differing keys {diff}")
    assert not fails, "CSV round-trip changed the bus model:\n  " + "\n  ".join(fails)
