"""Peripheral-reported status and error registers, decoded from the read replies.

The analyzer forms its own opinion of the bus by decoding the wire. A peripheral also
reports on ITSELF — through the ``IntStat_*`` interrupt-status bits it latches, the
``DevStat_*`` device-status bits, and the saturating ``EC_*`` error counters. That is the
device's own account of what went wrong, and it is on the wire whenever the Manager polls
it. The register map already resolved those replies to field names; nothing read their
VALUES, so a capture in which a peripheral raised ``IntStat_PortImpDef_1`` decoded
perfectly clean and said so.

Classification is by FIELD NAME, not by a table of addresses here, so the coverage grows
with ``data/registers.json`` instead of with this module:

===========================  ==================================================
``EC_*``                     a saturating error counter — non-zero is a FAULT
``IntStat_*`` / ``DevStat_*``an event the peripheral latched: a FAULT unless it is
                             lifecycle (PortReady / PortNotReady / CalibrationReq /
                             ReceivedSSP) or ImpDef / SDCA (which the base spec
                             leaves undefined, so it is reported and not judged)
``Reserved``                 non-zero means the reply disagrees with the spec
anything else                live state (PortStatus, ReadyCh, COUNT_*) — reported,
                             never judged
===========================  ==================================================

A reply whose CRC failed is ignored entirely: a corrupted byte must not invent a fault.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# Latched events that are part of normal port lifecycle, not faults: a port becoming
# ready or not-ready, a PHY3 peripheral asking for calibration, a DP noting it saw an SSP.
_LIFECYCLE = frozenset({
    "IntStat_PortReady", "IntStat_PortNotReady", "IntStat_CalibrationReq",
    "DevStat_ReceivedSSP",
})

FAULT, VENDOR, EVENT, STATE = "fault", "vendor", "event", "state"

# Registers this module reads: the peripheral's own reporting surface. Matched on the
# register NAME so both the DP block (IntStat, PortStatus, ReadyCh_L/H, the per-channel
# EC_TestFailCh counters, EC_*, COUNT_*) and the System & Link Control block (IntStat_Link,
# IntStat_PhyCal, IntStat_SDCA, IntStat_ImpDef, IntCascade_DP, DevStat, EC_BadCRC,
# EC_Bad8b10b, EC_LostLock, …) are covered by the same rule. IntEn_* is deliberately absent:
# an enable is configuration the Manager chose, not something the device reported.
_REPORTING_PREFIXES = ("IntStat", "IntCascade", "DevStat", "PortStatus", "ReadyCh",
                       "EC_", "COUNT_")

_rmap = None


def _register_map():
    """The shared spec register map, loaded once (this is called per command byte)."""
    global _rmap
    if _rmap is None:
        from ..model import RegisterMap
        _rmap = RegisterMap.load()
    return _rmap


def classify(field_name: str) -> str:
    """FAULT, IMPDEF (ImpDef / SDCA), lifecycle EVENT, or live STATE.

    The ImpDef bucket keeps the internal name VENDOR; ImpDef is what the SPEC calls it and
    is the term the UI shows.

    The split between EVENT and STATE is the register's own nature: an ``IntStat_*`` /
    ``DevStat_*`` bit is RW1C — a LATCHED event, so it is worth a count of how many polls
    found it set. ``PortStatus`` / ``ReadyCh`` / ``COUNT_*`` are RO live values, where only
    the latest reading means anything."""
    if field_name.startswith("EC_"):
        return FAULT
    if "ImpDef" in field_name or "SDCA" in field_name:
        return VENDOR
    if field_name.lower().startswith("reserved"):
        return FAULT                       # a reserved bit read back set
    if field_name in _LIFECYCLE:
        return EVENT
    if field_name.startswith(("IntStat", "DevStat")):
        return FAULT
    return STATE


@dataclass(frozen=True)
class Report:
    """One byte of one status register, as the peripheral returned it."""
    device: int
    dp: Optional[int]                       # data-port index, or None outside the DP block
    register: str                           # spec register name, e.g. "IntStat"
    address: int
    value: int
    bus_row: int
    start_sample: int
    faults: Tuple[Tuple[str, int], ...]     # (field, value) the spec calls a fault
    vendor: Tuple[Tuple[str, int], ...]     # ImpDef / SDCA events that fired
    events: Tuple[Tuple[str, int], ...]     # latched lifecycle events that fired
    state: Tuple[Tuple[str, int], ...]      # live RO state, reported as-is

    @property
    def port(self) -> str:
        return f"Dev{self.device}" + (f" DP{self.dp}" if self.dp is not None else "")

    def label(self) -> str:
        """One-line summary, faults first — what a command row should show.

        A counter's whole point is its value, so ``EC_*`` / ``COUNT_*`` always carry the
        number; a flag set to 1 reads better bare. With no fault to report, fall back to
        the live state — naming only the non-zero fields once a register holds more than a
        handful, so an 8-bit array doesn't spell out seven zeros to report one bit.

        An ImpDef/SDCA field is listed as-is, with nothing appended to classify it: the
        NAME already says ImpDef or SDCA, which is the spec's own term for it, and
        "(vendor-defined)" after it neither named the spec term nor added information."""
        counter = self.register.startswith(("EC_", "COUNT_"))

        def show(name: str, value: int) -> str:
            return f"{name}={value}" if counter or value != 1 else name

        named = [show(n, v) for n, v in self.faults]
        named += [show(n, v) for n, v in self.vendor]
        if not named:
            wide = len(self.state) > 4
            named = [f"{n}={v}" for n, v in self.state if v or not wide]
        return f"{self.port} {self.register}" + (": " + ", ".join(named) if named else "")


def _device_of(cmd: dict) -> int:
    """The single peripheral a Read addressed (Section 8.1.2.2), or -1."""
    mask = int(cmd.get("device_mask", 0) or 0)
    return next((d for d in range(12) if mask & (1 << d)), -1)


def reports_for(cmd: dict) -> List[Report]:
    """Decode every status-register byte this command's read reply returned.

    Empty for anything that isn't a Read carrying a CRC-valid reply, and for replies to
    registers outside the reporting surface (a Read of Interval_NEXT tells us nothing
    about the device's health)."""
    if not cmd.get("has_address") or not cmd.get("read_data"):
        return []
    # A reply that failed CRC is not evidence of anything.
    if not cmd.get("read_data_crc_valid", True):
        return []
    rmap = _register_map()
    dev = _device_of(cmd)
    out: List[Report] = []
    base = int(cmd["address"])
    for i, byte in enumerate(cmd["read_data"]):
        res = rmap.resolve(base + i)
        if res is None or not res.register.name.startswith(_REPORTING_PREFIXES):
            continue
        faults: List[Tuple[str, int]] = []
        vendor: List[Tuple[str, int]] = []
        events: List[Tuple[str, int]] = []
        state: List[Tuple[str, int]] = []
        buckets = {FAULT: faults, VENDOR: vendor, EVENT: events}
        for name, value, _f in res.register.decode(int(byte)):
            kind = classify(name)
            if kind == STATE:
                state.append((name, value))
            elif value:                     # a latched bit only counts when SET
                buckets[kind].append((name, value))
        out.append(Report(
            device=dev, dp=res.dp_index, register=res.register.name,
            address=base + i, value=int(byte),
            bus_row=int(cmd.get("bus_row", 0)), start_sample=int(cmd.get("start_sample", 0)),
            faults=tuple(faults), vendor=tuple(vendor), events=tuple(events),
            state=tuple(state)))
    return out


def fault_label(cmd: dict) -> Optional[str]:
    """Label for a command whose reply reported a fault or an ImpDef/SDCA event, else None.

    Plain state (PortSyncOK=0, ReadyCh=3, a zero counter) is NOT an error — it is normal
    polling, and flagging it would bury the real reports under thousands of rows."""
    for r in reports_for(cmd):
        if r.faults or r.vendor:
            return r.label()
    return None


@dataclass
class Summary:
    """What every peripheral reported about itself across a whole capture."""
    reads: int = 0                                        # status registers bytes decoded
    # (port, field) -> [times seen set, FIRST bus row, highest value reported]
    faults: Dict[Tuple[str, str], List[int]] = field(default_factory=dict)
    vendor: Dict[Tuple[str, str], List[int]] = field(default_factory=dict)
    events: Dict[Tuple[str, str], List[int]] = field(default_factory=dict)
    state: Dict[str, Dict[str, int]] = field(default_factory=dict)   # port -> field -> last value
    per_port: Dict[str, int] = field(default_factory=dict)           # port -> bytes decoded


def summarize(commands: Iterable[dict]) -> Summary:
    """Fold every read reply in a capture into one report per peripheral.

    Counters (``EC_*``, ``COUNT_*``) SATURATE rather than accumulate per read, so their
    'value' is the highest the device ever reported, not a sum over polls."""
    s = Summary()
    for cmd in commands:
        for r in reports_for(cmd):
            s.reads += 1
            s.per_port[r.port] = s.per_port.get(r.port, 0) + 1
            for bucket, items in ((s.faults, r.faults), (s.vendor, r.vendor),
                                  (s.events, r.events)):
                for name, value in items:
                    key = (r.port, name)
                    if key in bucket:
                        bucket[key][0] += 1
                        bucket[key][2] = max(bucket[key][2], value)
                    else:
                        bucket[key] = [1, r.bus_row, value]
            live = s.state.setdefault(r.port, {})
            for name, value in r.state:
                live[name] = value
    return s
