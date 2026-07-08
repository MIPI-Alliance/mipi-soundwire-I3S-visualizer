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
