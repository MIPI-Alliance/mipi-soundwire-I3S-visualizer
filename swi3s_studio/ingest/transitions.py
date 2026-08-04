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

# A SWI3S PHY2 link runs a 12.288 MHz forwarded DDR clock -> 24.576 MHz UI rate. A real
# analyzer oversamples that clock heavily (tens of samples per UI), which is what gives
# the Timing pane fine setup/hold resolution. Sampling only 4x/UI (as the demo first did)
# quantises setup/hold to ~10 ns steps and collapses the eye histogram to a single bin
# that touches 0 ns. 32 samples/UI (786.432 MHz) keeps the UI rate + edge COUNT identical
# (edges scale with bus activity, not sample rate — see Capture) while resolving a clean,
# center-aligned eye (~20 ns each side at ~1.3 ns resolution).
DEFAULT_SAMPLES_PER_UI = 32
DEFAULT_SAMPLE_RATE_HZ = 786_432_000       # 32 * 24.576 MHz UI rate (clock stays 12.288 MHz)
# PHY3 (DLV): the 16-col operational config holds the row rate at 3.072 MRows/s (the
# recovered-clock PLL's reference), so its UI rate is 16 * 3.072 = 49.152 MHz — double
# the FBCSE UI rate. A real logic analyzer samples at ONE fixed rate for the whole
# capture, sized for the fastest UI, so the DLV demo uses this single rate throughout
# (keeping 32 samples per operational UI, same eye resolution as FBCSE).
DLV_SAMPLE_RATE_HZ = 2 * DEFAULT_SAMPLE_RATE_HZ   # 1.572864 GHz -> 49.152 MHz UI @ 32 samples

# PHY1 (slow FBCSE): the demo runs a constant 4-column bus at 1.536 MRows/s (RowRate 1536
# in the reference configs), so its UI/bit rate is 4 * 1.536 = 6.144 MHz — a quarter of the
# PHY2 FBCSE UI. At 32 samples/UI that's this sample rate (keeps the same eye resolution).
PHY1_SAMPLE_RATE_HZ = 196_608_000                 # 32 * 6.144 MHz UI @ 4 cols -> 1.536 MRows/s

# A REAL logic analyzer samples at one fixed rate (its max), NOT a convenient multiple of
# the SWI3S UI — so the demo captures use 500 MHz (2 ns period) throughout, giving a
# NON-integer number of samples per UI (e.g. PHY2 24.576 MHz UI -> 20.345 samples/UI). This
# exercises the decode the way a bench capture would: the FBCSE clock-edge recovery and the
# DLV virtual PLL both work from actual (rounded, jittered) edge positions, not an assumed
# integer grid. The per-PHY UI rates below turn into the float UI period at this rate.
ANALYZER_SAMPLE_RATE_HZ = 500_000_000             # 2 ns period — the analyzer's fixed max rate
FBCSE_UI_RATE_HZ = 24_576_000                     # PHY2 UI (12.288 MHz DDR clock)
DLV_ROW_RATE_HZ = 3_072_000                       # PHY3 recovered-clock row reference
PHY1_UI_RATE_HZ = 6_144_000                       # PHY1 UI (4 cols @ 1.536 MRows/s)

# Nominal DP-low after the PhyStart falling edge before audio mode (Man_tPhyStart,
# 64–96 µs). Kept here for the synthetic Cold Start builder; mirrors the canonical
# value in analysis.link_control (decode side).
MAN_TPHYSTART_US_LOCAL = 80.0


def build_capture_from_levels(levels, samples_per_ui: float = DEFAULT_SAMPLES_PER_UI,
                              sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ) -> Capture:
    spu = float(samples_per_ui)                   # may be NON-integer (real analyzer rate)
    lv = np.asarray(levels, dtype=bool)           # per-UI levels (a leading low UI is prepended)
    n = lv.size + 1                               # + the leading alignment UI (data low)

    # One clock edge per UI at k*spu, ROUNDED to the sample grid — for a non-integer spu the
    # spacing then alternates by ±1 sample (the quantisation an off-multiple sample rate
    # produces on a bench). The decoder recovers the UI from the actual edges, so this is
    # exactly the jitter a real capture presents. Integer spu reduces to the old k*spu grid.
    clock_edges = np.rint(np.arange(1, n + 1, dtype=np.float64) * spu).astype(np.uint64)

    # A data transition wherever the level changes between consecutive UIs (with a leading
    # low before UI 0). Center it in the UI (midway between the two bounding clock edges): a
    # data edge on the clock edge shows ~0 setup, one hard against it ~0 hold, so centering
    # gives a balanced eye (setup ~= hold ~= half a UI) and stays strictly between the
    # neighbouring clock edges — the sampled value/decode is unchanged even for non-integer
    # spu. Vectorised: the UI k (1-based, plan element j = k-1) sits at (k+0.5)*spu = (j+1.5).
    prev = np.concatenate(([False], lv[:-1]))     # the previous UI's level (leading low at j=0)
    j = np.nonzero(lv != prev)[0]                 # plan indices where the level changes
    data_edges = np.rint((j.astype(np.float64) + 1.5) * spu).astype(np.uint64)

    return Capture(
        clock_edges=clock_edges,
        data_edges=data_edges,
        initial_clock=False,
        initial_data=False,                       # the leading alignment UI is data-low
        sample_rate_hz=sample_rate_hz,
    )


def build_dlv_capture_from_levels(levels, row_columns, samples_per_ui: int = DEFAULT_SAMPLES_PER_UI,
                                  sample_rate_hz: int = DLV_SAMPLE_RATE_HZ) -> Capture:
    """Inverse edge-builder for a PHY3 (DLV) per-UI LOGICAL differential level plan.

    Unlike :func:`build_capture_from_levels` (forwarded-clock FBCSE: one clock edge
    per UI + the NRZS level on the data line, sampled AT the edge), DLV has no
    forwarded clock. DP and DN are a complementary differential pair carrying one
    logical signal (1 = DP high / DN low), driven full-UI NRZ and sampled MID-UI. So
    each UI's level sits on the DP wire for the whole UI and transitions only at UI
    boundaries, and DN is the exact complement (toggles at the same samples).
    ``clock_edges`` = the DP wire, ``data_edges`` = the DN wire. The DLV decode
    front-end recovers the bit clock from the Sync1 (0->1) rising edges.

    CONSTANT ROW PERIOD: ``row_columns`` gives each row's column count (``levels`` is
    row-major). Every row occupies the SAME span ``samples_per_ui * max(columns)``, so
    the recovered-clock PLL sees a fixed once-per-row reference (3.072 MRows/s). A row
    of ``cols`` columns subdivides that span into ``cols`` equal UIs, so the UI/bit rate
    RISES when the geometry commits to more columns (Safe-Lock-4 -> 16: 12.288 -> 49.152
    MHz). ``samples_per_ui`` sizes the OPERATIONAL (widest) UI and may be NON-integer (a
    real analyzer's fixed rate isn't a multiple of the UI): edges are placed at the rounded
    sample grid, so the row period and per-column widths jitter by ±1 sample the way a bench
    capture does — the virtual PLL recovers the true cadence from the actual edges. Row r is
    placed at ``(r+1)*row_period`` so a leading-low row precedes row 0's Sync1 (a clean first
    rising edge, matching the FBCSE builder)."""
    spu = float(samples_per_ui)
    cpr = np.asarray(row_columns, dtype=np.int64)
    lv = np.asarray(levels, dtype=bool)
    if cpr.size == 0 or lv.size == 0:
        empty = np.zeros(0, dtype=np.uint64)
        return Capture(clock_edges=empty, data_edges=empty.copy(),
                       initial_clock=False, initial_data=True, sample_rate_hz=sample_rate_hz)
    row_period = spu * float(cpr.max())           # constant across the capture (may be non-integer)
    # Per-UI absolute sample position, fully vectorised (the old per-UI Python loop over ~25M
    # UIs cost seconds on the 1 s demo). Row r's Column 0 is at (r+1)*row_period (a leading-low
    # row precedes Row 0); its column c is c*(row_period/cols_r) past that. Build the row index
    # and within-row column index of every UI with repeat()/cumsum, then position = base + c*w.
    ui_row = np.repeat(np.arange(cpr.size, dtype=np.int64), cpr)     # row of each UI
    ui_start = np.concatenate(([0], np.cumsum(cpr)[:-1]))            # first UI index of each row
    col_idx = np.arange(lv.size, dtype=np.int64) - ui_start[ui_row]  # column within the row
    col_width = row_period / cpr.astype(np.float64)                 # UI width per row
    pos = (ui_row + 1) * row_period + col_idx * col_width[ui_row]    # absolute sample of each UI
    # DP toggles where the level changes (leading-low before UI 0), rounded to the sample grid.
    change = np.empty(lv.size, dtype=bool)
    change[0] = lv[0]                                                # vs the leading low
    change[1:] = lv[1:] != lv[:-1]
    dp = np.rint(pos[change]).astype(np.uint64)
    return Capture(
        clock_edges=dp,                           # DP wire (differential +)
        data_edges=dp.copy(),                     # DN wire = exact complement (same edge samples)
        initial_clock=False,                      # DP initial level (leading low)
        initial_data=True,                        # DN = complement of DP
        sample_rate_hz=sample_rate_hz,
    )


def cold_start_edges(phy_number: int = 2, sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
                     start_sample: int = 0):
    """Synthesize a §5.1.2 Cold Start link-control preamble as DP (clock) and DN
    (data) transition edges, the inverse of :func:`decode_link_control`.

    Drives, on the raw link pins (both starting low): a brief idle, the two-part
    Bus Reset — LC:00 (DP=0,DN=0, Man_tReset00 212–236 µs) then LC:10 (DP=1,DN=0,
    Man_tReset10 ~1600 µs) — a reset-recovery low, the 4-bit ``phy_number`` clocked
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

    # Bus Reset is two parts: LC:00 (DP=0) then LC:10 (DP=1). A short pre-reset high
    # gives LC:00 its own falling edge so the decoder can measure Man_tReset00; the
    # <150 µs pulse decodes as an LC_Ack/Request and is skipped when hunting the reset.
    clk.append(t); t += us(60)                    # rising → brief pre-reset high (LC_Ack, <150 µs)
    clk.append(t); t += us(224)                   # falling → Bus Reset LC:00 (Man_tReset00 212–236 µs)
    clk.append(t); t += us(1600)                  # rising → Bus Reset LC:10 (Man_tReset10 ~1600 µs)
    clk.append(t); t += us(75)                    # falling → reset recovery (DP low, 72.5–80 µs)
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
                 cold_start: bool = False, phy: int = 2,
                 variant: str = "") -> Capture:
    """Synthetic capture: config + commit + scrambled audio. ``phy`` selects the
    audio-mode PHY: 1 = slow FBCSE (forwarded clock, NRZS CDS in Column 0, a CONSTANT
    4-column bus whose ports are REPOSITIONED mid-capture), 2 = FBCSE (2->8->16 columns),
    3 = DLV (differential pair, plain-NRZ CDS at Column 2, Safe-Lock-4 then 16 columns;
    clock recovered from Sync1 edges). With ``cold_start=True`` the matching §5.1.2 Cold
    Start bring-up (selecting that PHY) is spliced in front so the link comes up from
    reset -> PHY-select -> audio on the wire.

    ``variant="flow_control"`` selects the PHY2-framed flow-control demo (four peripheral
    data ports, one per flow mode, with TX_PRESENT-gated jittered transport and a
    validated DRQ handshake) instead of the standard stereo-PCM + PDM content.

    All three demos sample at ANALYZER_SAMPLE_RATE_HZ (500 MHz / 2 ns) — a real analyzer's
    fixed max, NOT a multiple of the SWI3S UI — so the number of samples per UI is
    NON-integer and edges land on a jittered grid. A caller can pin a specific
    ``sample_rate_hz`` (+ ``samples_per_ui``) to override this, e.g. for the fine-eye tests."""
    pinned = sample_rate_hz != DEFAULT_SAMPLE_RATE_HZ     # caller chose a custom rate/eye
    rate = sample_rate_hz if pinned else ANALYZER_SAMPLE_RATE_HZ
    if variant == "flow_control":
        # PHY2 framing (FBCSE, 16 columns): same edge builder as make_demo_levels.
        levels = swi3score.make_demo_levels_flow_control(audio_samples_per_channel)
        spu = samples_per_ui if pinned else rate / FBCSE_UI_RATE_HZ
        cap = build_capture_from_levels(levels, spu, rate)
        return prepend_cold_start(cap, phy_number=2) if cold_start else cap
    if int(phy) == 3:
        d = swi3score.make_demo_levels_phy3(audio_samples_per_channel)
        # DLV: constant row period across the Safe-Lock-4 -> 16 commit, so the bit clock
        # rises 4x while the row rate holds at 3.072 MRows/s. The builder's samples_per_ui
        # sizes the OPERATIONAL UI (row_period / 16 columns) at the analyzer rate.
        spu = samples_per_ui if pinned else (rate / DLV_ROW_RATE_HZ) / 16.0
        cap = build_dlv_capture_from_levels(d["levels"], d["row_columns"], spu, rate)
        return prepend_cold_start(cap, phy_number=3) if cold_start else cap
    if int(phy) == 1:
        levels = swi3score.make_demo_levels_phy1(audio_samples_per_channel)
        # Slow FBCSE: 4-column bus at 1.536 MRows/s -> 6.144 MHz UI.
        spu = samples_per_ui if pinned else rate / PHY1_UI_RATE_HZ
        cap = build_capture_from_levels(levels, spu, rate)
        return prepend_cold_start(cap, phy_number=1) if cold_start else cap
    levels = swi3score.make_demo_levels(audio_samples_per_channel)
    spu = samples_per_ui if pinned else rate / FBCSE_UI_RATE_HZ   # 24.576 MHz PHY2 UI
    cap = build_capture_from_levels(levels, spu, rate)
    return prepend_cold_start(cap, phy_number=2) if cold_start else cap
