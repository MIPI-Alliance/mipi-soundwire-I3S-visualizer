"""Flow-control demo: bit-exact round-trip + the mid-phase SPM framing-lock fix.

The synthetic flow-control demo (swi3score.make_demo_levels_flow_control) carries the
four data ports a bus sniffer actually sees — one per flow mode, on four devices:

    dev0  NORMAL         ch4        48 ksps, every interval transports (no gating)
    dev1  TX_CONTROLLED  ch5,6      96 k transport opportunities/s, 0/1 interval jitter
    dev2  RX_CONTROLLED  ch7,8      "
    dev3  ASYNC          ch7,8      "

Audio is sampled UNIFORMLY at 48 kHz; only the TRANSPORT is jittered (deterministic
0/1-interval, gated by TX_PRESENT). The decoder's TX_PRESENT gating drops the unused
opportunities, so audio() is the de-jittered 48 ksps stream and round-trips bit-exact
to the generated sine.

This file also guards the decoder fix that made dev1 decode at all: a K.28.7 SPM
pattern that appears by chance inside a Write payload/CRC byte must NOT re-frame or
abandon the in-progress phase (SWI3S v1.1 §7.2.2 {ASW1907}: hold symbol alignment by
counting bits mod 10 mid-phase; §8.2.1 {ASW2206}: a mid-phase SPM only ARMS header
detection). Before the fix, dev1's WriteA32(0x2090, 0x60) forged a comma and its whole
config block was dropped.
"""
import math
import os
from collections import defaultdict

import pytest
import swi3score

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.join(_HERE, "fixtures")

# Per-(device, channel) tone table. MUST match Demo.cpp flowDemoFreq() exactly.
_FLOW_FREQ = {
    (0, 4): 20.0,
    (1, 5): 55.0, (1, 6): 110.0,
    (2, 7): 220.0, (2, 8): 440.0,
    (3, 7): 880.0, (3, 8): 1760.0,
}
_FLOW_SS = 15                       # 16-bit samples throughout
_FLOW_RATE_HZ = 48000.0


def _flow_expected(device: int, channel: int, index: int) -> int:
    """Regenerate the demo's uniform 48 kHz sine sample (mirrors Demo.cpp sineAt)."""
    amp = (1 << _FLOW_SS) * 0.45
    freq = _FLOW_FREQ[(device, channel)]
    v = int(round(amp * math.sin(2.0 * math.pi * freq * index / _FLOW_RATE_HZ)))
    return v & ((1 << (_FLOW_SS + 1)) - 1)


def _decode_flow(samples_per_channel: int = 200):
    levels = swi3score.make_demo_levels_flow_control(samples_per_channel)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    dec = swi3score.Decoder(src, swi3score.DecoderSettings())
    dec.run()
    return dec


def test_flow_demo_loads_clean_via_session_cold_start():
    """Load the demo the way the APP does — Session.from_demo(cold_start=True) — and
    assert the decode is error-free. A cold-started decoder leaves PHY-select seeded at
    Safe-Lock-2 and GROWS the column count by snooping the commit, so the capture must
    present the 2->16 ladder. Regression: the demo originally jumped straight to 16
    columns, so cold_start left the decoder stuck at 2 columns and EVERY command
    mis-framed (2622 CRC errors) — invisible to the raw-levels/cold_start=False path the
    other tests use. Runs at a realistic size so the audio phase (where it broke) is
    exercised."""
    from collections import Counter

    from swi3s_studio.analysis.errors import command_error, count_errors
    from swi3s_studio.session import Session

    s = Session.from_demo(4000, cold_start=True, variant="flow_control")
    d = s.decoder
    cmds = d.commands()
    assert d.column_count == 16, f"decoder stuck at {d.column_count} columns"
    assert [seg["column_count"] for seg in d.segments()] == [2, 16]
    n_err = count_errors(cmds)
    assert n_err == 0, (f"{n_err}/{len(cmds)} commands flagged as errors: "
                        f"{Counter(command_error(c) for c in cmds if command_error(c)).most_common(3)}")


def test_flow_all_four_devices_present():
    """All four peripheral devices (one per flow mode) must decode. Regression for the
    mid-phase-SPM desync that dropped device 1 (TX_CONTROLLED): its config-block write
    WriteA32(0x2090, 0x60) forms a spurious K.28.7 comma at a sub-symbol offset, which
    used to re-frame the decoder and wipe the device's registers."""
    dec = _decode_flow()
    devices = {dp["device_number"] for dp in dec.config_dataports()["dataports"]
               if dp["enabled"] and dp["enable_ch"]}
    assert devices == {0, 1, 2, 3}, f"missing device(s): {sorted({0,1,2,3} - devices)}"


def test_flow_modes_decoded_per_device():
    """Each device carries the expected flow mode (0/1/2/3)."""
    dec = _decode_flow()
    fm = {dp["device_number"]: dp["flow_mode"]
          for dp in dec.config_dataports()["dataports"] if dp["enabled"] and dp["enable_ch"]}
    assert fm == {0: 0, 1: 1, 2: 2, 3: 3}, fm


def test_flow_audio_bit_exact_all_modes():
    """Every stream — across all four flow modes, jittered transport included — must
    round-trip BIT-EXACT to the uniformly-sampled 48 kHz sine. This is the headline
    validation: TX_PRESENT gating de-jitters the transport back to the source stream."""
    dec = _decode_flow(200)
    by = defaultdict(list)
    for a in dec.audio():
        by[(a["device"], a["channel"])].append(a)
    assert set(by) == set(_FLOW_FREQ), f"stream set mismatch: {sorted(by)}"

    for (dev, ch), samples in sorted(by.items()):
        seq = [s["value"] for s in sorted(samples, key=lambda s: s["index"])]
        assert len(seq) >= 100, f"dev{dev} ch{ch}: only {len(seq)} samples"
        exp = [_flow_expected(dev, ch, i) for i in range(len(seq))]
        first_bad = next((i for i, (x, y) in enumerate(zip(seq, exp)) if x != y), None)
        assert seq == exp, f"dev{dev} ch{ch} diverges at sample {first_bad}"


def test_flow_flow_mode_surfaced_on_samples():
    """Each decoded sample carries its port's flow_mode (ABI 7 field)."""
    dec = _decode_flow(80)
    fm_by_dev = defaultdict(set)
    for a in dec.audio():
        fm_by_dev[a["device"]].add(a["flow_mode"])
    assert fm_by_dev[0] == {0} and fm_by_dev[1] == {1}
    assert fm_by_dev[2] == {2} and fm_by_dev[3] == {3}


def test_flow_all_modes_deliver_full_rate():
    """Every flow mode delivers the full audio rate over the capture — no sustained
    under-run. Guards the ASYNC model: the source must honor the sink's standing DRQ and
    deliver every sample (bounded 0/1-interval delay), NOT decline ~half of them (which
    made dev3 run at ~24 kHz / an octave low). All four devices' single-channel-equivalent
    counts must be within 15% of the NORMAL reference."""
    from swi3s_studio.session import Session
    s = Session.from_demo(4000, cold_start=True, variant="flow_control")
    counts = defaultdict(int)
    for a in s.audio:                      # the Session's copy (decoder's is released)
        counts[a["device"]] += 1
    # per-channel counts (dev0/1 have 1/2 ch; dev2/3 have 2 ch) -> normalize to per-channel
    nch = {0: 1, 1: 2, 2: 2, 3: 2}
    per_ch = {d: counts[d] / nch[d] for d in (0, 1, 2, 3)}
    ref = per_ch[0]
    for d in (1, 2, 3):
        assert abs(per_ch[d] - ref) / ref < 0.15, \
            f"dev{d} delivered {per_ch[d]:.0f}/ch vs NORMAL {ref:.0f} — under/over-delivery"


def test_flow_display_dejittered_uniform():
    """Flow-controlled audio is de-jittered for display: the audio store lays each
    stream's samples on a UNIFORM capture-sample grid (a receiver FIFO clocks out at the
    average input rate), so a bit-exact sine renders clean. The per-sample gaps of a
    flow-controlled stream must be uniform (max-min <= 1 tick of rounding), even though
    the raw transport gaps swing 2:1+."""
    import numpy as np

    from swi3s_studio.session import Session
    s = Session.from_demo(4000, cold_start=True, variant="flow_control")
    st = s.audio_store()
    for (dev, dp, ch) in [(1, 0, 5), (2, 0, 7), (3, 0, 7)]:   # TX, RX, ASYNC
        pos = st.sample_positions(dev, dp, ch)
        assert pos is not None and len(pos) > 10
        gaps = np.diff(np.asarray(pos, dtype=np.int64))
        assert int(gaps.max() - gaps.min()) <= 1, \
            f"dev{dev} not de-jittered: gap spread {int(gaps.max()-gaps.min())}"


def test_flow_drq_cells_on_bus_grid():
    """The DRQ (flow-control-port) cells must appear on the bus grid. The grid layout
    drives a CFlowControlPort per drq-enabled port in lockstep with the data port and
    emits DRQ cells; without it the FCP is invisible (the reported bug). Check the
    analyzer's navigated grid_cells_at path for slot==DRQ in the audio phase."""
    import swi3score

    from swi3s_studio.session import Session
    drq_slot = (int(swi3score.SLOT_NAMES.index("Drq"))
                if hasattr(swi3score, "SLOT_NAMES") else 6)
    s = Session.from_demo(4000, cold_start=True, variant="flow_control")
    # Navigate to the audio phase (a DRQ-mode sample) — the cold-start start has none.
    a = [x for x in s.audio if x["device"] == 2]      # Session copy, not the decoder's
    mid = int(a[len(a) // 2]["start_sample"])
    cells = s.grid_cells_at(mid, rows=32)
    drq = [c for c in cells if c["slot"] == drq_slot]
    assert drq, "no DRQ cells on the bus grid (FCP not placed)"
    # DRQ for a SOURCE data port (dev2/dev3) is a SINK DRQ (the FCP samples the bus).
    assert all(c["device"] in (2, 3) and not c["is_source"] for c in drq)


def test_flow_normal_reference_is_ungated():
    """The NORMAL device (dev0) has no TX_PRESENT, so every transport opportunity
    carries a sample — its count equals the requested samples/channel (+ the small
    tail the demo adds), and it is contiguous."""
    n = 120
    dec = _decode_flow(n)
    dev0 = [a for a in dec.audio() if a["device"] == 0]
    # dev0 is 1 channel; should have at least the requested count.
    assert len(dev0) >= n, f"NORMAL dev0: {len(dev0)} < {n}"
    idx = sorted(a["index"] for a in dev0)
    assert idx == list(range(len(idx))), "NORMAL sample index not contiguous"


def test_flow_writes_all_crc_valid():
    """Every config WriteA32 in the flow-control stream decodes CRC-valid — i.e. no
    frame is corrupted by a spurious in-payload comma (the framing-lock fix keeps the
    phase aligned through payload+CRC)."""
    dec = _decode_flow(60)
    writes = [c for c in dec.commands() if c["command"] == "WriteA32"]
    assert writes, "no writes decoded"
    bad = [c for c in writes if not c["crc_valid"]]
    assert not bad, f"{len(bad)} WriteA32 frames failed CRC (framing desync?)"


def test_flow_handshake_validates_clean():
    """The decoder re-drives the FCP, reads the DRQ bits off the bus, and validates the
    DRQ<->TxPresent handshake (SWI3S §14.2.2 {ASW5205}). For the well-formed demo every
    transported sample was correctly requested with the legal d = FlowControlDelay+1
    pipeline, so there must be zero handshake failures over many checked opportunities
    and many observed DRQ bits."""
    dec = _decode_flow(200)
    fc = dec.flow_control_stats()
    assert fc["drq_bits"] > 0, "no DRQ bits observed (FCP not re-driven?)"
    assert fc["checked"] > 0, "no transport opportunities validated"
    assert fc["fails"] == 0, f"{fc['fails']} handshake violations of {fc['checked']} checked"


def test_flow_delay_decoded_from_registers():
    """The DRQ->TxPresent pipeline depth (DPn_FlowControlDelay, 0x0E bit7) is decoded so
    the handshake validator uses the right lookback. The demo leaves it at the reset
    default (1 -> d=2 intervals) for the DRQ-mode ports."""
    dec = _decode_flow(60)
    dps = {dp["device_number"]: dp for dp in dec.config_dataports()["dataports"]
           if dp["enabled"] and dp["enable_ch"]}
    for dev in (2, 3):                      # RX_CONTROLLED, ASYNC
        assert "flow_control_delay" in dps[dev], "flow_control_delay not surfaced"
        assert dps[dev]["flow_control_delay"] in (0, 1)


def test_flow_control_delay_round_trips_through_registers():
    """A config CSV's DPn_FlowControlDelay must survive registers_from_csv — it is bit 7
    of 0x0E, and the register emit used to drop it (forcing 0), which made a config-CSV
    baseline mismatch a real decode for RX/ASYNC ports. The demo's config leaves it at the
    reset default (1), so every 0x0E register for a DRQ mode must carry bit 7 set.

    Resolved from __file__, not the working directory. registers_from_csv returns [] for a
    path it cannot open rather than raising, so a CWD-relative name failed here as
    `assert []` — reading like a decode regression instead of a missing file.

    The fixture lives under tests/fixtures/ rather than visualizer_examples/ deliberately:
    three suites glob that corpus recursively, and this config is the only one whose
    DataPortNumber differs from its slot index (four peripherals each using their own DP0),
    which trips a cross-engine divergence recorded in the maintainers' debt register."""
    import swi3score
    csv = os.path.join(_FIXTURES, "flow_control_demo.csv")
    assert os.path.isfile(csv), f"missing fixture {csv}"
    regs = swi3score.registers_from_csv(csv)
    flow0e = [(dev, val) for dev, addr, val in regs if (addr & 0xFF) == 0x0E]
    assert flow0e, "no 0x0E (FlowMode) registers emitted"
    # RX/ASYNC ports (mode 2/3 in the low bits) must carry FlowControlDelay=1 (bit 7).
    drq = [(dev, val) for dev, val in flow0e if (val & 0x3) in (2, 3)]
    assert drq, "no RX/ASYNC ports found"
    assert all(val & 0x80 for dev, val in drq), \
        f"FlowControlDelay bit7 dropped: {[(d, hex(v)) for d, v in drq]}"


def test_flow_drq_sample_annotated_on_samples():
    """Each decoded flow-controlled sample carries drq_sample — the capture position of
    the DRQ that permitted it (ABI 7). Guards against the field being left dead-zero."""
    dec = _decode_flow(200)
    drq_ports = [a for a in dec.audio() if a["device"] in (2, 3)]   # RX / ASYNC
    assert drq_ports, "no DRQ-mode samples"
    assert any(a["drq_sample"] > 0 for a in drq_ports), \
        "drq_sample never populated (dead reserved field)"


@pytest.mark.parametrize("phy,variant", [(1, ""), (2, ""), (3, ""), (2, "flow_control")])
def test_audio_idle_cds_is_d10_2_not_flat(phy, variant):
    """The CDS idle between keep-alive Pings during the audio phase must be the D10.2
    Protocol-Spacer pattern 0101010101 (SWI3S §8.1.2.9 / §7.1.1.3, {ASW2201}: with no
    Command to send the Manager drives a continuous alternating 0-1 idle stream, one CDS
    bit per Row), NOT all-ones. Regression: the demo synthesiser filled idle Rows with
    all-ones, which NRZS-holds the FBCSE bus flat (a dead line, no idle zebra) — invisible
    to the decode/bit-exact tests because idle bits carry no phase. All four menu demos
    share the fillIdleWithPings chokepoint, so this checks every PHY plus flow control.
    Framed on the audio segment, every full idle symbol must decode to 0x155 (0101010101)
    and none to 0x2AA (the 1010 inverse).

    All-ones (0x3FF) is NOT simply banned any more: since 3.0.11 a Ping's PingInfo field
    leaves ABSENT peripherals undriven, and an undriven bus IS all ones (it decodes to
    token -1 = NO_RESPONSE). Those are legitimate and bounded — at most one per
    peripheral, so at most kMaxPeripherals in a row. A flat-idle regression is the
    opposite shape: hundreds of consecutive all-ones symbols where the idle stream should
    be. So the assertion is on the RUN LENGTH, which still bites the original bug."""
    from collections import Counter

    from swi3s_studio.session import Session

    s = Session.from_demo(400, cold_start=True, phy=phy, variant=variant)
    a = list(s.audio)                                 # Session copy, not the decoder's
    mid = int(a[len(a) // 2]["start_sample"])
    raws = [int(sy.get("raw", -1)) & 0x3FF for sy in s.symbols_around(mid, max_symbols=1500)]
    c = Counter(raws)
    assert c.get(0x155, 0) > 50, f"no D10.2 (0x155) idle in audio CDS: {c.most_common(4)}"
    assert c.get(0x2AA, 0) == 0, f"idle framed as 0x2AA (1010, non-canonical): {c.get(0x2AA)}"

    longest = run = 0
    for r in raws:
        run = run + 1 if r == 0x3FF else 0
        longest = max(longest, run)
    assert longest <= 12, (
        f"{longest} consecutive all-ones (0x3FF) symbols — the idle CDS regressed to a "
        f"flat line (an undriven PingInfo field is at most 12 in a row)")


def test_manager_dataports_emit_no_peripheral_registers():
    """A MANAGER data port is not addressable as a peripheral register write, and must not be
    emitted as one.

    A config CSV encodes the manager as device 0 + ManagerDataport=True, because
    DeviceNumber_REG holds 0..11 and cannot carry the -1 sentinel. The C++ CSV reader took
    only the first half until 3.0.13 and explicitly documented ManagerDataport as ignored, so
    every manager port impersonated device 0 — which is a REAL peripheral address, and this
    fixture has a genuine device-0 port as well. The result was 103 register writes addressed
    to device 0 against 23 for each of its peers, including two conflicting values for the
    same DP0 FlowMode register.

    The emitter itself was always correct: both loops in registersFromConfig skip
    deviceNum < 0. Only the sentinel had to survive the parse.
    """
    import swi3score
    csv = os.path.join(_FIXTURES, "flow_control_demo.csv")
    regs = swi3score.registers_from_csv(csv)

    # Guard the fixture's shape by reading its own encoding, so the test cannot quietly stop
    # covering the case: at least one ENABLED port must be flagged ManagerDataport.
    rows = {}
    with open(csv, encoding="utf-8") as f:
        for line in f:
            cells = line.rstrip("\n").split(",")
            rows[cells[0]] = cells[1:]
    managed = [m == "True" for m in rows["ManagerDataport"]]
    enabled = [e == "True" for e in rows["Enabled"]]
    assert any(m and e for m, e in zip(managed, enabled)), \
        "fixture no longer declares an enabled manager data port — this test is now blind"
    assert any(not m and e for m, e in zip(managed, enabled)), \
        "fixture has no enabled PERIPHERAL port, so device 0 collision cannot occur"

    devices = sorted({dev for dev, _a, _v in regs})
    assert devices == [0, 1, 2, 3], f"expected writes to devices 0-3 only, got {devices}"

    per_device = {d: sum(1 for dev, _a, _v in regs if dev == d) for d in devices}
    assert len(set(per_device.values())) == 1, (
        "device 0 carries a different number of register writes from its peers, which is how "
        f"the manager's ports leaking onto it looked: {per_device}")

    seen = defaultdict(int)
    for dev, addr, _val in regs:
        seen[(dev, addr)] += 1
    duplicates = sorted(k for k, n in seen.items() if n > 1)
    assert not duplicates, (
        "the same (device, address) is written more than once, so one port's configuration "
        f"overwrites another's: {[f'dev{d} 0x{a:04X}' for d, a in duplicates]}")
