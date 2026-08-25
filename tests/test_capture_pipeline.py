"""End-to-end capture pipeline test (data path, no GUI):

    demo levels -> Capture (transition arrays) -> write Saleae binary files
    -> read them back -> TransitionSampleSource -> Decoder
    -> commands + audio, plus the Arrow command store round-trip.

Verifies the capture-backed source decodes identically to the in-memory source,
that the Saleae binary reader/writer round-trips, and that the command store
preserves the decode. Run: python3 -m pytest tests/test_capture_pipeline.py
"""
import math
import os
import tempfile

import swi3score

from swi3s_studio import decode, decode_capture
from swi3s_studio.ingest import saleae_binary, transitions
from swi3s_studio.store import command_store


def _expected_sine(channel, index, sample_size):
    amp = (1 << sample_size) * 0.45
    v = int(round(amp * math.sin(2.0 * math.pi * (channel + 1) * index / 64.0)))
    return v & ((1 << (sample_size + 1)) - 1)


def _assert_decode_good(res, label):
    writes = [c for c in res.commands if c["command"] == "WriteA32" and c["crc_valid"]]
    sscr = [c for c in res.commands if c["is_commit"] and c["commit_confirmed"]]
    assert res.column_count == 16, f"{label}: columns={res.column_count}"   # ends 16-col
    # Two geometry commits (8-col then 16-col), each 13 writes (NumColumns + 4 ports x 3),
    # plus a one-time PortControl (ScramblerEn=0) for DP0 and DP2 = 2, plus DP0's payload
    # skipping = 3 (SLC_SkippingDenominator is two bytes at two addresses, and
    # DP0_SkippingNumerator one write of two). Total 31.
    assert len(writes) == 31, f"{label}: writes={len(writes)}"
    assert len(sscr) == 2, f"{label}: sscr={len(sscr)}"
    assert res.audio, f"{label}: no audio"
    # dp0/dp1 are mono 16-bit PCM (tone == dp*2 on channel 0); verify both against the
    # sine, continuous across the 8->16 commit.
    pcm = {}
    for a in res.audio:
        if a.get("device") == 0 and a["dp"] in (0, 1):
            pcm.setdefault(a["dp"], []).append((a["value"], a["sample_size"]))
    for dp in (0, 1):
        vals = [v for v, _ in pcm[dp]]
        sample_size = pcm[dp][0][1] - 1
        assert sample_size == 15, f"{label}: dp{dp} sample_size={sample_size + 1}"
        for i, v in enumerate(vals):
            assert v == _expected_sine(dp * 2, i, sample_size), f"{label}: dp{dp}[{i}] {v}"
    # Four audio streams overall (dp0-dp3 on one device: 2 PCM + 2 PDM).
    assert {(a.get("device"), a["dp"]) for a in res.audio} == {(0, 0), (0, 1), (0, 2), (0, 3)}
    return writes


def test_transition_source_matches_memory_source():
    levels = swi3score.make_demo_levels(32)
    mem = decode(swi3score.MemorySampleSource(levels, 98_304_000, 4))
    cap = decode_capture(transitions.build_capture_from_levels(levels))
    assert [c["command"] for c in mem.commands] == [c["command"] for c in cap.commands]
    assert [a["value"] for a in mem.audio] == [a["value"] for a in cap.audio]
    _assert_decode_good(cap, "transition-source")


def test_saleae_binary_roundtrip_and_decode():
    cap = transitions.demo_capture(32)
    with tempfile.TemporaryDirectory() as d:
        clk = os.path.join(d, "clock.bin")
        dat = os.path.join(d, "data.bin")
        saleae_binary.write_capture(cap, clk, dat)
        cap2 = saleae_binary.load_capture(clk, dat, cap.sample_rate_hz)
        # Edge arrays survive the seconds<->samples round-trip exactly.
        assert (cap2.clock_edges == cap.clock_edges).all()
        assert (cap2.data_edges == cap.data_edges).all()
        assert cap2.initial_data == cap.initial_data
        res = decode_capture(cap2)
    _assert_decode_good(res, "saleae-binary")


def test_saleae_binary_negative_pretrigger():
    """Logic's binary export uses absolute times with a negative pre-trigger
    origin (capture start < t=0). load_capture must rebase to that origin instead
    of wrapping negatives through the uint64 cast."""
    cap = transitions.demo_capture(32)
    rate = cap.sample_rate_hz
    shift = 8.389700564                       # seconds of pre-trigger, like a real capture
    clk_t = cap.clock_edges.astype("<f8") / rate - shift
    dat_t = cap.data_edges.astype("<f8") / rate - shift
    with tempfile.TemporaryDirectory() as d:
        clk = os.path.join(d, "clock.bin")
        dat = os.path.join(d, "data.bin")
        saleae_binary.write_channel(clk, cap.initial_clock, clk_t, begin_time=-shift)
        saleae_binary.write_channel(dat, cap.initial_data, dat_t, begin_time=-shift)
        cap2 = saleae_binary.load_capture(clk, dat, rate)
        # Rebased to the capture-start origin: edges match the original samples.
        assert (cap2.clock_edges == cap.clock_edges).all()
        assert (cap2.data_edges == cap.data_edges).all()
        res = decode_capture(cap2)
    _assert_decode_good(res, "saleae-binary negative pre-trigger")


def test_column_detect_phase_offset():
    """A capture that starts mid-row (Column 0 is not the first rising edge) must
    still auto-detect the column count. With >2 columns several columns fall on a
    rising edge, so the detector has to search the Column-0 phase offset, not just
    the count -- this guards that search."""
    import numpy as np
    cap = transitions.demo_capture(32)
    ref = decode_capture(cap)
    assert ref.column_count == 16
    for drop in (2, 4, 6):                       # start 2/4/6 UIs into Row 0
        t0 = int(cap.clock_edges[drop])
        ce = np.ascontiguousarray(cap.clock_edges[cap.clock_edges >= t0] - t0)
        de = np.ascontiguousarray(cap.data_edges[cap.data_edges >= t0] - t0)
        ic = bool(cap.initial_clock) ^ (int(np.searchsorted(cap.clock_edges, t0)) & 1)
        idd = bool(cap.initial_data) ^ (int(np.searchsorted(cap.data_edges, t0)) & 1)
        src = swi3score.TransitionSampleSource(ce, de, ic, idd, cap.sample_rate_hz)
        dec = swi3score.Decoder(src, swi3score.DecoderSettings())
        dec.run()
        c = dec.commands()
        assert dec.column_count == 16, f"drop={drop}: cols={dec.column_count}"
        # Same valid commands as the aligned capture (minus at most the first row).
        assert sum(1 for x in c if x.get("crc_valid")) >= len(ref.commands) - 1, \
            f"drop={drop}: only {sum(1 for x in c if x.get('crc_valid'))} valid"


def test_saleae_binary_clock_autodetect():
    """The .bin open flow tells clock from data by transition count (the
    forwarded clock toggles every UI, so it has the most transitions). Verify the
    header-only count and that the choice is independent of file order."""
    cap = transitions.demo_capture(32)
    with tempfile.TemporaryDirectory() as d:
        clk = os.path.join(d, "a.bin")
        dat = os.path.join(d, "b.bin")
        saleae_binary.write_capture(cap, clk, dat)
        nclk = saleae_binary.transition_count(clk)
        ndat = saleae_binary.transition_count(dat)
        assert nclk == cap.clock_edges.size and ndat == cap.data_edges.size
        assert nclk > ndat                       # clock has more transitions
        # Whichever order the user picks, the higher-count file is the clock.
        for first, second in ((clk, dat), (dat, clk)):
            na, nb = saleae_binary.transition_count(first), saleae_binary.transition_count(second)
            chosen = second if (nb > na) else first
            assert chosen == clk
        assert saleae_binary.transition_count(os.path.join(d, "nope.bin")) == -1
        # The .bin has no rate field; infer it from the timestamp granularity.
        # (Real captures have sub-UI edge spacing -> exact rate; this synthetic
        # capture is fully UI-aligned, so inference yields a clean submultiple --
        # still collision-free, and order-independent.)
        r = saleae_binary.infer_sample_rate([clk, dat])
        assert r > 0 and cap.sample_rate_hz % r == 0
        assert saleae_binary.infer_sample_rate([dat, clk]) == r
        # With real-style sub-UI edge spacing (clock & data edges 1 sample apart),
        # inference recovers the exact rate.
        import numpy as np
        rate = 250_000_000
        a = os.path.join(d, "ca.bin"); b = os.path.join(d, "cb.bin")
        saleae_binary.write_channel(a, False, np.arange(0, 50) / rate)        # edges 1 sample apart
        saleae_binary.write_channel(b, False, (np.arange(0, 50) + 0.0) / rate)
        assert saleae_binary.infer_sample_rate([a, b]) == rate


def test_scrambler_override():
    """Per-(device,dp) scrambler override changes descrambling. DP1's payload is scrambled
    by default, so forcing its scrambler OFF must change that stream's values (the decode
    stops descrambling); an untouched port is unchanged; forcing it back ON matches the
    default. (DP0/DP2 ship unscrambled, so DP1 is the port to exercise here.)"""
    cap = transitions.demo_capture(16)
    ref = decode_capture(cap)
    off = decode_capture(cap, scrambler_overrides={(0, 1): False})
    def stream(res, key):
        return [a["value"] for a in res.audio
                if (a.get("device"), a["dp"], a["channel"]) == key]
    # (0,1,0): descrambled (ref) vs raw (off) -> different values
    assert stream(ref, (0, 1, 0)) != stream(off, (0, 1, 0))
    # an untouched stream is unchanged
    assert stream(ref, (0, 0, 0)) == stream(off, (0, 0, 0))
    # forcing it back ON matches the default (which already descrambles)
    on = decode_capture(cap, scrambler_overrides={(0, 1): True})
    assert stream(on, (0, 1, 0)) == stream(ref, (0, 1, 0))


def test_command_store_roundtrip():
    res = decode_capture(transitions.demo_capture(32))
    table = command_store.commands_to_table(res.commands)
    assert table.num_rows == len(res.commands)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "commands.parquet")
        command_store.save_commands(table, path)
        back = command_store.load_commands(path)
    assert back.column("command").to_pylist() == [c["command"] for c in res.commands]
    writes = list(command_store.register_writes(res.commands))
    assert len(writes) == 31       # see _assert_decode_good for the breakdown
    assert writes[0][1] == 0x1081   # first write programs NumColumns_NEXT


def test_zero_copy_lifetime():
    """TransitionSampleSource references the NumPy buffers zero-copy; keep_alive
    must hold them after all Python refs are dropped, and decode stays correct."""
    import gc

    import swi3score as core
    cap = transitions.demo_capture(8)
    ref = decode_capture(cap)
    ce, de = cap.clock_edges, cap.data_edges
    src = core.TransitionSampleSource(ce, de, bool(cap.initial_clock),
                                      bool(cap.initial_data), cap.sample_rate_hz)
    del cap, ce, de
    gc.collect()                                   # arrays survive via keep_alive
    dec = core.Decoder(src, core.DecoderSettings())
    dec.run()
    assert [c["command"] for c in dec.commands()] == [c["command"] for c in ref.commands]
    assert [a["value"] for a in dec.audio()] == [a["value"] for a in ref.audio]
