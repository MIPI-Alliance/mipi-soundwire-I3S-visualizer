"""SWI3S Visualizer parity — the Visualizer's REAL testsuite, run against Studio.

Bus-Visualizer mode is driven by the vendored Visualizer engine
(`swi3s_studio.model.viz_engine`), so Studio's bus model must reproduce the
Visualizer's golden output exactly — not just placement, but **bits + bus_clashes +
device_clashes + read_overlaps + warnings**. This is the verification the
Visualizer's own `test/testsuite.py` does (build the model from each config, dump
JSON, compare to `test_json_outputs/`), ported here over the vendored goldens.

Fixtures:
- `visualizer_examples/<category>/<name>.csv` (repo root) — the 89 example configs
  copied verbatim from the original visualizer; the single source of test inputs.
- `tests/visualizer/golden_json/<category>/<name>.json` — the Visualizer's golden
  serialized BusModel for each (vendored from its `test/test_json_outputs/`).

Run: PYTHONPATH=. python3 tests/test_visualizer_engine.py
"""
import glob
import json
import logging
import os

from swi3s_studio.model import viz_engine

# The vendored engine logs clash detections at INFO to stdout; quiet it for tests.
logging.disable(logging.CRITICAL)

_HERE = os.path.dirname(os.path.abspath(__file__))
_GOLD = os.path.join(_HERE, "visualizer", "golden_json")
# Example configs are the repo-root visualizer_examples/ folder (copied verbatim
# from the original visualizer); the goldens stay vendored under tests/visualizer.
_EXAMPLES = os.path.join(os.path.dirname(_HERE), "visualizer_examples")


def _cases():
    for gold in sorted(glob.glob(os.path.join(_GOLD, "**", "*.json"), recursive=True)):
        base = os.path.relpath(gold, _GOLD)[:-5]              # category/name
        csv = os.path.join(_EXAMPLES, base + ".csv")
        if os.path.exists(csv):
            yield base, csv, gold


def test_examples_present():
    cases = list(_cases())
    assert len(cases) == 89, f"expected 89 vendored configs, found {len(cases)}"


def test_bus_model_matches_golden():
    """Studio's bus model (via the vendored engine) == the Visualizer golden for
    every config — bits, clashes, and warnings included."""
    mismatches = []
    for base, csv, gold in _cases():
        want = json.load(open(gold))
        got = viz_engine.model_json(csv)
        if got != want:
            diff = [k for k in set(want) | set(got) if want.get(k) != got.get(k)]
            mismatches.append(f"{base}: differing keys {diff}")
    assert not mismatches, "bus-model mismatches:\n  " + "\n  ".join(mismatches)


if __name__ == "__main__":
    test_examples_present(); print("ok: 89 visualizer example configs + goldens vendored")
    test_bus_model_matches_golden()
    print("ok: Studio bus model matches the Visualizer golden for all 89 configs "
          "(bits + clashes + warnings)")
    print("ALL VISUALIZER ENGINE PARITY TESTS PASSED")
