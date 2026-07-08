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


def decode_demo(samples_per_channel: int = 32):
    levels = swi3score.make_demo_levels(samples_per_channel)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    settings = swi3score.DecoderSettings()  # auto column count, audio on
    dec = swi3score.Decoder(src, settings)
    dec.run()
    return dec


def test_column_autodetect():
    dec = decode_demo()
    assert dec.column_count == 16        # two devices, 16-column grid


def test_control_phases_decode():
    dec = decode_demo()
    cmds = dec.commands()
    writes = [c for c in cmds if c["command"] == "WriteA32" and c["crc_valid"]]
    pings = [c for c in cmds if c["command"] == "Ping"]
    sscr = [c for c in cmds if c["is_commit"] and c["commit_confirmed"]]
    # 2x NumColumns + 2 devices x 2 DPs x 4 register writes = 18.
    assert len(writes) == 18, f"expected 18 writes, got {len(writes)}"
    assert len(pings) >= 2
    assert len(sscr) == 1
    # The first write programs the column count (SLC NumColumns_NEXT @ 0x1081).
    assert writes[0]["address"] == 0x1081
    # Timestamps are monotonic.
    starts = [c["start_sample"] for c in cmds]
    assert starts == sorted(starts)


def test_scrambler_resets_on():
    # PortControl.ScramblerEn resets to 1 (scrambling ON). The demo never writes
    # PortControl, so every enabled DP must decode as scrambler-on by default — a
    # capture that leaves it at reset decodes as scrambled (else the payload, e.g.
    # a square-wave data stream, comes back as noise).
    dec = decode_demo()
    enabled = [dp for dp in dec.config_dataports()["dataports"] if dp["enabled"]]
    assert enabled and all(dp["scrambler_en"] for dp in enabled), \
        [(dp["device_number"], dp["dp_number"], dp["scrambler_en"]) for dp in enabled]


def test_audio_roundtrips_to_sine():
    dec = decode_demo()
    audio = dec.audio()
    assert audio, "no audio decoded"
    # Four streams: 2 devices x 2 DPs.
    streams = {(a["device"], a["dp"]) for a in audio}
    assert streams == {(0, 0), (0, 1), (1, 0), (1, 1)}, streams
    sample_size = audio[0]["sample_size"] - 1  # stored sampleSize = bits; field = bits-1
    # Dev0/DP0 uses tone == channel, so it matches _expected_sine directly.
    by_ch = {}
    for a in audio:
        if a["device"] == 0 and a["dp"] == 0:
            by_ch.setdefault(a["channel"], []).append(a["value"])
    assert set(by_ch) == {0, 1}
    for ch, vals in by_ch.items():
        assert len(vals) >= 16
        for i, v in enumerate(vals):
            assert v == _expected_sine(ch, i, sample_size), f"dev0 dp0 ch{ch}[{i}]"


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
    """Decoding WITH audio must not change the measured rates vs without — the
    cold-start regression was that extra (now CRC-valid) commands suppressed the
    rate-correcting resync only when audio decode was on, leaving the reported
    rate 4x low. Both paths must agree."""
    levels = swi3score.make_demo_levels(32)
    rates = []
    for audio_on in (False, True):
        src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
        s = swi3score.DecoderSettings()
        s.decode_audio = audio_on
        d = swi3score.Decoder(src, s)
        d.run()
        rates.append((d.column_count, round(d.row_rate_khz, 3)))
    assert rates[0] == rates[1], rates


def _audio_counts(overrides):
    """Per-(device, dp) audio-sample counts decoding the demo with the given
    what-if register overrides (list of (section, device, address, value))."""
    levels = swi3score.make_demo_levels(64)
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
    """The unification headline: a what-if register override applied in the C++
    decode changes the AUDIO output — not just the grid/register projection.
    Forcing DP0's EnableCh_L (0x2090) to 0 for section 0 drops that port's audio."""
    base = _audio_counts([])
    assert base.get((0, 0)), base
    ovr = _audio_counts([(0, 0, 0x2090, 0x00)])   # (section 0, device 0, addr, value)
    assert ovr != base, "override did not change audio"
    assert ovr.get((0, 0), 0) < base[(0, 0)], (base, ovr)


def test_register_override_section_scoped():
    """Overrides are section-scoped: one keyed to a section the capture doesn't
    have (index 5 in a single-section demo) must not touch the decode."""
    base = _audio_counts([])
    assert _audio_counts([(5, 0, 0x2090, 0x00)]) == base


def test_register_override_no_cross_port_corruption():
    """Applying an override to one port (via a COPY of the live register model)
    must not perturb the other ports' audio (Risk R2 — no live-staging corruption)."""
    base = _audio_counts([])
    ovr = _audio_counts([(0, 0, 0x2090, 0x00)])   # only dev0/dp0 affected
    for stream in ((0, 1), (1, 0), (1, 1)):
        assert ovr.get(stream) == base.get(stream), (stream, base, ovr)


def test_wide_bit_layout_spans_columns():
    """A data port with BitWidth>0 (wide bits) holds each data bit across
    (BitWidth+1) adjacent columns in the grid — so the renderer merges them into a
    `C{ch}B{bit} xN` cell, matching the Visualizer. The bit IDENTITY is stamped on
    EVERY UI of the wide bit (not just the sample point), so it's correct for a source
    (sampled first UI) AND a sink (sampled last UI). Previously a wide SINK bit was
    mislabelled — the pair straddled rows as `C0B15, C0B14` with a phantom leading
    `C-1B*` held column. A wide bit needs HorizontalCount increased to have room."""
    from swi3s_studio.session import Session
    from swi3s_studio.model.registers import DP_BASE, DP_STRIDE
    s = Session.from_demo(64, cold_start=True)
    smp = int(s.capture.clock_edges[-1])
    dev, dp = 0, 0
    sect = s.segment_index_for_sample(smp)
    base = DP_BASE + dp * DP_STRIDE

    def row0(is_sink: bool):
        s.clear_register_overrides()
        if is_sink:
            pc = s.register_files_at(smp)[dev].value(base + 0x0B)
            s.set_register_override(sect, dev, base + 0x0B, pc | 0x04)   # PortDirection=Sink
        s.set_register_override(sect, dev, base + 0x81, 0x86)   # EnableCh0=1, HorizontalCount=6
        s.set_register_override(sect, dev, base + 0x80, 0x41)   # BitWidth=1 (wide x2), HStart=1
        cells = sorted([c for c in s.grid_cells_at(smp, 4)
                        if c.get("dp") == dp and c.get("device") == dev and c.get("slot") == 1
                        and c["row"] == 0], key=lambda c: c["col"])
        return cells

    for is_sink in (False, True):
        cells = row0(is_sink)
        bits = [c["bit"] for c in cells]
        # Each bit repeats across 2 adjacent columns (wide x2): 7,7,6,6,5,5,…
        assert bits[:6] == [7, 7, 6, 6, 5, 5], (is_sink, bits)
        # No phantom held column, and each pair shares channel + sample so it merges.
        assert all(c["channel"] >= 0 for c in cells), (is_sink, cells)
        assert all(cells[2 * i]["channel"] == cells[2 * i + 1]["channel"]
                   and cells[2 * i]["sample"] == cells[2 * i + 1]["sample"] for i in range(3))
    s.clear_register_overrides()


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


if __name__ == "__main__":
    d = decode_demo()
    print(f"columns={d.column_count} row_rate={d.row_rate_khz:.1f}kHz "
          f"ui_rate={d.measured_ui_rate_hz/1e6:.3f}MHz "
          f"commands={len(d.commands())} audio={len(d.audio())}")
    test_column_autodetect()
    test_control_phases_decode()
    test_scrambler_resets_on()
    test_audio_roundtrips_to_sine()
    test_measured_ui_rate()
    test_audio_rate_stable_with_audio_decode()
    test_register_override_drives_audio()
    test_register_override_section_scoped()
    test_register_override_no_cross_port_corruption()
    test_wide_bit_layout_spans_columns()
    test_samples_around_window_and_filter_agree()
    test_native_abi_guard()
    print("ALL PYTHON CORE TESTS PASSED")
