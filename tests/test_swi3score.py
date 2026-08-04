"""End-to-end check of the swi3score pybind11 core.

Decodes the synthetic demo stream (config + commit + scrambled stereo audio)
through the SAME C++ decode the Saleae plugin uses, and asserts the control
phases, snooped geometry, and reconstructed audio all come back correctly.
Mirrors native/.../test_simulation_roundtrip.cpp, but exercised from Python.

Run:  pytest -q   (after `pip install ./native`)
"""
import math

import swi3score


def _expected_sine(channel: int, index: int, sample_size: int) -> int:
    amp = (1 << sample_size) * 0.45
    w = 2.0 * math.pi * (channel + 1) * index / 64.0
    v = int(round(amp * math.sin(w)))
    return v & ((1 << (sample_size + 1)) - 1)


def decode_demo(samples_per_channel: int = 300):
    # ~300 samples/channel: long enough for the 8-col and 16-col phases to both establish
    # (auto column-detect + UI-rate tracking need a few rows of each geometry).
    levels = swi3score.make_demo_levels(samples_per_channel)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    settings = swi3score.DecoderSettings()  # auto column count, audio on
    dec = swi3score.Decoder(src, settings)
    dec.run()
    return dec


def test_column_autodetect():
    dec = decode_demo()
    assert dec.column_count == 16        # ends in 16-column mode (safe-lock-2 -> 8 -> 16)


def test_control_phases_decode():
    dec = decode_demo()
    cmds = dec.commands()
    writes = [c for c in cmds if c["command"] == "WriteA32" and c["crc_valid"]]
    pings = [c for c in cmds if c["command"] == "Ping"]
    sscr = [c for c in cmds if c["is_commit"] and c["commit_confirmed"]]
    # Two commits (8-col then 16-col). Each geometry writes NumColumns + 4 ports x 3
    # register writes (SampleSizeGrouping, BitWidth/HStart/HCount/Spacing/Interval,
    # ChannelGrouping) = 13; plus a one-time PortControl (ScramblerEn=0) for DP0 and DP2
    # = 2. Total 26 + 2 = 28.
    assert len(writes) == 28, f"expected 28 writes, got {len(writes)}"
    assert len(pings) >= 2
    assert len(sscr) == 2, f"expected 2 confirmed commits (8-col, 16-col), got {len(sscr)}"
    # The first write programs the column count (SLC NumColumns_NEXT @ 0x1081).
    assert writes[0]["address"] == 0x1081
    # Timestamps are monotonic.
    starts = [c["start_sample"] for c in cmds]
    assert starts == sorted(starts)


def test_geometry_transitions_2_8_16():
    """The demo starts in safe-lock-2, commits to 8 columns (audio starts), then to
    16 columns (audio continues). The decode must see all three geometries in order."""
    from swi3s_studio.session import Session
    s = Session.from_demo(300, cold_start=False)
    cols = [int(seg["column_count"]) for seg in s.decoder.segments()]
    assert cols == [2, 8, 16], cols


def test_dscr_disable_drops_port_from_audio_and_samples():
    """A DSCR (a synced commit that does NOT re-anchor the SSP) that clears a port's
    EnableCh must DROP that port from BOTH the audio decode and the samples (bit) view,
    while the surviving port continues UNINTERRUPTED (a DSCR preserves interval phase).

    Regression: the decoder used to commit a DSCR immediately WITHOUT reconfiguring the
    payload engine, so a disabled port kept emitting audio + bit samples (the Dev6 DP3
    'enabled after EnableCh=0' bug). dp1 is disabled halfway; dp0 must stay bit-exact."""
    N = 64
    levels = swi3score.make_dscr_disable_levels(N)

    # A confirmed DSCR commit (is_commit, NOT a sync point) is present.
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    d = swi3score.Decoder(src, swi3score.DecoderSettings())
    d.run()
    dscr = [c for c in d.commands()
            if c.get("is_commit") and c.get("commit_confirmed") and not c.get("has_sync_point")]
    assert len(dscr) == 1, [c["command"] for c in d.commands() if c.get("is_commit")]

    # dp1 stops at the DSCR (only the first-phase N samples); dp0 runs both phases (2N),
    # bit-exact and continuous — proving the disable took effect AND dp0's phase survived.
    dp0 = [a["value"] for a in sorted((a for a in d.audio() if a["dp"] == 0),
                                      key=lambda a: a["start_sample"])]
    dp1 = [a["value"] for a in d.audio() if a["dp"] == 1]
    assert len(dp1) == N, f"dp1 kept emitting after the DSCR disable: {len(dp1)} (want {N})"
    assert len(dp0) == 2 * N, f"dp0 should span both phases: {len(dp0)}"
    assert all(v == _expected_sine(0, i, 15) for i, v in enumerate(dp0)), "dp0 phase broke at the DSCR"

    # Samples-view cousin: with bit-sample collection on, dp1 must produce NO bit samples
    # after the DSCR row (else the Raw Capture / samples view shows the disabled port too).
    src2 = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    st = swi3score.DecoderSettings()
    st.collect_bit_samples = True
    d2 = swi3score.Decoder(src2, st)
    d2.run()
    bs = d2.bit_samples()
    dp1_last = max((s for s, dp in zip(bs["sample"], bs["dp"]) if dp == 1), default=0)
    dp0_last = max((s for s, dp in zip(bs["sample"], bs["dp"]) if dp == 0), default=0)
    assert dp0_last > dp1_last, ("dp1 bit samples extend past dp0 — disabled port still "
                                 f"sampled (dp1_last={dp1_last}, dp0_last={dp0_last})")


def test_dscr_disabling_last_port_stops_audio():
    """Disabling the LAST enabled port must stop its audio. BuildConfig returns "no config"
    when nothing is enabled, so the reconfigure has to drop the port to an EMPTY engine
    rather than bailing and keeping it. Regression for the long-capture Dev6 DP3 bug (the
    port kept decoding to the end of the capture because it was the last one disabled);
    the earlier dp1-only fixture missed it because dp0 remained enabled."""
    N = 64
    levels = swi3score.make_dscr_disable_levels(N, disable_all=True)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    d = swi3score.Decoder(src, swi3score.DecoderSettings())
    d.run()
    # Both ports are disabled by the DSCR, so each stops at the commit (only the first-phase
    # N samples), instead of running to the end of the capture (2N).
    for dp in (0, 1):
        n = sum(1 for a in d.audio() if a["dp"] == dp)
        assert n == N, f"dp{dp} kept emitting after being disabled: {n} (want {N})"


def test_immediate_single_ranked_write_reconfigures_decode():
    """The register state drives the decode however it was set — not only at commits. A
    single-ranked WriteA32 (PortControl.ScramblerEn, 0x0B) takes committed effect
    immediately, so clearing it mid-stream must reconfigure the decode NOW. The port is
    scrambled on the wire throughout; before the write the decode descrambles (matches the
    sine), after it stops (diverges). Regression: a decode that only reconfigures at commits
    keeps descrambling and the whole stream would match."""
    N = 64
    levels = swi3score.make_immediate_scrambler_levels(N)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    d = swi3score.Decoder(src, swi3score.DecoderSettings())
    d.run()
    # The single-ranked PortControl write is present and CRC-valid.
    assert any(c["command"] == "WriteA32" and c["crc_valid"] and c["address"] == 0x200B
               for c in d.commands())
    vals = [a["value"] for a in sorted((a for a in d.audio() if a["dp"] == 0),
                                       key=lambda a: a["start_sample"])]
    assert len(vals) >= 2 * N - 4
    phase1, phase2 = vals[:N], vals[N + 2:2 * N]     # skip a couple transition samples
    assert all(v == _expected_sine(0, i, 15) for i, v in enumerate(phase1)), \
        "phase 1 should descramble to the sine"
    # After the immediate ScramblerEn=0 write the decode stops descrambling, so the raw
    # (still-scrambled) wire data no longer matches the sine.
    matched = sum(1 for j, v in enumerate(phase2) if v == _expected_sine(0, N + 2 + j, 15))
    assert matched < len(phase2) // 4, \
        f"immediate ScramblerEn=0 write did not reconfigure the decode ({matched} still match)"


def test_windowed_bit_samples_match_full_across_mid_interval_reconfigure():
    """C5: a windowed bit-sample re-decode whose window STRADDLES a MID-INTERVAL,
    phase-preserving reconfigure must place the surviving port's sample points on the SAME
    bus rows as the full decode. Regression: the windowed replay used to Configure() (which
    resets row_in_interval -> 0) at the reconfigure, where the live decode uses phase-
    preserving Reconfigure(); for an Interval>1 port reconfigured mid-interval that drifted
    the Raw-Capture sample-point markers after the reconfigure by the interval offset."""
    import numpy as np

    from swi3s_studio.ingest import transitions
    from swi3s_studio.session import Session
    N = 64
    # One Interval=64 PCM port; a single-ranked ScramblerEn write reconfigures the decode
    # MID-interval (geometry unchanged -> the survivor's phase is preserved by Reconfigure).
    levels = swi3score.make_immediate_scrambler_levels(N, mid_interval=True)
    cap = transitions.build_capture_from_levels(levels)   # seekable — same numbering both paths

    st = swi3score.DecoderSettings(); st.collect_bit_samples = True
    d = swi3score.Decoder(cap.sample_source(), st); d.run()
    dp0_full = np.sort(np.asarray(d.bit_samples()["sample"], np.int64)[
        np.asarray(d.bit_samples()["dp"], np.int32) == 0])
    assert dp0_full.size > 4 * N, "need bit samples on both sides of the reconfigure"
    lo, hi = int(dp0_full[dp0_full.size // 3]), int(dp0_full[2 * dp0_full.size // 3])

    win = Session(cap).port_bit_marks(lo, hi, max_marks=1_000_000)
    w_dp0 = np.sort(np.asarray(win["sample"], np.int64)[np.asarray(win["dp"], np.int32) == 0])
    expect = dp0_full[(dp0_full >= lo) & (dp0_full <= hi)]
    assert w_dp0.size == expect.size and np.array_equal(w_dp0, expect), (
        "windowed dp0 bit samples diverge from the full decode across a mid-interval "
        f"reconfigure (windowed {w_dp0.size} vs full {expect.size} in-window)")


def test_decode_progress_reaches_total_at_end():
    """Decoder.run() must leave progress_uis/total_uis reporting (essentially) complete: a
    seekable MemorySampleSource knows its UI count up front (total_uis > 0), and after
    run() returns, progress_uis has caught up to it. (progress_uis undercounts by the small
    bounded column-detect window's worth of UIs consumed before the first feed() call — see
    Decoder.h progressUis's comment — so this allows a small margin rather than requiring
    exact equality.)"""
    dec = decode_demo(300)
    assert dec.total_uis > 0, "MemorySampleSource should report a known total UI count"
    assert dec.progress_uis <= dec.total_uis, "progress must never exceed the total"
    assert dec.progress >= 0.99, f"progress should be ~complete after run(): {dec.progress}"
    # progress is exactly progress_uis / total_uis.
    assert dec.progress == dec.progress_uis / dec.total_uis


def test_decode_progress_unknown_before_run():
    """Before run() (or for a pure-streaming source whose TotalUiCount() defaults to 0),
    both counters start at 0 and progress() must report 0.0 (unknown), never a bogus
    ratio / NaN / divide-by-zero — Decoder::progress() explicitly guards total_uis == 0."""
    levels = swi3score.make_dscr_disable_levels(8)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    d = swi3score.Decoder(src, swi3score.DecoderSettings())
    assert d.total_uis == 0 and d.progress_uis == 0
    assert d.progress == 0.0, "progress before run() (total unknown) must be 0.0, not NaN"


def test_enablech_curr_write_error_rule():
    """FEATURE #3: a WriteA32 to an EnableCh _CURR register address (0xC1/0xD0/0xD1 — the
    dual-ranked channel-enable register's committed rank, written directly instead of via
    the normal _NEXT -> Commit path) must be flagged an error UNLESS the addressed port's
    Interval is 1 Row (Interval register == 0, i.e. every row transports so there's no
    mid-interval phase for the enabled-channel set to change under). Exercises the RULE
    end-to-end via a real decoded command, keyed only on interval != 0."""
    for interval, want_error in ((0, False), (1, True), (63, True)):
        levels = swi3score.make_enablech_curr_write_levels(32, interval)
        src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
        d = swi3score.Decoder(src, swi3score.DecoderSettings())
        d.run()
        curr_writes = [c for c in d.commands()
                       if c["command"] == "WriteA32" and c["crc_valid"]
                       and c["address"] == 0x20C1]
        assert len(curr_writes) == 1, (interval, [c["address"] for c in d.commands()
                                                   if c["command"] == "WriteA32"])
        assert curr_writes[0]["enablech_curr_error"] is want_error, (
            f"interval={interval}: expected enablech_curr_error={want_error}, "
            f"got {curr_writes[0]['enablech_curr_error']}")


def test_enablech_curr_error_field_defaults_false_on_normal_demo():
    """The new command field must exist on every decoded command and default to False:
    the synthetic demo's control traffic never writes an EnableCh _CURR address, so no
    command should be flagged (a false positive here would mean the address/offset check
    is too broad and is catching ordinary _NEXT writes or unrelated registers)."""
    dec = decode_demo()
    cmds = dec.commands()
    assert len(cmds) > 0
    assert all("enablech_curr_error" in c for c in cmds)
    assert not any(c["enablech_curr_error"] for c in cmds), (
        "false positive: the demo never writes an EnableCh _CURR address")


def test_scrambler_resets_on():
    # PortControl.ScramblerEn resets to 1 (scrambling ON). The demo writes it OFF only for
    # DP0/DP2 (to show unscrambled transport alongside scrambled); DP1/DP3 are never
    # written, so they must decode as scrambler-on by DEFAULT — the reset-seed regression
    # guard (a capture that leaves it at reset must decode as scrambled, else a square-wave
    # payload comes back as noise).
    dec = decode_demo()
    by_dp = {dp["dp_number"]: dp["scrambler_en"]
             for dp in dec.config_dataports()["dataports"] if dp["enabled"]}
    assert by_dp.get(1) and by_dp.get(3), by_dp          # un-written -> reset default ON
    assert not by_dp.get(0) and not by_dp.get(2), by_dp  # explicitly written OFF


def test_audio_roundtrips_to_sine():
    dec = decode_demo()
    audio = dec.audio()
    assert audio, "no audio decoded"
    # Four streams on one device: dp0/dp1 = 16-bit PCM, dp2/dp3 = 1-bit PDM.
    streams = {(a["device"], a["dp"]) for a in audio}
    assert streams == {(0, 0), (0, 1), (0, 2), (0, 3)}, streams
    # PCM ports round-trip bit-exactly to the generated sine, continuously across the
    # 8->16 commit (tone == dp*2 for the single enabled channel ch0).
    for dp in (0, 1):
        vals = [a["value"] for a in audio if a["device"] == 0 and a["dp"] == dp]
        ss = next(a["sample_size"] for a in audio if a["dp"] == dp) - 1
        assert ss == 15, f"dp{dp} expected 16-bit PCM, got {ss + 1}-bit"
        assert len(vals) >= 16
        for i, v in enumerate(vals):
            assert v == _expected_sine(dp * 2, i, ss), f"dp{dp}[{i}]: {v} != {_expected_sine(dp*2, i, ss)}"
    # PDM ports are 1-bit density streams (values in {0, 1}).
    for dp in (2, 3):
        vals = {a["value"] for a in audio if a["device"] == 0 and a["dp"] == dp}
        ss = next(a["sample_size"] for a in audio if a["dp"] == dp)
        assert ss == 1, f"dp{dp} expected 1-bit PDM, got {ss}-bit"
        assert vals <= {0, 1} and vals, f"dp{dp} PDM values {vals}"


def test_measured_ui_rate():
    """The decoder reports the forwarded-clock UI rate from the capture. The demo
    source is 4 samples/UI at 98.304 MHz, so the UI rate is ~24.576 MHz. (Guards
    the continuous UI-rate tracker: a wrong rate scales the reported row rate and
    every audio sample rate — the symptom of the cold-start clock-change bug where
    a tone played back at 1/4 pitch.)"""
    dec = decode_demo()
    assert abs(dec.measured_ui_rate_hz - 98_304_000 / 4) < 1000, dec.measured_ui_rate_hz
    # row rate = ui rate / columns.
    assert abs(dec.row_rate_khz - (dec.measured_ui_rate_hz / dec.column_count) / 1000) < 1e-6


def test_audio_rate_stable_with_audio_decode():
    """Decoding WITH audio must not change the measured UI rate vs without — the
    cold-start regression was that extra (now CRC-valid) commands suppressed the
    rate-correcting resync only when audio decode was on, leaving the reported rate 4x
    low. The UI rate (not the final column count, which now varies with how far each
    path tracks the 2->8->16 geometry) is the invariant."""
    levels = swi3score.make_demo_levels(300)
    rates = []
    for audio_on in (False, True):
        src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
        s = swi3score.DecoderSettings()
        s.decode_audio = audio_on
        d = swi3score.Decoder(src, s)
        d.run()
        rates.append(round(d.measured_ui_rate_hz, -3))   # nearest kHz; catches a 4x error
    assert rates[0] == rates[1], rates


def _audio_counts(overrides):
    """Per-(device, dp) audio-sample counts decoding the demo with the given what-if
    register overrides (list of (section, device, address, value)). Section 0 is the
    2-col safe-lock (no audio); 1 = 8-col, 2 = 16-col."""
    levels = swi3score.make_demo_levels(300)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    s = swi3score.DecoderSettings()
    s.register_overrides = overrides
    d = swi3score.Decoder(src, s)
    d.run()
    counts = {}
    for a in d.audio():
        counts[(a["device"], a["dp"])] = counts.get((a["device"], a["dp"]), 0) + 1
    return counts


def test_register_override_drives_audio():
    """The unification headline: a what-if register override applied in the C++ decode
    changes the AUDIO output — not just the grid/register projection. The decode reads
    the _CURR (committed) rank, so forcing DP0's EnableCh0/HCount _CURR alias (0x20C1,
    the committed peer of the _NEXT 0x2081) to 0 clears ch0 and drops that port's audio."""
    base = _audio_counts([])
    assert base.get((0, 0)), base
    ovr = _audio_counts([(1, 0, 0x20C1, 0x00)])   # (section 0, device 0, _CURR alias, value)
    assert ovr != base, "override did not change audio"
    assert ovr.get((0, 0), 0) < base[(0, 0)], (base, ovr)


def test_register_override_section_scoped():
    """Overrides are section-scoped: one keyed to a section the capture doesn't
    have (index 9 here) must not touch the decode."""
    base = _audio_counts([])
    assert _audio_counts([(9, 0, 0x20C1, 0x00)]) == base


def test_register_override_no_cross_port_corruption():
    """Applying an override to one port (via a COPY of the live register model)
    must not perturb the other ports' audio (Risk R2 — no live-staging corruption)."""
    base = _audio_counts([])
    ovr = _audio_counts([(1, 0, 0x20C1, 0x00)])   # only dev0/dp0 affected (_CURR alias)
    assert ovr.get((0, 0), 0) < base.get((0, 0), 0), (base, ovr)   # dp0 did drop
    for stream in ((0, 1), (0, 2), (0, 3)):
        assert ovr.get(stream) == base.get(stream), (stream, base, ovr)


def test_wide_bit_layout_spans_columns():
    """A data port with BitWidth>0 (wide bits) holds each data bit across
    (BitWidth+1) adjacent columns in the grid — so the renderer merges them into a
    `C{ch}B{bit} xN` cell, matching the Visualizer. The bit IDENTITY is stamped on
    EVERY UI of the wide bit (not just the sample point), so it's correct for a source
    (sampled first UI) AND a sink (sampled last UI). Previously a wide SINK bit was
    mislabelled — the pair straddled rows as `C0B15, C0B14` with a phantom leading
    `C-1B*` held column. A wide bit needs HorizontalCount increased to have room."""
    from swi3s_studio.model.registers import DP_BASE, DP_STRIDE
    from swi3s_studio.session import Session
    s = Session.from_demo(64, cold_start=True)
    smp = int(s.capture.clock_edges[-1])
    dev, dp = 0, 0
    sect = s.segment_index_for_sample(smp)
    base = DP_BASE + dp * DP_STRIDE

    def row0(is_sink: bool):
        s.clear_register_overrides()
        if is_sink:
            pc = s.register_files_at(smp)[dev].value(base + 0x0B)
            s.set_register_override(sect, dev, base + 0x0B, pc | 0x04)   # PortDirection=Sink (single-ranked)
        # The grid/decode reads the committed (_CURR) config, so force the _CURR aliases
        # (dual-ranked DP regs: _CURR = _NEXT + 0x40).
        s.set_register_override(sect, dev, base + 0xC1, 0x86)   # EnableCh0=1, HorizontalCount=6 (_CURR)
        s.set_register_override(sect, dev, base + 0xC0, 0x41)   # BitWidth=1 (wide x2), HStart=1 (_CURR)
        cells = sorted([c for c in s.grid_cells_at(smp, 4)
                        if c.get("dp") == dp and c.get("device") == dev and c.get("slot") == 1
                        and c["row"] == 0], key=lambda c: c["col"])
        return cells

    for is_sink in (False, True):
        cells = row0(is_sink)
        bits = [c["bit"] for c in cells]
        # Each bit repeats across 2 adjacent columns (wide x2). dp0 is 16-bit PCM
        # (SampleSize=15, single-rank), so MSB-first the columns are 15,15,14,14,13,13.
        assert bits[:6] == [15, 15, 14, 14, 13, 13], (is_sink, bits)
        # No phantom held column, and each pair shares channel + sample so it merges.
        assert all(c["channel"] >= 0 for c in cells), (is_sink, cells)
        assert all(cells[2 * i]["channel"] == cells[2 * i + 1]["channel"]
                   and cells[2 * i]["sample"] == cells[2 * i + 1]["sample"] for i in range(3))
    s.clear_register_overrides()


def test_samples_by_position_tiles_the_full_ordering():
    """The lazy Samples window is built from position slices: sample_marks_total /
    sample_position_of / samples_by_position must tile the same ascending order that a
    huge samples_around window produces, with no gaps or overlaps at chunk boundaries."""
    from swi3s_studio.session import Session
    s = Session.from_demo(64, cold_start=True)
    total = s.sample_marks_total()
    assert total > 0
    full = s.samples_around(0, half_window=total + 10)          # whole ordering in one shot
    assert len(full) == total
    full_starts = [d["start_sample"] for d in full]
    assert full_starts == sorted(full_starts)
    # Tile [0,total) in position chunks; the concatenation must equal `full` exactly.
    tiled = []
    for lo in range(0, total, 37):
        tiled += s.samples_by_position(lo, lo + 37)
    assert [d["start_sample"] for d in tiled] == full_starts
    assert [d["index"] for d in tiled] == [d["index"] for d in full]
    # sample_position_of lands a cursor on the first sample at/after it.
    mid_sample = full[total // 2]["start_sample"]
    pos = s.sample_position_of(mid_sample)
    assert full[pos]["start_sample"] == mid_sample or full_starts[pos] >= mid_sample
    # A permissive value filter keeps the full count.
    assert s.sample_marks_total(value_pred=(">=", -(1 << 40))) == total


def test_samples_around_window_and_filter_agree():
    """samples_around's no-filter fast path (direct bisect window) must agree with the
    filtered path. A permissive value filter that keeps everything should yield the
    same window as no filter; and the returned samples are centred on the cursor with
    correct signed values (cached in the mark cache)."""
    from swi3s_studio.session import Session
    s = Session.from_demo(64, cold_start=True)
    mid = int(s.capture.clock_edges[len(s.capture.clock_edges) // 2])
    hw = 50
    no_filter = s.samples_around(mid, half_window=hw)
    assert no_filter, "expected decoded samples around the cursor"
    # A predicate that keeps every value (>= a very negative threshold) hits the
    # filtered branch but must select the same rows as the fast path.
    keep_all = s.samples_around(mid, half_window=hw, value_pred=(">=", -(1 << 40)))
    assert [d["start_sample"] for d in no_filter] == [d["start_sample"] for d in keep_all]
    # Window is bounded and ascending by start_sample.
    starts = [d["start_sample"] for d in no_filter]
    assert starts == sorted(starts) and len(starts) <= 2 * hw
    # signed value matches a direct two's-complement interpretation for a multi-bit row.
    for d in no_filter:
        b = d["sample_size"]
        if b > 1:
            v = d["value"]
            assert d["signed"] == (v - (1 << b) if v >= (1 << (b - 1)) else v)


def test_native_abi_guard():
    """A stale swi3score .so (built before a binding's return shape changed) must fail
    with a clear 'rebuild' message, not a cryptic unpack error. The native module
    exposes score_abi; the Session checks it. Simulate an old module by lowering it."""
    import swi3score

    from swi3s_studio import session as S
    assert int(getattr(swi3score, "score_abi", 0)) >= S._REQUIRED_SCORE_ABI, \
        "the loaded swi3score is itself stale — rebuild the native core"
    saved = swi3score.score_abi
    try:
        swi3score.score_abi = S._REQUIRED_SCORE_ABI - 1     # pretend it's old
        try:
            S._require_score_abi()
            raise AssertionError("expected a stale-module error")
        except RuntimeError as e:
            msg = str(e).lower()
            assert "out of date" in msg and "rebuild" in msg, e
    finally:
        swi3score.score_abi = saved
