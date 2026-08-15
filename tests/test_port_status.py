"""Peripheral-reported status registers (`analysis.port_status`).

A peripheral reports on itself through its `IntStat_*` / `DevStat_*` latched bits and its
saturating `EC_*` error counters. Those replies were always on the wire and the register
map always resolved them to field names — but nothing read their VALUES, so a capture in
which a peripheral raised `IntStat_PortImpDef_1` decoded clean and said so.

Commands are built by hand here rather than from a demo fixture: the module is pure
(command dict + register map -> findings), and hand-built replies pin one classification
rule each, including the ones no capture to hand exercises (a set reserved bit, a
non-zero error counter, a corrupt reply).
"""
from __future__ import annotations

from swi3s_studio.analysis import errors, port_status

_DP0 = 0x2000            # Data Port 0 block base
_DP1 = 0x2100
_SLC = 0x1000


def _read(address: int, data: bytes, *, device: int = 7, row: int = 1000,
          crc_ok: bool = True) -> dict:
    """A ReadA32 command whose reply returned `data` from `address`."""
    return {"command": "ReadA32", "phase": "ReadSetup", "device_mask": 1 << device,
            "has_address": True, "address": address, "data": b"",
            "read_data": data, "read_data_crc_valid": crc_ok,
            "bus_row": row, "start_sample": row * 328, "has_manager_packet": True,
            "crc_valid": True}


def test_a_fault_bit_is_named_and_attributed_to_its_port():
    """IntStat bit 3 is `IntStat_UnexpectedSSP` — a fault the peripheral latched. It must
    come back named, attributed to the device and data port that reported it."""
    reps = port_status.reports_for(_read(_DP1 + 0x00, bytes([0x08])))
    assert len(reps) == 1
    r = reps[0]
    assert r.port == "Dev7 DP1" and r.register == "IntStat"
    assert [n for n, _ in r.faults] == ["IntStat_UnexpectedSSP"]
    assert r.label() == "Dev7 DP1 IntStat: IntStat_UnexpectedSSP"
    assert errors.command_error(_read(_DP1 + 0x00, bytes([0x08]))) == r.label()


def test_an_impdef_interrupt_is_reported_but_not_called_a_fault():
    """`IntStat_PortImpDef_1` (bit 7) is Implementation-Defined: the spec assigns it no
    meaning, so the analyzer must surface it rather than diagnose a fault it cannot
    actually name. This is the bit a real 44.1 kHz loopback capture raised on DP1 while
    decoding otherwise clean.

    The label carries the FIELD NAME and nothing else: `ImpDef` is in the name, which is
    the spec's own term, so the old trailing "(vendor-defined)" restated the name in a
    word the specification does not use."""
    r = port_status.reports_for(_read(_DP1 + 0x00, bytes([0x80])))[0]
    assert not r.faults
    assert [n for n, _ in r.vendor] == ["IntStat_PortImpDef_1"]
    assert r.label() == "Dev7 DP1 IntStat: IntStat_PortImpDef_1"
    assert "vendor" not in r.label().lower()


def test_lifecycle_bits_are_events_not_faults():
    """PortReady / PortNotReady are the normal port lifecycle. Flagging them would put an
    error on every bring-up and teardown poll and bury the real reports."""
    r = port_status.reports_for(_read(_DP0 + 0x00, bytes([0x06])))[0]
    assert not r.faults and not r.vendor
    assert sorted(n for n, _ in r.events) == ["IntStat_PortNotReady", "IntStat_PortReady"]
    assert errors.command_error(_read(_DP0 + 0x00, bytes([0x06]))) is None


def test_a_nonzero_error_counter_is_a_fault_and_carries_its_value():
    """EC_UnexpectedSSP (0x30) is a saturating error counter — any non-zero reading is the
    device telling us it lost SSP sync that many times."""
    r = port_status.reports_for(_read(_DP0 + 0x30, bytes([3])))[0]
    assert r.register == "EC_UnexpectedSSP"
    assert r.faults == (("EC_UnexpectedSSP", 3),)


def test_a_set_reserved_bit_is_a_fault():
    """IntStat bit 5 is Reserved and must read 0. Set, it means either the device or our
    decode disagrees with the spec — worth saying so, not worth silently dropping."""
    r = port_status.reports_for(_read(_DP0 + 0x00, bytes([0x20])))[0]
    assert [n for n, _ in r.faults] == ["Reserved"]


def test_live_state_is_reported_but_never_flagged():
    """PortStatus / ReadyCh are RO live values. PortSyncOK == 0 is expected after a
    teardown, so reporting the reading is right and calling it an error is not."""
    r = port_status.reports_for(_read(_DP0 + 0x05, bytes([0x00])))[0]
    assert dict(r.state) == {"PortReady": 0, "PortSyncOK": 0}
    assert not r.faults and not r.vendor and not r.events
    assert errors.command_error(_read(_DP0 + 0x05, bytes([0x00]))) is None


def test_a_reply_that_failed_crc_invents_nothing():
    """A corrupted byte must not manufacture a fault the device never reported."""
    assert port_status.reports_for(_read(_DP0 + 0x00, bytes([0xFF]), crc_ok=False)) == []


def test_registers_outside_the_reporting_surface_are_ignored():
    """Reading Interval_NEXT (0x84) says nothing about the device's health; only the
    status/error registers are the peripheral's report on itself."""
    assert port_status.reports_for(_read(_DP0 + 0x84, bytes([0x01]))) == []


def test_the_link_control_block_is_covered_by_the_same_rule():
    """Classification is by field NAME, read from data/registers.json, so the System &
    Link Control block's link-error interrupts come along for free — no second table."""
    r = port_status.reports_for(_read(_SLC + 0x22, bytes([0x40])))[0]   # IntStat_BadCRC
    assert r.register == "IntStat_Link" and r.dp is None and r.port == "Dev7"
    assert [n for n, _ in r.faults] == ["IntStat_BadCRC"]


def test_a_multi_byte_reply_decodes_each_register_it_spans():
    """One ReadA32 can return consecutive bytes; 0x30/0x31 are two different counters."""
    reps = port_status.reports_for(_read(_DP0 + 0x30, bytes([1, 2])))
    assert [(r.register, r.faults[0][1]) for r in reps] == \
        [("EC_UnexpectedSSP", 1), ("EC_FlowControlFail", 2)]


def test_the_summary_counts_polls_and_keeps_the_first_row():
    """Across a capture: how many polls found each latched bit set, and where it first
    appeared — the anchor for going and looking at the wire."""
    cmds = [_read(_DP0 + 0x00, bytes([0x02]), row=100),
            _read(_DP0 + 0x00, bytes([0x02]), row=200),
            _read(_DP1 + 0x00, bytes([0x80]), row=300),
            _read(_DP0 + 0x30, bytes([5]), row=400)]
    s = port_status.summarize(cmds)
    assert s.reads == 4
    assert s.events[("Dev7 DP0", "IntStat_PortReady")][:2] == [2, 100]
    assert s.vendor[("Dev7 DP1", "IntStat_PortImpDef_1")][:2] == [1, 300]
    assert s.faults[("Dev7 DP0", "EC_UnexpectedSSP")] == [1, 400, 5]


def test_a_saturating_counter_reports_its_highest_reading_not_a_sum():
    """EC_* counters saturate in the device, so polling one three times must not treble
    it — the summary keeps the highest value the device ever reported."""
    s = port_status.summarize([_read(_DP0 + 0x30, bytes([2]), row=r) for r in (10, 20, 30)])
    assert s.faults[("Dev7 DP0", "EC_UnexpectedSSP")] == [3, 10, 2]


def test_the_statistics_section_leads_with_the_counters_that_prompt_investigation():
    """A raised bit has to be visible WITHOUT expanding anything: faults and ImpDef
    interrupts are top-level rows, and only the per-port live state folds away.

    The count row is named for what actually fired — ImpDef here — because ImpDef is the
    spec's term where 'vendor' was not, and an SDCA bit would say SDCA."""
    from swi3s_studio.analysis.measurements import _peripheral_report_rows

    class FakeSession:
        commands = [_read(_DP1 + 0x00, bytes([0x80]), row=1_121_573),
                    _read(_DP0 + 0x05, bytes([0x00]), row=1_123_153)]

    rows = _peripheral_report_rows(FakeSession())
    labels = {r[0].strip(): r for r in rows}
    assert labels["faults reported"][1] == "none"
    assert labels["faults reported"][2] == "pass"
    assert labels["ImpDef interrupts"][1] == "1"
    assert not any("vendor" in r[0].lower() or "vendor" in str(r[1]).lower() for r in rows)
    impdef = labels["Dev7 DP1 IntStat_PortImpDef_1"]
    assert "1,121,573" in impdef[1]
    assert len(impdef) == 2, "the ImpDef row must be top level, not folded into a group"
    # The live state is a folded group, so it can't crowd out the summary.
    assert any(r[0].endswith("reported state") and r[2] == "group" for r in rows)


def test_the_impdef_row_is_named_for_what_actually_fired():
    """ImpDef and SDCA share one bucket — the base spec defines neither bit's meaning —
    but they are not the same term, so the count row names whichever fired rather than
    labelling an SDCA interrupt ImpDef (or either of them 'vendor', which is not a term
    the specification uses at all)."""
    from swi3s_studio.analysis.measurements import _peripheral_report_rows

    def rows_for(*cmds):
        class FakeSession:
            commands = list(cmds)
        return {r[0].strip(): r for r in _peripheral_report_rows(FakeSession())}

    impdef = _read(_DP1 + 0x00, bytes([0x80]), row=10)      # IntStat_PortImpDef_1
    sdca = _read(_SLC + 0x40, bytes([0x01]), row=20)        # IntStat_SDCA00
    assert rows_for(impdef)["ImpDef interrupts"][1] == "1"
    assert rows_for(sdca)["SDCA interrupts"][1] == "1"
    assert rows_for(impdef, sdca)["ImpDef / SDCA interrupts"][1] == "2"


def test_a_capture_that_polls_nothing_gets_no_section():
    """Most captures never read a status register. The section must vanish rather than
    show a row of zeros implying the device was asked and said it was healthy."""
    from swi3s_studio.analysis.measurements import _peripheral_report_rows

    class FakeSession:
        commands = [{"command": "Ping", "phase": "GetStatus", "device_mask": 0xFFF}]

    assert _peripheral_report_rows(FakeSession()) == []
