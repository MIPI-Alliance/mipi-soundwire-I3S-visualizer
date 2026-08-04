"""Symbolic response decoding test.

Run: python3 -m pytest tests/test_responses.py
"""
from swi3s_studio.analysis.responses import (
    manager_response_name,
    peripheral_response_name,
    response_summary,
)


def test_peripheral_names_by_phase():
    assert peripheral_response_name("Write", 0) == "WRITE_OK"
    assert peripheral_response_name("ReadSetup", 0) == "READ_DATA_NOW"
    assert peripheral_response_name("Commit", 0) == "COMMIT_READY"
    assert peripheral_response_name("Write", 4) == "WRITE_FAILED"
    assert peripheral_response_name("ReadSetup", 5) == "REMOTE_READ_DEFERRED"
    assert peripheral_response_name("Write", 13) == "COMMAND_ERROR"
    assert peripheral_response_name("Write", -1) == ""


def test_manager_names_by_phase():
    assert manager_response_name("Commit", 0) == "CONFIRM_COMMIT"
    assert manager_response_name("Commit", 15) == "CANCEL_COMMIT"
    assert manager_response_name("ReadSetup", 0) == "READ_DATA_OK"
    assert manager_response_name("ReadSetup", 15) == "READ_DATA_ERROR"


def test_response_summary():
    write_ok = {"phase": "Write", "peripheral_response": 0, "manager_response": -1,
                "has_ping_status": False, "ping_status": None}
    assert response_summary(write_ok) == "Periph: WRITE_OK"
    commit = {"phase": "Commit", "peripheral_response": 0, "manager_response": 0,
              "has_ping_status": False, "ping_status": None}
    s = response_summary(commit)
    # Manager and peripheral responses are labelled so they're differentiable.
    assert "Periph: COMMIT_READY" in s and "Mgr: CONFIRM_COMMIT" in s
    ping = {"phase": "GetStatus", "has_ping_status": True,
            "ping_status": [0, 8] + [-1] * 10, "peripheral_response": -1,
            "manager_response": -1}
    s = response_summary(ping)
    # Per-device breakdown; runs of same-status devices collapse into ranges.
    assert s == "0: PING_ATTACHED, 1: PING_ALERT, 2-11: NO_RESPONSE", s
    # The dp_dn.csv cases: one device responds, the rest are undriven (NO_RESPONSE).
    one = lambda t: response_summary({"phase": "GetStatus", "ping_status": [t] + [-1] * 11,
                                      "peripheral_response": -1, "manager_response": -1})
    assert one(0) == "0: PING_ATTACHED, 1-11: NO_RESPONSE", one(0)
    assert one(15) == "0: COMMANDS_BLOCKED, 1-11: NO_RESPONSE", one(15)


def test_commit_peripheral_response_decoded():
    """The Command Transport parser now surfaces a commit's peripheral response
    (COMMIT_READY/…): the 12 per-device tokens used to be consumed and dropped, so
    an SSCR showed only the Manager Response. The demo's SSCR must now carry a
    peripheral response, shown alongside the manager one."""
    from swi3s_studio.session import Session
    s = Session.from_demo(8)
    sscr = next((c for c in s.commands if c.get("command") in ("SSCR", "DSCR")), None)
    assert sscr is not None, "demo has no SSCR/DSCR commit"
    assert sscr.get("peripheral_response", -1) >= 0, "commit peripheral response not surfaced"
    summ = response_summary(sscr)
    assert "Periph:" in summ and "Mgr:" in summ, summ
