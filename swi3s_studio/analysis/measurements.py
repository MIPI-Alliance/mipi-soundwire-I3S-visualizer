"""Derived measurements over a decoded session (rates, SSP intervals, per-DP
bandwidth) for the Statistics panel.

Output is grouped into ordered, collapsible sections — each led by a
``(title, "", "header")`` row that MeasurementsView renders as a fold header:

    Link Control  →  Regions  →  Commands  →  Devices  →  Peripheral Reports  →  Data Ports

Rows within a section are ``(label, value)``
or ``(label, value, kind)``. `kind` is:

    'header'  a section's fold header (``value`` is "")
    'group'   a second fold level inside a section; the 'detail' rows after it fold with it
    'detail'  a breakdown row under the nearest 'group'
    'pass' / 'fail'  green / red value (a 'fail' row may also be a group's child)
    None      a plain row
"""
from __future__ import annotations

from collections import Counter
from typing import List, Tuple

from .errors import count_errors, is_error


def ssp_takes_effect(c) -> bool:
    """True for a sync-point command the bus acts on: CRC-valid, with no Row_Delay the
    decode rejected, and, for an SSCR, a commit that takes effect
    (`model.registers.commit_takes_effect`)."""
    if not (c.get("has_sync_point") and c.get("crc_valid")
            and not c.get("row_delay_error")):
        return False
    if c.get("is_commit"):
        from ..model.registers import commit_takes_effect
        return commit_takes_effect(c)
    return True

Row = Tuple  # (label, value) or (label, value, kind)


def _bringup_rows(lc, indent: str = "") -> List[Row]:
    """The rows of one link bring-up: its sequence, selected PHY, Safe-Lock width, and
    the §5.2.3 timing parameters vs their [min, max] limits (PASS/FAIL). `indent`
    prefixes every label."""
    rows: List[Row] = []
    seq = getattr(lc, "sequence", "none") if lc is not None else "none"
    if lc is not None and seq != "none":
        phy = getattr(lc, "phy_name", None)
        kind = getattr(lc, "phy_kind", None)
        rows.append((f"{indent}Sequence", f"{seq.title()} Start"))
        if phy:
            rows.append((f"{indent}Selected PHY", f"{phy}" + (f" ({kind})" if kind else "")))
        sl = getattr(lc, "safe_lock_columns", None)
        if sl:
            rows.append((f"{indent}Safe-Lock", f"{sl} columns"))

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
        rows.append((f"{indent}{t.get('param', '')}", val, kind))
    return rows


def _link_control_rows(session) -> List[Row]:
    """The Link Control section body: the decoded §5.1.2 bring-up sequence, the
    §5.2.3 timing parameters vs their spec limits (PASS/FAIL), and any decoded
    PM_Action link-state writes. A capture whose Link is brought up again lists each
    later bring-up as its own indented block, led by a "Bring-up N" row naming its
    sequence and the time its strip begins; the numbering starts at 1 when the first
    bring-up lists no rows (a capture that starts in audio mode). Empty when the capture
    has no observable bring-up and no PM actions."""
    lc = getattr(session, "link_control", None)
    rows: List[Row] = _bringup_rows(lc)
    rate = float(getattr(getattr(session, "capture", None), "sample_rate_hz", 0) or 0)
    later = list(getattr(session, "link_controls", None) or [])[1:]
    for n, r in enumerate(later, start=2 if rows else 1):
        sections = getattr(r, "sections", None) or []
        start = sections[0]["start"] if sections else (r.audio_start_sample or 0)
        when = f" at {start / rate * 1e3:,.3f} ms" if rate > 0 else ""
        rows.append((f"Bring-up {n}", f"{r.sequence.title()} Start{when}"))
        rows.extend(_bringup_rows(r, "  "))

    # PM_Action (link state-machine) writes, decoded.
    from .link_events import link_events
    events = link_events(session.commands, session.register_map)
    if events:
        rows.append(("PM actions", str(len(events))))
        for e in events[:8]:
            rows.append((f"  row {e['bus_row']:,}", e["action"]))
    return rows


def _region_head(session, section: int, cols: int) -> Row:
    """A region block's heading row: "Region n", counted from 1, and the region's name: the
    user's, followed by its column count, else the count numbered as Session.region_label
    numbers it."""
    name = (getattr(session, "region_names", None) or {}).get(section)
    if name:
        return (f"Region {section + 1}", f"{name} · {cols}-col")
    numbers = session.region_numbers() if hasattr(session, "region_numbers") else []
    k = numbers[section] if 0 <= section < len(numbers) else None
    return (f"Region {section + 1}", f"{cols}-col" if k is None else f"{cols}-col {k}")


def _region_rows(session) -> List[Row]:
    """The Regions section body: the bus-config summary, then one block per bus-config
    geometry (a 'region' is a span of constant column count; >1 means the bus
    reconfigured mid-capture, e.g. cold-start 2col → 8col). Lists each region's measured
    clock/UI/row rate; with a single geometry, the headline rates once."""
    rows: List[Row] = []
    segs = getattr(session, "segments", []) or []
    if segs:
        counts = sorted({int(s.get("column_count", 0)) for s in segs})
        rows.append(("Bus configs", str(len(segs))))
        rows.append(("  column counts", ", ".join(f"{c}col" for c in counts)))
        if len(segs) > 1:
            rows.append(("  reconfigurations", str(len(segs) - 1)))

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
            rows.append(_region_head(session, int(st.get("section", 0)), cols))
            rows.append(("  clock", f"{ui_hz / clk_div / 1e6:.4f} MHz"))
            rows.append(("  UI rate", f"{ui_hz / 1e6:.4f} MHz"))
            rows.append(("  row rate", f"{row_hz / 1e3:.3f} kHz"))
    else:
        rows.append((clk_label, f"{session.ui_rate_hz / clk_div / 1e6:.4f} MHz"))
        rows.append(("UI rate", f"{session.ui_rate_hz / 1e6:.4f} MHz"))
        rows.append(("Row rate", f"{session.row_rate_khz:.3f} kHz"))
        rows.append(("Columns", str(session.column_count)))
    return rows


def _devices_of(cmd: dict) -> List[int]:
    """Peripheral indices a command addressed. A broadcast Ping targets all 12; a
    normal Write/Read targets one. Derived from the device mask so a multicast write
    is counted against every device it actually wrote."""
    mask = int(cmd.get("device_mask", 0) or 0)
    return [d for d in range(12) if mask & (1 << d)]


def devices_on_bus(commands) -> List[int]:
    """The peripherals that drove their PingInfo token in any Ping of `commands`, sorted:
    the devices that answer on the Link's bus."""
    seen = set()
    for c in commands:
        for dev, tok in enumerate(c.get("ping_status") or ()):
            if int(tok) >= 0:
                seen.add(dev)
    return sorted(seen)


_PING_STATES = ("PING_ATTACHED", "PING_ATTACHED_BUSY", "PING_ALERT",
                "PING_ALERT_BUSY", "NO_RESPONSE")


def _ping_detail_rows(pings: List[dict]) -> List[Row]:
    """Per-peripheral Ping response breakdown.

    Each Ping carries 12 PingInfo tokens, one per peripheral (the "Assignment of Robust
    Token Numbers (Peripheral)" table). A token < 0 means the device never drove the field
    (the undriven all-ones symbol) = NO_RESPONSE. A device that only ever read NO_RESPONSE
    gets a single row."""
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
        # A device that ONLY ever read NO_RESPONSE is not on the bus: one row, no breakdown.
        if set(counts) == {"NO_RESPONSE"}:
            rows.append((f"    Device {dev}", f"{counts['NO_RESPONSE']:,} NO_RESPONSE",
                         "detail"))
            continue
        rows.append((f"    Device {dev}", f"{sum(counts.values()):,} responses", "detail"))
        for name in _PING_STATES:
            if counts.get(name):
                rows.append((f"      {name}", f"{counts[name]:,}", "detail"))
        # Tokens outside the five expected GetStatus states (a protocol/command error
        # token in the PingInfo field) are listed too.
        for name in sorted(set(counts) - set(_PING_STATES)):
            rows.append((f"      {name}", f"{counts[name]:,}", "detail", ))
    return rows


def _rw_detail_rows(cmds: List[dict], *, reading: bool) -> List[Row]:
    """Per-device Read/Write breakdown: command count, errors, and byte total.

    Bytes come from the payload actually on the wire — `data` for a Write, `read_data`
    for a Read — so a failed read, or a deferred read whose delivery is missing,
    contributes 0 bytes rather than its requested count. A delivered deferred read is
    folded into its setup first (_fold_deferred_reads), so its bytes count on the read
    that requested them."""
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


_RESP_REMOTE_READ_DEFERRED = 5      # token 5 on a Read phase (analysis/responses.py)


def _fold_deferred_reads(cmds: List[dict]) -> List[dict]:
    """One logical read, one entry — even when the wire carried it as two phases.

    A DEFERRED read is two command records: the ReadSetup answered REMOTE_READ_DEFERRED
    carrying no data, then a later ReadData carrying the payload and inheriting the setup's
    address and byte count. An IMMEDIATE read is a single record and is untouched here.

    The delivery is folded back into its setup, contributing its payload, its CRC verdict
    and its has-data flag so the byte totals and the reply-CRC count land on the read that
    requested them. A ReadData that carries NO data (a failed or repeated-deferred answer)
    is not the delivery: it stays a record of its own, so its error still counts, and the
    setup keeps waiting for the ReadData that does deliver. Input dicts are never mutated.

    An ORPHAN delivery — a capture that starts after the setup, so nothing to fold into —
    stays a record of its own.
    """
    out: List[dict] = []
    awaiting: dict = {}                     # device tuple -> index in `out` of its setup
    for c in cmds:
        name = str(c.get("command", ""))
        devs = tuple(_devices_of(c) or [-1])
        if name == "ReadData" and c.get("has_read_data"):
            i = awaiting.pop(devs, None)
            if i is not None:
                merged = dict(out[i])
                merged["has_read_data"] = c.get("has_read_data")
                merged["read_data"] = c.get("read_data")
                merged["read_data_crc_valid"] = c.get("read_data_crc_valid")
                out[i] = merged
                continue                    # not a command in its own right
        elif name == "ReadA32" and c.get("peripheral_response") == _RESP_REMOTE_READ_DEFERRED:
            # Keyed on the DEFERRED response, not on "has no data": a READ_FAILED setup also
            # carries none, and must not swallow an unrelated later delivery.
            awaiting[devs] = len(out)
        out.append(c)
    return out


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
    # Grouped on LOGICAL command identity, so a deferred read is one entry
    # (_fold_deferred_reads). Count/Errors above count the raw wire records.
    for c in _fold_deferred_reads(session.commands):
        k = c.get("command", "")
        if k:
            by_kind.setdefault(k, []).append(c)
    for kind in sorted(by_kind):
        cmds = by_kind[kind]
        detail = _command_detail_rows(kind, cmds)
        # Only make it a fold when there is something behind it; otherwise a plain row.
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

    ssp_all = [c["start_sample"] for c in session.commands if ssp_takes_effect(c)]
    rows.append(("SSP count", str(len(ssp_all))))
    return rows


def _data_port_rows(session, store) -> List[Row]:
    """The Data Ports section body: per-(device, dataport) channel count, on-bus
    sample rate / size, bandwidth, and average SSP interval. A row is tagged
    `Dev<n> DP<p>`, or `Manager DP<p>` for a Manager data port (device -1)."""
    rows: List[Row] = []
    sr = session.sample_rate_hz or 1
    ssp_by_dev = _ssp_samples_by_device(session.commands)
    store = store if store is not None else session.audio_store()
    for dev, dp in store.streams():
        chs = store.channels(dev, dp)
        rate = store.native_rate(dev, dp)        # on-bus rate, not the 48 kHz playback decimation
        bits = max((store.native_sample_bits(dev, dp, ch) for ch in chs), default=0)
        bw = rate * len(chs) * bits              # true DP (on-bus) bandwidth
        tag = f"{'Manager' if dev < 0 else f'Dev{dev}'} DP{dp}"   # as Devices names it
        rows.append((f"{tag} channels", str(len(chs))))
        rows.append((f"{tag} sample rate", f"{rate / 1000:.2f} kHz"))
        mixed = [r for r in (store.native_size_range(dev, dp, ch) for ch in chs) if r]
        if mixed:
            # A rate and a size of one run each: their product is no run's bandwidth.
            lo, hi = min(r[0] for r in mixed), max(r[1] for r in mixed)
            rows.append((f"{tag} sample size", f"{lo} to {hi} bits (changes between runs)"))
            rows.append((f"{tag} bandwidth", "not measured (the sample size changes "
                                             "between runs)"))
        else:
            rows.append((f"{tag} sample size", f"{bits} bits"))
            rows.append((f"{tag} bandwidth", f"{bw / 1000:.1f} kbit/s"))
        samples = ssp_by_dev.get(dev, [])
        if len(samples) >= 2:
            diffs = [samples[i + 1] - samples[i] for i in range(len(samples) - 1)]
            avg = sum(diffs) / len(diffs)
            rows.append((f"{tag} avg SSP interval", f"{avg / sr * 1e6:,.2f} µs"))
    return rows


def _peripheral_report_rows(session) -> List[Row]:
    """The Peripheral Reports section body: what each device said about ITSELF.

    Every ``IntStat_*`` / ``DevStat_*`` bit and ``EC_*`` counter the Manager read back,
    decoded from the replies (see :mod:`.port_status`). Faults and ImpDef/SDCA
    interrupts each get a top-level, always-visible row; the live state folds away per
    port."""
    from .port_status import summarize

    s = summarize(session.commands)
    if not s.reads:
        return []
    rows: List[Row] = [("status registers read", f"{s.reads:,}")]
    rows.append(("faults reported", f"{len(s.faults)}" if s.faults else "none",
                 "fail" if s.faults else "pass"))
    for (port, name), (count, first_row, value) in sorted(s.faults.items()):
        detail = f"{value}" if name.startswith(("EC_", "COUNT_")) else f"{count}×"
        rows.append((f"  {port} {name}", f"{detail}, from row {first_row:,}", "fail"))
    if s.vendor:
        # Name whichever of ImpDef / SDCA (the spec's terms) actually fired; SDCA fields
        # share the vendor bucket.
        kinds = sorted({("SDCA" if "SDCA" in name else "ImpDef") for _p, name in s.vendor})
        rows.append((f"{' / '.join(kinds)} interrupts", str(len(s.vendor))))
        for (port, name), (count, first_row, _v) in sorted(s.vendor.items()):
            # Uncoloured: the spec assigns these fields no meaning.
            rows.append((f"  {port} {name}", f"{count}× at row {first_row:,}"))
    for port in sorted(s.state):
        live = s.state[port]
        events = {n: v for (p, n), v in s.events.items() if p == port}
        if not live and not events:
            continue
        rows.append((f"{port} reported state", f"{s.per_port.get(port, 0):,} reads", "group"))
        # Latched lifecycle events (RW1C) get a count of the polls that found them set;
        # live RO fields get their latest reading, which is all such a field means.
        for name in sorted(events):
            rows.append((f"    {name}", f"{events[name][0]}×", "detail"))
        for name in sorted(live):
            rows.append((f"    {name}", str(live[name]), "detail"))
    return rows


def _device_rows(session, store) -> List[Row]:
    """The Devices section body: one fold per peripheral that answers the Link's Pings, is
    addressed by a command or carries a data port, led by "Device n" with its name and its
    command count, then a Manager fold for the Manager's own data ports.

    A device's fold holds its Ping responses (and alerts), the commands addressed to it by
    kind (a multicast command counts on each device it addresses; a deferred read is one
    command), the bytes read and written, its errors by kind, its reply-CRC failures and
    reported faults, the SSPs that targeted it and its data ports."""
    from .errors import command_error, ping_errors
    from .port_status import fault_label, reports_for
    from .responses import peripheral_response_name

    commands = list(session.commands)
    names = getattr(session, "device_names", None) or {}
    store = store if store is not None else session.audio_store()
    ports: dict = {}
    for dev, dp in store.streams():
        ports.setdefault(int(dev), []).append(int(dp))

    pings: dict = {}                        # device -> [responses, alerts]
    for c in commands:
        for dev, tok in enumerate(c.get("ping_status") or ()):
            if int(tok) < 0:
                continue
            slot = pings.setdefault(dev, [0, 0])
            slot[0] += 1
            if "ALERT" in str(peripheral_response_name("GetStatus", int(tok)) or ""):
                slot[1] += 1

    per: dict = {}                          # device -> its counters
    def of(dev):
        return per.setdefault(dev, {"kinds": Counter(), "errors": Counter(), "read": 0,
                                    "written": 0, "crc": 0, "faults": 0, "ssp": 0})
    for c in _fold_deferred_reads(commands):
        if c.get("command") == "Ping":
            continue
        devs = _devices_of(c)
        msg = command_error(c)
        kind = ("reported fault or interrupt" if msg is not None and msg == fault_label(c)
                else msg)
        for dev in devs:
            d = of(dev)
            d["kinds"][str(c.get("command", "") or "?")] += 1
            d["read"] += len(c.get("read_data") or b"")
            d["written"] += len(c.get("data") or b"")
            if kind is not None:
                d["errors"][kind] += 1
            if c.get("has_read_data") and c.get("read_data_crc_valid") is False:
                d["crc"] += 1
            if ssp_takes_effect(c):
                d["ssp"] += 1
        for r in reports_for(c):
            if r.faults:
                of(r.device)["faults"] += len(r.faults)
    for c in commands:                      # a Ping's error is the answering device's
        if c.get("command") == "Ping":
            for dev, label in ping_errors(c):
                of(dev)["errors"][f"Ping {label}"] += 1

    rows: List[Row] = []
    for dev in sorted(set(pings) | set(per) | {d for d in ports if d >= 0}):
        d = of(dev)
        n = sum(d["kinds"].values())
        name = names.get(dev)
        head = f"{n:,} commands" + (f" · {name}" if name else "")
        rows.append((f"Device {dev}", head, "group"))
        if dev in pings:
            rows.append(("    Ping responses", f"{pings[dev][0]:,}", "detail"))
            if pings[dev][1]:
                rows.append(("      alerts", f"{pings[dev][1]:,}", "detail"))
        for kind in sorted(d["kinds"]):
            rows.append((f"    {kind}", f"{d['kinds'][kind]:,}", "detail"))
        if d["read"]:
            rows.append(("    bytes read", f"{d['read']:,}", "detail"))
        if d["written"]:
            rows.append(("    bytes written", f"{d['written']:,}", "detail"))
        errs = sum(d["errors"].values())
        if errs:
            rows.append(("    errors", f"{errs:,}", "fail"))
            for kind in sorted(d["errors"]):
                rows.append((f"      {kind}", f"{d['errors'][kind]:,}", "fail"))
        if d["crc"]:
            rows.append(("    reply CRC failures", f"{d['crc']:,}", "fail"))
        if d["faults"]:
            rows.append(("    faults reported", f"{d['faults']:,}", "fail"))
        if d["ssp"]:
            rows.append(("    SSPs", f"{d['ssp']:,}", "detail"))
        if ports.get(dev):
            rows.append(("    data ports", ", ".join(f"DP{p}" for p in sorted(ports[dev])),
                         "detail"))
    manager = sorted(ports.get(-1, []))
    if manager:
        rows.append(("Manager", f"{len(manager)} data port" + ("s" if len(manager) > 1 else ""),
                     "group"))
        rows.append(("    data ports", ", ".join(f"DP{p}" for p in manager), "detail"))
    return rows


def capture_measurements(session, store=None) -> List[Row]:
    """The full Statistics table: six ordered, collapsible sections (Link Control,
    Regions, Commands, Devices, Peripheral Reports, Data Ports), each led by a
    ``(title, "", "header")`` row. Pass the already-built AudioStore as `store` to avoid
    rebuilding it here, on the GUI thread."""
    rows: List[Row] = []

    def section(title: str, body: List[Row]) -> None:
        if body:
            rows.append((title, "", "header"))
            rows.extend(body)

    section("Link Control", _link_control_rows(session))
    section("Regions", _region_rows(session))
    section("Commands", _command_rows(session))
    section("Devices", _device_rows(session, store))
    section("Peripheral Reports", _peripheral_report_rows(session))
    section("Data Ports", _data_port_rows(session, store))
    return rows


def _ssp_samples_by_device(commands) -> "dict[int, List[int]]":
    """Start samples of the sync-point commands the bus acts on (`ssp_takes_effect`),
    grouped by each targeted device. DPs on the same device share the bus SSP cadence;
    different devices can differ."""
    by_dev: dict = {}
    for c in commands:
        if not ssp_takes_effect(c):
            continue
        mask = c.get("device_mask", 0)
        for dev in range(12):
            if mask & (1 << dev):
                by_dev.setdefault(dev, []).append(c["start_sample"])
    return by_dev
