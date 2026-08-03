"""Register-model tests: spec load, address->field resolution, provenance.

Run: python3 -m pytest tests/test_register_model.py
"""
from swi3s_studio import decode_capture
from swi3s_studio.ingest import transitions
from swi3s_studio.model import DeviceRegisterFile, Provenance, RegisterMap, apply_commands


def test_load_and_resolve():
    rm = RegisterMap.load()
    assert "SLC" in rm.blocks and "DP" in rm.blocks

    # SLC NumColumns @ 0x1081 (dual-ranked _NEXT), excess-1 ColumnCount field.
    r = rm.resolve(0x1081)
    assert r and r.block == "SLC" and r.register.name == "NumColumns"
    assert r.rank == "NEXT"
    nc = next(f for f in r.register.fields if f.name == "NumColumns")
    assert nc.excess1 and (nc.hi, nc.lo) == (4, 0)
    # _CURR alias resolves to the same register.
    rc = rm.resolve(0x1081 + 0x40)
    assert rc and rc.register.name == "NumColumns" and rc.rank == "CURR"

    # DP0 PortControl @ 0x200B: ScramblerEn bit3 RESETS TO 1 (the key caveat).
    pc = rm.resolve(0x200B)
    assert pc and pc.dp_index == 0 and pc.register.name == "PortControl"
    se = next(f for f in pc.register.fields if f.name == "ScramblerEn")
    assert se.reset == 1, "ScramblerEn must reset to 1"

    # DP3 offset math.
    d3 = rm.resolve(0x2000 + 3 * 0x100 + 0x09)
    assert d3 and d3.dp_index == 3 and d3.register.name == "SampleSizeGrouping"


def test_field_summary():
    rm = RegisterMap.load()
    # Write of 0x03 to NumColumns -> ColumnCount = 3+1 reported as raw field=3.
    s = rm.field_summary(0x1081, 0x03)
    assert "NumColumns=3" in s
    # DP0 0x81 byte 0x82 -> EnableCh0=1, HorizontalCount=2.
    s2 = rm.field_summary(0x2081, 0x82)
    assert "EnableCh0=1" in s2 and "HorizontalCount=2" in s2


def test_enum_decode():
    rm = RegisterMap.load()
    # PM_Action enum: link state-machine actions decode to names (Table 20).
    assert "PM_Action=Enter_Dormant" in rm.field_summary(0x1011, 0x04)
    assert "PM_Action=Enter_Sleeping" in rm.field_summary(0x1011, 0x08)
    assert "PM_Action=Stay_Attached" in rm.field_summary(0x1011, 0x00)
    # WakeReqEnable bit.
    assert "WakeReqEnable=1" in rm.field_summary(0x1010, 0x01)


def test_phy_and_cds_blocks_resolve():
    rm = RegisterMap.load()
    assert {"CDS", "PHY1", "PHY2", "PHY3"} <= set(rm.blocks)
    # CDS transport: DriveType (0x1186) enum, and the dual-ranked _CURR alias.
    assert "CDS_DriveType=Normal" in rm.field_summary(0x1186, 0x80)
    assert rm.resolve(0x11C6).rank == "CURR"           # _CURR = _NEXT + 0x40
    # CDS bit/guard/tail field decode.
    s = rm.field_summary(0x1187, 0x08)                 # GuardEnable bit3
    assert "CDS_GuardEnable=add_guard_bit" in s
    # PHY blocks resolve (electrical params) with ramp-type enums.
    assert rm.resolve(0x182).block == "PHY1"
    assert "G-Ramp" in rm.field_summary(0x182, 0x06)
    assert rm.resolve(0x382).block == "PHY3"


def test_link_events():
    from swi3s_studio.analysis import link_events
    rm = RegisterMap.load()
    cmds = [
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x1011, "data": bytes([0x04]), "device_mask": 0b10,
         "bus_row": 1234, "start_sample": 5000},
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x2081, "data": bytes([0x81])},        # not PM_Action
        {"command": "WriteA32", "crc_valid": False, "has_address": True,
         "address": 0x1011, "data": bytes([0x08])},        # bad CRC -> ignored
    ]
    ev = link_events(cmds, rm)
    assert len(ev) == 1
    assert ev[0]["action"] == "Enter_Dormant" and ev[0]["bus_row"] == 1234


def test_reset_seed_and_provenance():
    rm = RegisterMap.load()
    dev = DeviceRegisterFile(rm, 0)
    # Reset seed: ScramblerEn defaults to 1 (DP0 PortControl @ 0x200B bit3).
    assert dev.value(0x200B) & 0x08, "ScramblerEn reset bit not seeded"
    assert dev.provenance(0x200B) == Provenance.DEFAULT
    # Apply a write -> provenance flips to WRITTEN.
    dev.apply_write(0x200B, bytes([0x00]))
    assert dev.value(0x200B) == 0x00
    assert dev.provenance(0x200B) == Provenance.WRITTEN


def test_decode_reset_table_matches_spec():
    # The C++ decode path sources un-written register values from a single reset
    # table (CRegisterModel::resetOf). It must match the spec model
    # (data/registers.json) for EVERY DP-block register, so a non-zero spec reset
    # can never be silently dropped (the bug that made ScramblerEn default OFF).
    import swi3score

    from swi3s_studio.model.registers import DP_BASE
    rm = RegisterMap.load()
    for r in rm.blocks.get("DP", []):
        spec = r.reset_byte()
        cpp = swi3score.register_reset(DP_BASE + r.offset)
        assert cpp == spec, f"DP 0x{r.offset:02X} {r.name}: decode 0x{cpp:02X} != spec 0x{spec:02X}"
    # The SLC registers the decode reads must reset to 0 (so the decode's 0 default
    # is correct); their non-zero handling is special-cased (cold-start / SSP).
    slc = {r.name: r.reset_byte() for r in rm.blocks.get("SLC", [])}
    for nm in ("NumColumns", "SkippingDenominator_L", "SkippingDenominator_H",
               "SyncPointOffset", "ShortProtocolSpacer"):
        assert slc.get(nm, 0) == 0, f"SLC {nm} reset expected 0, got 0x{slc.get(nm,0):02X}"


def test_apply_decoded_commands():
    res = decode_capture(transitions.demo_capture(8))
    rm = RegisterMap.load()
    files = apply_commands(rm, res.commands)
    assert 0 in files, "device 0 should have register writes"
    dev0 = files[0]
    # Demo writes NumColumns_NEXT=15 (16 cols), DP0 SampleSize=15 (16-bit PCM), and
    # EnableCh0 (0x2081 bit7); all WRITTEN.
    assert dev0.value(0x1081) == 0x0F
    assert dev0.provenance(0x1081) == Provenance.WRITTEN
    assert dev0.value(0x2009) == 0x0F                     # SampleSizeGrouping = 16-bit PCM
    assert dev0.value(0x2081) & 0x80                      # EnableCh0 set
    assert dev0.provenance(0x2081) == Provenance.WRITTEN
    # The demo is a single device with four data ports.
    assert set(files) == {0}


def test_register_override_unifies_grid_registers_audio():
    """A section-scoped what-if override of a register's _CURR rank re-decodes and drives
    ALL of the audio decode, bus grid, and register map from the one C++ config model —
    no independent notions. The decode reads _CURR, so forcing DP0 EnableCh0's _CURR to
    0 changes the audio; the register map shows it with Provenance.UI on _CURR; clearing
    restores everything. (A _NEXT-rank override stages only — see the next test.)"""
    from swi3s_studio.session import Session
    s = Session.from_demo(64)
    dev = sorted(s.register_files_at(None))[0]
    smp = int(s.capture.clock_edges[-1])
    sect = s.segment_index_for_sample(smp)
    base_audio = s.audio_count

    s.set_register_override(sect, dev, 0x20C1, 0x00)     # _CURR alias of DP0 EnableCh0/HCount (0x2081)
    assert s.audio_count != base_audio, "CURR override did not reach the audio decode"
    f = s.register_files_at(smp)
    assert f[dev].curr_value(0x2081) == 0x00
    assert f[dev].curr_provenance(0x2081) == Provenance.UI   # display provenance, section-scoped

    s.clear_register_overrides()                          # re-decodes back
    assert s.audio_count == base_audio
    assert s.register_files_at(smp)[dev].curr_provenance(0x2081) != Provenance.UI


def test_register_override_next_rank_only():
    """Overriding a register's _NEXT rank stages ONLY _NEXT: _CURR is untouched, so the
    decode (which reads _CURR) is unchanged. This is the causal model — to change the
    committed value, edit _CURR directly (previous test). Regression for a _NEXT edit
    copying to _CURR."""
    from swi3s_studio.session import Session
    s = Session.from_demo(64)
    dev = sorted(s.register_files_at(None))[0]
    smp = int(s.capture.clock_edges[-1])
    sect = s.segment_index_for_sample(smp)
    base_audio = s.audio_count
    curr0 = s.register_files_at(smp)[dev].curr_value(0x2090)

    s.set_register_override(sect, dev, 0x2090, 0x00)     # _NEXT-base rank
    f = s.register_files_at(smp)[dev]
    assert f.value(0x2090) == 0x00 and f.provenance(0x2090) == Provenance.UI   # NEXT forced
    assert f.curr_value(0x2090) == curr0                 # CURR untouched
    assert s.audio_count == base_audio, "a NEXT-only override must not change the decode"


def test_register_map_sourced_from_cpp_matches_python_oracle():
    """The register map's VALUES now come from the C++ decode authority (same model
    as grid + audio) via register_files_at. The retired Python replay (apply_commands)
    is kept as an independent oracle: at several cursor samples, the C++-sourced files
    must match the oracle for NEXT + CURR + written/default provenance. This guards
    the migration and asserts the two implementations can't silently drift."""
    from swi3s_studio.model import DeviceRegisterFile, apply_commands
    from swi3s_studio.session import Session
    s = Session.from_demo(64)
    rm = s.register_map
    last = int(s.capture.clock_edges[-1])
    samples = [0, last // 4, last // 2, (3 * last) // 4, last]
    checked = 0
    for smp in samples:
        cpp = s.register_files_at(smp)                              # production: C++-sourced
        oracle = apply_commands(rm, s._commands_in_effect(smp),     # independent Python replay
                                peripheral_maps=s.peripheral_maps)
        for dev in sorted(set(cpp) | set(oracle)):
            cf = cpp.get(dev) or DeviceRegisterFile(rm, dev)
            of = oracle.get(dev) or DeviceRegisterFile(rm, dev)
            addrs = {a for a, _ in cf.items()} | {a for a, _ in of.items()}
            for a in addrs:
                res = rm.resolve(a)
                dual = bool(res and res.register.dual_ranked)
                assert cf.value(a) == of.value(a), \
                    f"NEXT mismatch @{smp} dev{dev} 0x{a:04X}: {cf.value(a)} vs {of.value(a)}"
                if dual:
                    assert cf.curr_value(a) == of.curr_value(a), \
                        f"CURR mismatch @{smp} dev{dev} 0x{a:04X}"
                assert cf.provenance(a) == of.provenance(a), \
                    f"provenance mismatch @{smp} dev{dev} 0x{a:04X}"
                checked += 1
    assert checked > 0


def test_register_read_overlay():
    """Reads reveal the device's live value (READ provenance) — now modelled by the
    C++ authority (CRegisterModel::ApplyRead), not a Python overlay. A CRC-valid
    ReadA32 routed through _build_register_files must surface with READ provenance."""
    from swi3s_studio.session import Session
    s = Session.from_demo(8)
    read_cmd = {"has_read_data": True, "read_data_crc_valid": True, "has_address": True,
                "device_mask": 0b1, "address": 0x1081, "read_data": bytes([0x0A])}
    files = s._build_register_files([read_cmd], None)
    assert files[0].value(0x1081) == 0x0A
    assert files[0].provenance(0x1081) == Provenance.READ


def test_register_reads_sourced_from_cpp_match_oracle():
    """Reads are now modelled in the C++ authority (register_files come from
    registers_from_commands via ApplyRead). Cross-check against the independent
    apply_commands oracle (which models reads its own way): a write→commit→read
    stream must agree on value/curr_value AND provenance (WRITTEN vs READ) for every
    touched register. Locks the C++ read modelling to the oracle so they can't drift."""
    from swi3s_studio.model import DeviceRegisterFile, apply_commands
    from swi3s_studio.session import Session
    s = Session.from_demo(8)
    rm = s.register_map
    # Write NumColumns_NEXT, commit it (→ _CURR), then read the DP0 PortControl the
    # device reports live, plus a read of a NEXT-base dual register.
    cmds = [
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x1081, "data": bytes([0x0F]), "device_mask": 0b1},
        {"is_commit": True, "commit_confirmed": True, "group_mask": 0x01},
        {"has_read_data": True, "read_data_crc_valid": True, "has_address": True,
         "device_mask": 0b1, "address": 0x200B, "read_data": bytes([0x08])},   # single-ranked
        {"has_read_data": True, "read_data_crc_valid": True, "has_address": True,
         "device_mask": 0b1, "address": 0x1081, "read_data": bytes([0x07])},   # dual _NEXT
    ]
    cpp = s._build_register_files(cmds, None)                 # production: C++-sourced (reads in C++)
    oracle = apply_commands(rm, cmds)                          # independent Python replay + read overlay
    checked = 0
    for dev in sorted(set(cpp) | set(oracle)):
        cf = cpp.get(dev) or DeviceRegisterFile(rm, dev)
        of = oracle.get(dev) or DeviceRegisterFile(rm, dev)
        addrs = {a for a, _ in cf.items()} | {a for a, _ in of.items()}
        for a in addrs:
            res = rm.resolve(a)
            dual = bool(res and res.register.dual_ranked)
            assert cf.value(a) == of.value(a), \
                f"value mismatch dev{dev} 0x{a:04X}: {cf.value(a):#x} vs {of.value(a):#x}"
            if dual:
                assert cf.curr_value(a) == of.curr_value(a), f"curr mismatch dev{dev} 0x{a:04X}"
            assert cf.provenance(a) == of.provenance(a), \
                f"provenance mismatch dev{dev} 0x{a:04X}: {cf.provenance(a)} vs {of.provenance(a)}"
            checked += 1
    # The reads must actually be present and READ-tagged (not just trivially equal).
    assert cpp[0].value(0x200B) == 0x08 and cpp[0].provenance(0x200B) == Provenance.READ
    assert cpp[0].value(0x1081) == 0x07 and cpp[0].provenance(0x1081) == Provenance.READ
    assert checked > 0


def test_last_access_provenance_wins():
    """The register map colours a value by its LAST access: a write AFTER a read reads
    WRITTEN (green), a read AFTER a write reads READ (purple), with the value from that
    last access. Regression for the two-pass bug where registers_from_commands applied
    every write/commit then every read, so a read always won and a write-after-read
    stayed purple. Covers a dual-ranked _NEXT register (the case that surfaced it) and a
    single-ranked one."""
    from swi3s_studio.session import Session
    s = Session.from_demo(8)
    for addr in (0x1081, 0x200B):     # dual-ranked _NEXT, then single-ranked
        rd = {"has_read_data": True, "read_data_crc_valid": True, "has_address": True,
              "device_mask": 0b1, "address": addr, "read_data": bytes([0x0A])}
        wr = {"command": "WriteA32", "crc_valid": True, "has_address": True,
              "device_mask": 0b1, "address": addr, "data": bytes([0x0F])}
        f = s._build_register_files([dict(rd), dict(wr)], None)          # read → write
        assert f[0].value(addr) == 0x0F and f[0].provenance(addr) == Provenance.WRITTEN, \
            f"write after read @0x{addr:04X}: {f[0].value(addr):#x}/{f[0].provenance(addr)}"
        f = s._build_register_files([dict(wr), dict(rd)], None)          # write → read
        assert f[0].value(addr) == 0x0A and f[0].provenance(addr) == Provenance.READ, \
            f"read after write @0x{addr:04X}: {f[0].value(addr):#x}/{f[0].provenance(addr)}"


def test_commit_takes_effect_at_ssp_not_command():
    """Selecting a commit COMMAND places the cursor at the command (so the grid /
    register map show the PRE-commit state there); the separate 'Commit Point' row
    sits at the SSP where the commit actually takes effect. Regression for the commit
    appearing to take effect at the command (command_cursor_sample used to jump the
    commit command to its SSP)."""
    from swi3s_studio.session import Session
    s = Session.from_demo(64, cold_start=True)
    commit = next(c for c in s.commands
                  if c.get("is_commit") and c.get("commit_confirmed") and c.get("has_sync_point"))
    # Selecting the commit COMMAND anchors the cursor at ITS row's Row Sync Point (the
    # command's row start = pre-commit state), NOT at the SSP. The RSP is the rising edge
    # opening the command's Column 0 (spec: a command phase begins at a Row Sync Point).
    cmd_cur = s.command_cursor_sample(commit)
    assert cmd_cur == s._sample_at_bus_row(int(commit["bus_row"]))
    assert s.bus_row_for_sample(cmd_cur) == int(commit["bus_row"])
    # The load-bearing guard against the one-UI-late regression: the RSP the cursor lands
    # on must be STRICTLY BEFORE the command's Column-0 closing edge (start_sample). If
    # _sample_at_bus_row ever regressed back to the closing edge, cmd_cur would equal
    # start_sample and this fails — the tautology above (cmd_cur == _sample_at_bus_row)
    # would not, since both sides move together.
    assert cmd_cur < int(commit["start_sample"]), (
        f"command cursor {cmd_cur} must precede the Column-0 closing edge "
        f"{int(commit['start_sample'])} by one UI (the RSP), not sit on it")
    cps = s.commit_point_rows()
    assert cps, "demo has a confirmed sync-point commit"
    cp = cps[0]
    # The Commit Point row is at the SSP (the effective sample), at/after the command, and
    # its cursor is that SSP's Row Sync Point (the Commit Synchronization Point).
    assert s.command_cursor_sample(cp) == int(cp["start_sample"])
    assert int(cp["start_sample"]) == s._effective_commit_sample(commit)
    assert cmd_cur <= int(cp["start_sample"])


def test_commit_point_uses_core_effective_row():
    """The commit point (where a confirmed sync-point commit takes effect) is sourced
    from the decode core's authoritative `effective_row` — the SSP the streaming decode
    actually committed at — not a Python re-estimate. Regression for the pdm768 capture
    where the re-estimated marker sat ~16 rows AFTER the real SSP, so a correctly decoded
    first audio sample (emitted right at the SSP) looked like it preceded the commit."""
    from swi3s_studio.session import Session
    s = Session.from_demo(64, cold_start=True)
    commit = next(c for c in s.commands
                  if c.get("is_commit") and c.get("commit_confirmed") and c.get("has_sync_point"))
    eff_row = int(commit["effective_row"])
    assert eff_row >= 0, "core must record the SSP row the sync-commit fired at"
    assert eff_row >= int(commit["bus_row"]), "the SSP is at/after the commit command"
    eff = s._effective_commit_sample(commit)
    assert eff == s._sample_at_bus_row(eff_row), "marker must be the core's effective row, not an estimate"
    assert s.bus_row_for_sample(eff) == eff_row


def test_commit_and_command_cursor_land_on_rsp():
    """Spec: the Commit Synchronization Point is coincident with a Row Sync Point, and a
    command phase begins at a Row Sync Point. So a commit point / command cursor must land
    ON the RSP rising edge (where the CDS / commit markers sit), not the Column-0 closing
    (falling) edge one UI later. Regression for cursors sitting on the falling edge that
    follows the correct RSP. The RSP still maps back to the row it opens (side='right')."""
    from swi3s_studio.session import Session
    s = Session.from_demo(64, cold_start=True)
    spu = int(max(1, s.sample_rate_hz / s.ui_rate_hz))
    commit = next(c for c in s.commands
                  if c.get("is_commit") and c.get("commit_confirmed") and c.get("has_sync_point"))
    eff = s._effective_commit_sample(commit)
    mk = set(int(m) for m in s.commit_column_samples(eff - 5 * spu, eff + 5 * spu))
    assert eff in mk, (eff, sorted(mk))                     # commit point == its RSP marker
    w = next(c for c in s.commands if c.get("command") == "WriteA32")
    cur = s.command_cursor_sample(w)
    cds = set(int(m) for m in s.cds_column_samples(cur - 2 * spu, cur + 2 * spu))
    assert cur in cds                                       # command cursor sits on a CDS RSP edge
    assert cur < int(w["start_sample"])                     # one UI before the Column-0 close
    assert s.bus_row_for_sample(cur) == int(w["bus_row"])   # ...and still maps to its own row


def test_segment_selection_is_rsp_aligned():
    """A width-changing sync-point commit takes effect at its SSP row's Row Sync Point (the
    rising edge opening Column 0), one UI before the segment's closing-edge start_sample.
    The bus grid forces the column count from _segment_for_sample, so that selection must
    treat the RSP as already IN the new segment — else the grid draws the OLD width for one
    UI after the commit (register map updated at the RSP, grid lagging). Regression for the
    large capture's first commit; the single-segment demo never exercised the force path."""
    import numpy as np

    from swi3s_studio.session import Session
    s = Session.from_demo(16)
    ce = np.asarray(s.capture.clock_edges)
    assert ce.size > 120
    # Synthetic two-width geometry (2 col then 16 col); segment B opens at Column-0 UI 100.
    s.segments = [
        {"start_ui": 10, "start_sample": int(ce[10]), "row_base": 0, "column_count": 2},
        {"start_ui": 100, "start_sample": int(ce[100]), "row_base": 45, "column_count": 16},
    ]
    rsp_b = int(ce[99])            # seg B's first-row RSP: opening edge, one UI before start_sample
    assert rsp_b < int(ce[100])
    # At the RSP the cursor is already in seg B (where the width-changing commit takes effect).
    assert s._segment_for_sample(rsp_b)["column_count"] == 16
    assert s.segment_index_for_sample(rsp_b) == 1
    assert s.column_count_at(rsp_b) == 16
    # One UI earlier it's still seg A (the old width).
    assert s._segment_for_sample(int(ce[98]))["column_count"] == 2
    assert s.column_count_at(int(ce[98])) == 2


def test_sample_at_bus_row_roundtrips():
    """_sample_at_bus_row inverts bus_row_for_sample: row -> its Column-0 sample -> row."""
    from swi3s_studio.session import Session
    s = Session.from_demo(16)
    edges = s.capture.clock_edges
    for smp in (int(edges[len(edges) // 4]), int(edges[len(edges) // 2]), int(edges[-2])):
        r = s.bus_row_for_sample(smp)
        assert s.bus_row_for_sample(s._sample_at_bus_row(r)) == r, r


def test_windowed_bit_samples_match_full_decode():
    """Windowed bit-sample decode is the on-demand path behind the Raw Capture overlay:
    it reconstructs each visible window from transport checkpoints instead of collecting
    the whole capture. It must be bit-exact — for any UI window, bit_samples_window must
    return exactly the bits the authoritative full decode (collect_bit_samples) produced
    in that window. Regression guard: the full decode is the oracle, so a phase/anchor
    bug in the windowed path surfaces here."""
    import numpy as np
    import swi3score

    from swi3s_studio.session import Session
    s = Session.from_demo(64, cold_start=True)
    ce = np.asarray(s.capture.clock_edges, dtype=np.int64)

    # Oracle: a full-capture collect through the same core.
    src = s.capture.sample_source()
    st = swi3score.DecoderSettings()
    st.collect_bit_samples = True
    d = swi3score.Decoder(src, st)
    d.run()
    full = d.bit_samples()
    fs = np.asarray(full["sample"], dtype=np.int64)
    assert fs.size, "demo should produce data-bit sample points"

    def key_set(b, lo_s, hi_s):
        smp = np.asarray(b["sample"], dtype=np.int64)
        m = (smp >= lo_s) & (smp <= hi_s)
        dev = np.asarray(b["device"], dtype=np.int64)[m]
        dp = np.asarray(b["dp"], dtype=np.int64)[m]
        ch = np.asarray(b["channel"], dtype=np.int64)[m]
        iss = np.asarray(b["is_start"], dtype=np.int64)[m]
        return set(zip(smp[m].tolist(), dev.tolist(), dp.tolist(), ch.tolist(), iss.tolist()))

    n = int(ce.size)
    # Windows INSIDE the audio region (so the comparison exercises real bits, not just
    # empty==empty), including one crossing the region's midpoint.
    blo = int(np.searchsorted(ce, int(fs.min())))
    bhi = int(np.searchsorted(ce, int(fs.max())))
    span = max(1, (bhi - blo) // 4)
    matched = 0
    for lo_ui, hi_ui in ((blo, blo + span),
                         (blo + span, blo + 2 * span),
                         (blo + span, bhi - span)):
        wb = s.decoder.bit_samples_window(int(lo_ui), int(hi_ui))
        lo_s, hi_s = int(ce[lo_ui]), int(ce[hi_ui])
        win = key_set(wb, lo_s, hi_s)
        oracle = key_set(full, lo_s, hi_s)
        assert win == oracle, (lo_ui, hi_ui, len(win), len(oracle))
        matched += len(oracle)
    assert matched > 0, "windows should have covered real bit samples"

    # And the Session's overlay provider (sample-space) returns sorted marks for a
    # narrow window inside the audio region (a wide window trips the density cap and is
    # intentionally empty).
    mid_s = int(fs[fs.size // 2])
    mid_ui = int(np.searchsorted(ce, mid_s))
    marks = s.port_bit_marks(int(ce[mid_ui]), int(ce[min(mid_ui + 200, n - 1)]))
    assert marks["sample"].size > 0
    assert list(marks["sample"]) == sorted(marks["sample"])


def test_override_does_not_promote_other_staged_registers():
    """A what-if register override forces ONLY its own register (both ranks); it must
    NOT commit (_NEXT->_CURR) every other staged register. Regression for editing
    NumColumns_NEXT copying NEXT->CURR across all registers — the override path used to
    ForceWrite then Commit(0xFF) (force every group), promoting all staged values."""
    from swi3s_studio.session import Session
    s = Session.from_demo(8)
    cmds = [   # two dual-ranked DP writes, no commit -> staged in _NEXT, _CURR at reset
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x2080, "data": bytes([0x01]), "device_mask": 0b1},
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x2083, "data": bytes([0x0F]), "device_mask": 0b1},
    ]
    base = s._build_register_files(cmds, None)[0]
    assert base.value(0x2080) == 1 and base.curr_value(0x2080) == 0      # staged, uncommitted
    assert base.value(0x2083) == 0x0F and base.curr_value(0x2083) == 0
    s.set_register_override(0, 0, 0x2080, 0x02)                          # override ONE register's NEXT
    f = s._build_register_files(cmds, 0)[0]
    assert f.value(0x2080) == 2 and f.provenance(0x2080) == Provenance.UI   # NEXT forced
    assert f.curr_value(0x2080) == 0                     # NEXT edit does NOT promote to CURR
    assert f.value(0x2083) == 0x0F and f.curr_value(0x2083) == 0, \
        "override must not promote OTHER staged registers to _CURR"
