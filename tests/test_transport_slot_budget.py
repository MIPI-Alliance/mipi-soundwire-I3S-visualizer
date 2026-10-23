"""Transport-budget invariant: each interval carries channels x samples x bits slots.

A data port's transport pattern is fully determined by its register set — for every
interval it must place exactly

    enabled_channels x (SampleGrouping_REG + 1) x (SampleSize_REG + 1)

data slots, wherever ChannelGrouping happens to break them across rows. Grouping is a
*layout* control: it decides how the slots are arranged, never how many there are.

When the enabled-channel count is not a multiple of ChannelGrouping the trailing group
is PARTIAL (3 channels in groups of 2 leaves a group of 1) and transports only its real
channel count. The vendored swviz engine is the reference.
"""
import os
import pathlib
import tempfile

import pytest
import swi3score

_HERE = os.path.dirname(os.path.abspath(__file__))
_CSV = os.path.join(os.path.dirname(_HERE), "visualizer_examples", "directed_tests",
                    "intentional_errors", "partial_channel_group_sample_grouping.csv")

# The directed CSV is the template: it already carries every field the loader needs
# (including the Enabled / DisplayFields rows a hand-built file is easy to omit), so a
# sweep only has to rewrite the registers under test.
_INTERVAL = 11           # Interval_REG in the template -> 12 rows per interval
_ROWS = 24               # exactly 2 intervals


def _config(nch: int, cg: int, sg: int, ss: int, *, rows: int = _ROWS) -> str:
    """Template CSV with DP0 rewritten to (channels, grouping, sample-grouping,
    sample-size) and DP1 disabled. Returns a temp path the caller must delete."""
    lines = []
    for line in pathlib.Path(_CSV).read_text(encoding="utf-8").splitlines():
        key = line.split(",", 1)[0]
        if key == "EnableCh_REG":
            line = f"EnableCh_REG,{bin((1 << nch) - 1)},0b0," + ",".join(["0b0"] * 10)
        elif key == "ChannelGrouping_REG":
            line = f"ChannelGrouping_REG,{cg},0," + ",".join(["0"] * 10)
        elif key == "SampleGrouping_REG":
            line = f"SampleGrouping_REG,{sg},0," + ",".join(["0"] * 10)
        elif key == "SampleSize_REG":
            line = f"SampleSize_REG,{ss},0," + ",".join(["0"] * 10)
        elif key == "HorizontalCount_REG":
            line = "HorizontalCount_REG,11,0," + ",".join(["0"] * 10)   # room for the widest case
        elif key == "Enabled":
            line = "Enabled,True,False," + ",".join(["False"] * 10)
        elif key == "RowsToDraw":
            line = f"RowsToDraw,{rows}"
        lines.append(line)
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _data_cells(path: str, dp: int = 0, rows: int = _ROWS):
    return [c for c in swi3score.grid_from_csv(path, rows)
            if not c["is_cds"] and c.get("dp", -1) == dp and c["slot"] == 1]


def test_the_reported_config_places_six_slots_per_interval():
    """3 channels, ChannelGrouping 2, SampleGrouping 1 (2 samples), 1 bit each -> 6
    slots per interval."""
    cells = _data_cells(_CSV, rows=8)          # the file's own RowsToDraw = 8, one interval
    assert len(cells) == 3 * 2 * 1


def test_reported_config_matches_the_reference_model():
    """Placement + channel/bit identity must equal the vendored swviz engine. (The
    `sample` field is deliberately NOT compared: swviz
    reports an engine-reconstructed ABSOLUTE ordinal, the core reports the
    transport-scoped sample_in_group — a documented convention difference, not a
    placement one. See swviz/core/engine.py's global_sample.)"""
    from swi3s_studio.model import viz_engine

    core = sorted((c["row"], c["col"], c["channel"], c["bit"])
                  for c in _data_cells(_CSV, rows=8))
    model = viz_engine.model_json(_CSV)
    ref = sorted((rec["row"], rec["column"], s["channel"], s["bit"])
                 for rec in model["bits"].values() for s in rec["slots"]
                 if s["slot"] == "DATA" and s["dp"] == 0)
    assert core == ref


@pytest.mark.parametrize("nch", range(1, 9))
@pytest.mark.parametrize("cg", range(0, 9))
def test_slot_budget_holds_for_every_grouping(nch, cg):
    """channels x samples x bits per interval, for every channel/grouping pair.

    Includes the multiples, where grouping divides evenly. ChannelGrouping 0
    means "one group of all channels"; a grouping wider than the channel count is
    clamped — both must still balance the budget.
    """
    sg, ss = 1, 0                                    # 2 samples, 1 bit
    path = _config(nch, cg, sg, ss)
    try:
        n = len(_data_cells(path))
    finally:
        os.remove(path)
    assert n == nch * (sg + 1) * (ss + 1) * 2, (      # 24 rows = 2 intervals
        f"{nch} channels, grouping {cg}: placed {n} slots, expected "
        f"{nch * (sg + 1) * (ss + 1) * 2} over 2 intervals")


@pytest.mark.parametrize("sg", range(0, 3))
@pytest.mark.parametrize("ss", (0, 1, 3))
def test_slot_budget_holds_across_sample_shapes(sg, ss):
    """Same budget with a PARTIAL trailing group (3 channels in groups of 2) as the
    sample count and bit width vary — the multiplier must scale cleanly."""
    nch, cg = 3, 2
    path = _config(nch, cg, sg, ss)
    try:
        n = len(_data_cells(path))
    finally:
        os.remove(path)
    assert n == nch * (sg + 1) * (ss + 1) * 2


def test_channel_index_never_escapes_the_enabled_set():
    """Every emitted cell must name a channel that is actually enabled."""
    for nch in range(1, 9):
        for cg in range(0, 9):
            path = _config(nch, cg, 1, 0)
            try:
                seen = {c["channel"] for c in _data_cells(path)}
            finally:
                os.remove(path)
            assert seen <= set(range(nch)), (
                f"{nch} channels, grouping {cg}: emitted channels {sorted(seen)}")


def test_every_sample_and_channel_appears_exactly_once_per_interval():
    """With a partial trailing group each (sample, channel) pair must be transported
    exactly once per interval."""
    path = _config(3, 2, 1, 0, rows=12)              # one interval
    try:
        cells = _data_cells(path, rows=12)
    finally:
        os.remove(path)
    pairs = sorted((c["sample"], c["channel"]) for c in cells)
    assert pairs == [(s, ch) for s in range(2) for ch in range(3)]
