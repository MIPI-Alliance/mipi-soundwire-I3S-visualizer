"""Link-Control (§5.1.2) decoder tests: Cold/Warm Start classification + PHY-number
recovery from synthetic DP/DN transition edges.

Run: PYTHONPATH=. python3 -m pytest tests/test_link_control.py
"""
import numpy as np

from swi3s_studio.analysis import decode_link_control
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
        # audio starts after PhyStart, near the end of the synthesized preamble.
        assert r.audio_start_sample > r.phystart_sample
        assert abs(r.audio_start_sample - end) < RATE // 1000   # within ~1 ms


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
    """Regression for a real cold-start capture: the waveform has TWO long
    Bus-Reset highs (not one), a >48 µs recovery low, and a sub-sample glitch edge
    in the PHY-number burst. Earlier these made the decoder mis-detect PhyStart and
    drop/duplicate a bit. DN here has a single ~10 µs high over the 3rd of 4 bit
    falling edges → 0b0010 = PHY2. Sample rate 250 MHz (250 samples/µs)."""
    import numpy as np
    spp = 250.0
    us = lambda x: int(round(x * spp))

    clk, t = [], us(50)
    clk += [t]; t += us(1600)       # Bus Reset high #1
    clk += [t]; t += us(305)        # inter-reset low
    clk += [t]; t += us(1600)       # Bus Reset high #2
    clk += [t]; t += us(75)         # recovery low (> PhyStart gap — must not trip)
    # 4 clock cycles (bits 3..0). Insert a sub-sample glitch (a +1-sample pip) at
    # the start of the 3rd low, like the real capture.
    bit_edges = []
    for k in range(4):
        clk += [t]; t += us(30)     # rising
        clk += [t]; bit_edges.append(t); t += us(30)   # falling (bit sampled)
        if k == 1:                  # glitch: two extra coincident-ish edges
            clk += [t, t + 1]       # +1-sample pip → must be coalesced away
    clk += [t]; t += us(30)         # final clock high
    clk += [t]; t += us(80)         # PhyStart falling → long low
    # DN: a single high pulse covering ONLY the 3rd bit falling edge (value 0b0010).
    third = bit_edges[2]
    dat = [third - us(5), third + us(5)]

    cap = Capture(clock_edges=np.asarray(sorted(clk), np.uint64),
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


@pytest.mark.parametrize("phy, variant", [(1, ""), (2, ""), (2, "flow_control")])
def test_a_capture_without_a_bringup_has_none(phy, variant):
    s = Session.from_demo(300, phy=phy, variant=variant)
    assert decode_link_control(s.capture).sequence != "none"     # the edges alone: fooled
    lc = s.link_control
    assert not s.has_bringup and lc.sequence == "none"
    assert not lc.lc_on_data_line and lc.sections == [] and s.audio_start_sample == 0
    assert "CRC-valid command" in lc.note                       # says why


def test_a_capture_joined_after_audio_began_has_no_bringup():
    s = _joined_late()
    assert decode_link_control(s.capture).sequence == "warm"     # the phantom
    assert not s.has_bringup and not s.link_control.lc_on_data_line


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
    stands, and the decode is its steered one (as before this check existed)."""
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
