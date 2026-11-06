"""A source DRQ's guard and tail follow FCP_GuardEnable, FCP_GuardPolarity and FCP_TailWidth.

The Flow Control Port of a SINK data port drives a SOURCE DRQ. In the DRQ's row the bit is
followed by one guard UI when FCP_GuardEnable is set, a Guard1 slot when FCP_GuardPolarity
is set and a Guard0 slot when it is clear, then FCP_TailWidth Tail slots (0..3).
`make_fcp_guard_tail_levels` builds such a port, RX_CONTROLLED or ASYNC, and commits a new
FCP placement (FCP_HorizontalStart, FCP_Offset, FCP_BitWidth) together with the three
fields by an SSCR or a DSCR. The decoder reads the committed fields off the bus: its grid
places the DRQ, guard and tail cells at the committed placement, every handshake check
passes and the audio decodes to the sine. The capture drives each guard UI to its polarity.
With `first_guard_enable` the port starts with its guard on, so a commit can change
FCP_GuardPolarity alone.
"""
import pytest
import swi3score
from test_native_decoder_run import _sine

_SAMPLES_PER_UI = 4
_COLUMNS = 16
_INTERVAL_ROWS = 16
_SLOT = {name: i for i, name in enumerate(swi3score.SLOT_NAMES)}
_FCP_SLOTS = {_SLOT["Drq"], _SLOT["Guard0"], _SLOT["Guard1"], _SLOT["Tail"]}


def _decode(levels):
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, _SAMPLES_PER_UI),
                            swi3score.DecoderSettings())
    dec.run()
    return dec


def _expected_cells(h_start, offset, bit_width, tail, guard, polarity):
    """{(row, col): slot} of the FCP's cells in one interval."""
    cells = {(offset, h_start + i): _SLOT["Drq"] for i in range(bit_width + 1)}
    col = h_start + bit_width + 1
    if guard:
        cells[(offset, col)] = _SLOT["Guard1" if polarity else "Guard0"]
        col += 1
    cells.update({(offset, col + i): _SLOT["Tail"] for i in range(tail)})
    return cells


_UNMOVED = (11, 12, 0)           # the fixture's placement before the commit
_GUARDS = {"off": (False, False), "off_polarity1": (False, True), "guard0": (True, False),
           "guard1": (True, True)}
# Each placement with each FCP_TailWidth and guard setting. At the unmoved placement the
# commit changes only FCP_TailWidth and the guard fields, and the setting that leaves all
# three at their old values commits no change. The fixture's TX_PRESENT follows the DRQ
# history from the port's enable across the commit, as the decode reads it.
_CASES = [pytest.param(*place, tail, *_GUARDS[g], id="h{}_o{}_w{}".format(*place) +
                       f"-tail{tail}-{g}")
          for place in (_UNMOVED, (6, 12, 0), (3, 0, 3), (10, 5, 1))
          for tail in range(4) for g in _GUARDS]


@pytest.mark.parametrize("async_mode", [False, True], ids=["rx", "async"])
@pytest.mark.parametrize("dscr", [False, True], ids=["sscr", "dscr"])
@pytest.mark.parametrize("h_start,offset,bit_width,tail,guard,polarity", _CASES)
def test_the_guard_and_tail_follow_the_committed_fields(h_start, offset, bit_width, tail,
                                                        guard, polarity, dscr, async_mode):
    """The committed FCP_GuardEnable, FCP_GuardPolarity and FCP_TailWidth place one guard
    cell of the selected polarity (none when the guard is disabled, whatever its polarity)
    and `tail` Tail cells after the DRQ, at the committed placement, also when the commit
    changes those fields alone; the decode passes every handshake check and recovers the
    sine."""
    levels = swi3score.make_fcp_guard_tail_levels(96, dscr, h_start, offset, bit_width, tail,
                                                  guard, polarity, async_mode)
    dec = _decode(levels)
    commits = [c for c in dec.commands() if c["command"] in ("SSCR", "DSCR")]
    assert [c["command"] for c in commits] == ["SSCR", "DSCR" if dscr else "SSCR"]
    stats = dec.flow_control_stats()
    assert stats["checked"] >= 96 and stats["fails"] == 0, stats
    audio = sorted(dec.audio(), key=lambda a: a["index"])
    assert len(audio) > 96
    assert [a["value"] for a in audio] == [_sine(0, i, 15) for i in range(len(audio))]

    fcp = [c for c in dec.grid_cells(_INTERVAL_ROWS) if c["slot"] in _FCP_SLOTS]
    assert all(c["is_source"] and c["device"] == 0 and c["dp"] == 0 for c in fcp)
    got = {(c["row"], c["col"]): c["slot"] for c in fcp}
    assert got == _expected_cells(h_start, offset, bit_width, tail, guard, polarity)

    if guard:
        # Level index of (bus row R, column c): the segment's first Column 0 sits one
        # level before its start_ui.
        seg = dec.segments()[-1]
        column0 = seg["start_ui"] - 1 + (commits[-1]["effective_row"] - seg["row_base"]) * _COLUMNS
        guard_col = h_start + bit_width + 1
        wire = {levels[column0 + (k * _INTERVAL_ROWS + offset) * _COLUMNS + guard_col]
                for k in range(90)}
        assert wire == {polarity}


def _guard_wire(dec, levels, commit, guard_col, offset, intervals):
    """The wire level of the guard UI (`offset`, `guard_col`) in each of the first
    `intervals` intervals from `commit`'s effective row."""
    seg = dec.segments()[-1]
    column0 = seg["start_ui"] - 1 + (commit["effective_row"] - seg["row_base"]) * _COLUMNS
    return {levels[column0 + (k * _INTERVAL_ROWS + offset) * _COLUMNS + guard_col]
            for k in range(intervals)}


@pytest.mark.parametrize("async_mode", [False, True], ids=["rx", "async"])
@pytest.mark.parametrize("dscr", [False, True], ids=["sscr", "dscr"])
@pytest.mark.parametrize("first_polarity", [False, True], ids=["guard0_to_1", "guard1_to_0"])
def test_a_polarity_only_commit_with_the_guard_on_flips_the_guard(first_polarity, dscr,
                                                                  async_mode):
    """A commit that changes FCP_GuardPolarity alone, the guard enabled before and after,
    turns the guard cell from Guard0 to Guard1 (or back) and the guard UI's wire level with
    it; every handshake check passes and the audio decodes to the sine."""
    h_start, offset, bit_width = _UNMOVED
    polarity = not first_polarity
    levels = swi3score.make_fcp_guard_tail_levels(
        96, dscr, h_start, offset, bit_width, 0, True, polarity, async_mode,
        first_guard_enable=True, first_guard_polarity=first_polarity)
    dec = _decode(levels)
    commits = [c for c in dec.commands() if c["command"] in ("SSCR", "DSCR")]
    assert [c["command"] for c in commits] == ["SSCR", "DSCR" if dscr else "SSCR"]
    stats = dec.flow_control_stats()
    assert stats["checked"] >= 96 and stats["fails"] == 0, stats
    audio = sorted(dec.audio(), key=lambda a: a["index"])
    assert len(audio) > 96
    assert [a["value"] for a in audio] == [_sine(0, i, 15) for i in range(len(audio))]

    fcp = [c for c in dec.grid_cells(_INTERVAL_ROWS) if c["slot"] in _FCP_SLOTS]
    got = {(c["row"], c["col"]): c["slot"] for c in fcp}
    assert got == _expected_cells(h_start, offset, bit_width, 0, True, polarity)
    assert got != _expected_cells(h_start, offset, bit_width, 0, True, first_polarity)

    guard_col = h_start + bit_width + 1
    assert _guard_wire(dec, levels, commits[0], guard_col, offset, 90) == {first_polarity}
    assert _guard_wire(dec, levels, commits[1], guard_col, offset, 90) == {polarity}


def test_the_first_guard_arguments_default_to_the_guard_off_fixture():
    assert swi3score.make_fcp_guard_tail_levels() == swi3score.make_fcp_guard_tail_levels(
        first_guard_enable=False, first_guard_polarity=False)


_WIDTH = "fcp_bit_width and fcp_tail_width must be 0..3"
_CELL = "must lie in Columns 1..15 and Rows 0..15"


@pytest.mark.parametrize("args,message", [
    (dict(fcp_tail_width=4), _WIDTH), (dict(fcp_bit_width=4), _WIDTH),
    (dict(fcp_tail_width=-1), _WIDTH), (dict(fcp_bit_width=-1), _WIDTH),
    (dict(fcp_h_start=12, fcp_tail_width=3, fcp_guard_enable=True), _CELL),
    (dict(fcp_h_start=1, fcp_offset=3, fcp_tail_width=1), _CELL),
    (dict(fcp_h_start=0), _CELL), (dict(fcp_h_start=16), _CELL),
    (dict(fcp_offset=16), _CELL), (dict(fcp_offset=-1), _CELL),
    (dict(fcp_h_start=2**31 - 1), _CELL),
    (dict(fcp_h_start=2**31 - 2, fcp_guard_enable=True, fcp_tail_width=1), _CELL)])
def test_a_field_or_cell_outside_the_fixtures_range_raises(args, message):
    """Each argument is rejected for its own reason at a length that builds."""
    assert swi3score.make_fcp_guard_tail_levels(96)
    with pytest.raises(ValueError, match=message):
        swi3score.make_fcp_guard_tail_levels(96, **args)


@pytest.mark.parametrize("intervals", [1, 64, 85])
def test_a_count_too_small_for_the_setup_reports_the_count_passed(intervals):
    with pytest.raises(ValueError, match=f"intervals_per_phase {intervals} leaves no room"):
        swi3score.make_fcp_guard_tail_levels(intervals)
    assert swi3score.make_fcp_guard_tail_levels(86)
