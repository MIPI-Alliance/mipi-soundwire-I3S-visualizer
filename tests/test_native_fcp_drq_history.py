"""A flow-controlled port's DRQ history runs from the port's enable.

A Source DP takes FCP_DRQ as 1 for the startup intervals counted from when it was enabled,
and after them the DRQ of the interval d = FlowControlDelay + 1 earlier ({ASW5204},
Table 160 (v1.1 r10)). A commit that changes the port's config while the port stays
enabled and keeps its FCP, an SSCR or a DSCR, carries the history when the port is at the
start of an interval there: the decoder checks the first intervals after it against the
DRQs sent before it. The history starts again, with TX_PRESENT 1 for the first d intervals,
when the port is disabled and enabled again, when its FlowMode gains the DRQ ({ASW5201}),
and at a commit that lands part-way through the port's interval.

`make_fcp_move_levels` commits a new FCP placement to an RX_CONTROLLED port; with
`reenable` it disables the port by one commit and enables it again by a third, with
`commit_row_offset` it lands the commit that many rows into an interval, and with
`columns_after` the commit also changes the column count. At the fixture's default length
the d DRQs before the commit are 0, so a TX_PRESENT taken from a restarted history (1) and
one taken from the carried history (0) differ there: the tests read those DRQs and the
first TX_PRESENT after the commit off the wire.
"""
import pytest
import swi3score
from test_native_decoder_run import _sine

_SAMPLES_PER_UI = 4
_INTERVAL_ROWS = 16
_INTERVALS = 96
_DRQ_CELL = (12, 11)           # the fixture's DRQ before the commit: (row in interval, column)
_TXP = swi3score.SLOT_NAMES.index("TxPresent")


def _decode(levels):
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, _SAMPLES_PER_UI),
                            swi3score.DecoderSettings())
    dec.run()
    return dec


def _wire(dec, levels):
    """The level at (bus row, column), read in the segment that holds the row."""
    segs = dec.segments()

    def level(row, col):
        seg = [s for s in segs if s["row_base"] <= row][-1]
        return bool(levels[seg["start_ui"] - 1 + (row - seg["row_base"]) * seg["column_count"]
                           + col])
    return level


def _commits(dec):
    return [c for c in dec.commands() if c["command"] in ("SSCR", "DSCR")]


def _drqs_before(dec, levels, commit, d):
    """The last `d` DRQs the FCP sent before `commit`."""
    wire = _wire(dec, levels)
    first = _commits(dec)[0]["effective_row"]
    k = (commit["effective_row"] - first) // _INTERVAL_ROWS
    row, col = _DRQ_CELL
    return [wire(first + i * _INTERVAL_ROWS + row, col) for i in range(k - d, k)]


def _tx_present_after(dec, levels, commit, d):
    """TX_PRESENT of the first `d` intervals from `commit`."""
    (row, col), = {(c["row"], c["col"]) for c in dec.grid_cells(_INTERVAL_ROWS)
                   if c["slot"] == _TXP}
    wire = _wire(dec, levels)
    return [wire(commit["effective_row"] + i * _INTERVAL_ROWS + row, col) for i in range(d)]


@pytest.mark.parametrize("delay", [0, 1], ids=["d1", "d2"])
@pytest.mark.parametrize("offset", [0, 12], ids=["drq_before_data", "drq_after_data"])
@pytest.mark.parametrize("dscr", [False, True], ids=["sscr", "dscr"])
def test_a_config_changing_commit_carries_the_drq_history(dscr, offset, delay):
    """The first d intervals after a commit that moves the FCP carry the DRQs sent before
    it as their TX_PRESENT, not the startup 1, and the decoder passes every handshake check
    reading them against the carried history."""
    d = delay + 1
    levels = swi3score.make_fcp_move_levels(_INTERVALS, dscr, 13, offset, delay)
    dec = _decode(levels)
    commits = _commits(dec)
    assert [c["command"] for c in commits] == ["SSCR", "DSCR" if dscr else "SSCR"]
    assert _drqs_before(dec, levels, commits[1], d) == [False] * d, "test premise"
    assert _tx_present_after(dec, levels, commits[1], d) == [False] * d
    stats = dec.flow_control_stats()
    assert stats["checked"] >= _INTERVALS and stats["fails"] == 0, stats
    audio = sorted(dec.audio(), key=lambda a: a["index"])
    assert len(audio) > _INTERVALS
    assert [a["value"] for a in audio] == [_sine(0, i, 15) for i in range(len(audio))]


@pytest.mark.parametrize("delay", [0, 1], ids=["d1", "d2"])
@pytest.mark.parametrize("dscr", [False, True], ids=["sscr", "dscr"])
def test_re_enabling_the_port_restarts_the_drq_history(dscr, delay):
    """A port disabled by one commit and enabled by the next takes TX_PRESENT 1 for its
    first d intervals although the DRQs before the disable were 0; the decoder passes every
    handshake check and the audio starts again from the sine's first sample."""
    d = delay + 1
    levels = swi3score.make_fcp_move_levels(_INTERVALS, dscr, 13, 12, delay, reenable=True)
    dec = _decode(levels)
    commits = _commits(dec)
    kind = "DSCR" if dscr else "SSCR"
    assert [c["command"] for c in commits] == ["SSCR", kind, kind]
    assert _drqs_before(dec, levels, commits[1], d) == [False] * d, "test premise"
    assert _tx_present_after(dec, levels, commits[2], d) == [True] * d
    stats = dec.flow_control_stats()
    assert stats["checked"] >= _INTERVALS and stats["fails"] == 0, stats

    seg = dec.segments()[-1]
    cut = (seg["start_ui"] - 1
           + (commits[2]["effective_row"] - seg["row_base"]) * seg["column_count"])
    cut *= _SAMPLES_PER_UI
    for run in ([a for a in dec.audio() if a["start_sample"] < cut],
                [a for a in dec.audio() if a["start_sample"] >= cut]):
        run.sort(key=lambda a: a["index"])
        assert len(run) > _INTERVALS // 2
        assert [a["value"] for a in run] == [_sine(0, i, 15) for i in range(len(run))]


def _assert_sine(dec):
    audio = sorted(dec.audio(), key=lambda a: a["index"])
    assert len(audio) > _INTERVALS
    assert [a["value"] for a in audio] == [_sine(0, i, 15) for i in range(len(audio))]


@pytest.mark.parametrize("delay", [0, 1], ids=["d1", "d2"])
@pytest.mark.parametrize("dscr", [False, True], ids=["sscr", "dscr"])
def test_a_commit_that_changes_the_column_count_carries_the_drq_history(dscr, delay):
    """An SSCR or a DSCR that moves the FCP and commits 32 columns rebuilds the port at the
    new width; the first d intervals after it carry the DRQs sent before it as their
    TX_PRESENT, and the decoder passes every handshake check reading them."""
    d = delay + 1
    levels = swi3score.make_fcp_move_levels(_INTERVALS, dscr, 13, 12, delay, columns_after=32)
    dec = _decode(levels)
    commits = _commits(dec)
    assert [c["command"] for c in commits] == ["SSCR", "DSCR" if dscr else "SSCR"]
    assert [s["column_count"] for s in dec.segments()][-2:] == [16, 32]
    assert _drqs_before(dec, levels, commits[1], d) == [False] * d, "test premise"
    assert _tx_present_after(dec, levels, commits[1], d) == [False] * d
    stats = dec.flow_control_stats()
    assert stats["checked"] >= _INTERVALS and stats["fails"] == 0, stats
    _assert_sine(dec)


@pytest.mark.parametrize("delay", [0, 1], ids=["d1", "d2"])
@pytest.mark.parametrize("rows_in", [1, 8, 15])
def test_an_sscr_part_way_through_an_interval_restarts_the_drq_history(rows_in, delay):
    """An SSCR whose SSP lands `rows_in` rows into the port's interval re-anchors the port
    there, and its DRQ history starts again: the first d intervals after it take
    TX_PRESENT 1 although the DRQs before it were 0, and the decoder passes every handshake
    check. The rule is the interval-start condition of CPayloadEngine::Configure."""
    d = delay + 1
    levels = swi3score.make_fcp_move_levels(_INTERVALS, False, 13, 12, delay,
                                            commit_row_offset=rows_in)
    dec = _decode(levels)
    commits = _commits(dec)
    assert [c["command"] for c in commits] == ["SSCR", "SSCR"]
    assert (commits[1]["effective_row"] - commits[0]["effective_row"]) % _INTERVAL_ROWS == rows_in
    assert _drqs_before(dec, levels, commits[1], d) == [False] * d, "test premise"
    assert _tx_present_after(dec, levels, commits[1], d) == [True] * d
    stats = dec.flow_control_stats()
    assert stats["checked"] >= _INTERVALS and stats["fails"] == 0, stats
    _assert_sine(dec)


@pytest.mark.parametrize("delay", [0, 1], ids=["d1", "d2"])
def test_a_port_whose_flow_mode_gains_the_drq_at_a_commit_restarts_its_history(delay):
    """With FlowMode overridden to TX_CONTROLLED (no FCP_DRQ) in the 16-column config region
    (section 1), the commit that opens the 32-column region gives the port its DRQ: the
    decoder takes the startup 1 for its first d intervals, and the wire, whose TX_PRESENT
    continues the DRQs sent before the commit (0), fails exactly those d checks."""
    d = delay + 1
    levels = swi3score.make_fcp_move_levels(_INTERVALS, False, 13, 12, delay, columns_after=32)
    settings = swi3score.DecoderSettings()
    flow_mode = 0x2000 + 0x0E                       # dp0's FlowControl register
    settings.register_overrides = [(1, 0, flow_mode, 0x01 | (0x80 if delay else 0))]
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, _SAMPLES_PER_UI),
                            settings)
    dec.run()
    commits = _commits(dec)
    assert [s["column_count"] for s in dec.segments()] == [2, 16, 32]
    assert _drqs_before(dec, levels, commits[1], d) == [False] * d, "test premise"
    assert _tx_present_after(dec, levels, commits[1], d) == [False] * d, "test premise"
    stats = dec.flow_control_stats()
    assert stats["checked"] >= _INTERVALS and stats["fails"] == d, stats


@pytest.mark.parametrize("args", [
    dict(commit_row_offset=-1), dict(commit_row_offset=16),
    dict(commit_row_offset=4, dscr=True), dict(commit_row_offset=4, reenable=True),
    dict(columns_after=8), dict(columns_after=24), dict(columns_after=32, reenable=True)])
def test_a_commit_row_offset_or_column_count_outside_the_fixtures_range_raises(args):
    with pytest.raises(ValueError, match="commit_row_offset|columns_after"):
        swi3score.make_fcp_move_levels(_INTERVALS, **args)
