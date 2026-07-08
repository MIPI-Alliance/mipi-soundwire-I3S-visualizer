"""Measured bus setup/hold ("eye") margins from the capture's raw edges.

SWI3S PHY2 is a forwarded-clock DDR link: the data line is sampled once per UI (one
clock edge per UI, alternating rising/falling); the decoder samples just before each
clock edge (the settled end of the UI). How much margin there is depends on *where* the
analyzer probes the bus — a sub-optimal tap pushes data transitions toward the sample
point and erodes setup/hold, especially on one clock-edge polarity (duty is rarely 50%).

Straight off `Capture.clock_edges`/`data_edges` (no decode), for EVERY data transition
in [start_sample, end_sample) it reports: setup (transition -> the sample edge that
samples the new value), hold (the previous sample edge -> transition), the polarity of
each of those two edges, the transition direction, the capture sample and the frame
column — enough for the Timing pane to bin/colour the four (clock-edge polarity × data
direction) categories, filter by column/driver, and find the tightest margins. Plus the
UI period and clock high/low split (duty).

Measuring every transition is data-edge-bound, so it stays cheap even on multi-GB
captures and catches a tight edge anywhere in the range; only the duty is estimated from
a window. The range is one audio-mode REGION (Session.timing_regions): a commit can
change the bus clock rate mid-capture (e.g. 2 col -> 16 col) and setup/hold only makes
sense within a single clock rate, so each region is measured on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..nputil import searchsorted as _ss


@dataclass
class BusTiming:
    sample_rate_hz: int
    ui_ns: float
    clock_high_ns: float     # median gap after a rising edge (high phase)
    clock_low_ns: float      # median gap after a falling edge (low phase)
    # Per-transition arrays (parallel, one entry per measured data transition): the
    # source of truth the Timing pane bins, colours (4-way clock-pol × data-dir), filters
    # by column and searches for the tightest margins.
    tr_setup_ns: np.ndarray          # setup of each transition (-> its sampling edge)
    tr_hold_ns: np.ndarray           # hold of each transition (-> the previous edge)
    tr_data_rising: np.ndarray       # bool: this transition drove the data line HIGH
    tr_setup_clk_rising: np.ndarray  # bool: the sampling edge (setup) is a rising clock edge
    tr_hold_clk_rising: np.ndarray   # bool: the previous edge (hold) is a rising clock edge
    tr_sample: np.ndarray            # capture sample number of each transition
    tr_column: np.ndarray            # frame column of each transition (-1 if geometry unknown)
    n_clock_edges: int          # clock edges in the region
    n_data_edges: int           # data transitions measured


def measure_bus_timing(capture, *, segments=None, start_sample: int = 0,
                       end_sample: Optional[int] = None, max_edges: int = 3_000_000) -> Optional[BusTiming]:
    """Measure setup/hold for `capture` over the audio-mode region [start_sample,
    end_sample). Returns None if there aren't enough edges to measure.

    Every data transition in the range is measured (data-edge-bound, so cheap even on
    huge captures) — a windowed sample would miss a localized tight edge. The §5.1.2
    link-control / cold-start region (long idle gaps, not real sampled UIs) is excluded
    because it is before the audio start. Only the clock duty / UI period is estimated
    from a representative window of up to `max_edges` clock gaps within the region.

    `segments` (a list of {start_ui, column_count}, from the decode) lets each
    transition be tagged with its frame column (`tr_column`) so the caller can filter
    by column / data port / driver; omit it and `tr_column` is all -1 (unknown)."""
    clk = np.asarray(capture.clock_edges, dtype=np.int64)
    dat = np.asarray(capture.data_edges, dtype=np.int64)
    rate = int(capture.sample_rate_hz) or 1
    if clk.size < 100 or dat.size < 16:
        return None
    to_ns = 1e9 / rate
    n = clk.size
    initial_clock = bool(capture.initial_clock)
    initial_data = bool(capture.initial_data)

    # Clock-edge bounds of the region: [c0, c1) in edge-index space. c1 is the boundary
    # edge (the next region's first edge) — a transition sampled there sits in the
    # handover UI that spans the clock-rate change, so it's excluded from BOTH regions.
    c0 = int(_ss(clk, int(start_sample), side="left")) if start_sample > 0 else 0
    c1 = int(_ss(clk, int(end_sample), side="left")) if end_sample is not None else n

    # Clock duty + UI period from a representative window of THIS region's gaps. Duty is a
    # global property within a region, so a window is enough and keeps this O(window).
    # (Windowing to the region — not the whole capture — is what keeps a short region's
    # duty region-local instead of leaking in the excluded link-control gaps.) Edge k
    # (0-based) leaves state = initial_clock ^ ((k+1) & 1); `rising` is that state.
    w0 = c0
    # Clamp the duty window STRICTLY to the region [c0, c1): never reach past c1, or a
    # tiny region's UI/duty would be measured across the handover edge into the next
    # region (the very edge the setup/hold path below excludes). A region with < 2
    # edges then has no measurable gap and ui/high/low fall back to 0 (honest
    # "unknown") rather than reporting the adjacent region's clock rate.
    clkw = clk[w0:min(c1, w0 + max_edges)]
    rising_w = (initial_clock ^ (((np.arange(w0, w0 + clkw.size) + 1) & 1) == 1))
    gapw = np.diff(clkw) * to_ns
    after_rising = rising_w[:-1]
    ui_ns = float(np.median(gapw)) if gapw.size else 0.0
    clk_high = float(np.median(gapw[after_rising])) if after_rising.any() else ui_ns
    clk_low = float(np.median(gapw[~after_rising])) if (~after_rising).any() else ui_ns

    # Setup/hold, measured PER DATA TRANSITION over the region so that every transition
    # contributes exactly one setup AND one hold (a transition sits between two sample
    # edges, so both are defined and the counts must agree), and a tight edge anywhere in
    # the range is caught.
    #
    # A transition d falls in the UI (clk[e-1], clk[e]] whose END sample edge is e (the
    # edge that samples the new value). Its SETUP is that edge's margin (clk[e] - d, how
    # long the value was stable before it was sampled). Its HOLD is the PREVIOUS edge's
    # margin (d - clk[e-1], how long the old value was held after it was last sampled).
    # Require both bounding edges strictly inside the region (c0 < end_i < c1), so the
    # handover UI at the boundary — whose UI already runs at the adjacent region's rate —
    # doesn't leak in.
    end_i = np.searchsorted(clk, dat, side="left")       # first sample edge >= d (UI end)
    ok = (end_i >= c0 + 1) & (end_i <= min(c1, n) - 1)
    d, end_i = dat[ok], end_i[ok]
    start_i = end_i - 1
    setup = (clk[end_i] - d) * to_ns                     # data edge -> its sampling edge
    hold = (d - clk[start_i]) * to_ns                    # previous sample edge -> data edge
    # Polarity from the absolute edge-index parity (no need to materialise a full array).
    setup_pol = (initial_clock ^ (((end_i + 1) & 1) == 1))      # sampling edge
    hold_pol = (initial_clock ^ ((start_i & 1) == 0))           # previous edge (== end_i-1)
    # Data-transition direction: the data line's level *after* the edge (its index in
    # `dat` sets the toggle parity, same convention as the clock's `rising`).
    d_abs = np.nonzero(ok)[0]
    data_rising = (initial_data ^ (((d_abs + 1) & 1) == 1))
    # Frame column of each transition: its UI (end_i) minus the containing segment's
    # start_ui, modulo that segment's column count. -1 when geometry is unknown.
    tr_column = np.full(d.size, -1, dtype=np.int64)
    if segments:
        starts = np.array([int(s["start_ui"]) for s in segments], dtype=np.int64)
        colcs = np.array([max(1, int(s["column_count"])) for s in segments], dtype=np.int64)
        seg_i = np.clip(np.searchsorted(starts, end_i, side="right") - 1, 0, starts.size - 1)
        tr_column = (end_i - starts[seg_i]) % colcs[seg_i]

    return BusTiming(
        sample_rate_hz=rate, ui_ns=ui_ns, clock_high_ns=clk_high, clock_low_ns=clk_low,
        tr_setup_ns=setup, tr_hold_ns=hold, tr_data_rising=data_rising,
        tr_setup_clk_rising=setup_pol, tr_hold_clk_rising=hold_pol,
        tr_sample=d, tr_column=tr_column,
        n_clock_edges=int(n), n_data_edges=int(d.size),
    )
