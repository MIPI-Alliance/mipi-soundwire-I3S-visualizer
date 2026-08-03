"""Derived measurements over a decoded session (rates, SSP intervals, per-DP
bandwidth) for the Statistics panel.

Output is grouped into ordered, collapsible sections — each led by a
``(title, "", "header")`` row that MeasurementsView renders as a fold header:

    Link Control  →  Regions  →  Commands  →  Data Ports

(Link Control first because it's the bring-up context; then the bus geometry, then
what ran on it, then the audio streams.) Rows within a section are ``(label, value)``
or ``(label, value, kind)`` where kind is 'pass'/'fail' (green/red value) or None.
"""
from __future__ import annotations

from collections import Counter
from typing import List, Tuple

from .errors import count_errors, is_error

Row = Tuple  # (label, value) or (label, value, kind)


def _link_control_rows(session) -> List[Row]:
    """The Link Control section body: the decoded §5.1.2 bring-up sequence, the
    §5.2.3 timing parameters vs their spec limits (PASS/FAIL), and any decoded
    PM_Action link-state writes. Empty when the capture has no observable bring-up
    and no PM actions."""
    rows: List[Row] = []
    lc = getattr(session, "link_control", None)
    seq = getattr(lc, "sequence", "none") if lc is not None else "none"
    if lc is not None and seq != "none":
        phy = getattr(lc, "phy_name", None)
        kind = getattr(lc, "phy_kind", None)
        rows.append(("Sequence", f"{seq.title()} Start"))
        if phy:
            rows.append(("Selected PHY", f"{phy}" + (f" ({kind})" if kind else "")))
        sl = getattr(lc, "safe_lock_columns", None)
        if sl:
            rows.append(("Safe-Lock", f"{sl} columns"))

    # §5.2.3 timing parameters vs their [min, max] limits.
    for t in list(getattr(lc, "timing", []) or []):
        lo, hi = t.get("min_us"), t.get("max_us")
        if lo is not None and hi is not None:
            limit = f"[{lo:g}–{hi:g} µs]"
        elif lo is not None:
            limit = f"[≥ {lo:g} µs]"
        elif hi is not None:
            limit = f"[≤ {hi:g} µs]"
        else:
            limit = "(no limit)"
        ok = t.get("ok")
        status = "PASS" if ok else ("FAIL" if ok is False else "")
        kind = "pass" if ok else ("fail" if ok is False else None)
        val = f"{t['measured_us']:,.1f} µs   {limit}   {status}".rstrip()
        rows.append((t.get("param", ""), val, kind))

    # PM_Action (link state-machine) writes, decoded.
    from .link_events import link_events
    events = link_events(session.commands, session.register_map)
    if events:
        rows.append(("PM actions", str(len(events))))
        for e in events[:8]:
            rows.append((f"  row {e['bus_row']:,}", e["action"]))
    return rows


def _region_rows(session) -> List[Row]:
    """The Regions section body: one block per bus-config geometry (a 'region' is a
    span of constant column count; >1 means the bus reconfigured mid-capture, e.g.
    cold-start 2col → 8col). Lists each region's measured clock/UI/row rate; with a
    single geometry, the headline rates once."""
    rows: List[Row] = []
    # FBCSE is a forwarded-clock DDR link (clock = UI rate / 2, two UIs per clock cycle);
    # DLV has no forwarded clock — its RECOVERED clock runs at the full bit/UI rate. So the
    # "clock" figure is UI/2 for FBCSE, UI for DLV.
    dlv = bool(getattr(session, "is_dlv", False))
    clk_div = 1.0 if dlv else 2.0
    clk_label = "Recovered clock" if dlv else "Clock"
    try:
        stats = session.section_ui_stats() or []
    except Exception:  # noqa: BLE001 - fall back to the whole-capture figures
        stats = []
    if len(stats) > 1:
        for st in stats:
            ui_ns = float(st.get("ui_ns", 0.0))
            ui_hz = (1e9 / ui_ns) if ui_ns > 0 else 0.0
            cols = int(st.get("column_count", 0))
            row_hz = (ui_hz / cols) if cols else 0.0
            rows.append((f"Region {st.get('section', 0)}", f"{cols}col"))
            rows.append(("  clock", f"{ui_hz / clk_div / 1e6:.4f} MHz"))
            rows.append(("  UI rate", f"{ui_hz / 1e6:.4f} MHz"))
            rows.append(("  row rate", f"{row_hz / 1e3:.3f} kHz"))
    else:
        rows.append((clk_label, f"{session.ui_rate_hz / clk_div / 1e6:.4f} MHz"))
        rows.append(("UI rate", f"{session.ui_rate_hz / 1e6:.4f} MHz"))
        rows.append(("Row rate", f"{session.row_rate_khz:.3f} kHz"))
        rows.append(("Columns", str(session.column_count)))

    segs = getattr(session, "segments", []) or []
    if segs:
        counts = sorted({int(s.get("column_count", 0)) for s in segs})
        rows.append(("Bus configs", str(len(segs))))
        rows.append(("  column counts", ", ".join(f"{c}col" for c in counts)))
        if len(segs) > 1:
            rows.append(("  reconfigurations", str(len(segs) - 1)))
    return rows


def _devices_of(cmd: dict) -> List[int]:
    """Peripheral indices a command addressed. A broadcast Ping targets all 12; a
    normal Write/Read targets one. Derived from the device mask so a multicast write
    is counted against every device it actually wrote."""
    mask = int(cmd.get("device_mask", 0) or 0)
    return [d for d in range(12) if mask & (1 << d)]


_PING_STATES = ("PING_ATTACHED", "PING_ATTACHED_BUSY", "PING_ALERT",
                "PING_ALERT_BUSY", "NO_RESPONSE")


def _ping_detail_rows(pings: List[dict]) -> List[Row]:
    """Per-peripheral Ping response breakdown.

    Each Ping carries 12 PingInfo tokens, one per peripheral (Table 67). A token < 0
    means the device never drove the field (the undriven all-ones symbol) = NO_RESPONSE.
    Only devices that ever responded get a row — listing all 12 for a two-device bus
    would bury the signal."""
    from .responses import peripheral_response_name

    per_dev: dict = {}                     # device -> Counter of state names
    for c in pings:
        status = c.get("ping_status")
        if not status:
            continue
        for dev, tok in enumerate(status):
            tok = int(tok)
            name = ("NO_RESPONSE" if tok < 0
                    else peripheral_response_name("GetStatus", tok) or f"RT{tok}")
            per_dev.setdefault(dev, Counter())[name] += 1

    rows: List[Row] = []
    for dev in sorted(per_dev):
        counts = per_dev[dev]
        # A device that ONLY ever read NO_RESPONSE isn't on the bus; don't give it 5 rows.
        if set(counts) == {"NO_RESPONSE"}:
            rows.append((f"    Device {dev}", f"{counts['NO_RESPONSE']:,} NO_RESPONSE",
                         "detail"))
            continue
        rows.append((f"    Device {dev}", f"{sum(counts.values()):,} responses", "detail"))
        for name in _PING_STATES:
            if counts.get(name):
                rows.append((f"      {name}", f"{counts[name]:,}", "detail"))
        # Anything outside the five expected GetStatus states (a protocol/command error
        # token in the PingInfo field) is worth surfacing rather than silently dropping.
        for name in sorted(set(counts) - set(_PING_STATES)):
            rows.append((f"      {name}", f"{counts[name]:,}", "detail", ))
    return rows


def _rw_detail_rows(cmds: List[dict], *, reading: bool) -> List[Row]:
    """Per-device Read/Write breakdown: command count, errors, and byte total.

    Bytes come from the payload actually on the wire — `data` for a Write, `read_data`
    for a Read — so a deferred or failed read contributes 0 bytes rather than its
    requested count."""
    per_dev: dict = {}                     # device -> [count, errors, bytes]
    for c in cmds:
        payload = c.get("read_data") if reading else c.get("data")
        nbytes = len(payload or b"")
        err = 1 if is_error(c) else 0
        for dev in _devices_of(c) or [-1]:
            slot = per_dev.setdefault(dev, [0, 0, 0])
            slot[0] += 1
            slot[1] += err
            slot[2] += nbytes

    verb = "read" if reading else "written"
    rows: List[Row] = []
    for dev in sorted(per_dev):
        n, errs, nbytes = per_dev[dev]
        label = "    (no device)" if dev < 0 else f"    Device {dev}"
        rows.append((label, f"{n:,}", "detail"))
        rows.append((f"      bytes {verb}", f"{nbytes:,}", "detail"))
        if errs:
            rows.append(("      errors", f"{errs:,}", "fail"))
    return rows


def _command_detail_rows(kind: str, cmds: List[dict]) -> List[Row]:
    """The expandable breakdown for one command kind (empty = nothing to expand)."""
    if kind == "Ping":
        return _ping_detail_rows(cmds)
    phase = (cmds[0].get("phase") or "") if cmds else ""
    if "Read" in kind or phase in ("ReadSetup", "ReadData"):
        return _rw_detail_rows(cmds, reading=True)
    if "Write" in kind or phase == "Write":
        return _rw_detail_rows(cmds, reading=False)
    return []


def _command_rows(session) -> List[Row]:
    """The Commands section body: total + error counts, the per-kind breakdown, Ping
    cadence (in bus rows), and the Stream-Sync-Point count.

    Each per-kind row is a collapsible GROUP: clicking it reveals the detail rows —
    per-peripheral response states for Ping, per-device counts / errors / byte totals
    for Reads and Writes."""
    rows: List[Row] = []
    rows.append(("Count", str(len(session.commands))))
    rows.append(("Errors", str(count_errors(session.commands))))

    by_kind: dict = {}
    for c in session.commands:
        k = c.get("command", "")
        if k:
            by_kind.setdefault(k, []).append(c)
    for kind in sorted(by_kind):
        cmds = by_kind[kind]
        detail = _command_detail_rows(kind, cmds)
        # Only make it a fold when there is something behind it; otherwise a plain row
        # (an arrow that expands to nothing is worse than no arrow).
        rows.append((f"  {kind}", str(len(cmds)), "group" if detail else None))
        rows.extend(detail)

    # Ping cadence in bus rows: the manager pings on a schedule, so the row gap between
    # consecutive Pings is near-constant. Min/max/mean expose jitter or a schedule change
    # (rows, not time, so it's independent of the per-region clock rate).
    ping_rows = [int(c.get("bus_row", 0)) for c in session.commands if c.get("command") == "Ping"]
    periods = [b - a for a, b in zip(ping_rows, ping_rows[1:]) if b - a > 0]
    if periods:
        mean = sum(periods) / len(periods)
        rows.append(("Ping period", f"{len(ping_rows):,} pings"))
        rows.append(("  min", f"{min(periods):,} rows"))
        rows.append(("  max", f"{max(periods):,} rows"))
        rows.append(("  mean", f"{mean:,.1f} rows"))

    ssp_all = [c["start_sample"] for c in session.commands if c.get("has_sync_point")]
    rows.append(("SSP count", str(len(ssp_all))))
    return rows


def _data_port_rows(session, store) -> List[Row]:
    """The Data Ports section body: per-(device, dataport) channel count, on-bus
    sample rate / size, bandwidth, and average SSP interval."""
    rows: List[Row] = []
    sr = session.sample_rate_hz or 1
    ssp_by_dev = _ssp_samples_by_device(session.commands)
    store = store if store is not None else session.audio_store()
    for dev, dp in store.streams():
        chs = store.channels(dev, dp)
        rate = store.native_rate(dev, dp)        # on-bus rate, not the 48 kHz playback decimation
        bits = max((store.native_sample_bits(dev, dp, ch) for ch in chs), default=0)
        bw = rate * len(chs) * bits              # true DP (on-bus) bandwidth
        tag = f"Dev{dev} DP{dp}"
        rows.append((f"{tag} channels", str(len(chs))))
        rows.append((f"{tag} sample rate", f"{rate / 1000:.2f} kHz"))
        rows.append((f"{tag} sample size", f"{bits} bits"))
        rows.append((f"{tag} bandwidth", f"{bw / 1000:.1f} kbit/s"))
        samples = ssp_by_dev.get(dev, [])
        if len(samples) >= 2:
            diffs = [samples[i + 1] - samples[i] for i in range(len(samples) - 1)]
            avg = sum(diffs) / len(diffs)
            rows.append((f"{tag} avg SSP interval", f"{avg / sr * 1e6:,.2f} µs"))
    return rows


def capture_measurements(session, store=None) -> List[Row]:
    """The full Statistics table: four ordered, collapsible sections (Link Control,
    Regions, Commands, Data Ports), each led by a ``(title, "", "header")`` row. Pass
    the already-built AudioStore as `store` to avoid rebuilding it here — rebuilding a
    multi-million-sample store on the GUI thread froze the UI at load."""
    rows: List[Row] = []

    def section(title: str, body: List[Row]) -> None:
        if body:
            rows.append((title, "", "header"))
            rows.extend(body)

    section("Link Control", _link_control_rows(session))
    section("Regions", _region_rows(session))
    section("Commands", _command_rows(session))
    section("Data Ports", _data_port_rows(session, store))
    return rows


def _ssp_samples_by_device(commands) -> "dict[int, List[int]]":
    """Sync-point command start samples grouped by each targeted device. DPs on
    the same device share the bus SSP cadence; different devices can differ."""
    by_dev: dict = {}
    for c in commands:
        if not c.get("has_sync_point"):
            continue
        mask = c.get("device_mask", 0)
        for dev in range(12):
            if mask & (1 << dev):
                by_dev.setdefault(dev, []).append(c["start_sample"])
    return by_dev
