"""What a capture file is, before anything is decoded: the Open Capture dialog's Source.

`probe(paths)` reads only what is cheap and bounded: a .sal's zip directory, a header, the
tail of a text file for its last timestamp, and a capped prefix for edge counts.
Whatever a format cannot report that way is None, and the dialog
shows "—" rather than a guess.

A probe also says whether a time window is READ on its own (`windowed`: a .sal seeks its
block chain) or the file is read whole and cut afterwards (everything else), and roughly
what loading costs, so the dialog can say whether All fits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

# Edge arrays are uint64: one transition is 8 bytes once loaded.
_EDGE_BYTES = 8


@dataclass
class ProbedChannel:
    value: Any                   # what the loader takes: a channel number, column, ident, name, path
    label: str
    edges: Optional[int] = None  # transitions, if known
    approx: bool = False         # `edges` is an estimate (an upper bound, or a prefix count)


@dataclass
class CaptureProbe:
    kind: str                    # "sal", "saleae_binary", "digital_csv", "vcd", "wfm", "analog_csv"
    paths: List[str]
    format_label: str
    channels: List[ProbedChannel] = field(default_factory=list)
    sample_rate_hz: Optional[float] = None
    duration_s: Optional[float] = None
    windowed: bool = False       # a window is read without the rest of the file
    analog: bool = False         # volts, thresholded on load (per-channel hi/lo)
    two_file: bool = False       # the two channels are two files (.bin / .wfm pair)
    cost_all_bytes: Optional[int] = None   # rough peak to load ONE Link, all of it
    # A windowed read's model (saleae_sal.SalWindowModel) over one Link's two channels:
    # the figure the loader's guard prices a window by.
    window_model: Any = None

    @property
    def name(self) -> str:
        return " + ".join(os.path.basename(p) for p in self.paths)

    def cost_bytes(self, t0: Optional[float] = None, t1: Optional[float] = None) -> Optional[int]:
        """Rough peak to load one Link's [t0, t1) s (None: all). A windowed read is priced
        by the loader's own window model (saleae_sal.SalWindowModel.peak_bytes, the
        blocks the window decodes), so a window this calls fitting is one the loader's
        guard admits; any other format reads the whole file whatever the window. Each
        further Link from the file is loaded, and kept, as well: the dialog
        multiplies."""
        if self.cost_all_bytes is None:
            return None
        if t0 is None or t1 is None or not self.windowed or not self.duration_s:
            return self.cost_all_bytes
        rate = float(self.sample_rate_hz or 0.0)
        if self.window_model is not None and rate > 0:
            # The samples the loader will be asked for (Session.window_samples: the
            # nearest samples, the end one past).
            s0, s1 = int(round(t0 * rate)), int(round(t1 * rate)) + 1
            return self.window_model.peak_bytes(s0, s1)
        return int(self.cost_all_bytes * max(0.0, min(1.0, (t1 - t0) / self.duration_s)))

    def largest_window_s(self, budget_bytes: int) -> Optional[float]:
        """Length in seconds of the longest window from the capture start whose
        `cost_bytes` is within `budget_bytes` (one Link); None when the cost or the
        length is unknown, or a window is not read on its own; 0.0 when no window fits."""
        span = float(self.duration_s or 0.0)
        if self.cost_all_bytes is None or not self.windowed or span <= 0:
            return None
        if (self.cost_bytes(0.0, span) or 0) <= budget_bytes:
            return span
        lo, hi = 0.0, span
        if (self.cost_bytes(0.0, 0.0) or 0) > budget_bytes:
            return 0.0
        for _ in range(60):                  # cost_bytes is non-decreasing in the length
            mid = (lo + hi) / 2.0
            if (self.cost_bytes(0.0, mid) or 0) <= budget_bytes:
                lo = mid
            else:
                hi = mid
        return lo

    def busiest_pair(self, among=None):
        """(clock, data) indices to offer for a Link among channel indices `among` (default
        all), the busier as the clock (a forwarded clock toggles every UI).

        The wiring is read first, since edge counts alone cannot pair Links whose clocks
        run at different rates: when the channels pair up (an even number of them) and
        the adjacent pairs, in file order, look like Links, every pair's busier line
        busier than every pair's quieter one, the first pair is offered. Otherwise the
        busiest is the clock and data the busiest with under half its edges. File order
        when the counts are unknown."""
        idx = sorted(range(len(self.channels)) if among is None else among)
        if len(idx) < 2:
            raise ValueError("a capture needs at least two channels")
        edges = {i: self.channels[i].edges for i in idx}
        if all(e is None for e in edges.values()):
            return idx[0], idx[1]
        pairs = [(a, b) if (edges[a] or 0) > (edges[b] or 0) else (b, a)
                 for a, b in zip(idx[0::2], idx[1::2])]
        known = all(edges[c] is not None and edges[d] is not None for c, d in pairs)
        if known and len(idx) % 2 == 0 and min(edges[c] for c, _ in pairs) > max(edges[d] for _, d in pairs):
            return pairs[0]
        order = sorted(idx, key=lambda i: -1 if edges[i] is None else edges[i], reverse=True)
        clock = order[0]
        top = edges[clock] or 0
        data = next((i for i in order[1:] if edges[i] is not None and edges[i] * 2 < top),
                    order[1])
        return clock, data


def _last_line(path: str, max_tail: int = 1 << 16) -> str:
    """The file's last non-empty line, read from its end."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_tail))
        tail = f.read()
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if line:
            return line.decode("utf-8", errors="replace")
    return ""


def _probe_sal(path: str) -> CaptureProbe:
    from . import saleae_sal
    info = saleae_sal.read_info(path)
    pr = CaptureProbe("sal", [path], "Logic 2 project (.sal)",
                      sample_rate_hz=float(info.sample_rate_hz) or None, windowed=True)
    try:
        cost = saleae_sal.estimate_cost(path)
        per = cost.per_channel
    except Exception:  # noqa: BLE001 - a hint; the dialog works without
        cost, per = None, {}
    pr.channels = [ProbedChannel(c, f"Channel {c}", per.get(c), approx=True)
                   for c in info.channels]
    # Two channels are loaded per Link: price, and measure, the busiest two, as a first
    # Link would.
    two = sorted(info.channels, key=lambda c: per.get(c, 0), reverse=True)[:2]
    model = None
    try:
        model = saleae_sal.window_model(path, channels=two)
        if model.span_samples and info.sample_rate_hz:
            pr.duration_s = model.span_samples / float(info.sample_rate_hz)
    except Exception:  # noqa: BLE001
        pass
    if cost is not None:
        edge_bytes = sum(per.get(c, 0) for c in two) * _EDGE_BYTES
        frac = (edge_bytes / cost.est_edge_bytes) if cost.est_edge_bytes else 1.0
        pr.cost_all_bytes = int(cost.est_peak_bytes * min(1.0, max(frac, 0.0)))
        # A v0 blob has no block chain: a window is read whole, like the other formats.
        pr.windowed = cost.streamable
        if cost.streamable and model is not None and model.span_samples:
            pr.window_model = model
    return pr


def _probe_bin(paths: List[str]) -> CaptureProbe:
    from . import saleae_binary
    pr = CaptureProbe("saleae_binary", list(paths), "Saleae binary (.bin pair)",
                      two_file=True)
    pr.sample_rate_hz = float(saleae_binary.infer_sample_rate(paths)) or None
    ends = []
    for p in paths:
        n = saleae_binary.transition_count(p)
        pr.channels.append(ProbedChannel(p, os.path.basename(p), n if n >= 0 else None))
        try:
            with open(p, "rb") as f:
                head = f.read(saleae_binary._HEADER.size)
            _m, _v, _t, _i, begin, end, _n = saleae_binary._HEADER.unpack(head)
            ends.append(float(end) - float(begin))
        except Exception:  # noqa: BLE001
            pass
    pr.duration_s = max(ends) if ends else None
    # The loader's own pre-flight figure, so "fits" here is what its guard admits.
    try:
        pr.cost_all_bytes = saleae_binary.predict_peak_bytes(paths[0], paths[1])
    except OSError:
        pr.cost_all_bytes = None
    return pr


def _probe_digital_csv(path: str) -> CaptureProbe:
    from . import digital_csv
    header = digital_csv.read_header(path)
    cols = list(range(1, len(header)))
    try:
        counts, rate = digital_csv.scan(path)          # one bounded pass: counts and rate
    except Exception:  # noqa: BLE001
        counts, rate = [], 0
    with open(path, "rb") as fb:
        sample = fb.read(64 << 10)
    rows = digital_csv._predict_rows(path, len(header), sample)
    pr = CaptureProbe("digital_csv", [path], "Digital CSV (.csv)")
    # The counts read the first 2,000,000 rows: exact for a file that short, else a prefix.
    approx = rows > 2_000_000
    pr.channels = [ProbedChannel(c, header[c], counts[i] if i < len(counts) else None,
                                 approx=approx) for i, c in enumerate(cols)]
    pr.sample_rate_hz = float(rate) or None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            next(f)
            first = next(f).split(",")[0]
        last = _last_line(path).split(",")[0]
        pr.duration_s = max(0.0, float(last) - float(first))
    except Exception:  # noqa: BLE001
        pass
    # The loader's own pre-flight figure, so "fits" here is what its guard admits.
    pr.cost_all_bytes = (digital_csv.predict_peak_bytes(rows, len(header), sample)
                         if rows else None)
    return pr


def _probe_vcd(path: str) -> CaptureProbe:
    from . import vcd
    info = vcd.read_info(path)
    sigs = info.scalar_signals
    idents = [s.ident for s in sigs]
    try:
        per, truncated, t_read = vcd.transition_counts_until(path, idents, 2_000_000)
    except Exception:  # noqa: BLE001
        per, truncated, t_read = {}, False, 0
    pr = CaptureProbe("vcd", [path], "Value change dump (.vcd)")
    pr.channels = [ProbedChannel(s.ident, s.name, per.get(s.ident), approx=truncated)
                   for s in sigs]
    tick = float(info.timescale_s or 0.0)
    pr.sample_rate_hz = float(info.sample_rate_hz) or None
    last = 0
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - (1 << 16)))
            for tok in f.read().split():
                if tok.startswith(b"#") and tok[1:].isdigit():
                    last = int(tok[1:])
        if tick and last:
            pr.duration_s = last * tick
    except Exception:  # noqa: BLE001
        pass
    two = sorted((v for v in per.values() if v), reverse=True)[:2]   # one Link's lines
    edges = sum(two)
    if truncated:
        # The counts cover #0 to #t_read: scale them to the dump's last time stamp.
        edges = int(edges * last / t_read) if (edges and last and t_read > 0) else 0
    pr.cost_all_bytes = edges * vcd.BYTES_PER_EDGE if edges else None
    return pr


def _probe_wfm(paths: List[str]) -> CaptureProbe:
    from . import wfm
    pr = CaptureProbe("wfm", list(paths), "Tektronix waveform (.wfm pair)", analog=True,
                      two_file=True)
    heads: List[Optional[dict]] = []
    for p in paths:
        try:
            heads.append(wfm.read_header(p))
        except Exception:  # noqa: BLE001
            heads.append(None)
        pr.channels.append(ProbedChannel(p, os.path.basename(p)))
    good = [h for h in heads if h]
    if good:
        pr.sample_rate_hz = good[0]["sample_rate_hz"] or None
        pr.duration_s = max(h["duration_s"] for h in good)
        # The loader's own pre-flight figure (read_wfm prices the pair).
        pr.cost_all_bytes = wfm.predict_pair_peak_bytes(int(max(h["n_points"] for h in good)))
    return pr


def _probe_analog_csv(path: str) -> CaptureProbe:
    from . import analog_csv
    _pre, header, offset, _sample = analog_csv._scan_header(path)
    names = header[1:]
    pr = CaptureProbe("analog_csv", [path], "Analog CSV (.csv, scope export)", analog=True)
    pr.channels = [ProbedChannel(n, n) for n in names]
    try:
        import csv as _csv
        times = []
        with open(path, newline="", encoding="utf-8") as f:
            f.seek(offset)                   # the first data row, past the TIME header
            for row in _csv.reader(f):
                if row and row[0].strip():
                    times.append(float(row[0]))
                if len(times) >= 2000:
                    break
        if len(times) >= 2:
            import numpy as np
            dt = np.diff(np.asarray(times))
            dt = dt[dt > 0]
            if dt.size:
                pr.sample_rate_hz = 1.0 / float(np.median(dt))
        last = float(_last_line(path).split(",")[0])
        pr.duration_s = max(0.0, last - times[0]) if times else None
    except Exception:  # noqa: BLE001
        pass
    try:
        # The loader's own pre-flight figure, so "fits" here is what its guard admits.
        pr.cost_all_bytes = analog_csv.predict_peak_bytes(path)
    except Exception:  # noqa: BLE001
        pass
    return pr


def probe(paths: List[str]) -> CaptureProbe:
    """Probe the file(s) a user picked: one capture, or both files of a .bin / .wfm pair.
    Raises ValueError for a pick that is not one capture (one .wfm, three files, a
    Visualizer config CSV) with a message the dialog can show."""
    paths = list(paths)
    if not paths:
        raise ValueError("no file chosen")
    low = [p.lower() for p in paths]
    bins = [p for p, lp in zip(paths, low) if lp.endswith(".bin")]
    wfms = [p for p, lp in zip(paths, low) if lp.endswith(".wfm")]
    if len(paths) == 2 and len(bins) == 2:
        return _probe_bin(bins)
    if len(paths) == 2 and len(wfms) == 2:
        return _probe_wfm(wfms)
    if len(paths) > 2 or (len(paths) == 2 and (bins or wfms)):
        # Not quietly the pair out of a bigger pick: the rest would be ignored unseen.
        raise ValueError("Select one capture file, or both files of one .bin / .wfm pair "
                         "and nothing else.")
    if wfms or bins:
        raise ValueError("A .bin or .wfm capture is two files, one channel each: select "
                         "BOTH files together.")
    if len(paths) > 1:
        raise ValueError("Select one capture file (or both files of a .bin / .wfm pair).")
    path, lp = paths[0], low[0]
    if lp.endswith(".sal"):
        return _probe_sal(path)
    if lp.endswith(".vcd"):
        return _probe_vcd(path)
    if lp.endswith(".csv"):
        from . import analog_csv
        with open(path, encoding="utf-8", errors="replace") as f:
            head = [next(f, "") for _ in range(40)]
        # A config CSV is "Name,value" rows, a register's name ending _REG; a capture CSV's
        # first field is Time, so a CHANNEL named …_REG does not make it one.
        if any(ln.startswith("AppVersion") or ln.split(",")[0].strip().endswith("_REG")
               or ln.startswith("NumColumns") for ln in head):
            raise ValueError("This looks like a Visualizer config CSV, not a capture.\n\n"
                             "Open it in the Bus Visualizer with File ▸ Open Visualizer CSV.")
        if analog_csv.is_analog_csv(path):
            return _probe_analog_csv(path)
        return _probe_digital_csv(path)
    raise ValueError(f"{os.path.basename(path)}: not a capture format this app opens "
                     f"(.sal, .csv, .vcd, or a .bin / .wfm pair).")
