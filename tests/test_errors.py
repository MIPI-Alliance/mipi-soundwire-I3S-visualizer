"""Per-command error classification (analysis.errors).

Run: PYTHONPATH=. python3 tests/test_errors.py
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


def test_ping_status_error_is_flagged():
    # GetStatus error tokens live in the per-device ping_status list, NOT the scalar
    # peripheral_response — command_error must still flag them (regression).
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


if __name__ == "__main__":
    test_clean_command_has_no_error(); print("ok: clean command -> no error")
    test_crc_and_mask_and_response_errors(); print("ok: CRC / device-mask / response errors")
    test_ping_status_error_is_flagged(); print("ok: GetStatus ping_status error is flagged")
    test_commit_group_mask_errors(); print("ok: commit group mask (empty / reserved bits) errors")
    test_count_errors(); print("ok: count_errors")
    print("ALL ERRORS TESTS PASSED")
