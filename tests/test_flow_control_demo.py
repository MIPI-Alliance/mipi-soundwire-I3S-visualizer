"""Flow-control demo: bit-exact round-trip, the mid-phase SPM framing lock, the FCP
scrambler and DRQ placement.

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

It also pins the mid-phase framing lock: a K.28.7 SPM pattern that appears by chance
inside a Write payload/CRC byte must NOT re-frame or abandon the in-progress phase
(SWI3S v1.1 §7.2.2 {ASW1907}: hold symbol alignment by counting bits mod 10 mid-phase;
§8.2.1 {ASW2206}: a mid-phase SPM only ARMS header detection). dev1's
WriteA32(0x2090, 0x60) forms such a pattern.

Also pinned: FCP_ScramblerEn decodes from register 0x0E bit 3 and a scrambled FCP decodes
to its generator; a config CSV keeps the FlowControlDelay and FCP_ScramblerEn resets; a
decoded DRQ bit is read on its last UI, whether the bit is wide or cut short by the row
edge; drq_sample marks the DRQ that permitted each sample.
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


def _decode_flow(samples_per_channel: int = 200, fcp_scrambled: bool = False):
    levels = swi3score.make_demo_levels_flow_control(samples_per_channel,
                                                     fcp_scrambled=fcp_scrambled)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    dec = swi3score.Decoder(src, swi3score.DecoderSettings())
    dec.run()
    return dec


def test_flow_demo_loads_clean_via_session_cold_start():
    """Load the demo the way the APP does — Session.from_demo(cold_start=True) — and
    assert the decode is error-free. A cold-started decoder leaves PHY-select seeded at
    Safe-Lock-2 and GROWS the column count by snooping the commit, so the capture must
    present the 2->16 ladder. Runs at a realistic size so the audio phase is
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
    """All four peripheral devices (one per flow mode) must decode, including device 1
    (TX_CONTROLLED), whose config write WriteA32(0x2090, 0x60) forms a spurious K.28.7
    comma at a sub-symbol offset."""
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
    round-trip BIT-EXACT to the uniformly-sampled 48 kHz sine: TX_PRESENT gating
    de-jitters the transport back to the source stream."""
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
    under-run. In the ASYNC model the source honors the sink's standing DRQ and delivers
    every sample (bounded 0/1-interval delay). All four devices' single-channel-equivalent
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
    emits DRQ cells. Checks the analyzer's navigated grid_cells_at path for slot==DRQ in
    the audio phase."""
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
    frame is corrupted by a spurious in-payload comma (the framing lock keeps the
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
    the handshake validator uses the right lookback. The demo writes 0x0E with bit 7
    clear, so every port decodes 0 (d=1 interval)."""
    dec = _decode_flow(60)
    dps = {dp["device_number"]: dp for dp in dec.config_dataports()["dataports"]
           if dp["enabled"] and dp["enable_ch"]}
    for dev in (2, 3):                      # RX_CONTROLLED, ASYNC
        assert "flow_control_delay" in dps[dev], "flow_control_delay not surfaced"
        assert dps[dev]["flow_control_delay"] == 0


def test_a_config_csv_keeps_the_flow_control_delay_reset():
    """A config CSV has no FlowControlDelay field, so the config keeps the register reset
    (FlowControlDelay 1) on the DRQ-mode ports."""
    levels = swi3score.make_demo_levels_flow_control(60)
    src = swi3score.MemorySampleSource(levels, 98_304_000, 4)
    settings = swi3score.DecoderSettings()
    settings.config_csv_path = os.path.join(_FIXTURES, "flow_control_demo.csv")
    dec = swi3score.Decoder(src, settings)
    dec.run()
    dps = {dp["device_number"]: dp for dp in dec.config_dataports()["dataports"] if dp["enabled"]}
    for dev in (2, 3):                      # RX_CONTROLLED, ASYNC
        assert dps[dev]["flow_control_delay"] == 1


def test_registers_from_csv_carry_the_flow_control_delay_reset():
    """A config CSV has no FlowControlDelay field, so the config keeps the register reset
    (FlowControlDelay 1) and registers_from_csv emits 0x0E with bit 7 set for every DRQ
    mode.

    The fixture path is resolved from __file__: registers_from_csv returns [] for a path it
    cannot open rather than raising.

    The fixture stays under tests/fixtures/, outside the visualizer_examples/ corpus that
    three suites glob: its DataPortNumber differs from its slot index (four peripherals each
    using their own DP0)."""
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


def _fcp_scrambler_by_device(dec):
    return {dp["device_number"]: dp["fcp_scrambler_en"]
            for dp in dec.config_dataports()["dataports"] if dp["enabled"]}


def test_fcp_scrambler_en_decodes_from_0x0e_bit3():
    """FCP_ScramblerEn is 0x0E bit 3: the plain demo writes it clear, the fcp_scrambled
    demo writes it set."""
    plain = _fcp_scrambler_by_device(_decode_flow(40))
    scrambled = _fcp_scrambler_by_device(_decode_flow(40, fcp_scrambled=True))
    for dev in (2, 3):                      # RX_CONTROLLED, ASYNC
        assert plain[dev] is False
        assert scrambled[dev] is True


def test_a_scrambled_fcp_decodes_to_its_generator():
    """With FCP_ScramblerEn = 1 every DRQ bit goes through its port's LFSR, seeded when the
    port is enabled and stepped once per DRQ bit across intervals and SSPAs. The decode
    descrambles each DRQ with a per-port LFSR that persists the same way, so every
    TxPresent pairs with the request the generator made and the audio round-trips."""
    plain = swi3score.make_demo_levels_flow_control(200)
    assert swi3score.make_demo_levels_flow_control(200, fcp_scrambled=True) != plain, \
        "the DRQ cells carry the same levels scrambled and raw"
    dec = _decode_flow(200, fcp_scrambled=True)
    fc = dec.flow_control_stats()
    assert fc["drq_bits"] > 0 and fc["checked"] > 0
    assert fc["fails"] == 0, f"{fc['fails']} handshake violations of {fc['checked']} checked"
    by = defaultdict(list)
    for a in dec.audio():
        by[(a["device"], a["channel"])].append(a["value"])
    assert set(by) == set(_FLOW_FREQ)
    for (dev, ch), seq in by.items():
        assert seq == [_flow_expected(dev, ch, i) for i in range(len(seq))], (dev, ch)


def test_a_config_csv_leaves_fcp_scrambler_en_at_its_reset():
    """A config CSV has no FCP_ScramblerEn field: the config keeps the register's reset
    (1), registers_from_csv emits 0x0E with bit 3 set for every port, and a decode under
    the CSV descrambles the DRQ, so the plain demo's raw DRQ fails the handshake."""
    csv = os.path.join(_FIXTURES, "flow_control_demo.csv")
    regs = swi3score.registers_from_csv(csv)
    flow0e = [val for _dev, addr, val in regs if (addr & 0xFF) == 0x0E]
    assert flow0e and all(val & 0x08 for val in flow0e), [hex(v) for v in flow0e]
    settings = swi3score.DecoderSettings()
    settings.config_csv_path = csv
    dec = swi3score.Decoder(swi3score.MemorySampleSource(
        swi3score.make_demo_levels_flow_control(200), 98_304_000, 4), settings)
    dec.run()
    assert all(_fcp_scrambler_by_device(dec)[dev] for dev in (2, 3))
    assert dec.flow_control_stats()["fails"] > 0


def test_flow_drq_sample_annotated_on_samples():
    """Each decoded flow-controlled sample carries drq_sample, the capture position of the
    DRQ that permitted it (ABI 7). Only the first sample of each RX_CONTROLLED channel
    (device 2, channels 7 and 8), taken in the startup interval where DRQ=1 is assumed,
    stays unset; the ASYNC port (device 3) has none."""
    dec = _decode_flow(200)
    seen: dict = defaultdict(int)
    unset = set()
    drq_ports = []
    for a in dec.audio():
        key = (a["device"], a["channel"])
        index, seen[key] = seen[key], seen[key] + 1
        if a["device"] in (2, 3):                                    # RX / ASYNC
            drq_ports.append(a)
            if a["drq_sample"] <= 0:
                unset.add((a["device"], a["channel"], index))
    assert drq_ports, "no DRQ-mode samples"
    assert unset == {(2, 7, 0), (2, 8, 0)}
    assert all(a["drq_sample"] <= a["start_sample"] for a in drq_ports if a["drq_sample"] > 0)


@pytest.mark.parametrize("phy,variant", [(1, ""), (2, ""), (3, ""), (2, "flow_control")])
def test_audio_idle_cds_is_d10_2_not_flat(phy, variant):
    """The CDS idle between keep-alive Pings during the audio phase must be the D10.2
    Protocol-Spacer pattern 0101010101 (SWI3S §8.1.2.9 / §7.1.1.3, {ASW2201}: with no
    Command to send the Manager drives a continuous alternating 0-1 idle stream, one CDS
    bit per Row), NOT all-ones, which NRZS-holds the FBCSE bus flat. All four menu demos
    share the fillIdleWithPings chokepoint, so this checks every PHY plus flow control.
    Framed on the audio segment, every full idle symbol must decode to 0x155 (0101010101)
    and none to 0x2AA (the 1010 inverse).

    All-ones (0x3FF) symbols are legitimate where a Ping's PingInfo field leaves ABSENT
    peripherals undriven (an undriven bus decodes to token -1 = NO_RESPONSE): at most one
    per peripheral, so at most kMaxPeripherals in a row. The assertion is therefore on the
    RUN LENGTH of consecutive all-ones symbols."""
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
    DeviceNumber_REG holds 0..11 and cannot carry the -1 sentinel. The CSV reader must decode
    the pair to the sentinel; both loops in registersFromConfig skip deviceNum < 0. This
    fixture also has a genuine device-0 peripheral port, so every device must carry the same
    number of writes and no (device, address) may be written twice.
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
        "every device carries the same number of register writes, "
        f"so device 0 must not carry the manager's ports: {per_device}")

    seen = defaultdict(int)
    for dev, addr, _val in regs:
        seen[(dev, addr)] += 1
    duplicates = sorted(k for k, n in seen.items() if n > 1)
    assert not duplicates, (
        "the same (device, address) is written more than once, so one port's configuration "
        f"overwrites another's: {[f'dev{d} 0x{a:04X}' for d, a in duplicates]}")


# ---- the DRQ observe point: a decode reads a DRQ bit on its last UI ----
# The flow demo's config CSV, with every DRQ port's FCP_HorizontalStart and FCP_BitWidth
# replaced. Its DRQ ports pair a Manager port with a peripheral port, so each DRQ is seen
# in both directions: a sink data port's (source DRQ) and a source data port's (sink DRQ).
_FLOW_COLUMNS = 16


def _fcp_csv(tmp_path, hstart, width):
    import csv
    with open(os.path.join(_FIXTURES, "flow_control_demo.csv"), newline="") as f:
        rows = list(csv.reader(f))
    modes = next(r for r in rows if r[0] == "FlowMode_REG")[1:]
    new = {"FCP_HorizontalStart_REG": hstart, "FCP_BitWidth_REG": width}
    for r in rows:
        if r[0] in new:
            r[1:] = [str(new[r[0]]) if m in ("2", "3") else v for m, v in zip(modes, r[1:])]
    path = tmp_path / f"fcp_{hstart}_{width}.csv"
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)
    return str(path)


def _drq_columns(levels, csv_path):
    """(drq_bits, {(device, dp): bus-column distances from each DRQ-gated sample's DRQ read
    to the sample's last bit})."""
    st = swi3score.DecoderSettings()
    st.config_csv_path = csv_path
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4), st)
    dec.run()
    dist = defaultdict(set)
    for a in dec.audio():
        if a["drq_sample"]:
            dist[(a["device"], a["dp"])].add(
                ((a["end_sample"] - a["drq_sample"]) // 4) % _FLOW_COLUMNS)
    return dec.flow_control_stats()["drq_bits"], dict(dist)


@pytest.fixture(scope="module")
def flow_levels_120():
    return swi3score.make_demo_levels_flow_control(120)


def test_a_wide_drq_bit_is_read_on_its_last_ui(tmp_path, flow_levels_120):
    """A 3-UI DRQ bit (FCP_BitWidth 2) is read two columns after a 1-UI one at the same
    FCP_HorizontalStart, in both directions."""
    bits0, narrow = _drq_columns(flow_levels_120, _fcp_csv(tmp_path, 11, 0))
    bits2, wide = _drq_columns(flow_levels_120, _fcp_csv(tmp_path, 11, 2))
    assert {dev for dev, _ in narrow} >= {-1, 2, 3}       # Manager and peripheral ends
    assert bits2 == bits0 > 0 and set(wide) == set(narrow)
    for key, cols in narrow.items():
        assert wide[key] == {(c - 2) % _FLOW_COLUMNS for c in cols}, key


def test_a_drq_bit_cut_short_by_the_row_edge_is_read_on_the_rows_last_ui(
        tmp_path, flow_levels_120):
    """A wide DRQ bit starting on a row's last column ends there: each one is still read, on
    that column, in both directions."""
    last = _FLOW_COLUMNS - 1
    bits0, narrow = _drq_columns(flow_levels_120, _fcp_csv(tmp_path, last, 0))
    bits1, cut = _drq_columns(flow_levels_120, _fcp_csv(tmp_path, last, 1))
    assert bits1 == bits0 > 0
    assert cut == narrow and {dev for dev, _ in cut} >= {-1, 2, 3}
