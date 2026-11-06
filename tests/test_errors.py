"""Per-command error classification (analysis.errors).

Run: PYTHONPATH=. python3 -m pytest tests/test_errors.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from swi3s_studio.analysis.errors import command_error, count_errors


def _write(**kw):
    cmd = {"command": "WriteA32", "phase": "Write", "device_mask": 1,
           "has_manager_packet": True, "crc_valid": True, "peripheral_response": 0,
           "is_commit": False, "group_mask": 0, "ping_status": None}
    cmd.update(kw)
    return cmd


def test_clean_command_has_no_error():
    assert command_error(_write()) is None


def test_crc_and_mask_and_response_errors():
    assert command_error(_write(crc_valid=False)) == "CRC error"
    assert "expected 1" in command_error(_write(device_mask=0))
    assert command_error(_write(peripheral_response=13)) == "COMMAND_ERROR"


def test_the_device_mask_cardinality_rule_by_phase():
    """Write, ReadSetup, ReadData and CalibratePhy select exactly one device; any other
    phase needs at least one. Bits above device 11 do not count."""
    for phase in ("Write", "ReadSetup", "ReadData", "CalibratePhy"):
        assert command_error(_write(phase=phase, device_mask=0)) == "device mask: expected 1, got 0"
        assert command_error(_write(phase=phase, device_mask=0x004)) is None
        assert command_error(_write(phase=phase, device_mask=0x005)) == \
            "device mask: expected 1, got 2"
        assert command_error(_write(phase=phase, device_mask=0x1004)) is None
    for phase in ("GetStatus", "Ping"):
        assert command_error(_write(phase=phase, device_mask=0)) == "device mask: no devices selected"
        assert command_error(_write(phase=phase, device_mask=0x005)) is None


def test_the_device_mask_counts_every_device_from_0_to_11():
    """The mask is 12 bits: device 11 (0x800) is a device, the top bit of 0xFFF counts, and bit
    12 (0x1000) is not one."""
    for phase in ("Write", "CalibratePhy"):
        assert command_error(_write(phase=phase, device_mask=0x800)) is None
        assert command_error(_write(phase=phase, device_mask=0x801)) == \
            "device mask: expected 1, got 2"
        assert command_error(_write(phase=phase, device_mask=0xFFF)) == \
            "device mask: expected 1, got 12"
        assert command_error(_write(phase=phase, device_mask=0x1000)) == \
            "device mask: expected 1, got 0"
    assert command_error(_write(phase="GetStatus", device_mask=0x800)) is None
    assert command_error(_write(phase="GetStatus", device_mask=0x1000)) == \
        "device mask: no devices selected"
    assert command_error(_write(phase="GetStatus", device_mask=0xFFF)) is None


def test_ping_status_error_is_flagged():
    # GetStatus error tokens live in the per-device ping_status list, NOT the scalar
    # peripheral_response; command_error flags them.
    ping = [0] * 12
    ping[3] = 15                                    # device 3 reports COMMANDS_BLOCKED
    cmd = {"command": "GetStatus", "phase": "GetStatus", "device_mask": 0xFFF,
           "has_manager_packet": True, "crc_valid": True, "peripheral_response": -1,
           "is_commit": False, "group_mask": 0, "ping_status": ping}
    err = command_error(cmd)
    assert err is not None and "COMMANDS_BLOCKED" in err and "dev 3" in err, err
    # A clean ping (all zero) is not an error.
    cmd["ping_status"] = [0] * 12
    assert command_error(cmd) is None


def test_commit_group_mask_errors():
    """A commit selecting NO group is malformed; so is one with a RESERVED bit set —
    only CG0..CG3 (bits [3:0]) exist, so a group mask like 0xFF (bits 4-7 set) is an
    error. A valid single/multi-group mask (0x01, 0x0F) is clean."""
    def commit(gm):
        return {"command": "SSCR", "phase": "Commit", "device_mask": 0xFFF,
                "has_manager_packet": True, "crc_valid": True, "peripheral_response": 0,
                "is_commit": True, "group_mask": gm, "ping_status": None}
    assert command_error(commit(0x00)) == "group mask: no commit groups selected"
    assert command_error(commit(0x01)) is None
    assert command_error(commit(0x0F)) is None
    err = command_error(commit(0xFF))
    assert err is not None and "reserved" in err and "0xFF" in err, err
    assert "reserved" in command_error(commit(0x10))     # a single reserved bit


def test_count_errors():
    cmds = [_write(), _write(crc_valid=False), _write(peripheral_response=12)]
    assert count_errors(cmds) == 2


def test_enablech_curr_error_is_flagged():
    # Decoder-flagged EnableCh_CURR write with Interval != 1 Row (native Decoder::feed
    # / CommandRec::enablechCurrError) counts as an error. command_table's row shading,
    # its "Errors only" filter and the status-bar "(N errors)" count key off this predicate.
    cmd = _write(enablech_curr_error=True)
    assert command_error(cmd) == "Write to EnableCh_CURR with Interval != 1 Row"
    assert count_errors([cmd]) == 1
    assert command_error(_write(enablech_curr_error=False)) is None


def test_a_running_port_write_covering_several_control_registers_names_all_of_them():
    one = {"running_port_write_error": True, "address": 0x210B, "data": b"\x00"}
    assert command_error(one) == "Programming error: PortControl of DP1 changed while the port runs"
    wide = {"running_port_write_error": True, "address": 0x2109, "data": b"\x00\x00\x05"}
    msg = command_error(wide)
    assert msg == ("Programming error: one of DP1.SampleSizeGrouping, DP1.PortControl "
                   "changed while the port runs")


def test_a_running_port_write_message_has_three_forms():
    base = {"running_port_write_error": True}
    # SkippingNumerator sits at 0x0C and 0x0D: a write covering both names it once.
    both = {**base, "address": 0x200C, "data": b"\x01\x02"}
    assert command_error(both) == (
        "Programming error: SkippingNumerator of DP0 changed while the port runs")
    # Bytes of the block that are no control register: the fallback.
    other = {**base, "address": 0x2010, "data": b"\x01\x02"}
    assert command_error(other) == "Programming error: a running port's control register changed"
    # An address below the data-port window names no port.
    below = {**base, "address": 0x1FFF, "data": b"\x01"}
    assert command_error(below) == "Programming error: a running port's control register changed"
    # Registers of two ports, in address order.
    two = {**base, "address": 0x200E, "data": b"\x00" * 0xFC}
    assert command_error(two) == ("Programming error: one of DP0.FlowControl, "
                                  "DP1.SampleSizeGrouping changed while the port runs")


def test_a_write_to_a_read_only_register_is_a_programming_error():
    cmd = {"command": "WriteA32", "phase": "Write", "crc_valid": True,
           "has_manager_packet": True, "device_mask": 1}
    assert command_error(cmd) is None
    assert command_error({**cmd, "read_only_write_error": True}) == \
        "Programming error: write to a read-only register"


def test_a_command_whose_header_is_unreadable_is_labelled_so():
    """A bad-header row carries device mask 0 and phase '?': it is reported as an unreadable
    header, not as an empty device mask."""
    cmd = {"command": "?", "phase": "?", "header_valid": False, "crc_valid": False,
           "has_manager_packet": False, "device_mask": 0}
    assert command_error(cmd) == "header unreadable"
    assert command_error({**cmd, "header_valid": True}) == "device mask: no devices selected"
