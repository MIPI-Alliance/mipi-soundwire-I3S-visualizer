"""The Flow Control Port shares its data port's Column-0 phase.

A config-CSV decode of a capture that starts partway into a row seeds every data port
with the capture's start column. The FCP beside an RX_CONTROLLED or ASYNC port places its
DRQ bit at the absolute bus column FCP_HorizontalStart, so it is seeded with the same
start column. These tests decode the flow-control demo once from the wire (the reference)
and then from slices that start at every column of a row, with the demo's config CSV, and
check that each DRQ keeps its distance from the data it gates.

A port's FCP placement (FCP_HorizontalStart, FCP_Offset, FCP_BitWidth, FCP_TailWidth and
the FCP guard) is part of its transport: a commit that changes only those fields
reconfigures the port, and the DRQ is read at the new cell from the commit point.
`make_fcp_move_levels` commits a change of FCP_HorizontalStart, FCP_Offset and
FCP_BitWidth (a wider sink DRQ is sampled on its last UI) with an SSCR or a DSCR; the
fixture's DRQ is a sink, which carries no guard or tail. The decode passes
every handshake check, which compares interval T's TX_PRESENT with the DRQ of interval
T - d, whether the DRQ lies before or after the data window and for both FlowControlDelay
settings. With the port's ScramblerEn set, TX_PRESENT passes through the channel's
descrambler like a data bit: the handshake check reads the recovered TX_PRESENT and the
audio decodes to the sine, while the same capture read with the scrambler forced off fails
the check.
"""
import collections
import os

import pytest
import swi3score
from test_native_decoder_run import _sine

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_CSV = os.path.join(_FIXTURES, "flow_control_demo.csv")
_SAMPLES_PER_UI = 4
_COLUMNS = 16
_DRQ_DEVICES = (2, 3)            # RX_CONTROLLED, ASYNC


def _decode(levels, csv=None):
    st = swi3score.DecoderSettings()
    if csv:
        st.config_csv_path = csv
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, _SAMPLES_PER_UI), st)
    dec.run()
    return dec


def _drq_to_sample_end(dec):
    """Bus-column distance from each DRQ-gated sample's DRQ bit to the sample's last bit."""
    return collections.Counter(
        ((a["end_sample"] - a["drq_sample"]) // _SAMPLES_PER_UI) % _COLUMNS
        for a in dec.audio() if a["device"] in _DRQ_DEVICES and a["drq_sample"])


@pytest.fixture(scope="module")
def flow_levels():
    levels = swi3score.make_demo_levels_flow_control(120)
    ref = _decode(levels)
    assert ref.segments()[-1]["column_count"] == _COLUMNS
    assert ref.flow_control_stats()["fails"] == 0
    return levels, ref


@pytest.mark.parametrize("offset", range(_COLUMNS))
def test_csv_decode_from_mid_row_keeps_drq_column(flow_levels, offset):
    levels, ref = flow_levels
    want = set(_drq_to_sample_end(ref))
    assert want, "the reference decode carries no DRQ-gated samples"
    start = ref.segments()[-1]["start_ui"] + 64 * _COLUMNS + offset
    got = _drq_to_sample_end(_decode(levels[start:], _CSV))
    assert got, f"offset {offset}: no DRQ-gated samples decoded"
    assert set(got) <= want, (
        f"offset {offset}: DRQ-to-sample distances {dict(got)}, wire decode {sorted(want)}")


_INTERVAL_UIS = 16 * _COLUMNS    # the move fixture's 16-row interval


def _move_decode(dscr, h_start, offset, delay=0, scrambled=False, bit_width=0, **settings):
    levels = swi3score.make_fcp_move_levels(96, dscr, h_start, offset, delay, scrambled,
                                            bit_width)
    st = swi3score.DecoderSettings()
    for k, v in settings.items():
        setattr(st, k, v)
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, 98_304_000, _SAMPLES_PER_UI), st)
    dec.run()
    commit = [c for c in dec.commands() if c["command"] in ("SSCR", "DSCR")]
    assert [c["command"] for c in commit] == ["SSCR", "DSCR" if dscr else "SSCR"]
    seg = dec.segments()[-1]
    cut = (seg["start_ui"] + (commit[-1]["effective_row"] - seg["row_base"]) * _COLUMNS)
    return dec, cut * _SAMPLES_PER_UI


@pytest.mark.parametrize("dscr", [False, True], ids=["sscr", "dscr"])
@pytest.mark.parametrize("h_start,offset,bit_width", [
    (13, 12, 0), (11, 10, 0), (11, 0, 0), (5, 5, 0), (11, 12, 1), (11, 12, 3), (12, 10, 2)])
def test_a_committed_fcp_placement_moves_the_drq_read(dscr, h_start, offset, bit_width):
    """A commit that changes only FCP_HorizontalStart, FCP_Offset and FCP_BitWidth moves
    the DRQ the decoder reads to the new cell, the last UI of a wide DRQ: every handshake
    check passes, the audio is the generated sine, and each gated sample's DRQ sits the
    placement's distance away."""
    dec, cut = _move_decode(dscr, h_start, offset, bit_width=bit_width)
    stats = dec.flow_control_stats()
    assert stats["checked"] >= 96 and stats["fails"] == 0, stats
    audio = sorted(dec.audio(), key=lambda a: a["index"])
    assert [a["value"] for a in audio] == [_sine(0, i, 15) for i in range(len(audio))]
    gap = collections.defaultdict(collections.Counter)
    for a in audio:
        gap[a["drq_sample"] >= cut][
            ((a["end_sample"] - a["drq_sample"]) // _SAMPLES_PER_UI) % _INTERVAL_UIS] += 1
    before = gap[False].most_common(1)[0][0]
    moved = (offset - 12) * _COLUMNS + (h_start - 11) + bit_width
    assert set(gap[True]) == {(before - moved) % _INTERVAL_UIS}, (dict(gap[False]), dict(gap[True]))


@pytest.mark.parametrize("delay", [0, 1], ids=["d1", "d2"])
@pytest.mark.parametrize("offset", [0, 12], ids=["drq_before_data", "drq_after_data"])
def test_the_drq_history_reaches_back_d_intervals(delay, offset):
    """Interval T's TX_PRESENT is checked against the DRQ of interval T - d, d =
    FlowControlDelay + 1, whether the DRQ cell lies before or after the data window."""
    dec, _ = _move_decode(True, 11, offset, delay)
    stats = dec.flow_control_stats()
    assert stats["checked"] == 192 and stats["fails"] == 0, stats


@pytest.mark.parametrize("dscr", [False, True], ids=["sscr", "dscr"])
def test_a_scrambled_port_descrambles_its_tx_present(dscr):
    """TX_PRESENT and each data bit, carrying a sample or not, step the channel's LFSR."""
    dec, _ = _move_decode(dscr, 13, 12, scrambled=True)
    stats = dec.flow_control_stats()
    assert stats["checked"] >= 96 and stats["fails"] == 0, stats
    audio = sorted(dec.audio(), key=lambda a: a["index"])
    assert len(audio) > 96
    assert [a["value"] for a in audio] == [_sine(0, i, 15) for i in range(len(audio))]
    raw, _ = _move_decode(dscr, 13, 12, scrambled=True, scrambler_overrides=[(0, 0, False)])
    assert raw.flow_control_stats()["fails"] > 0, "test premise: TX_PRESENT is scrambled"


@pytest.mark.parametrize("h_start,bit_width", [(1, 1), (1, 2), (1, 3), (2, 0), (14, 0)])
def test_a_drq_reaching_into_the_data_window_raises(h_start, bit_width):
    """A DRQ whose span (fcp_bit_width + 1 UIs) touches Columns 2..14 of Rows 2..4 is
    rejected; the same cell with a width that stays in Column 1 is built."""
    with pytest.raises(ValueError, match="off Columns 2..14 of Rows 2..4"):
        swi3score.make_fcp_move_levels(96, False, h_start, 3, 0, False, bit_width)
    if h_start == 1:
        assert swi3score.make_fcp_move_levels(96, False, 1, 3, 0, False, 0)


@pytest.mark.parametrize("intervals", [1, 64])
def test_a_count_too_small_for_the_setup_reports_the_count_passed(intervals):
    with pytest.raises(ValueError, match=f"intervals_per_phase {intervals} leaves no room"):
        swi3score.make_fcp_move_levels(intervals)
    assert swi3score.make_fcp_move_levels(65)
