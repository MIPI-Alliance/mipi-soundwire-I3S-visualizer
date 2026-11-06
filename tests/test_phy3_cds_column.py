"""The PHY3 (DLV) CDS column is CDS_HorizontalStart[4:0], whose reset value is 2.

* A capture decode reads the CDS at Column 2 until a commit makes a CDS_HorizontalStart
  current, and at that column from the row the commit takes effect. The move opens a new
  segment, which carries its CDS column, and the symbol viewer, the TX raster and the bus
  rows follow it.
* An authored config places the CDS as the visualizer does: Sync1 at Column 0, then S1's
  tail (S1TailWidth_REG), its handover (EnforceS1Handover), then the CDS. The expected
  registers carry CDS_HorizontalStart when it is not the reset value.

Every input is synthetic: the PHY3 demo (make_demo_levels_phy3, which programs a CDS column
with its 16-column config) and the demo config with cells edited.
"""
import csv
import io

import pytest
import swi3score

from swi3s_studio.ingest import transitions
from swi3s_studio.model.bus_config import demo_config
from swi3s_studio.model.viz_engine import build_bus_model
from swi3s_studio.session import Session

pytestmark = pytest.mark.gui

_CDS_H = 0x1184          # CDS_HorizontalStart _NEXT
_SAMPLES = 100


def _session(cds_column: int) -> Session:
    d = swi3score.make_demo_levels_phy3(_SAMPLES, cds_column=cds_column)
    rate = transitions.ANALYZER_SAMPLE_RATE_HZ
    cap = transitions.build_dlv_capture_from_levels(
        d["levels"], d["row_columns"], (rate / transitions.DLV_ROW_RATE_HZ) / 16.0, rate)
    s = Session(cap)
    assert s.is_dlv
    return s


@pytest.fixture(scope="module")
def reset():
    return _session(2)


@pytest.fixture(scope="module")
def moved():
    return _session(3)


def test_the_cds_starts_at_its_reset_column_and_moves_at_the_commit(reset, moved):
    assert [(g["column_count"], g["cds_column"]) for g in reset.decoder.segments()] == \
        [(4, 2), (16, 2)]
    assert [(g["column_count"], g["cds_column"]) for g in moved.decoder.segments()] == \
        [(4, 2), (16, 3)]
    commit_row = int(moved.decoder.segments()[1]["row_base"])
    after = [c for c in moved.commands if c["bus_row"] >= commit_row]
    assert len(after) >= 2
    assert all(c["crc_valid"] for c in moved.commands)
    assert [c["command"] for c in moved.commands] == \
        [c["command"] for c in reset.commands][:3] + ["WriteA32"] + \
        [c["command"] for c in reset.commands][3:]
    assert len(moved.audio_columns()["start_sample"]) == len(reset.audio_columns()["start_sample"])


def test_the_views_follow_the_cds_column_of_their_region(moved):
    first, late = moved.commands[0], moved.commands[-1]
    assert moved.cds_column_at(int(first["start_sample"])) == 2
    assert moved.cds_column_at(int(late["start_sample"])) == 3
    assert moved.bus_row_for_sample(int(late["start_sample"])) == late["bus_row"]
    syms = moved.symbols_around(int(late["start_sample"]), 256)
    assert syms and any(int(x["row"]) == late["bus_row"] for x in syms)
    raster = moved.tx_raster(0, 4, int(late["start_sample"]))
    assert raster["cds_col"] == 3 and raster["column_count"] == 16
    grid = moved.grid_cells_at(int(late["start_sample"]))
    assert {c["col"] for c in grid if c["is_cds"]} == {3}


def _csv(tmp_path, cells: dict[str, str]) -> str:
    rows = list(csv.reader(io.StringIO(demo_config().to_csv())))
    for key, value in cells.items():
        next(r for r in rows if r and r[0] == key)[1] = value
    path = tmp_path / "phy3.csv"
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("tail,handover", [(0, True), (1, True), (2, True), (0, False),
                                           (1, False)])
def test_an_authored_phy3_config_places_the_cds_as_the_visualizer_does(tmp_path, tail,
                                                                       handover):
    path = _csv(tmp_path, {"PHY3Enabled": "True", "S1TailWidth_REG": str(tail),
                           "EnforceS1Handover": str(handover)})
    _bus, iface, _viz = build_bus_model(path)
    want = iface.CDS_HorizontalStart_REG
    assert want == 1 + tail + (1 if handover else 0)
    assert {c["col"] for c in swi3score.grid_from_csv(path, 4) if c["is_cds"]} == {want}
    values = {v for _d, a, v in swi3score.registers_from_csv(path) if a == _CDS_H}
    assert values == (set() if want == 2 else {want})       # 2 is the reset value


def test_a_fbcse_config_keeps_the_cds_at_column_0(tmp_path):
    path = _csv(tmp_path, {"S1TailWidth_REG": "2"})
    assert {c["col"] for c in swi3score.grid_from_csv(path, 4) if c["is_cds"]} == {0}
    assert not [a for _d, a, _v in swi3score.registers_from_csv(path) if a == _CDS_H]
