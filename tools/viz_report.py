#!/usr/bin/env python3
"""Dedicated visualizer test runner + statistics report.

The upstream visualizer suite (`test/testsuite.py`) was a single command that ran the
regression + CSV round-trip over every example config AND emitted a `summary.md` with
aggregate statistics and run-over-run deltas. Studio moved the pass/fail gate to pytest
(test_visualizer_engine / _placement / _roundtrip / _grid_cross_engine), which is
stronger for CI but dropped that human-readable report. This restores it — and goes a
little beyond: it also checks the CSV round-trip and prints per-config metrics.

    python3 tools/viz_report.py                 # run + write tests/visualizer/summary.md
    python3 tools/viz_report.py --check         # also exit non-zero on any regression/round-trip fail

Outputs (both gitignored — regenerate on demand):
    tests/visualizer/summary.md          the report (metrics, slot distribution, per-config)
    tests/visualizer/previous_stats.json last run's aggregates, for the delta column
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from typing import Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from swi3s_studio.model import viz_engine  # noqa: E402
from swi3s_studio.swviz.io.csv_handler import CSVHandler  # noqa: E402
from swi3s_studio.swviz.models.interface import Interface  # noqa: E402
from swi3s_studio.swviz.viz import VizConfig  # noqa: E402

_EXAMPLES = os.path.join(_ROOT, "visualizer_examples")
_GOLD = os.path.join(_ROOT, "tests", "visualizer", "golden_json")
_OUT_DIR = os.path.join(_ROOT, "tests", "visualizer")
_SUMMARY = os.path.join(_OUT_DIR, "summary.md")
_PREV = os.path.join(_OUT_DIR, "previous_stats.json")


def _stats(model: dict) -> dict:
    """Aggregate stats for one built model (mirrors the upstream parse_json_stats)."""
    s = {"num_rows": model.get("num_rows", 0), "num_columns": model.get("num_columns", 0),
         "total_bit_positions": 0, "total_slots": 0,
         "slot_types": defaultdict(int), "direction_counts": defaultdict(int),
         "bus_clashes": len(model.get("bus_clashes", [])),
         "device_clashes": len(model.get("device_clashes", [])),
         "read_overlaps": len(model.get("read_overlaps", [])),
         "clash_positions": model.get("bus_clashes", []) + model.get("device_clashes", []),
         "handover_count": 0}
    bits = model.get("bits", {})
    s["total_bit_positions"] = len(bits)
    for bit in bits.values():
        slots = bit.get("slots", [])
        s["total_slots"] += len(slots)
        for slot in slots:
            st = slot.get("slot", "UNKNOWN")
            s["slot_types"][st] += 1
            if st == "HANDOVER":
                s["handover_count"] += 1
            s["direction_counts"][slot.get("direction", "UNKNOWN")] += 1
    return s


def _delta(cur: int, prev: Optional[int]) -> str:
    if prev is None:
        return ""
    d = cur - prev
    return "0" if d == 0 else (f"+{d:,}" if d > 0 else f"{d:,}")


def _resave(src: str, dst: str) -> None:
    iface, viz = Interface(), VizConfig()
    CSVHandler.load_csv(src, iface, viz)
    CSVHandler.save_csv(dst, iface, viz)


def _run():
    csvs = sorted(glob.glob(os.path.join(_EXAMPLES, "**", "*.csv"), recursive=True))
    results = []          # (name, regression_ok, roundtrip_ok, stats)
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as d:
        for i, csv in enumerate(csvs):
            rel = os.path.relpath(csv, _EXAMPLES)
            name = os.path.splitext(rel)[0]
            model = viz_engine.model_json(csv)
            gold_path = os.path.join(_GOLD, os.path.splitext(rel)[0] + ".json")
            reg_ok = os.path.exists(gold_path) and json.load(open(gold_path)) == model
            resaved = os.path.join(d, f"{i}.csv")
            _resave(csv, resaved)
            rt_ok = viz_engine.model_json(resaved) == model
            results.append((name, reg_ok, rt_ok, _stats(model)))
    return results, time.perf_counter() - t0


def _write_summary(results, elapsed):
    prev = {}
    if os.path.exists(_PREV):
        try:
            prev = json.load(open(_PREV))
        except (OSError, json.JSONDecodeError):
            prev = {}
    total = len(results)
    reg_pass = sum(1 for _, r, _, _ in results if r)
    rt_pass = sum(1 for _, _, rt, _ in results if rt)
    agg = {"bits": 0, "slots": 0, "handovers": 0, "bus": 0, "dev": 0, "ro": 0}
    slot_types, directions = defaultdict(int), defaultdict(int)
    for _, _, _, st in results:
        agg["bits"] += st["total_bit_positions"]; agg["slots"] += st["total_slots"]
        agg["handovers"] += st["handover_count"]
        agg["bus"] += st["bus_clashes"]; agg["dev"] += st["device_clashes"]; agg["ro"] += st["read_overlaps"]
        for k, v in st["slot_types"].items():
            slot_types[k] += v
        for k, v in st["direction_counts"].items():
            directions[k] += v
    pt = prev.get("totals", {})
    ps = prev.get("slot_types", {}); pd = prev.get("directions", {})
    ptime = prev.get("elapsed")
    os.makedirs(_OUT_DIR, exist_ok=True)
    with open(_SUMMARY, "w") as f:
        f.write("# SWI3S Visualizer Test Summary\n\n")
        f.write(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        td = f" ({_delta(int(elapsed), int(ptime))}s)" if ptime is not None else ""
        f.write(f"**Total time:** {elapsed:.1f}s{td}\n\n")
        f.write("## Results\n\n| Metric | Count | Delta |\n|---|---|---|\n")
        f.write(f"| Configs | {total} | {_delta(total, pt.get('configs'))} |\n")
        f.write(f"| Regression pass | {reg_pass} | {_delta(reg_pass, pt.get('reg_pass'))} |\n")
        f.write(f"| Round-trip pass | {rt_pass} | {_delta(rt_pass, pt.get('rt_pass'))} |\n\n")
        f.write("## Aggregate statistics\n\n| Metric | Value | Delta |\n|---|---|---|\n")
        f.write(f"| Total bit positions | {agg['bits']:,} | {_delta(agg['bits'], pt.get('bits'))} |\n")
        f.write(f"| Total slots | {agg['slots']:,} | {_delta(agg['slots'], pt.get('slots'))} |\n")
        f.write(f"| Total handovers | {agg['handovers']:,} | {_delta(agg['handovers'], pt.get('handovers'))} |\n")
        f.write(f"| Bus clashes | {agg['bus']} | {_delta(agg['bus'], pt.get('bus'))} |\n")
        f.write(f"| Device clashes | {agg['dev']} | {_delta(agg['dev'], pt.get('dev'))} |\n")
        f.write(f"| Read overlaps | {agg['ro']} | {_delta(agg['ro'], pt.get('ro'))} |\n\n")
        f.write("## Slot-type distribution\n\n| Slot | Count | % | Delta |\n|---|---|---|---|\n")
        for st in sorted(slot_types):
            c = slot_types[st]; pct = 100 * c / agg["slots"] if agg["slots"] else 0
            f.write(f"| {st} | {c:,} | {pct:.1f}% | {_delta(c, ps.get(st))} |\n")
        f.write("\n## Direction distribution\n\n| Direction | Count | Delta |\n|---|---|---|\n")
        for dr in sorted(directions):
            f.write(f"| {dr} | {directions[dr]:,} | {_delta(directions[dr], pd.get(dr))} |\n")
        fails = [(n, r, rt) for n, r, rt, _ in results if not (r and rt)]
        if fails:
            f.write("\n## Failures\n\n| Config | Regression | Round-trip |\n|---|---|---|\n")
            for n, r, rt in fails:
                f.write(f"| {n} | {'PASS' if r else 'FAIL'} | {'PASS' if rt else 'FAIL'} |\n")
        f.write("\n## Per-config metrics\n\n")
        f.write("| Config | Rows | Cols | Bits | Slots | Handovers | Bus | Dev |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for n, _, _, st in results:
            f.write(f"| {n} | {st['num_rows']} | {st['num_columns']} | {st['total_bit_positions']} "
                    f"| {st['total_slots']} | {st['handover_count']} | {st['bus_clashes']} "
                    f"| {st['device_clashes']} |\n")
        clashers = [(n, st) for n, _, _, st in results if st["bus_clashes"] or st["device_clashes"]]
        if clashers:
            f.write("\n## Configs with clashes (debug)\n\n")
            for n, st in clashers:
                f.write(f"### {n}\n- bus clashes: {st['bus_clashes']}, device clashes: "
                        f"{st['device_clashes']}\n")
                cols = st["num_columns"] or 32
                for pos in st["clash_positions"][:10]:
                    f.write(f"  - (row {pos // cols}, col {pos % cols})\n")
                if len(st["clash_positions"]) > 10:
                    f.write(f"  - ... and {len(st['clash_positions']) - 10} more\n")
    json.dump({"totals": {"configs": total, "reg_pass": reg_pass, "rt_pass": rt_pass, **agg},
               "slot_types": dict(slot_types), "directions": dict(directions), "elapsed": elapsed},
              open(_PREV, "w"), indent=2, sort_keys=True)
    return total, reg_pass, rt_pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the visualizer regression + round-trip and "
                                             "write tests/visualizer/summary.md.")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if any config fails regression or round-trip")
    args = ap.parse_args(argv)
    results, elapsed = _run()
    total, reg_pass, rt_pass = _write_summary(results, elapsed)
    print(f"{total} configs | regression {reg_pass}/{total} | round-trip {rt_pass}/{total} "
          f"| {elapsed:.1f}s")
    print(f"report: {os.path.relpath(_SUMMARY, _ROOT)}")
    ok = (reg_pass == total and rt_pass == total)
    if not ok:
        print("FAILURES:", ", ".join(n for n, r, rt, _ in results if not (r and rt)))
    return 0 if (ok or not args.check) else 1


if __name__ == "__main__":
    raise SystemExit(main())
