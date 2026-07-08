"""Config-vs-decoded comparison: diff an expected grid (from a CSV config)
against the decoded grid, cell by cell, and summarise the differences."""
from __future__ import annotations

from typing import Dict, List, Tuple

import swi3score


def grid_diff(decoded: List[dict], expected: List[dict]) -> Tuple[List[dict], int]:
    """Annotate the decoded grid with how each cell compares to `expected`.

    Returns (cells, n_diff). Each cell gets a ``diff`` key:
      - "same"          : decoded and expected agree (slot + channel)
      - "changed"       : both present but differ
      - "decoded_only"  : only the decoded grid has audio here
      - "expected_only" : only the expected config places audio here
    CDS (Column 0) cells are always "same" and never counted. "changed" and
    "expected_only" cells also carry exp_slot / exp_channel / exp_dp so the
    report can show what was expected.

    The grid is multi-emit (a slot can carry a source *and* a sink transport), so
    cells are keyed by (row, col, is_source): a source bit and the sink reading it
    are compared independently.
    """
    dec = {(c["row"], c["col"], bool(c.get("is_source"))): c for c in decoded}
    exp = {(c["row"], c["col"], bool(c.get("is_source"))): c for c in expected}
    out: List[dict] = []
    n_diff = 0
    for key in sorted(set(dec) | set(exp)):
        d = dec.get(key)
        e = exp.get(key)
        cell = dict(d or e)
        if cell.get("is_cds"):
            cell["diff"] = "same"
        elif d and e:
            same = (d["slot"] == e["slot"] and d["channel"] == e["channel"])
            cell["diff"] = "same" if same else "changed"
            if not same:
                cell["exp_slot"], cell["exp_channel"], cell["exp_dp"] = (
                    e["slot"], e["channel"], e["dp"])
        elif d:
            cell["diff"] = "decoded_only"
        else:
            cell["diff"] = "expected_only"
            cell["exp_slot"], cell["exp_channel"], cell["exp_dp"] = (
                e["slot"], e["channel"], e["dp"])
        if cell["diff"] != "same":
            n_diff += 1
        out.append(cell)
    return out, n_diff


def _slot(slot: int) -> str:
    names = swi3score.SLOT_NAMES
    return names[slot] if 0 <= slot < len(names) else str(slot)


def _cell_decoded(cell: dict) -> str:
    return f"DP{cell['dp']} ch{cell['channel']} {_slot(cell['slot'])}"


def _cell_expected(cell: dict) -> str:
    return f"DP{cell.get('exp_dp')} ch{cell.get('exp_channel')} {_slot(cell.get('exp_slot', 0))}"


def grid_diff_report(cells: List[dict], max_lines: int = 60) -> Tuple[str, str]:
    """(summary, detail) text for a diffed grid. Summary is a one-liner with the
    per-category counts; detail lists each differing cell as decoded vs expected,
    grouped by category and capped at `max_lines`."""
    cats = {"changed": [], "decoded_only": [], "expected_only": []}
    for c in cells:
        d = c.get("diff")
        if d in cats:
            cats[d].append(c)
    total = sum(len(v) for v in cats.values())
    if total == 0:
        return "Config matches the decoded bus — 0 differing cells.", ""
    summary = (f"{total} differing grid cell(s): "
               f"{len(cats['changed'])} changed, "
               f"{len(cats['decoded_only'])} only-on-bus, "
               f"{len(cats['expected_only'])} only-in-config.")
    lines: List[str] = []
    headers = {"changed": "Changed (bus → config):",
               "decoded_only": "Only on the decoded bus:",
               "expected_only": "Only in the expected config:"}
    for cat in ("changed", "decoded_only", "expected_only"):
        group = cats[cat]
        if not group:
            continue
        lines.append(headers[cat])
        for c in group:
            loc = f"  row {c['row']:>3}, col {c['col']:>2}: "
            if cat == "changed":
                lines.append(loc + f"{_cell_decoded(c)}  →  {_cell_expected(c)}")
            elif cat == "decoded_only":
                lines.append(loc + _cell_decoded(c))
            else:
                lines.append(loc + _cell_expected(c))
            if len(lines) >= max_lines:
                lines.append(f"  … (+{total - sum(1 for x in lines if x.startswith('  row'))} more)")
                return summary, "\n".join(lines)
        lines.append("")
    return summary, "\n".join(lines).rstrip()


def register_diff(expected_writes, baseline_writes, rmap) -> List[dict]:
    """Compare expected register writes (device, address, value) against a baseline
    write set — the decoder's EFFECTIVE config (so cold-start-preloaded configs the
    bus never re-wrote still compare correctly). Returns differing registers with
    device, address, label, decoded value, expected value. A baseline address that
    is absent falls back to the spec reset value."""
    base = {(int(d), int(a)): int(v) for d, a, v in baseline_writes}
    diffs: List[dict] = []
    for dev, addr, exp in expected_writes:
        cur = base.get((int(dev), int(addr)))
        if cur is None:
            res = rmap.resolve(addr)
            cur = res.register.reset_byte() if res else 0
        if cur != exp:
            res = rmap.resolve(addr)
            label = res.register.name if res else f"0x{addr:04X}"
            diffs.append({"device": int(dev), "address": int(addr), "label": label,
                          "decoded": cur, "expected": int(exp)})
    return diffs


def register_diff_report(diffs: List[dict], n_expected: int,
                         max_lines: int = 80) -> Tuple[str, str]:
    """(summary, detail) text for a register-map comparison. `diffs` is the output
    of register_diff(); `n_expected` is how many expected registers were checked.
    Detail lists each differing register as decoded vs expected (hex), capped at
    `max_lines`."""
    if not diffs:
        return (f"Config matches the decoded registers — "
                f"0 of {n_expected} register(s) differ.", "")
    summary = (f"{len(diffs)} of {n_expected} register(s) differ between the "
               f"expected config and the decoded bus.")
    by_dev: Dict[int, List[dict]] = {}
    for d in diffs:
        by_dev.setdefault(d["device"], []).append(d)
    lines: List[str] = []
    for dev in sorted(by_dev):
        lines.append(f"Device {dev}:")
        for d in sorted(by_dev[dev], key=lambda x: x["address"]):
            lines.append(f"  0x{d['address']:04X} {d['label']}: "
                         f"decoded 0x{d['decoded']:02X}  →  expected 0x{d['expected']:02X}")
            if len(lines) >= max_lines:
                lines.append(f"  … (+{len(diffs) - sum(1 for x in lines if x.startswith('  0x'))} more)")
                return summary, "\n".join(lines)
        lines.append("")
    return summary, "\n".join(lines).rstrip()
