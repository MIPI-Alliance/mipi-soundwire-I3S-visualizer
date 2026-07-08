"""Link-Control (§5.1.2) decoder tests: Cold/Warm Start classification + PHY-number
recovery from synthetic DP/DN transition edges.

Run: PYTHONPATH=. python3 tests/test_link_control.py
"""
import numpy as np

from swi3s_studio.ingest.capture import Capture
from swi3s_studio.ingest import transitions
from swi3s_studio.analysis import decode_link_control


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
    # The plain demo (no bring-up) has only fast audio-rate clocking → no PHY known.
    cap = transitions.demo_capture(32)
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
    """Regression for the ColdStart_Lusk capture: the real waveform has TWO long
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


if __name__ == "__main__":
    test_cold_start_recovers_phy_number(); print("ok: cold start recovers PHY1/2/3 number")
    test_warm_start_has_no_phy_number(); print("ok: warm start carries no PHY number")
    test_audio_only_capture_is_none(); print("ok: audio-only capture → sequence 'none'")
    test_prepend_cold_start_then_decode_and_still_decodes_audio()
    print("ok: prepend cold start → decodes PHY2 + audio still intact")
    test_real_world_cold_start_quirks()
    print("ok: real-world cold start (2 resets + recovery low + glitch) → PHY2")
    test_self_orients_when_bringup_is_on_the_data_line()
    print("ok: self-orients when the bring-up is on the data line (audio orientation)")
    test_cold_start_emits_sections_and_timing()
    print("ok: cold start emits named sections + §5.2.3 timing (all pass)")
    print("ALL LINK-CONTROL TESTS PASSED")
