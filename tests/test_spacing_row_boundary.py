"""Channel-group spacing must not carry across a row boundary.

Reported on spacing_question.csv (v2.1.12, reproduced identically in the v3 C++
core): with Spacing=2, HorizontalStart=1, HorizontalCount=1, the first data UI of
row 1 landed in column 2 instead of column 1. The spacing that should have been
terminated at the end of row 0 was still counting down and consumed row 1's first
in-window UI; the error then alternates row to row as it re-accrues.

Spacing is an inter-channel-group gap WITHIN a row. It only ticks inside the
transport window, so if the window closes before the countdown finishes, the
remainder must be discarded rather than carried forward.

The 1.74 visualizer (the original golden bus model) got this right via its
`done_with_row` latch, which cleared `channel_group_is_spacing` when the column
passed HorizontalStart+HorizontalCount:

    elif column_number > self.horizontal_start_REG + self.horizontal_count_REG :
        self.done_with_row = True
        self.channel_group_is_spacing = 0        # <-- terminates AT the row end

The v2/v3 rewrite folded that latch into the in-transport-window predicate, which
reproduces the gating but silently dropped this side effect. Fixed in
CDataPort::advanceRow().

Expected placement is 1.74's, obtained by driving its DataPort class directly with
the same registers (note 1.74 encodes channels_REG as N-1).
"""
import os
import tempfile

import pytest
import swi3score

_SLOT = list(swi3score.SLOT_NAMES)


def _csv(tmpdir, **over):
    """A single-DP config exercising spacing against a narrow transport window."""
    reg = {
        "EnableCh_REG": "0b11", "SampleSize_REG": 0, "SampleGrouping_REG": 1,
        "ChannelGrouping_REG": 1, "Spacing_REG": 2, "Interval_REG": 11,
        "Offset_REG": 0, "HorizontalStart_REG": 1, "HorizontalCount_REG": 1,
        "TailWidth_REG": 0, "BitWidth_REG": 0, "SkippingNumerator_REG": 0,
    }
    reg.update(over)
    n = 12
    def row(k, v, rest="0"):
        return f"{k}," + ",".join([str(v)] + [rest] * (n - 1))
    lines = [
        "AppVersion,3.0.10", "NumColumns_REG,15", "SkippingDenominator_REG,1",
        "PHY3Enabled,False", "S0Width,1", "S1TailWidth_REG,0",
        "EnforceS1Handover,True", "CDS_BitWidth_REG,0", "CDS_GuardEnabled_REG,False",
        "CDS_GuardPolarity_REG,False", "CDS_TailWidth_REG,0",
        "EnforceCDSHandover,False", "RowRate,3072.0", "Description,", "RowsToDraw,64",
        row("DeviceNumber_REG", 0),
        row("ManagerDataport", "False", "False"),
    ]
    for k, v in reg.items():
        lines.append(row(k, v))
    lines += [
        row("PortDirection_REG", "False", "False"),
        row("GuardEnable_REG", "False", "False"),
        row("GuardPolarity_REG", "False", "False"),
        row("SubRowInterval_REG", "False", "False"),
        row("FlowMode_REG", 0), row("PortMode_REG", 0),
        row("ScramblerEn_REG", "False", "False"),
        row("FCP_HorizontalStart_REG", 0), row("FCP_BitWidth_REG", 0),
        row("FCP_TailWidth_REG", 0), row("FCP_Offset_REG", 0),
        row("FCP_GuardEnable_REG", "False", "False"),
        row("FCP_GuardPolarity_REG", "False", "False"),
        "Name," + ",".join(f"DP{i}" for i in range(n)),
        row("EnforceHandover", "True", "True"),
        row("Enabled", "True", "False"),
        row("DisplayFields", "sc", "sc"),
    ]
    p = os.path.join(tmpdir, "cfg.csv")
    with open(p, "w") as f:
        f.write("\n".join(lines) + "\n")
    return p


def _data_cols(path, rows):
    """{row: [columns carrying DATA]} for the single enabled DP, via the C++ core."""
    out = {}
    for c in swi3score.grid_from_csv(path, rows):
        if c["is_cds"] or c["dp"] < 0:
            continue
        if _SLOT[c["slot"]] == "Data":
            out.setdefault(c["row"], []).append(c["col"])
    return {r: sorted(v) for r, v in out.items()}


def _data_cols_swviz(path, rows):
    """Same, via the swviz authoring engine — Studio's OTHER placement engine.

    Studio runs two: the C++ decode core (a capture on the wire) and swviz (a config
    CSV in the Visualizer tab). They implement the same §14.2.5 algorithm, so a
    placement bug must be fixed in BOTH — the first pass at this fix landed only in
    the C++ core, and the Visualizer tab still mis-placed the reported CSV.
    """
    from swi3s_studio.model import viz_engine
    model = viz_engine.model_json(path, num_rows=rows)
    bits = model["bits"]
    cells = bits.values() if isinstance(bits, dict) else bits
    out = {}
    for cell in cells:
        for s in cell["slots"]:
            if s["slot"] == "DATA" and s.get("dp") is not None and cell["column"] != 0:
                out.setdefault(cell["row"], []).append(cell["column"])
    return {r: sorted(v) for r, v in out.items()}


_ENGINES = [("cpp", _data_cols), ("swviz", _data_cols_swviz)]


@pytest.mark.parametrize("engine,place", _ENGINES)
def test_spacing_does_not_leak_into_the_next_row(engine, place):
    """The reported case: every row's first data UI must sit at HorizontalStart.

    Without the fix row 1 starts at column 2 (spacing from row 0 ate column 1) and
    row 2 swings back to column 1 — the alternating error the user observed.
    1.74 golden: {0: [1, 2], 1: [1, 2]}.

    Run against BOTH placement engines: the first fix landed only in the C++ core, so
    this same CSV still rendered wrong in the Visualizer tab (swviz).
    """
    with tempfile.TemporaryDirectory() as td:
        got = place(_csv(td), 4)
    assert got == {0: [1, 2], 1: [1, 2]}, (
        f"spacing leaked across the row boundary: {got}")


@pytest.mark.parametrize("engine,place", _ENGINES)
@pytest.mark.parametrize("spacing,hcount,expect", [
    # (Spacing, HorizontalCount) -> 1.74's placement, HorizontalStart=1, 2 channels.
    (2, 1, {0: [1, 2], 1: [1, 2]}),      # the reported config
    (3, 1, {0: [1, 2], 1: [1, 2]}),      # wider gap, same narrow window
    (2, 0, {0: [1], 1: [1], 2: [1], 3: [1]}),   # single-column window
    (1, 1, {0: [1, 2], 1: [1, 2]}),      # Spacing=1 -> no gap at all
])
def test_spacing_matches_1_74_golden(spacing, hcount, expect, engine, place):
    """Cross-checked against the 1.74 visualizer's DataPort driven with the same
    registers. These four are a spot-check of a 768-configuration sweep in which
    the unfixed core mis-placed 180 and the fixed core matched 1.74 exactly."""
    with tempfile.TemporaryDirectory() as td:
        path = _csv(td, Spacing_REG=spacing, HorizontalCount_REG=hcount)
        got = place(path, 4)
    assert got == expect, f"Spacing={spacing} HCount={hcount}: {got} != {expect}"


@pytest.mark.parametrize("engine,place", _ENGINES)
def test_first_data_ui_is_always_at_horizontal_start(engine, place):
    """Invariant behind the bug: whatever the spacing, no row may begin its data
    later than HorizontalStart. Any row that does has inherited a stale countdown."""
    for spacing in (0, 1, 2, 3, 4):
        for hstart in (1, 2, 3):
            with tempfile.TemporaryDirectory() as td:
                path = _csv(td, Spacing_REG=spacing, HorizontalStart_REG=hstart)
                got = place(path, 6)
            for r, cols in got.items():
                assert cols[0] == hstart, (
                    f"Spacing={spacing} HStart={hstart}: row {r} starts at "
                    f"col {cols[0]}, expected {hstart} — stale spacing carried over")
