"""Symbolic names for SWI3S Command Transport response tokens.

Python port of native/swi3score (SwI3sResponseNames.h): a Robust-Token number's
meaning depends on the PhaseID, so token 0 resolves to WRITE_OK / READ_DATA_NOW /
COMMIT_READY / … by phase. Used by the command table + CSV export.
"""
from __future__ import annotations

_RW = ("ReadSetup", "ReadData")
# Phases that always carry a peripheral-response field. If no response decoded
# (robust token -1 / no per-device ping status), the field was the undriven
# all-ones symbol (0b1111111111) — i.e. the peripheral gave NO_RESPONSE.
_RESP_PHASES = ("Write", "ReadSetup", "ReadData", "GetStatus")


def peripheral_response_name(phase: str, token: int) -> str:
    if token < 0:
        return ""
    if token == 0:
        return {"Write": "WRITE_OK", "ReadSetup": "READ_DATA_NOW",
                "ReadData": "READ_DATA_NOW", "Commit": "COMMIT_READY",
                "CalibratePhy": "CALIBRATE_NOW", "GetStatus": "PING_ATTACHED"}.get(phase, "RT0")
    if token == 2:
        return ("COMMIT_NOT_READY" if phase == "Commit"
                else "?" if phase == "GetStatus" else "RT2")
    if token == 4:
        return ("WRITE_FAILED" if phase == "Write"
                else "READ_FAILED" if phase in _RW else "RT4")
    if token == 5:
        return ("REMOTE_WRITE_BUFFERED" if phase == "Write"
                else "REMOTE_READ_DEFERRED" if phase in _RW else "RT5")
    if token == 6:
        return "PING_ATTACHED_BUSY" if phase == "GetStatus" else "REMOTE_WRITE_BUSY"
    if token == 7:
        return "REMOTE_ACCESS_DISABLED"
    if token == 8:
        return "PING_ALERT" if phase == "GetStatus" else "RT8"
    if token == 10:
        return "PING_ALERT_BUSY" if phase == "GetStatus" else "RT10"
    if token == 12:
        return "PROTOCOL_ERROR"
    if token == 13:
        return "COMMAND_ERROR"
    if token == 14:
        return "TRANSPORT_ERROR"
    if token == 15:
        return "COMMANDS_BLOCKED"
    return "RESERVED"          # 1, 3, 9, 11


def manager_response_name(phase: str, token: int) -> str:
    if token == 0:
        return "CONFIRM_COMMIT" if phase == "Commit" else "READ_DATA_OK"
    if token == 15:
        return "CANCEL_COMMIT" if phase == "Commit" else "READ_DATA_ERROR"
    return ""


def ping_name(token: int) -> str:
    """Per-device PingInfo token (Table 67)."""
    return {0: "ATTACHED", 6: "ATTACHED_BUSY", 8: "ALERT", 10: "ALERT_BUSY"}.get(token, "")


def _group_devices(names) -> str:
    """Collapse a per-device name list into 'dev: NAME' entries, merging runs of
    consecutive devices that share a name: e.g. ['PING_ATTACHED','NO_RESPONSE',
    'NO_RESPONSE', ...] -> '0: PING_ATTACHED, 1-11: NO_RESPONSE'."""
    out = []
    i = 0
    while i < len(names):
        j = i
        while j + 1 < len(names) and names[j + 1] == names[i]:
            j += 1
        dev = f"{i}" if i == j else f"{i}-{j}"
        out.append(f"{dev}: {names[i]}")
        i = j + 1
    return ", ".join(out)


def response_summary(cmd: dict) -> str:
    """Human-readable response cell for a decoded command."""
    phase = cmd.get("phase", "")
    parts = []
    ping = cmd.get("ping_status")
    if ping is not None and len(ping):
        # Ping (GetStatus): per-device response, grouped into device ranges. A token
        # >= 0 resolves to its GetStatus peripheral-response name (0 = PING_ATTACHED,
        # 15 = COMMANDS_BLOCKED, ...); a token of -1 means the device didn't drive
        # the field (the undriven all-ones symbol) = NO_RESPONSE.
        names = [(peripheral_response_name("GetStatus", t) or f"RT{t}") if t >= 0
                 else "NO_RESPONSE" for t in ping]
        parts.append(_group_devices(names))
    else:
        # Single peripheral-response token (Write / Read), or a Ping with no
        # per-device status at all → the undriven all-ones field = NO_RESPONSE.
        pr = cmd.get("peripheral_response", -1)
        if pr >= 0:
            parts.append("Periph: " + (peripheral_response_name(phase, pr) or f"RT{pr}"))
        elif phase in _RESP_PHASES:
            parts.append("Periph: NO_RESPONSE")
    mr = cmd.get("manager_response", -1)
    if mr >= 0:
        name = manager_response_name(phase, mr)
        if name:
            parts.append("Mgr: " + name)
    return "   ".join(parts)
