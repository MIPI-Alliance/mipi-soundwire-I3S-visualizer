"""The Capture container: a SWI3S bus capture as two transition (edge) lists.

A capture is represented by the *edges* of the forwarded clock and the data
line, not per-sample data — the compact form a .sal / digital CSV decodes to,
and exactly what the decode core walks. This keeps even multi-GB captures light:
edge counts scale with bus activity, not capture duration × sample rate.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import swi3score


@dataclass
class Capture:
    clock_edges: np.ndarray      # uint64 sample numbers, ascending
    data_edges: np.ndarray       # uint64 sample numbers, ascending
    initial_clock: bool          # clock level before the first edge
    initial_data: bool           # data level before the first edge
    sample_rate_hz: int

    def __post_init__(self) -> None:
        self.clock_edges = np.ascontiguousarray(self.clock_edges, dtype=np.uint64)
        self.data_edges = np.ascontiguousarray(self.data_edges, dtype=np.uint64)

    @property
    def duration_s(self) -> float:
        last = 0
        if self.clock_edges.size:
            last = max(last, int(self.clock_edges[-1]))
        if self.data_edges.size:
            last = max(last, int(self.data_edges[-1]))
        return last / self.sample_rate_hz if self.sample_rate_hz else 0.0

    def samples_per_ui(self) -> int:
        """Samples between consecutive clock edges (one edge per UI). Estimated
        from the first two edges; used to map a sample number to a UI index."""
        if self.clock_edges.size >= 2:
            d = int(self.clock_edges[1]) - int(self.clock_edges[0])
            if d > 0:
                return d
        return 1

    def sample_source(self) -> "swi3score.TransitionSampleSource":
        """Build the C++ ISampleSource that walks these edges at native speed."""
        return swi3score.TransitionSampleSource(
            self.clock_edges, self.data_edges,
            bool(self.initial_clock), bool(self.initial_data), int(self.sample_rate_hz),
        )

    def subcapture(self, start_sample: int, end_sample: int) -> "Capture":
        """A new Capture holding just the edges in ``[start_sample, end_sample)``,
        rebased so the window starts at sample 0. Each channel's initial level is the
        level it held AT `start_sample` (the parity of the edges before it), so the
        windowed capture reproduces the original signal over the range. Used to export
        a sub-range of a capture."""
        s0 = max(0, int(start_sample))
        s1 = int(end_sample)
        if s1 <= s0:
            raise ValueError(f"subcapture: end_sample ({s1}) must be past start ({s0})")

        def window(edges: np.ndarray, initial: bool):
            # Fold an edge landing exactly AT s0 into the initial level (side='right'):
            # it would otherwise become a rebased edge at sample 0 (a zero-length gap the
            # .sal v3 codec rejects). The level held from s0 is the parity of every edge
            # at/before it; kept edges are strictly inside the window, rebased to 0.
            lo = int(np.searchsorted(edges, s0, side="right"))    # edges at/before s0
            hi = int(np.searchsorted(edges, s1, side="left"))     # first edge at/after s1
            kept = edges[lo:hi].astype(np.int64) - s0             # rebased to 0 (all > 0)
            new_initial = bool(initial) ^ (lo % 2 == 1)           # level held at s0
            return np.ascontiguousarray(kept, dtype=np.uint64), new_initial

        clk, ic = window(self.clock_edges, self.initial_clock)
        dat, idt = window(self.data_edges, self.initial_data)
        return Capture(clk, dat, ic, idt, int(self.sample_rate_hz))

