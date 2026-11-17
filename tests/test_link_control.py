"""Link-Control (§5.1.2) decoder tests: Cold/Warm Start classification + PHY-number
recovery from synthetic DP/DN transition edges.

Run: PYTHONPATH=. python3 -m pytest tests/test_link_control.py
"""
import numpy as np

from swi3s_studio.analysis import decode_link_control
from swi3s_studio.analysis.link_control import LC_TIMING_LIMITS
from swi3s_studio.ingest import transitions
from swi3s_studio.ingest.capture import Capture

RATE = transitions.DEFAULT_SAMPLE_RATE_HZ


def test_cold_start_recovers_phy_number():
    for phy, name, kind, safelock in [(1, "PHY1", "FBCSE-slow", 2),
                                       (2, "PHY2", "FBCSE-fast", 2),
                                       (3, "PHY3", "DLV", 4)]:
        clk, dat, end, end_data = transitions.cold_start_edges(phy, RATE)
        cap = Capture(clock_edges=clk, data_edges=dat,
                      initial_clock=False, initial_data=False, sample_rate_hz=RATE)
        r = decode_link_control(cap)
        assert r.sequence == "cold", (phy, r.sequence)
        assert r.phy_number == phy, (phy, r.phy_number)
        assert r.phy_name == name and r.phy_kind == kind
        # Initial Safe-Lock column count is fixed by the PHY (FBCSE → 2, DLV → 4).
        assert r.safe_lock_columns == safelock, (name, r.safe_lock_columns)
        assert r.bus_reset_sample is not None and r.bus_reset_sample < r.phy_select_sample
        assert r.phystart_sample is not None
        # audio starts after PhyStart, at the end of the synthesized preamble.
        assert r.audio_start_sample > r.phystart_sample
        assert r.audio_start_sample == end


def test_warm_start_has_no_phy_number():
    # A lone Warm-Start DP-high pulse (~460 µs) then PhyStart low.
    spp = RATE / 1e6
    us = lambda x: int(round(x * spp))
    t = us(50)
    clk = [t]; t += us(460)        # rising → warm-start high
    clk.append(t); t += us(200)    # falling = PhyStart → long low
    clk.append(t)                  # a later edge (audio-ish) so low is bounded
    cap = Capture(clock_edges=np.asarray(clk, np.uint64),
                  data_edges=np.asarray([], np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    r = decode_link_control(cap)
    assert r.sequence == "warm", r.sequence
    assert r.phy_number is None and r.phy_name is None
    assert r.phystart_sample is not None and r.audio_start_sample > r.phystart_sample


def test_audio_only_capture_is_none():
    # A capture that is pure audio-rate clocking — a uniform forwarded clock with no Bus
    # Reset / PHY-select / Warm-Start pulse — has no observable bring-up. (The demo
    # without a cold start still reads as a bring-up to THIS edge-only detector; the
    # Session overrules it from the decode, see the tests at the end. So build the
    # audio-only case explicitly, like the synthetic warm/cold cases above.)
    clk = np.arange(1, 4000, 4, dtype=np.uint64)      # uniform UI-rate clock, short pulses
    cap = Capture(clock_edges=clk, data_edges=np.asarray([], np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    r = decode_link_control(cap)
    assert r.sequence == "none"
    assert not r.phy_known
    assert "No PHY selected" in r.label()


def test_prepend_cold_start_then_decode_and_still_decodes_audio():
    from swi3s_studio.session import Session
    base = transitions.demo_capture(64)
    spliced = transitions.prepend_cold_start(base, phy_number=2)
    r = decode_link_control(spliced)
    assert r.sequence == "cold" and r.phy_name == "PHY2"
    # The audio region after the preamble must still decode to the same commands.
    plain = Session(base)
    bringup = Session(spliced)
    assert len(bringup.commands) == len(plain.commands), "splice broke audio decode"
    assert bringup.column_count == plain.column_count


def test_real_world_cold_start_quirks():
    """A cold start with TWO long Bus-Reset highs (not one), a >48 µs recovery low,
    and a sub-sample glitch edge in the PHY-number burst still yields the right
    PhyStart and PHY number. DN here has a single ~10 µs high over the 3rd of 4 bit
    falling edges → 0b0010 = PHY2. Sample rate 250 MHz (250 samples/µs)."""
    import numpy as np
    spp = 250.0
    us = lambda x: int(round(x * spp))

    clk, t = [], us(50)
    clk += [t]; t += us(1600)       # Bus Reset high #1
    clk += [t]; t += us(305)        # inter-reset low
    clk += [t]; t += us(1600)       # Bus Reset high #2
    clk += [t]; t += us(75)         # recovery low (> PhyStart gap — must not trip)
    # 4 clock cycles (bits 3..0). Insert a sub-sample glitch (a +1-sample pip) inside
    # the 2nd low.
    bit_edges = []
    for k in range(4):
        clk += [t]; t += us(30)     # rising
        clk += [t]; bit_edges.append(t); t += us(30)   # falling (bit sampled)
        if k == 1:                  # glitch: a 1-sample pip strictly inside the low
            pip = bit_edges[-1] + us(5)
            clk += [pip, pip + 1]   # must be coalesced away
    clk += [t]; t += us(30)         # final clock high
    clk += [t]; t += us(80)         # PhyStart falling → long low
    # DN: a single high pulse covering ONLY the 3rd bit falling edge (value 0b0010).
    third = bit_edges[2]
    dat = [third - us(5), third + us(5)]

    edges = np.asarray(sorted(clk), np.uint64)
    assert np.all(np.diff(edges) > 0), "clock edges must be strictly ascending"
    cap = Capture(clock_edges=edges,
                  data_edges=np.asarray(dat, np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=250_000_000)
    r = decode_link_control(cap)
    assert r.sequence == "cold", r.sequence
    assert r.phy_number == 2 and r.phy_name == "PHY2", (r.phy_number, r.phy_name)


def test_self_orients_when_bringup_is_on_the_data_line():
    """In FBCSE the forwarded clock moves to DN for audio mode, so a capture stored
    in its audio orientation has the §5.1.2 bring-up on the DATA line, not the clock
    line. decode_link_control must self-orient and still find it. Build a cold-start
    capture, then SWAP clock/data and assert it still decodes PHY2."""
    clk, dat, end, _ = transitions.cold_start_edges(2, RATE)
    swapped = Capture(clock_edges=dat, data_edges=clk,        # bring-up now on 'data'
                      initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    r = decode_link_control(swapped)
    assert r.sequence == "cold" and r.phy_name == "PHY2", (r.sequence, r.phy_name)


def test_cold_start_emits_sections_and_timing():
    """A cold start yields named timeline sections (Idle → Bus Reset → Cold Start →
    PhyStart) and a §5.2.3 timing table with the observable Manager parameters, each
    flagged pass/fail against its limit (the synthetic cold start is in-spec)."""
    clk, dat, _end, _ed = transitions.cold_start_edges(2, RATE)
    cap = Capture(clock_edges=clk, data_edges=dat,
                  initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    r = decode_link_control(cap)
    names = [s["name"] for s in r.sections]
    assert names == ["Idle", "Bus Reset", "Cold Start", "PhyStart"], names
    # Sections are contiguous and ascending.
    for a, b in zip(r.sections, r.sections[1:]):
        assert a["end"] == b["start"] and a["start"] < a["end"], (a, b)
    params = {t["param"]: t for t in r.timing}
    assert "Man_tReset10" in params and "Man_tResetRecovery" in params
    assert "Man_tClock1" in params and "Man_tClock0" in params
    assert params["Man_tReset10"]["measured_us"] >= 1569 and params["Man_tReset10"]["ok"]
    assert all(t["ok"] for t in r.timing), [(t["param"], t["measured_us"]) for t in r.timing]


# ---- the Session overrules a bring-up the decode contradicts ----
#
# The detector above reads only the edges, and a capture with NO bring-up can satisfy it.
# A real bring-up precedes audio mode, so no CRC-valid command decodes inside one; the
# Session drops any detection with such commands before its audio start. These pin both
# directions: every phantom is dropped, every genuine bring-up is kept.

import pytest  # noqa: E402

from swi3s_studio.session import Session  # noqa: E402


def _joined_late(cut: int = 1_500_000) -> Session:
    """The PHY2 cold-start demo as a recording started 3 ms in, after audio began: what a
    real mid-stream capture looks like."""
    full = transitions.demo_capture(300, cold_start=True, phy=2)
    ce = full.clock_edges[full.clock_edges >= cut] - np.uint64(cut)
    de = full.data_edges[full.data_edges >= cut] - np.uint64(cut)
    level_c = bool(full.initial_clock) ^ bool(int(np.sum(full.clock_edges < cut)) % 2)
    level_d = bool(full.initial_data) ^ bool(int(np.sum(full.data_edges < cut)) % 2)
    return Session(Capture(ce, de, level_c, level_d, full.sample_rate_hz),
                   source={"type": "fixture"})


@pytest.mark.parametrize("phy, variant, edges_fooled",
                         [(1, "", True), (2, "", True), (2, "flow_control", False)])
def test_a_capture_without_a_bringup_has_none(phy, variant, edges_fooled):
    """With or without an edge-only phantom, the Session reports no bring-up; when it
    drops a phantom, the note says why."""
    s = Session.from_demo(300, phy=phy, variant=variant)
    assert (decode_link_control(s.capture).sequence != "none") == edges_fooled
    lc = s.link_control
    assert not s.has_bringup and lc.sequence == "none"
    assert not lc.lc_on_data_line and lc.sections == [] and s.audio_start_sample == 0
    assert ("CRC-valid command" in lc.note) == edges_fooled


def test_a_capture_joined_after_audio_began_has_no_bringup():
    """The edge-only detector reads a Cold Start that never reaches audio into the
    recording; a CRC-valid command decodes inside its phases, so the Session drops it."""
    s = _joined_late()
    phantom = decode_link_control(s.capture)
    assert phantom.sequence == "cold" and phantom.audio_start_sample is None
    assert not s.has_bringup and not s.link_control.lc_on_data_line


@pytest.mark.parametrize("cut, evidence", [(1_800_000, "a CRC-valid command at sample"),
                                           (2_000_000, "clock edges")])
def test_a_warm_start_the_link_was_in_audio_mode_up_to_is_dropped(cut, evidence):
    """Joined later, the edge-only detector reads a Warm Start that fits between two Pings
    and reaches audio, with no command inside it. A Warm Start restarts a Link that went
    inactive, and the Link was in audio mode up to the pulse with no clock stop between: a
    CRC-valid command (at 1.8 M) or the clock running at audio rate (at 2.0 M) before it."""
    s = _joined_late(cut)
    phantom = decode_link_control(s.capture)
    assert phantom.sequence == "warm" and phantom.audio_start_sample is not None
    assert not s.has_bringup and s.link_control.sequence == "none"
    assert "audio mode up to it" in s.link_control.note and evidence in s.link_control.note


def test_a_warm_start_after_a_clock_stop_is_kept():
    """The same recording with the clock stopped for longer than LINK_IDLE_MIN_US before the
    Warm Start pulse: the Link went inactive there, so the Warm Start is kept."""
    from swi3s_studio.analysis.link_control import LINK_IDLE_MIN_US
    cap = _joined_late(2_000_000).capture
    rise = next(int(x["start"]) for x in decode_link_control(cap).sections
                if x["name"] == "Warm Start")
    gap = np.uint64(int(1.2 * LINK_IDLE_MIN_US * cap.sample_rate_hz / 1e6))

    def stopped(edges):
        return np.where(edges >= np.uint64(rise), edges + gap, edges)
    s = Session(Capture(stopped(cap.clock_edges), stopped(cap.data_edges), cap.initial_clock,
                        cap.initial_data, cap.sample_rate_hz), source={"type": "fixture"})
    assert s.has_bringup and s.link_control.sequence == "warm"
    assert s.link_control.audio_start_sample > rise + int(gap)


@pytest.mark.parametrize("phy, variant", [(1, ""), (2, ""), (3, ""), (2, "flow_control")])
def test_every_genuine_bringup_is_kept(phy, variant):
    s = Session.from_demo(300, phy=phy, variant=variant, cold_start=True)
    assert s.has_bringup and s.link_control.phy_name == f"PHY{phy}"
    assert s.link_control.note == ""


def _phantom(cols):
    from swi3s_studio.analysis.link_control import LinkControlResult
    return LinkControlResult(sequence="cold", phy_number=1, phy_name="PHY1",
                             phy_kind="FBCSE-slow", safe_lock_columns=cols,
                             audio_start_sample=2_000_000,
                             sections=[{"name": "Bus Reset", "start": 10, "end": 2_000_000}])


def _same_decode(a, b):
    assert [c["start_sample"] for c in a.commands] == [c["start_sample"] for c in b.commands]
    assert [(g["start_sample"], g["column_count"]) for g in a.segments] == \
        [(g["start_sample"], g["column_count"]) for g in b.segments]


@pytest.mark.parametrize("cols, why", [
    (8, "steered, with valid commands left inside it: rejected, decoded again"),
    (4, "steered into NO valid command, which leaves nothing to contradict it"),
])
def test_a_phantom_that_named_a_phy_does_not_steer_the_decode(monkeypatch, cols, why):
    """A phantom naming a PHY seeds the decoder with its Safe-Lock width, on a capture that
    opens in 2 columns. Seeded at 8 it still decodes 18 valid commands inside the phantom;
    seeded at 4 it decodes none at all. Either way the result must be the decode of a
    capture with no bring-up."""
    from swi3s_studio import session as session_mod
    clean = Session.from_demo(300, phy=2)
    monkeypatch.setattr(session_mod, "decode_link_control", lambda cap: _phantom(cols))
    s = Session.from_demo(300, phy=2)
    assert not s.has_bringup, why
    _same_decode(s, clean)


def test_a_steered_detection_without_evidence_either_way_is_kept(monkeypatch):
    """No valid command with it or without it: nothing contradicts the detection, so it
    stands, and the decode is its steered one."""
    from swi3s_studio import session as session_mod
    noise = Capture(np.arange(1, 4000, 4, dtype=np.uint64), np.asarray([], np.uint64),
                    False, False, RATE)
    monkeypatch.setattr(session_mod, "decode_link_control", lambda cap: _phantom(4))
    s = Session(noise, source={"type": "fixture"})
    assert s.has_bringup and s.link_control.safe_lock_columns == 4


def test_traffic_before_a_bus_reset_does_not_reject_it():
    """A bus that was running and then reset: commands BEFORE the Bus Reset are real, and
    the window starts at the first non-Idle phase."""
    s = Session.from_demo(300, phy=2, cold_start=True)
    lc = s.link_control
    first = next(sec for sec in lc.sections if sec["name"] != "Idle")
    s.commands = [{"crc_valid": True, "start_sample": max(0, first["start"] - 1)}] + s.commands
    assert s._valid_commands_inside(lc) == 0


def test_the_window_rejects_one_valid_command_inside_it():
    s = Session.from_demo(300, phy=2, cold_start=True)
    lc = s.link_control
    s.commands = [{"crc_valid": True, "start_sample": (lc.audio_start_sample or 0) - 1},
                  {"crc_valid": False, "start_sample": (lc.audio_start_sample or 0) - 2}]
    assert s._valid_commands_inside(lc) == 1             # the CRC-valid one only


def test_rate_regions_need_no_guard_against_the_phantom():
    """The no-bring-up PHY2 demo's first measured region starts at its first decode
    segment (its 2-column opening), not at a phantom's audio start."""
    s = Session.from_demo(300, phy=2)
    regions = [r for r in s.rate_regions() if r[2] is not None]
    assert regions[0][0] == int(s.segments[0]["start_sample"])
    assert regions[0][3] == pytest.approx(12_288_000, rel=1e-3)   # 2 columns at 24.576 MHz


def test_a_clock_row_reports_the_cycle_nearest_a_limit():
    from swi3s_studio.analysis.link_control import _clock_timing_rows
    rows = {r["param"]: r for r in _clock_timing_rows([5.1, 20.0], [20.0, 31.9])}
    assert rows["Man_tClock1"]["measured_us"] == 5.1       # 0.1 us above the 5 us minimum
    assert rows["Man_tClock0"]["measured_us"] == 31.9      # 0.1 us below the 32 us maximum
    assert rows["Man_tClock1"]["ok"] is True
    broken = {r["param"]: r for r in _clock_timing_rows([4.0, 33.0], [4.5])}
    assert broken["Man_tClock1"]["measured_us"] == 33.0    # equal overshoot: the longer cycle
    assert broken["Man_tClock1"]["ok"] is False
    assert broken["Man_tClock0"]["measured_us"] == 4.5


def test_a_limit_with_no_maximum_has_none_to_exceed():
    """The bounds themselves are pinned by test_a_timing_limit_includes_both_of_its_bounds; a
    limit with no maximum passes however long the measurement."""
    from swi3s_studio.analysis.link_control import _timing_row
    assert LC_TIMING_LIMITS["Man_tReset10"][1] is None
    assert _timing_row("Man_tReset10", 5000.0)["ok"] is True
    assert _timing_row("Man_tReset10", 1e9)["ok"] is True


def test_a_parameter_with_no_bound_is_shown_and_not_judged(monkeypatch):
    from swi3s_studio.analysis import link_control
    from swi3s_studio.analysis.link_control import _timing_row
    assert _timing_row("Not_aParameter", 12.0)["ok"] is None
    monkeypatch.setitem(link_control.LC_TIMING_LIMITS, "Man_tNoBound", (None, None, ""))
    row = _timing_row("Man_tNoBound", 1e6)
    assert row["ok"] is None and row["min_us"] is None and row["max_us"] is None


@pytest.mark.parametrize("param", sorted(LC_TIMING_LIMITS))
def test_a_timing_limit_includes_both_of_its_bounds(param):
    """[min-max] is inclusive: a measurement exactly on a bound passes, one just outside fails."""
    from swi3s_studio.analysis.link_control import _timing_row
    lo, hi, _desc = LC_TIMING_LIMITS[param]
    if lo is not None:
        assert _timing_row(param, lo)["ok"] is True
        assert _timing_row(param, lo - 0.01)["ok"] is False
    if hi is not None:
        assert _timing_row(param, hi)["ok"] is True
        assert _timing_row(param, hi + 0.01)["ok"] is False


def test_clock_cycles_exactly_on_a_limit_pass():
    from swi3s_studio.analysis.link_control import _clock_timing_rows
    rows = {r["param"]: r for r in _clock_timing_rows([5.0, 32.0], [5.0, 32.0])}
    assert rows["Man_tClock1"]["ok"] is True and rows["Man_tClock0"]["ok"] is True


def test_the_manager_timing_limits_are_the_specified_numbers():
    """The (min, max) microsecond pairs of the §5.2.3 table; the descriptions are prose."""
    assert {k: v[:2] for k, v in LC_TIMING_LIMITS.items()} == {
        "Man_tReset00": (212.0, 236.0),
        "Man_tReset10": (1569.0, None),
        "Man_tResetRecovery": (72.5, 80.0),
        "Man_tClock1": (5.0, 32.0),
        "Man_tClock0": (5.0, 32.0),
        "Man_tWarmStart10": (436.0, 482.0),
        "Man_tPhyStart": (64.0, 96.0),
    }


def test_one_out_of_range_clock_cycle_fails_the_whole_row():
    """A row passes only if EVERY cycle of the PHY-number burst is within [5, 32] us."""
    from swi3s_studio.analysis.link_control import _clock_timing_rows
    rows = {r["param"]: r for r in _clock_timing_rows([10.0, 10.0, 40.0, 10.0],
                                                      [10.0, 4.0, 10.0, 10.0])}
    assert rows["Man_tClock1"]["ok"] is False and rows["Man_tClock1"]["measured_us"] == 40.0
    assert rows["Man_tClock0"]["ok"] is False and rows["Man_tClock0"]["measured_us"] == 4.0
    assert "4 cycles" in rows["Man_tClock1"]["desc"]
    good = {r["param"]: r for r in _clock_timing_rows([10.0, 12.0], [10.0, 12.0])}
    assert good["Man_tClock1"]["ok"] is True and good["Man_tClock0"]["ok"] is True


def _start_pulse_capture(pulse_us):
    """A DP-high pulse of `pulse_us` after 50 us idle, then a low and one later edge, at
    1 GS/s so a whole number of microseconds is a whole number of samples."""
    spp = 1000.0
    us = lambda x: int(round(x * spp))
    t = us(50)
    clk = [t]
    t += us(pulse_us)
    clk.append(t)
    t += us(200)
    clk.append(t)
    return Capture(clock_edges=np.asarray(clk, np.uint64), data_edges=np.asarray([], np.uint64),
                   initial_clock=False, initial_data=False, sample_rate_hz=1_000_000_000)


def test_warm_start_pulses_are_judged_against_the_482_us_limit():
    def row(pulse_us):
        r = decode_link_control(_start_pulse_capture(pulse_us))
        assert r.sequence == "warm", (pulse_us, r.sequence)
        return next(t for t in r.timing if t["param"] == "Man_tWarmStart10")
    assert row(482.0)["ok"] is True
    assert row(483.0)["ok"] is False
    assert row(436.0)["ok"] is True
    assert row(435.0)["ok"] is False


def test_the_150_and_500_us_pulses_fall_on_the_warm_side_of_their_boundary():
    """Warm Start is [150, 500) us of DP high; below is LC_Acknowledge, from 500 a Bus Reset."""
    assert decode_link_control(_start_pulse_capture(149.0)).sequence == "none"
    assert decode_link_control(_start_pulse_capture(150.0)).sequence == "warm"
    assert decode_link_control(_start_pulse_capture(499.0)).sequence == "warm"
    assert decode_link_control(_start_pulse_capture(500.0)).sequence == "cold"


def _us_capture(edges_us):
    """A clock-only capture at 1 GS/s with edges at the given microsecond times, DP low first."""
    return Capture(clock_edges=np.asarray([int(round(x * 1000)) for x in edges_us], np.uint64),
                   data_edges=np.asarray([], np.uint64), initial_clock=False,
                   initial_data=False, sample_rate_hz=1_000_000_000)


def test_a_capture_that_ends_on_or_just_after_the_start_pulse_decodes_without_error():
    """A start pulse is found from a measured gap, so it always has its falling edge: a capture
    ending right after the pulse takes that edge as PhyStart, one ending on the next rising
    edge has none, and a lone edge has no sequence."""
    assert decode_link_control(_us_capture([50])).sequence == "none"
    for edges, sequence, phystart in (([50, 650], "cold", 650_000),
                                      ([50, 650, 1300], "cold", None),
                                      ([50, 200], "warm", 200_000),
                                      ([50, 200, 400], "warm", 200_000)):
        r = decode_link_control(_us_capture(edges))
        assert (r.sequence, r.phystart_sample) == (sequence, phystart), edges


def test_a_dp_low_longer_than_48_us_after_a_falling_edge_is_phystart_and_exactly_48_is_not():
    """A burst-terminating falling edge is PhyStart when the DP-low after it exceeds 48 us;
    exactly 48 us still clocks a PHY-number bit."""
    base = [50, 650, 670, 700]                       # Bus Reset pulse, then burst pulses
    for low_us, phystart in ((47.9, False), (48.0, False), (48.1, True), (60.0, True)):
        r = decode_link_control(_us_capture(base + [700 + low_us]))
        assert r.sequence == "cold"
        assert (r.phystart_sample == 700_000) is phystart, (low_us, r.phystart_sample)
        assert (r.audio_start_sample is not None) is phystart


def _phystart_row(r):
    return next((t for t in r.timing if t["param"] == "Man_tPhyStart"), None)


def test_man_tphystart_is_measured_to_the_first_audio_edge_and_judged_against_64_to_96_us():
    """Cold Start: the span from the PhyStart falling edge to the first edge after it, on
    the clock line or the data line, is a Man_tPhyStart row; 64 and 96 us pass, 63 and 97
    fail, and a 130 us low is flagged."""
    base = [50, 650, 670, 700]                       # Bus Reset pulse, then burst pulses
    for low_us, ok in ((63.0, False), (64.0, True), (80.0, True), (96.0, True),
                       (97.0, False), (130.0, False)):
        r = decode_link_control(_us_capture(base + [700 + low_us, 700 + low_us + 1]))
        row = _phystart_row(r)
        assert row is not None and row["ok"] is ok, (low_us, row)
        assert abs(row["measured_us"] - low_us) < 1e-6
        assert (row["min_us"], row["max_us"]) == (64.0, 96.0)
    # The first edge after PhyStart on the DATA line counts as well (audio clock stored there).
    cap = Capture(clock_edges=np.asarray([int(x * 1000) for x in base + [800]], np.uint64),
                  data_edges=np.asarray([int(x * 1000) for x in (770,)], np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000_000)
    row = _phystart_row(decode_link_control(cap))
    assert abs(row["measured_us"] - 70.0) < 1e-6 and row["ok"] is True


def test_man_tphystart_on_a_warm_start_and_when_no_edge_follows():
    r = decode_link_control(_us_capture([50, 500, 500 + 130]))
    assert r.sequence == "warm" and _phystart_row(r)["ok"] is False
    assert abs(_phystart_row(r)["measured_us"] - 130.0) < 1e-6
    assert _phystart_row(decode_link_control(_us_capture([50, 500, 500 + 80]))) ["ok"] is True
    # A capture that ends at PhyStart observes no audio edge: no row rather than a guess.
    assert _phystart_row(decode_link_control(_us_capture([50, 500]))) is None
    assert _phystart_row(decode_link_control(_us_capture([50, 650, 670, 700, 800]))) is not None


def test_a_synthetic_cold_start_spliced_before_audio_is_in_spec_on_man_tphystart():
    audio = transitions.demo_capture(32, cold_start=False)
    r = decode_link_control(transitions.prepend_cold_start(audio, 2))
    row = _phystart_row(r)
    assert row is not None and row["ok"] is True, row


def test_man_tphystart_skips_a_ring_at_phystart_and_a_glitch_on_either_line():
    """PhyStart is an edge of the glitch-coalesced clock: the rest of a ring at its falling
    edge, and a glitch pair on the data line, are not the first edge of audio mode."""
    ring = [50, 650, 670, 700, 700.1, 700.2, 780, 781]
    r = decode_link_control(_us_capture(ring))
    assert r.phystart_sample == 700_000
    assert abs(_phystart_row(r)["measured_us"] - 80.0) < 1e-6
    cap = Capture(clock_edges=np.asarray([int(round(x * 1000)) for x in ring], np.uint64),
                  data_edges=np.asarray([710_000, 710_200], np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000_000)
    row = _phystart_row(decode_link_control(cap))
    assert abs(row["measured_us"] - 80.0) < 1e-6 and row["ok"] is True
    # A single data-line edge is a real level change and still counts.
    cap = Capture(clock_edges=np.asarray([int(round(x * 1000)) for x in ring], np.uint64),
                  data_edges=np.asarray([770_000], np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000_000)
    assert abs(_phystart_row(decode_link_control(cap))["measured_us"] - 70.0) < 1e-6


def _phystart_first_edge_by_scan(edges, phystart, min_gap):
    """The first edge of audio mode on one line by a scan of every edge after PhyStart: the
    ring chained to PhyStart skipped, then the first cluster that is odd or spans more than
    the gap."""
    e = [int(x) for x in edges if int(x) > phystart]
    i, prev = 0, phystart
    while i < len(e) and e[i] - prev <= min_gap:
        prev, i = e[i], i + 1
    while i < len(e):
        j = i + 1
        while j < len(e) and e[j] - e[j - 1] <= min_gap:
            j += 1
        if (j - i) % 2 == 1 or e[j - 1] - e[i] > min_gap:
            return e[i]
        i = j
    return None


def test_man_tphystart_matches_a_scan_and_reads_only_the_edges_near_phystart():
    """The Man_tPhyStart row's first edge equals a scan of every edge on random lines, and a
    dense audio clock or a long chained ring after PhyStart is not read edge by edge."""
    from swi3s_studio.analysis import link_control as lc
    spp, phystart = 1000.0, 10_000
    gap = max(1, int(lc.GLITCH_MAX_US * spp))
    rng = np.random.default_rng(7)
    for _ in range(300):
        steps = rng.choice([1, gap // 2, gap, gap + 1, 3 * gap], size=int(rng.integers(0, 40)))
        edges = np.asarray(phystart - 2 * gap + np.cumsum(steps), np.uint64)
        want = _phystart_first_edge_by_scan(edges, phystart, gap)
        rows = lc._phystart_row(phystart, spp, edges)
        got = (round(rows[0]["measured_us"] * spp) + phystart) if rows else None
        assert got == want, (edges.tolist(), got, want)

    class Counted(np.ndarray):
        reads = 0

        def __getitem__(self, key):
            Counted.reads += 1
            return super().__getitem__(key)
    clock = np.arange(phystart + 80_000, phystart + 80_000 + 20 * 400_000, 20,
                      dtype=np.uint64).view(Counted)                 # a dense audio clock
    ring = np.concatenate([np.arange(phystart + 5, phystart + 5 + 10 * 400_000, 10),
                           [phystart + 9_000_000]]).astype(np.uint64).view(Counted)
    Counted.reads = 0
    rows = lc._phystart_row(phystart, spp, clock, ring)
    assert abs(rows[0]["measured_us"] - 80.0) < 1e-9
    assert Counted.reads < 2_000, Counted.reads
