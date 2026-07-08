"""Register-model tests: spec load, address->field resolution, provenance.

Run: python3 tests/test_register_model.py
"""
from swi3s_studio.model import RegisterMap, DeviceRegisterFile, Provenance, apply_commands
from swi3s_studio import decode_capture
from swi3s_studio.ingest import transitions


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
    # Demo writes NumColumns_NEXT=15 (16 cols) and EnableCh1=0x02; both WRITTEN.
    assert dev0.value(0x1081) == 0x0F
    assert dev0.provenance(0x1081) == Provenance.WRITTEN
    assert dev0.value(0x2090) == 0x02
    assert dev0.provenance(0x2090) == Provenance.WRITTEN
    # Device 1 also configured (two-device demo).
    assert 1 in files and files[1].value(0x1081) == 0x0F


def test_register_override_unifies_grid_registers_audio():
    """A section-scoped what-if register override re-decodes and drives ALL of the
    audio decode, bus grid, and register map from the one C++ config model — no
    independent notions. Forcing DP0 EnableCh_L=0 changes the audio; the register
    map shows the forced value with Provenance.UI; clearing restores everything."""
    from swi3s_studio.session import Session
    s = Session.from_demo(64)
    dev = sorted(s.register_files_at(None))[0]
    smp = int(s.capture.clock_edges[-1])
    sect = s.segment_index_for_sample(smp)
    base_audio = s.audio_count

    s.set_register_override(sect, dev, 0x2090, 0x00)     # re-decodes
    assert s.audio_count != base_audio, "override did not reach the audio decode"
    f = s.register_files_at(smp)
    assert f[dev].value(0x2090) == 0x00
    assert f[dev].provenance(0x2090) == Provenance.UI    # display provenance, section-scoped

    s.clear_register_overrides()                          # re-decodes back
    assert s.audio_count == base_audio
    assert s.register_files_at(smp)[dev].provenance(0x2090) == Provenance.WRITTEN


def test_register_map_sourced_from_cpp_matches_python_oracle():
    """The register map's VALUES now come from the C++ decode authority (same model
    as grid + audio) via register_files_at. The retired Python replay (apply_commands)
    is kept as an independent oracle: at several cursor samples, the C++-sourced files
    must match the oracle for NEXT + CURR + written/default provenance. This guards
    the migration and asserts the two implementations can't silently drift."""
    from swi3s_studio.session import Session
    from swi3s_studio.model import apply_commands, DeviceRegisterFile
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
    from swi3s_studio.session import Session
    from swi3s_studio.model import apply_commands, DeviceRegisterFile
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
    # Commit command -> its own start (not the SSP).
    assert s.command_cursor_sample(commit) == int(commit["start_sample"])
    cps = s.commit_point_rows()
    assert cps, "demo has a confirmed sync-point commit"
    cp = cps[0]
    # The Commit Point row is at the SSP (the effective sample), at/after the command.
    assert s.command_cursor_sample(cp) == int(cp["start_sample"]) >= int(commit["start_sample"])
    assert int(cp["start_sample"]) == s._effective_commit_sample(commit)


if __name__ == "__main__":
    test_load_and_resolve(); print("ok: load + address->register/field resolve")
    test_field_summary(); print("ok: field summary (command address -> field names)")
    test_enum_decode(); print("ok: enum decode (PM_Action / WakeReqEnable symbolic values)")
    test_phy_and_cds_blocks_resolve(); print("ok: PHY1/2/3 + CDS blocks resolve + enums")
    test_link_events(); print("ok: link events (PM_Action writes decoded)")
    test_reset_seed_and_provenance(); print("ok: reset seeding + provenance (ScramblerEn=1)")
    test_decode_reset_table_matches_spec(); print("ok: C++ decode reset table matches the spec model")
    test_apply_decoded_commands(); print("ok: apply decoded WriteA32s -> per-device register files")
    test_register_override_unifies_grid_registers_audio()
    print("ok: what-if register override unifies grid/registers/audio (section-scoped)")
    test_register_map_sourced_from_cpp_matches_python_oracle()
    print("ok: register map sourced from C++ authority == Python oracle (multi-sample)")
    test_register_read_overlay()
    print("ok: read overlay surfaces READ provenance")
    test_register_reads_sourced_from_cpp_match_oracle()
    print("ok: C++-modelled reads match the apply_commands oracle (value + provenance)")
    test_last_access_provenance_wins()
    print("ok: register colour reflects the LAST access (write-after-read = WRITTEN)")
    test_commit_takes_effect_at_ssp_not_command()
    print("ok: commit command stays at the command; Commit Point row is at the SSP")
    print("ALL REGISTER-MODEL TESTS PASSED")
