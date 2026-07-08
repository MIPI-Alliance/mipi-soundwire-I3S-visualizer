"""Derived measurements over a decoded session (rates, SSP intervals, per-DP
bandwidth) for the Measurements panel."""
from __future__ import annotations

from collections import Counter
from typing import List, Tuple

from .errors import count_errors


def capture_measurements(session, store=None) -> List[Tuple[str, str]]:
    """A flat (label, value) list of headline metrics for the capture. Pass the
    already-built AudioStore as `store` to avoid rebuilding it here — rebuilding a
    multi-million-sample store on the GUI thread froze the UI at load."""
    rows: List[Tuple[str, str]] = []
    sr = session.sample_rate_hz or 1

    # Clock / UI / row rate and column count are per config-section: the bus can
    # reconfigure mid-capture (e.g. cold-start 2col -> 8col at a different clock), so
    # a single whole-capture figure is misleading. With one config, list the headline
    # rates once; with several, list each config's own measured values so every
    # geometry is visible (the panel isn't cursor-driven).
    stats = []
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
            tag = f"Config {st.get('section', 0)}"
            rows.append((tag, f"{cols}col"))
            rows.append(("  clock", f"{ui_hz / 2e6:.4f} MHz"))
            rows.append(("  UI rate", f"{ui_hz / 1e6:.4f} MHz"))
            rows.append(("  row rate", f"{row_hz / 1e3:.3f} kHz"))
    else:
        rows.append(("Clock", f"{session.ui_rate_hz / 2e6:.4f} MHz"))
        rows.append(("UI rate", f"{session.ui_rate_hz / 1e6:.4f} MHz"))
        rows.append(("Row rate", f"{session.row_rate_khz:.3f} kHz"))
        rows.append(("Columns", str(session.column_count)))
    rows.append(("Commands", str(len(session.commands))))
    rows.append(("Errors", str(count_errors(session.commands))))

    # Bus-config segments (one per column-count geometry; >1 means the bus
    # reconfigured mid-capture, e.g. cold-start 2col -> 8col).
    segs = getattr(session, "segments", []) or []
    if segs:
        counts = sorted({int(s.get("column_count", 0)) for s in segs})
        rows.append(("Bus configs", str(len(segs))))
        rows.append(("  column counts", ", ".join(f"{c}col" for c in counts)))
        if len(segs) > 1:
            rows.append(("  reconfigurations", str(len(segs) - 1)))

    for kind, n in sorted(Counter(c.get("command", "") for c in session.commands).items()):
        if kind:
            rows.append((f"  {kind}", str(n)))

    # Ping cadence in bus rows: the manager pings on a schedule, so the row gap between
    # consecutive Pings is near-constant. Min/max/mean expose jitter or a schedule change
    # (rows, not time, so it's independent of the per-section clock rate).
    ping_rows = [int(c.get("bus_row", 0)) for c in session.commands if c.get("command") == "Ping"]
    periods = [b - a for a, b in zip(ping_rows, ping_rows[1:]) if b - a > 0]
    if periods:
        mean = sum(periods) / len(periods)
        rows.append(("Ping period", f"{len(ping_rows):,} pings"))
        rows.append(("  min", f"{min(periods):,} rows"))
        rows.append(("  max", f"{max(periods):,} rows"))
        rows.append(("  mean", f"{mean:,.1f} rows"))

    # Link-control events: PM_Action (link state-machine) writes, decoded.
    from .link_events import link_events
    events = link_events(session.commands, session.register_map)
    if events:
        rows.append(("Link PM actions", str(len(events))))
        for e in events[:8]:
            rows.append((f"  row {e['bus_row']:,}", e["action"]))

    # SSP cadence. A Stream Sync Point is carried by SSCR (confirmed commit) and
    # SSPA commands; each targets a device set (device_mask). DPs on different
    # devices — or driven by differently-cadenced sync points — can have distinct
    # SSP intervals, so we attribute SSPs to their target devices and report the
    # interval per audio stream rather than one bus-wide average.
    ssp_all = [c["start_sample"] for c in session.commands if c.get("has_sync_point")]
    rows.append(("SSP count", str(len(ssp_all))))
    ssp_by_dev = _ssp_samples_by_device(session.commands)

    # Per-(device, dataport) audio bandwidth + SSP interval.
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
