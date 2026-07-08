"""Cold/Warm Start Link-Control sequence decoder (spec §5.1.2).

Before audio mode the Manager drives the two link pins — DP (the forwarded
clock) and DN (data) — with single-ended Link-Control signaling to bring
Peripherals out of an inactive state. **PHY selection happens HERE, on the raw
wire** — it is *not* on the Control Data Stream, *not* in the command transport,
and *not* in any register (there is no ``ActivePHY`` field in the SLC map). So
the one place an analyzer can recover which PHY (PHY1/PHY2/PHY3) is active is by
decoding this pre-audio waveform from the two transition-edge lists; that is what
this module does. (The CDS decoder only starts once audio mode begins, locking on
the K.28.7 SPM — everything before that is what we recover here.)

The three Manager start sequences are told apart purely by the duration of the
initial DP=high pulse (spec §5.1.2.1):

    Bus Reset (Cold Start)   DP high ≳ 500 µs   (Man_tReset10 ≥ 1569 µs)
    Warm Start               DP high ~150–500µs (Man_tWarmStart10 436–482 µs)
    LC_Acknowledge / request DP high < ~150 µs

On a **Cold Start** the Manager then clocks a 4-bit PHY number on DN — sampled
**MSb-first on DP falling edges** — then drives a PhyStart falling edge that hands
off to the selected audio PHY. A **Warm Start** carries **no** PHY number: the
previously-selected PHY is reused (so a decoder must carry it over).

All thresholds and the PHY-number→type map are draft-sourced (MIPI SWI3S v1.1
r06) and kept as table-driven constants — notably the PHY-number *encoding*
(Table 9) is still ``###TBD`` in the draft, so the value→PHY map is a best-effort
1-based literal and easy to revise against the adopted spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from ..nputil import searchsorted as _ss

# ---- thresholds (µs), from Tables 17/24 + §5.1.2.1 decision boundaries ----
# Decision boundaries chosen inside the guaranteed Manager/Peripheral gaps:
ACK_VS_WARM_US = 150.0      # < this DP-high pulse = LC_Ack/Request (ignored here)
WARM_VS_COLD_US = 500.0     # [150,500) = Warm Start; ≥ 500 = Cold Start bus reset
# After PhyStart the Manager holds DP low (Man_tPhyStart 64–96 µs) with NO further
# rising edge, vs a normal inter-bit clock-low of ≤32 µs — so a DP-low longer than
# anything in (32, 64) µs that ends the clock burst marks PhyStart.
PHYSTART_LOW_MIN_US = 48.0
MAN_TPHYSTART_US = 80.0     # nominal DP-low after PhyStart before audio mode
# PHY-number clock pulse bound: Man_tClock1/0 are 5–32 µs. A pulse ≤ this is a
# clock-burst pulse; longer pulses are the Bus Reset (>500 µs), the inter-reset /
# recovery lows (≥70 µs), or the PhyStart low (64–96 µs). 50 µs sits in the gap
# above the 32 µs clock max and below the 64 µs recovery/PhyStart floor — so it
# cleanly separates the PHY-number burst from the reset structure around it.
CLOCK_PULSE_MAX_US = 50.0
# Edges closer than this are a sub-sample capture glitch, not a real LC pulse (the
# shortest real pulse — an audio-mode CDS half-bit — is ~2.6 µs); coalesced away
# so a glitch can't add a spurious PHY-number bit.
GLITCH_MAX_US = 0.5

PHY_NUMBER_BITS = 4         # normative 4-bit PHY number, MSb first

# PHY number → (name, kind). Encoding is TBD in the draft (Table 9 is ###TBD);
# the names/order (#1 FBCSE-slow, #2 FBCSE-fast, #3 DLV) are stable, so default to
# a 1-based literal map. Revise here when the spec fixes the encoding.
PHY_NUMBER_MAP = {
    0b0001: ("PHY1", "FBCSE-slow"),
    0b0010: ("PHY2", "FBCSE-fast"),
    0b0011: ("PHY3", "DLV"),
}

# After PhyStart the bus enters audio mode in a Safe-Lock pattern whose column
# count is fixed BY THE SELECTED PHY (not decoded from framing): the FBCSE PHYs
# (PHY1/PHY2) start in Safe-Lock-2 (2 columns), DLV (PHY3) in Safe-Lock-4 (4
# columns). The bus then reconfigures to its operational geometry.
PHY_SAFE_LOCK_COLUMNS = {"PHY1": 2, "PHY2": 2, "PHY3": 4}

# Manager Link-Control timing limits — Table 24, §5.2.3 (MIPI SWI3S v1.1 r06).
# (param -> (min_us, max_us, description)); None = no bound on that side. The Link
# Control Timing pane measures each observable parameter and flags it against these.
LC_TIMING_LIMITS = {
    "Man_tReset00":        (212.0, 236.0, "Bus Reset part 1 — DP=0,DN=0"),
    "Man_tReset10":        (1569.0, None, "Bus Reset part 2 — DP=1,DN=0 (rec max 2000)"),
    "Man_tResetRecovery":  (72.5, 80.0,  "Idle DP=0,DN=0 after reset, before PHY clock"),
    "Man_tClock1":         (5.0, 32.0,   "PHY-number clock high (per cycle)"),
    "Man_tClock0":         (5.0, 32.0,   "PHY-number clock low (per cycle)"),
    "Man_tWarmStart10":    (436.0, 482.0, "Warm Start pulse — DP=1,DN=0"),
    "Man_tPhyStart":       (64.0, 96.0,  "Final DP falling edge → audio-mode PHY"),
}
MAN_TRESET10_REC_MAX_US = 2000.0     # recommended (not mandatory) upper bound


def _timing_row(param: str, measured_us: float) -> dict:
    """One Link-Control timing measurement vs its §5.2.3 [min,max] limit. `ok` is
    True/False when a bound is exceeded, or None when the parameter has no bound on
    the relevant side (still shown, just not pass/failed)."""
    lo, hi, desc = LC_TIMING_LIMITS.get(param, (None, None, ""))
    ok = True
    if lo is not None and measured_us < lo:
        ok = False
    if hi is not None and measured_us > hi:
        ok = False
    if lo is None and hi is None:
        ok = None
    return {"param": param, "measured_us": float(measured_us),
            "min_us": lo, "max_us": hi, "ok": ok, "desc": desc}


@dataclass
class LinkControlResult:
    """The decoded pre-audio link bring-up. ``sequence`` is 'cold' | 'warm' |
    'none' (no observable bring-up — e.g. a capture that starts mid-stream in
    audio mode). Sample fields are absolute capture sample numbers, or None when
    not applicable to the detected sequence."""
    sequence: str = "none"
    phy_number: Optional[int] = None
    phy_name: Optional[str] = None          # 'PHY1' | 'PHY2' | 'PHY3'
    phy_kind: Optional[str] = None          # 'FBCSE-slow' | 'FBCSE-fast' | 'DLV'
    bus_reset_sample: Optional[int] = None
    phy_select_sample: Optional[int] = None  # first PHY-number bit (DP falling)
    phystart_sample: Optional[int] = None
    audio_start_sample: Optional[int] = None
    safe_lock_columns: Optional[int] = None  # initial column count, fixed by the PHY
    ext_bits: Optional[int] = None           # provisional draft extension (rising edges)
    lc_on_data_line: bool = False            # True ⇒ the LC clock (DP) is the capture's
    #                                          data line (audio clock = DN on the clock line)
    note: str = ""
    # Named, timed sub-phases for the timeline band (Idle / Bus Reset / Cold Start /
    # Warm Start / LC_Request / PhyStart), each {"name", "start", "end"} in samples.
    sections: List[dict] = field(default_factory=list)
    # Measured LC timing parameters vs §5.2.3 (see _timing_row): {"param","measured_us",
    # "min_us","max_us","ok","desc"} — populated for the observable parameters.
    timing: List[dict] = field(default_factory=list)

    @property
    def phy_known(self) -> bool:
        return self.phy_name is not None

    def label(self) -> str:
        """Short human label for the status bar / grid overlay."""
        if self.sequence == "none":
            return "No PHY selected (no link bring-up in capture)"
        if self.phy_name:
            sl = f", Safe-Lock-{self.safe_lock_columns}" if self.safe_lock_columns else ""
            return f"{self.phy_name} selected ({self.phy_kind}{sl})"
        if self.sequence == "warm":
            return "Warm Start — PHY reused (not captured)"
        return "PHY selected (number unrecognized)"


def _level_at(edges: np.ndarray, initial: bool, sample: int) -> bool:
    """Logic level of a line at `sample`: `initial` XORed with the parity of the
    number of transitions at or before `sample` (each edge toggles the level)."""
    n = int(_ss(edges, sample, side="right"))
    return bool(initial) ^ bool(n & 1)


def _coalesce_edges(edges: np.ndarray, min_gap: int = 0) -> np.ndarray:
    """Collapse transitions closer together than `min_gap` samples — sub-sample
    glitches in a real capture (a real LC pulse is ≥ ~2.6 µs, so any cluster of
    edges within a fraction of a µs is spurious). Each cluster of k toggles is k
    net toggles, so an even cluster cancels (drop all) and an odd one leaves a
    single edge (kept at the cluster's first sample). The result strictly
    alternates rising/falling at well-separated samples, which the structure logic
    below (rising/falling by index parity) relies on — without this, a glitch pair
    shifts the parity and mis-aligns the PHY-number bits (off-by-one → wrong PHY).
    `min_gap <= 0` collapses only exactly-coincident edges."""
    if edges.size == 0:
        return edges
    e = np.sort(edges.astype(np.int64))
    if min_gap <= 0:
        vals, counts = np.unique(e, return_counts=True)
        return np.ascontiguousarray(vals[counts % 2 == 1], dtype=np.uint64)
    cut = np.diff(e) > min_gap                     # cluster boundary where gap is real
    starts = np.concatenate(([0], np.nonzero(cut)[0] + 1))
    ends = np.concatenate((np.nonzero(cut)[0] + 1, [e.size]))
    keep = [int(e[s]) for s, en in zip(starts, ends) if (en - s) % 2 == 1]
    return np.ascontiguousarray(keep, dtype=np.uint64)


def decode_link_control(capture) -> LinkControlResult:
    """Recover the §5.1.2 Cold/Warm Start sequence from a :class:`Capture`.

    **Self-orienting.** The forwarded clock in FBCSE is on DP during link control
    but moves to DN for audio mode, so a capture stored in its audio orientation
    (clock_edges = the audio clock = DN) has its *link-control* clock on the data
    line. We therefore try the bring-up decode on BOTH physical lines as the LC
    clock and return whichever carries the sequence; ``sequence == 'none'`` only
    when neither does (a mid-stream capture with no bring-up)."""
    rate = int(capture.sample_rate_hz) or 1
    a = _decode_oriented(capture.clock_edges, capture.initial_clock,
                         capture.data_edges, capture.initial_data, rate)
    if a.sequence != "none":
        return a                                   # LC clock (DP) is the clock line
    b = _decode_oriented(capture.data_edges, capture.initial_data,
                         capture.clock_edges, capture.initial_clock, rate)
    if b.sequence != "none":
        b.lc_on_data_line = True                   # LC clock (DP) is the DATA line
        return b
    return a


def _decode_oriented(clock_edges, initial_clock, data_edges, initial_data,
                     rate: int) -> LinkControlResult:
    """Decode the bring-up treating `clock_edges` as DP (the LC clock) and
    `data_edges` as DN (where the PHY number is clocked)."""
    rate = int(rate) or 1
    spp = rate / 1e6                              # samples per microsecond
    clk = _coalesce_edges(np.asarray(clock_edges, dtype=np.uint64),
                          min_gap=max(1, int(GLITCH_MAX_US * spp)))
    dat = np.asarray(data_edges, dtype=np.uint64)
    if clk.size < 2:
        return LinkControlResult(sequence="none")

    # On the coalesced clock, edges strictly alternate, so rising/falling is pure
    # index parity: with DP low before edge 0, even indices rise and odd fall.
    invert = bool(initial_clock)                  # True ⇒ DP high before edge 0
    gaps = np.diff(clk.astype(np.int64))          # duration DP holds each level
    first_rise_idx = 1 if invert else 0

    seq_idx = None
    seq_kind = None
    for i in range(first_rise_idx, gaps.size, 2):   # step over rising edges only
        dur_us = gaps[i] / spp
        if dur_us >= WARM_VS_COLD_US:
            seq_idx, seq_kind = i, "cold"
            break
        if dur_us >= ACK_VS_WARM_US:
            seq_idx, seq_kind = i, "warm"
            break
        # shorter pulses are LC_Ack/Request — keep scanning for the real start.

    if seq_idx is None:
        return LinkControlResult(sequence="none")

    # The start pulse needs its own falling edge to define PhyStart; if it's the
    # very last edge in the capture there's nothing after it to measure.
    if seq_idx + 1 >= clk.size:
        return LinkControlResult(sequence="none",
                                 note="Start pulse at the final clock edge — capture ends before PhyStart.")

    rise_sample = int(clk[seq_idx])
    fall_sample = int(clk[seq_idx + 1])

    if seq_kind == "warm":
        # No PHY-number transfer; the warm pulse's own falling edge is PhyStart.
        audio = fall_sample + int(round(MAN_TPHYSTART_US * spp))
        sections = []
        if rise_sample > 0:
            sections.append({"name": "Idle", "start": 0, "end": rise_sample})
        sections.append({"name": "Warm Start", "start": rise_sample, "end": fall_sample})
        sections.append({"name": "PhyStart", "start": fall_sample, "end": audio})
        return LinkControlResult(
            sequence="warm",
            phystart_sample=fall_sample,
            audio_start_sample=audio,
            note="Warm Start: PHY reused from the prior Cold Start (not in this capture).",
            sections=sections,
            timing=[_timing_row("Man_tWarmStart10", (fall_sample - rise_sample) / spp)],
        )

    # ---- Cold Start: reset structure → PHY-number clock burst → PhyStart ----
    # The reset structure is variable (one or more long DP-high Bus-Reset pulses,
    # inter-reset lows, and a recovery low — all ≥ ~70 µs, some > 1 ms). Rather than
    # assume a fixed shape, skip every long pulse and land on the PHY-number CLOCK
    # BURST: the first run of medium (≤ CLOCK_PULSE_MAX) pulses. Then sample DN on
    # each falling edge (MSb first) until a falling edge is followed by a DP-low
    # longer than PHYSTART_LOW_MIN — that edge is PhyStart, ending the burst.
    bus_reset_sample = rise_sample
    phystart_low_min = PHYSTART_LOW_MIN_US * spp
    clock_pulse_max = CLOCK_PULSE_MAX_US * spp
    burst_start = seq_idx
    while burst_start < gaps.size and gaps[burst_start] > clock_pulse_max:
        burst_start += 1                          # skip Bus-Reset / recovery pulses

    bits: List[int] = []
    ext_bits: List[int] = []
    phystart_sample = None
    phy_select_sample = None
    clock_highs: List[float] = []      # DP=1 per-cycle durations (Man_tClock1), µs
    clock_lows: List[float] = []       # DP=0 per-cycle durations (Man_tClock0), µs
    for j in range(burst_start, clk.size):
        is_falling = invert ^ ((j % 2) == 1)      # level-before-edge high ⇒ falling
        edge = int(clk[j])
        low_after = (int(clk[j + 1]) - edge) if (j + 1) < clk.size else None
        hold_us = (gaps[j] / spp) if j < gaps.size else None    # duration held after this edge
        if is_falling:
            # PhyStart? (burst-terminating falling edge with a long DP-low after).
            if low_after is None or low_after > phystart_low_min:
                phystart_sample = edge
                break
            if hold_us is not None:
                clock_lows.append(hold_us)          # this falling edge starts a DP=0 half-cycle
            # otherwise this falling edge clocks one PHY-number bit (MSb first)
            if len(bits) < PHY_NUMBER_BITS:
                if phy_select_sample is None:
                    phy_select_sample = edge
                bits.append(1 if _level_at(dat, initial_data, edge) else 0)
        else:
            if hold_us is not None:
                clock_highs.append(hold_us)         # rising edge starts a DP=1 half-cycle
            # rising edge — provisional draft extension bits (Ext4..Ext0/ExtEn)
            ext_bits.append(1 if _level_at(dat, initial_data, edge) else 0)

    phy_number = None
    for b in bits[:PHY_NUMBER_BITS]:
        phy_number = ((phy_number or 0) << 1) | b   # MSb first
    if not bits:
        phy_number = None

    name_kind = PHY_NUMBER_MAP.get(phy_number) if phy_number is not None else None
    phy_name, phy_kind = name_kind if name_kind else (None, None)
    ext_val = None
    if ext_bits:
        ext_val = 0
        for b in ext_bits:
            ext_val = (ext_val << 1) | b

    audio_start = (phystart_sample + int(round(MAN_TPHYSTART_US * spp))
                   if phystart_sample is not None else None)
    note = ""
    if phy_number is not None and name_kind is None:
        note = (f"PHY number 0x{phy_number:X} not in the (draft/TBD) PHY-number map "
                f"— encoding is ###TBD in the spec.")

    # ---- Named sections (timeline) + measured timing (§5.2.3) ----
    lc00_start = int(clk[seq_idx - 1]) if seq_idx > 0 else rise_sample
    reset_end = fall_sample                                  # end of the LC:10 pulse
    cold_end = phystart_sample if phystart_sample is not None else int(clk[-1])
    sections = []
    if lc00_start > 0:
        sections.append({"name": "Idle", "start": 0, "end": lc00_start})
    sections.append({"name": "Bus Reset", "start": lc00_start, "end": reset_end})
    if cold_end > reset_end:
        sections.append({"name": "Cold Start", "start": reset_end, "end": cold_end})
    if phystart_sample is not None and audio_start is not None:
        sections.append({"name": "PhyStart", "start": phystart_sample, "end": audio_start})

    timing = []
    if seq_idx > 0:                                          # the LC:00 low before the reset high
        timing.append(_timing_row("Man_tReset00", gaps[seq_idx - 1] / spp))
    timing.append(_timing_row("Man_tReset10", (fall_sample - rise_sample) / spp))
    # Recovery low: the DP=0 pulse immediately before the clock burst (falling-edge-led).
    if 0 <= burst_start - 1 < gaps.size and (invert ^ ((burst_start - 1) % 2 == 1)):
        timing.append(_timing_row("Man_tResetRecovery", gaps[burst_start - 1] / spp))
    # PHY-number clock: report the worst-case (nearest a limit) high/low over the burst,
    # pass/failed across every cycle; the range is in the description.
    timing += _clock_timing_rows(clock_highs, clock_lows)

    return LinkControlResult(
        sequence="cold",
        phy_number=phy_number,
        phy_name=phy_name,
        phy_kind=phy_kind,
        safe_lock_columns=PHY_SAFE_LOCK_COLUMNS.get(phy_name) if phy_name else None,
        bus_reset_sample=bus_reset_sample,
        phy_select_sample=phy_select_sample,
        phystart_sample=phystart_sample,
        audio_start_sample=audio_start,
        ext_bits=ext_val,
        note=note,
        sections=sections,
        timing=timing,
    )


def _clock_timing_rows(highs: List[float], lows: List[float]) -> List[dict]:
    """Man_tClock1/Man_tClock0 rows from the per-cycle high/low durations (µs). Each
    reports the cycle NEAREST a limit as the measured value, passes only if EVERY
    cycle is within [5, 32] µs, and carries the observed range + cycle count in its
    description."""
    rows = []
    for param, vals in (("Man_tClock1", highs), ("Man_tClock0", lows)):
        if not vals:
            continue
        lo, hi = min(vals), max(vals)
        b_lo, b_hi, _desc = LC_TIMING_LIMITS[param]
        # Representative value = the extreme closest to (or past) a bound.
        worst = hi if (b_hi is not None and hi > b_hi) else (
            lo if (b_lo is not None and lo < b_lo) else hi)
        row = _timing_row(param, worst)
        row["ok"] = all((b_lo is None or v >= b_lo) and (b_hi is None or v <= b_hi)
                        for v in vals)
        row["desc"] = f"{row['desc']} — {len(vals)} cycles, {lo:.1f}–{hi:.1f} µs"
        rows.append(row)
    return rows
