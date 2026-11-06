"""A Write that changes a single-ranked control register of a running data port is flagged
as a programming error (CommandRec::runningPortWriteError, analysis.errors.command_error).

SampleSizeGrouping (0x09), PortControl (0x0B), SkippingNumerator (0x0C-0x0D) and
FlowControl (0x0E) take effect at once, so they are set while the port's channels are
disabled. Writing one while a channel is enabled in the committed rank is legal on the bus
— not a protocol error — but changes the running transport under the port. A write of the
value already there, a register outside the set, and a write before the port runs are not
flagged. The decode still follows the write.

A WriteA32 covering a read-only byte (any register block) carries read_only_write_error
whether or not the port runs: the cases write the data port's read-only registers, read-only
SLC registers and a CDS register's _CURR alias while the port runs, and a data-port
read-only register before it runs.

Every input is synthetic: make_running_port_write_levels (one running 16-bit PCM port, one
WriteA32 mid-stream or among the setup writes) and the demos.
"""
import math

import pytest
import swi3score

from swi3s_studio.analysis.errors import command_error

N = 32


def _sine(index: int) -> int:
    v = int(round((1 << 15) * 0.45 * math.sin(2 * math.pi * index / 64)))
    return v & 0xFFFF


def _decode(levels):
    d = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4),
                          swi3score.DecoderSettings())
    d.run()
    return d


def _write_at(d, address):
    """The fixture's write: the last one to `address` (setup may write it first)."""
    return [c for c in d.commands() if c["command"] == "WriteA32" and c["crc_valid"]
            and c["address"] == address][-1]


def _write_to(d, offset):
    """The fixture's mid-stream write to dp0's register at `offset`."""
    return _write_at(d, 0x2000 + offset)


@pytest.mark.parametrize("offset,value,register", [
    (0x09, 0x17, "SampleSizeGrouping"),      # SampleSize 16 -> 24 bits
    (0x0B, 0x00, "PortControl"),             # ScramblerEn off
    (0x0C, 0x05, "SkippingNumerator"),
    (0x0D, 0x01, "SkippingNumerator"),       # SkippingNumerator[11:8]
    (0x0E, 0x8A, "FlowControl"),             # FlowMode Normal -> RX controlled
])
def test_changing_a_single_ranked_register_of_a_running_port_is_a_programming_error(
        offset, value, register):
    d = _decode(swi3score.make_running_port_write_levels(N, offset, value))
    setup = [c for c in d.commands() if c["command"] == "WriteA32"
             and c["address"] == 0x2000 + offset][:-1]
    assert not any(c["running_port_write_error"] for c in setup)    # before the port ran
    w = _write_to(d, offset)
    assert w["running_port_write_error"]
    assert not (w["enablech_curr_error"] or w["unexpected_ssp_error"])
    assert command_error(w) == f"Programming error: {register} of DP0 changed while the port runs"
    # The decode follows the write; the audio before it is untouched.
    audio = sorted(d.audio(), key=lambda a: a["start_sample"])
    before = [a for a in audio if a["end_sample"] < w["start_sample"]]
    assert len(before) >= N - 2
    assert [a["value"] for a in before] == [_sine(i) for i in range(len(before))]


@pytest.mark.parametrize("offset,value", [
    (0x0E, 0x88),     # FlowControl's value already: nothing changes
    (0x0D, 0x00),     # SkippingNumerator[11:8] already 0
    (0x0A, 0x03),     # commit-group membership: not in the set
])
def test_a_write_that_changes_nothing_or_is_outside_the_set_is_not_flagged(offset, value):
    d = _decode(swi3score.make_running_port_write_levels(N, offset, value))
    w = _write_to(d, offset)
    assert not w["running_port_write_error"]
    assert command_error(w) is None
    assert len(d.audio()) >= 2 * N - 2


@pytest.mark.parametrize("levels", [
    lambda: swi3score.make_demo_levels(64),             # PortControl set before the ports run
    lambda: swi3score.make_demo_levels_flow_control(16),
    lambda: swi3score.make_skipping_levels(200),        # SkippingNumerator likewise
], ids=["demo", "flow-control", "skipping"])
def test_control_registers_set_before_the_port_runs_are_not_flagged(levels):
    d = _decode(levels())
    assert not [c["address"] for c in d.commands() if c["running_port_write_error"]]


@pytest.mark.parametrize("offset,value,flagged", [
    (0x05, 0x03, True),                      # PortStatus: RO
    (0x06, 0xFF, True),                      # ReadyCh_L: RO
    (0x07, 0x01, True),                      # ReadyCh_H: RO
    (0x20, 0x01, True),                      # EC_TestFailCh0: RO
    (0x31, 0x01, True),                      # EC_FlowControlFail: RO
    (0x3F, 0x01, True),                      # COUNT_ReceivedSSP: RO
    (0x00, 0x01, False),                     # IntStat: RW1C, a legal clear
    (0x0A, 0x01, False),                     # CommitGroupMemb: RW, the value already there
])
def test_a_write_to_a_read_only_register_is_flagged(offset, value, flagged):
    """A WriteA32 covering a read-only byte carries read_only_write_error, whatever the port
    is doing; no other command does."""
    d = _decode(swi3score.make_running_port_write_levels(N, offset, value))
    w = _write_to(d, offset)
    assert w["read_only_write_error"] is flagged
    assert not any(c["read_only_write_error"] for c in d.commands()
                   if c["start_sample"] != w["start_sample"])


@pytest.mark.parametrize("base,offset,value,flagged", [
    (0x1000, 0x00, 0x01, True),              # SLC ID0: RO
    (0x1000, 0x60, 0x01, True),              # SLC EC_* error counter: RO
    (0x1100, 0xC9, 0x08, True),              # CDS_ApertureAdjust at its _CURR alias: RO
    (0x1100, 0x89, 0x00, False),             # CDS_ApertureAdjust (_NEXT): RW
])
def test_a_write_to_a_read_only_slc_or_cds_curr_register_is_flagged(base, offset, value,
                                                                     flagged):
    """A WriteA32 covering a read-only SLC byte or a CDS register's _CURR alias carries
    read_only_write_error; the same CDS register's writable _NEXT address does not, and no
    other command does."""
    d = _decode(swi3score.make_running_port_write_levels(N, offset, value, base=base))
    w = _write_at(d, base + offset)
    assert w["read_only_write_error"] is flagged
    assert not w["running_port_write_error"]
    assert not any(c["read_only_write_error"] for c in d.commands()
                   if c["start_sample"] != w["start_sample"])


def test_a_read_only_write_before_the_port_runs_is_flagged():
    """The read-only flag does not depend on the port running: a PortStatus write among the
    setup writes, before the SSCR, carries it, and no other command does."""
    d = _decode(swi3score.make_running_port_write_levels(N, 0x05, 0x03, before_run=True))
    w = _write_to(d, 0x05)
    sscr = next(c for c in d.commands() if c["command"] == "SSCR")
    assert w["start_sample"] < sscr["start_sample"]
    assert w["read_only_write_error"] and not w["running_port_write_error"]
    assert not any(c["read_only_write_error"] for c in d.commands()
                   if c["start_sample"] != w["start_sample"])
