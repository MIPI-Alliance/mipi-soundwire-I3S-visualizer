"""Authoring-render characterization golden — locks the Visualizer authoring output
(issue list + clash cells + grid dimensions) across every example config, so the
in-progress placement merge (routing the authoring grid through the C++ placer +
a Python decoration/validation post-pass) can be proven to reproduce today's
behaviour exactly before swviz is retired from the live path.

Cells themselves are already pinned by test_visualizer_placement (C++ placement ==
golden) and test_visualizer_engine (swviz model == golden); this snapshot covers the
part those DON'T — render_payload's issues + clashes (the decoration/validation
layer most at risk in the merge).

Regenerate (only with a reviewed diff — see MEMORY 'golden diff review before edit'):
    PYTHONPATH=. python3 tests/test_authoring_render_golden.py --regen

Run: PYTHONPATH=. python3 -m pytest tests/test_authoring_render_golden.py
"""
import glob
import json
import os
import sys

from swi3s_studio.model import viz_engine

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLES = os.path.join(os.path.dirname(_HERE), "visualizer_examples")
_GOLDEN = os.path.join(_HERE, "visualizer", "authoring_render_golden.json")


def _signature(csv_path: str) -> dict:
    """Normalized render_payload output: grid dims, cell count, sorted clash cells,
    and the sorted issue list (severity, source, message, highlighted cells)."""
    cells, clashes, issues, ncols, nrows = viz_engine.render_payload(csv_path, 32)
    return {
        "ncols": ncols, "nrows": nrows, "ncells": len(cells),
        "clashes": sorted([[list(k), v] for k, v in clashes.items()]),
        "issues": sorted([[i.severity, i.source, i.message,
                           sorted([list(c) for c in i.cells])] for i in issues]),
    }


def _all_configs():
    return sorted(glob.glob(os.path.join(_EXAMPLES, "**", "*.csv"), recursive=True))


def _rel(p: str) -> str:
    """Golden key: relpath with forward slashes, so the golden is portable across OSes
    (os.path.relpath yields backslashes on Windows; the golden is stored posix-style)."""
    return os.path.relpath(p, _EXAMPLES).replace(os.sep, "/")


def _regen() -> None:
    snap = {_rel(p): _signature(p) for p in _all_configs()}
    os.makedirs(os.path.dirname(_GOLDEN), exist_ok=True)
    json.dump(snap, open(_GOLDEN, "w"), indent=1, sort_keys=True)
    print(f"regenerated {_GOLDEN}: {len(snap)} configs")


def test_authoring_render_matches_golden():
    assert os.path.isfile(_GOLDEN), f"missing golden {_GOLDEN} (run --regen)"
    golden = json.load(open(_GOLDEN))
    configs = _all_configs()
    assert configs, f"no example configs under {_EXAMPLES}"
    # Every config still present + its render signature unchanged.
    rel = {_rel(p) for p in configs}
    assert rel == set(golden), (
        f"config set changed: new={sorted(rel - set(golden))[:5]} "
        f"gone={sorted(set(golden) - rel)[:5]}")
    for p in configs:
        key = _rel(p)
        got = _signature(p)
        # Compare field-by-field for a readable failure.
        for field in ("ncols", "nrows", "ncells", "clashes", "issues"):
            assert got[field] == golden[key][field], (
                f"{key}: {field} changed\n got={got[field]}\n exp={golden[key][field]}")


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen()
    else:
        test_authoring_render_matches_golden()
        print("ok: authoring render output matches golden for all example configs")
