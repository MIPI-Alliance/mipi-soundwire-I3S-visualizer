"""Config-vs-decoded comparison.

`register_diff` / `register_diff_report` compare the register writes a config expects
against the writes a capture carries; the Compare action uses them."""
from __future__ import annotations

from typing import Dict, List, Tuple


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
    Detail lists each differing register as decoded vs expected (hex), grouped by device,
    capped at `max_lines` lines (headers and separators count, and a device header is written
    only with a register under it); registers past the cap are counted on one extra
    "(+N more)" line, which appears only when there are some."""
    if not diffs:
        return (f"Config matches the decoded registers — "
                f"0 of {n_expected} register(s) differ.", "")
    summary = (f"{len(diffs)} of {n_expected} register(s) differ between the "
               f"expected config and the decoded bus.")
    by_dev: Dict[int, List[dict]] = {}
    for d in diffs:
        by_dev.setdefault(d["device"], []).append(d)
    lines: List[str] = []
    shown = 0
    for dev in sorted(by_dev):
        if len(lines) + 2 > max_lines:
            # No room for a header and one register under it.
            if lines:
                lines.pop()
            lines.append(f"  … (+{len(diffs) - shown} more)")
            return summary, "\n".join(lines)
        lines.append(f"Device {dev}:")
        for d in sorted(by_dev[dev], key=lambda x: x["address"]):
            if len(lines) >= max_lines:
                lines.append(f"  … (+{len(diffs) - shown} more)")
                return summary, "\n".join(lines)
            lines.append(f"  0x{d['address']:04X} {d['label']}: "
                         f"decoded 0x{d['decoded']:02X}  →  expected 0x{d['expected']:02X}")
            shown += 1
        lines.append("")
    return summary, "\n".join(lines).rstrip()
