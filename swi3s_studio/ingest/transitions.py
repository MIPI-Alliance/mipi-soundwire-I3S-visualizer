"""Build a Capture from a per-UI data-level plan, and the synthetic demo capture.

`build_capture_from_levels` is the inverse of the decoder's edge walk: it turns
an absolute per-UI data-level list into clock + data transition arrays, with a
single leading "alignment" UI (data low) prepended so that, after the decoder
aligns to the first rising clock edge, plan element 0 lands on Column 0 of Row 0
(matching the C++ MemorySampleSource semantics). Used for tests, the demo
capture, and as the reference for the digital-CSV reader.
"""
from __future__ import annotations

import numpy as np

import swi3score

from .capture import Capture

# 12.288 MHz forwarded DDR clock -> 24.576 MHz UI rate; >=8 samples/clock-cycle.
DEFAULT_SAMPLES_PER_UI = 4
DEFAULT_SAMPLE_RATE_HZ = 98_304_000

# Nominal DP-low after the PhyStart falling edge before audio mode (Man_tPhyStart,
# 64–96 µs). Kept here for the synthetic Cold Start builder; mirrors the canonical
# value in analysis.link_control (decode side).
MAN_TPHYSTART_US_LOCAL = 80.0


def build_capture_from_levels(levels, samples_per_ui: int = DEFAULT_SAMPLES_PER_UI,
                              sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ) -> Capture:
    spu = int(samples_per_ui)
    ui = [False] + [bool(x) for x in levels]      # leading alignment UI (data low)
    n = len(ui)

    # One clock edge per UI; clock starts low so the first edge is rising.
    clock_edges = (np.arange(1, n + 1, dtype=np.uint64) * spu)

    # A data transition wherever the level changes between consecutive UIs,
    # placed at the previous clock-edge sample (k*spu) so it is in effect when
    # UI k is sampled at (k+1)*spu - 1, but not when UI k-1 is sampled.
    data_edges = [k * spu for k in range(1, n) if ui[k] != ui[k - 1]]

    return Capture(
        clock_edges=clock_edges,
        data_edges=np.asarray(data_edges, dtype=np.uint64),
        initial_clock=False,
        initial_data=ui[0],
        sample_rate_hz=sample_rate_hz,
    )


def cold_start_edges(phy_number: int = 2, sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
                     start_sample: int = 0):
    """Synthesize a §5.1.2 Cold Start link-control preamble as DP (clock) and DN
    (data) transition edges, the inverse of :func:`decode_link_control`.

    Drives, on the raw link pins (both starting low): an idle gap, a Bus-Reset
    DP-high pulse (~1600 µs), a reset-recovery low, the 4-bit ``phy_number`` clocked
    on DN MSb-first (DN set at each rising edge, sampled at the falling edge), a
    final clock-high, then the PhyStart falling edge and the Man_tPhyStart low.

    Returns ``(clock_edges, data_edges, end_sample, end_data_level)`` — absolute
    sample numbers offset by `start_sample`, ready to prepend before audio mode.
    DP is low at `start_sample` and again at `end_sample` (clean seam either side).
    """
    spp = sample_rate_hz / 1e6
    us = lambda x: int(round(x * spp))            # noqa: E731 — terse local
    clk: list = []
    dat: list = []
    t = start_sample + us(50)                     # initial idle (DP low, DN low)
    data_level = False

    def set_dn(level: bool, at: int) -> None:
        nonlocal data_level
        if bool(level) != data_level:
            dat.append(at)
            data_level = bool(level)

    clk.append(t); t += us(1600)                  # rising → Bus Reset (DP high)
    clk.append(t); t += us(75)                    # falling → reset recovery (DP low)
    for i in (3, 2, 1, 0):                         # 4 PHY-number bits, MSb first
        bit = bool((phy_number >> i) & 1)
        set_dn(bit, t - us(2))                     # DN stable before the rising edge
        clk.append(t); t += us(15)                # rising (DP high, DN = bit)
        clk.append(t); t += us(15)                # falling ← bit sampled here
    set_dn(False, t - us(2))                       # DN back to idle low
    clk.append(t); t += us(15)                    # final clock high (step 6)
    clk.append(t); t += us(MAN_TPHYSTART_US_LOCAL)  # PhyStart falling → DP low
    end_sample = t
    return (np.asarray(clk, dtype=np.uint64), np.asarray(dat, dtype=np.uint64),
            int(end_sample), bool(data_level))


def prepend_cold_start(capture: Capture, phy_number: int = 2) -> Capture:
    """Return a copy of `capture` with a Cold Start LC preamble (selecting
    `phy_number`) spliced in front, so the bring-up is observable from t=0. The
    original audio-mode edges are shifted to start right after PhyStart."""
    pre_clk, pre_dat, end, end_data = cold_start_edges(
        phy_number, capture.sample_rate_hz, start_sample=0)
    shift = np.uint64(end)
    clk = np.concatenate([pre_clk, capture.clock_edges.astype(np.uint64) + shift])
    shifted_dat = capture.data_edges.astype(np.uint64) + shift
    # Seam: if the preamble leaves DN at a different level than the audio region
    # begins with, insert a transition at the splice so levels stay consistent.
    parts = [pre_dat]
    if bool(end_data) != bool(capture.initial_data):
        parts.append(np.asarray([end], dtype=np.uint64))
    parts.append(shifted_dat)
    dat = np.concatenate(parts)
    return Capture(clock_edges=clk, data_edges=dat,
                   initial_clock=False, initial_data=False,
                   sample_rate_hz=capture.sample_rate_hz)


def demo_capture(audio_samples_per_channel: int = 32,
                 samples_per_ui: int = DEFAULT_SAMPLES_PER_UI,
                 sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
                 cold_start: bool = False) -> Capture:
    """Synthetic PHY2 capture: config + commit + scrambled stereo audio. With
    ``cold_start=True``, a §5.1.2 Cold Start bring-up (selecting PHY2) is spliced
    in front so the link comes up from reset → PHY-select → audio on the wire."""
    levels = swi3score.make_demo_levels(audio_samples_per_channel)
    cap = build_capture_from_levels(levels, samples_per_ui, sample_rate_hz)
    return prepend_cold_start(cap, phy_number=2) if cold_start else cap
