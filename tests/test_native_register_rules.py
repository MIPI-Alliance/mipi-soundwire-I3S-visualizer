"""Rules of the C++ register model that the streaming decode and the Registers pane read.

- WriteIsImmediate: a write to a single-ranked register or to a writable _CURR alias takes
  committed effect at once; a write to a dual-ranked _NEXT register stages; a write to a
  read-only _CURR alias (CDS block) changes nothing: the Registers pane holds no value for
  it or for its _NEXT register, with or without a Commit after it, and a value staged in the
  _NEXT register is unchanged.
- A write that changes SkippingNumerator[11:8] (offset 0x0D) of a running port is flagged as
  a running-port programming error, as the low byte is.
- A Commit promotes SLC, CDS and PHY Control registers only when its group mask includes
  Commit Group 0 (Section 9.1.10.4.1).
- A bus write of 0 to SkippingDenominator reads as the register's reset value 1, in the
  replayed config and in the streaming decode.
- NumColumns reads all five bits [4:0], so a committed 16..31 gives 17..32 columns.
- SyncPointOffset reads all three bits [2:0]: a commit's SSP row comes SyncPointOffset rows
  before Row_Delay, for each of its valid values 0..5.
"""
import pytest
import swi3score
from test_register_history import _commit, _write

from swi3s_studio.session import Session

SLC_NEXT, SLC_CURR = 0x1081, 0x10C1
ENABLE_CH_NEXT, ENABLE_CH_CURR = 0x2081, 0x20C1
PORT_CONTROL = 0x200B
CDS_CURR = 0x11C1
CDS_NEXT = 0x1181                         # the _NEXT register CDS_CURR aliases
# Dual-ranked _NEXT registers of the CDS and PHY Control blocks (Commit Group 0 members).
CDS_HORIZONTAL_START_NEXT = 0x1184
PHY_NEXT = (0x180, 0x280, 0x380)          # PHY1, PHY2, PHY3 RiseFallTime


def test_write_is_immediate_follows_the_rank_the_address_names():
    imm = swi3score.register_write_is_immediate
    assert imm(PORT_CONTROL)                  # single-ranked
    assert imm(ENABLE_CH_CURR)                # writable _CURR alias: applied at once
    assert imm(SLC_CURR)
    assert not imm(ENABLE_CH_NEXT)            # dual-ranked _NEXT: staged until a Commit
    assert not imm(SLC_NEXT)
    assert not imm(CDS_CURR)                  # CDS _CURR is read-only: no effect


def _pane(cmds):
    return Session.from_demo(32)._build_register_files(cmds, None)


def test_a_commit_without_group_zero_leaves_the_slc_register_staged():
    cmds = [_write(SLC_NEXT, [3], sample=1),
            _commit(0b0010, sample=2)]                 # Commit Group 1 only
    dev0 = _pane(cmds)[0]
    assert dev0.value(SLC_NEXT) == 3
    assert dev0.curr_value(SLC_NEXT) == 0              # not promoted


def test_a_commit_with_group_zero_promotes_the_slc_register():
    cmds = [_write(SLC_NEXT, [3], sample=1),
            _commit(0b0011, sample=2)]
    assert _pane(cmds)[0].curr_value(SLC_NEXT) == 3


@pytest.mark.parametrize("addr", [CDS_HORIZONTAL_START_NEXT, *PHY_NEXT])
def test_a_commit_promotes_a_cds_or_phy_register_only_with_group_zero(addr):
    staged = [_write(addr, [3], sample=1)]
    reset = _pane(staged)[0].curr_value(addr)
    assert reset != 3
    dev0 = _pane(staged + [_commit(0b0010, sample=2)])[0]          # Commit Group 1 only
    assert dev0.value(addr) == 3 and dev0.curr_value(addr) == reset
    assert _pane(staged + [_commit(0b0011, sample=2)])[0].curr_value(addr) == 3


def _denominator(lo, hi=0):
    writes = [{"is_write": True, "device_mask": 1, "address": SLC_NEXT, "data": bytes([7])},
              {"is_write": True, "device_mask": 1, "address": 0x1012, "data": bytes([hi, lo])}]
    return swi3score.config_from_commands(writes)["skipping_denominator"]


def test_a_bus_write_of_zero_to_the_skipping_denominator_reads_as_one():
    assert _denominator(0) == 1
    assert (_denominator(5), _denominator(0x34, 0x1)) == (5, 0x134)

    def rates(denominator):
        levels = swi3score.make_skipping_levels(40, 1, denominator)
        d = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4),
                              swi3score.DecoderSettings())
        d.run()
        return dict(d.audio_sample_rates())

    # Numerator 1 against a denominator of 1 skips every interval: no rate, as for a 1
    # written; a denominator of 2 transports half of them.
    assert rates(0) == rates(1) == {(0, 0): 0.0}
    assert rates(2) == {(0, 0): 48_000.0}


@pytest.mark.parametrize("commit", [False, True], ids=["staged", "committed"])
def test_a_write_to_a_read_only_curr_alias_changes_nothing(commit):
    after = [_commit(0b0011, sample=3)] if commit else []
    assert _pane([_write(CDS_CURR, [3], sample=1), *after]) == {}
    staged = [_write(CDS_NEXT, [5], sample=1)]
    want = _pane(staged + after)[0]
    got = _pane(staged + [_write(CDS_CURR, [3], sample=2)] + after)[0]
    for addr in (CDS_NEXT, CDS_CURR):
        assert (got.value(addr), got.curr_value(addr)) == \
            (want.value(addr), want.curr_value(addr)), hex(addr)


def test_changing_the_skipping_numerator_high_byte_of_a_running_port_is_flagged():
    def flagged(value):
        levels = swi3score.make_running_port_write_levels(32, 0x0D, value)
        d = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, 4),
                              swi3score.DecoderSettings())
        d.run()
        [w] = [c for c in d.commands() if c["command"] == "WriteA32" and c["crc_valid"]
               and c["address"] == 0x200D]
        return w["running_port_write_error"]
    assert flagged(0x01) is True
    assert flagged(0x00) is False                # the value already there


def test_num_columns_reads_its_five_bits():
    for value in (3, 15, 16, 17, 30, 31):
        cmds = [{"is_write": True, "device_mask": 1, "address": SLC_NEXT, "data": bytes([value])},
                {"is_commit": True, "commit_confirmed": True, "device_mask": 1,
                 "group_mask": 0b0001}]
        assert swi3score.config_from_commands(cmds)["num_columns"] == value, value


SYNC_POINT_OFFSET = 0x1014


def _commit_row(offset):
    """The effective row of an SSCR (Row_Delay 14) after device 0's SyncPointOffset is
    written `offset`, on the synthetic 4-column bus of test_native_command_parser."""
    import test_native_command_parser as cp
    phases = [cp._ping(), cp._write(packet=[0, 0, 0, SYNC_POINT_OFFSET >> 8,
                                            SYNC_POINT_OFFSET & 0xFF, offset]),
              cp._confirmed_commit(0x00)] + [cp._ping()] * 20
    cds = cp._idle(4)
    for p in phases:
        cds += p + cp._idle(2)
    st = swi3score.DecoderSettings()
    st.forced_column_count, st.decode_audio = 4, False
    d = swi3score.Decoder(swi3score.MemorySampleSource(cp._bus(4, {0: cds + cp._idle(4)}),
                                                       98_304_000, 4), st)
    d.run()
    [sscr] = [c for c in d.commands() if c["command"] == "SSCR"]
    return sscr["effective_row"]


def test_the_sync_point_offset_moves_the_commit_row_by_its_three_bits():
    base = _commit_row(0)
    assert base > 0
    assert [base - _commit_row(offset) for offset in range(1, 6)] == [1, 2, 3, 4, 5]
