"""A config CSV imported into ONE region of a capture that reconfigures mid-stream.

The whole-capture import (Session.apply_config_csv) puts the decoder in its CSV mode:
one config from row 0, no snooped width changes, no re-sync. On a capture with a
Safe-Lock-2 -> 8 -> 16 column cold start that framed every region at the CSV's width,
turned every command CRC-red and lost the audio of the regions the CSV was not for (a
user's 8-column CSV on a three-region capture: 5,978 valid commands became 1,971 invalid
ones, 43M audio samples became 6M). The app warned and then did it anyway.

Session.apply_config_csv_at makes the CSV the region's register state instead, through
the section-scoped what-if path, and the window takes that route whenever the capture
has more than one region. The demo's cold start has the same three regions.
"""
import os
import tempfile

import numpy as np
import pytest

from swi3s_studio.model import Provenance
from swi3s_studio.model.bus_config import BusConfig
from swi3s_studio.session import Session

_SAMPLES = 6000


@pytest.fixture(scope="module")
def demo():
    s = Session.from_demo(_SAMPLES, phy=2, cold_start=True)
    assert [int(g["column_count"]) for g in s.segments] == [2, 8, 16], "test premise"
    return s


def _mid(s, i):
    lo = int(s.segments[i]["start_sample"])
    hi = (int(s.segments[i + 1]["start_sample"]) if i + 1 < len(s.segments)
          else int(s.capture.clock_edges[-1]))
    return (lo + hi) // 2


def _csv_of_region(s, i, path, drop_dp=None):
    """Region `i`'s own config as a Visualizer CSV, optionally with one port disabled —
    so the import visibly changes that region and nothing else."""
    cfg, _n = BusConfig.from_decoder_config(s.config_dataports_at(_mid(s, i)))
    if drop_dp is not None:
        for dp in cfg.dataports:
            if dp.enabled and int(dp.dp_number) == drop_dp:
                dp.enabled = False
    return cfg.to_csv_file(str(path))


def _ports_by_region(s):
    c = s.audio_columns()
    out = []
    for i, g in enumerate(s.segments):
        lo = int(g["start_sample"])
        hi = int(s.segments[i + 1]["start_sample"]) if i + 1 < len(s.segments) else 1 << 62
        m = (c["start_sample"] >= lo) & (c["start_sample"] < hi)
        out.append((sorted(set(zip(c["device"][m].tolist(), c["dp"][m].tolist()))),
                    int(np.count_nonzero(m))))
    return out


def _fresh():
    return Session.from_demo(_SAMPLES, phy=2, cold_start=True)


def test_the_csv_changes_its_region_and_no_other(demo, tmp_path):
    before = _ports_by_region(demo)
    assert (0, 3) in before[1][0], "test premise: region 1 carries DP3"
    s = _fresh()
    csv = _csv_of_region(s, 1, tmp_path / "r1.csv", drop_dp=3)
    assert s.apply_config_csv_at(_mid(s, 1), csv) == 1
    after = _ports_by_region(s)
    assert [int(g["column_count"]) for g in s.segments] == [2, 8, 16]
    assert after[0] == before[0] and after[2] == before[2], "another region changed"
    assert (0, 3) not in after[1][0], "the CSV did not reach region 1's decode"
    assert set(after[1][0]) == set(before[1][0]) - {(0, 3)}


def test_every_command_still_decodes(demo, tmp_path):
    """The defect the user saw first: commands are framed by the region's width, and the
    whole-capture import framed them all at the CSV's."""
    s = _fresh()
    s.apply_config_csv_at(_mid(s, 1), _csv_of_region(s, 1, tmp_path / "r1.csv", drop_dp=3))
    assert len(s.commands) == len(demo.commands)
    assert all(c.get("crc_valid") for c in s.commands) == all(
        c.get("crc_valid") for c in demo.commands)


def test_the_grid_and_register_map_show_it_as_csv(tmp_path):
    s = _fresh()
    s.apply_config_csv_at(_mid(s, 1), _csv_of_region(s, 1, tmp_path / "r1.csv", drop_dp=3))
    cells = s.grid_cells_at(_mid(s, 1), 16)
    assert not any(c.get("dp") == 3 for c in cells), "the grid still draws the dropped port"
    assert any(c.get("dp") == 3 for c in s.grid_cells_at(_mid(s, 2), 16)), "region 2 lost it"
    provs = {f.provenance(a) for f in s.register_files_at(_mid(s, 1)).values()
             for a in range(0x2100, 0x2400)}
    assert Provenance.CSV in provs and Provenance.UI not in provs


def test_a_what_if_still_wins_over_the_regions_csv(tmp_path):
    s = _fresh()
    mid = _mid(s, 1)
    s.apply_config_csv_at(mid, _csv_of_region(s, 1, tmp_path / "r1.csv"))
    files = s.register_files_at(mid)
    dev = next(d for d, f in files.items() if f.provenance(0x2109) == Provenance.CSV)
    s.set_register_override(1, dev, 0x2109, 0x05)
    after = s.register_files_at(mid)[dev]
    assert after.value(0x2109) == 0x05 and after.provenance(0x2109) == Provenance.UI


def test_it_reopens_from_the_source_and_can_be_removed(demo, tmp_path):
    s = _fresh()
    mid = _mid(s, 1)
    csv = _csv_of_region(s, 1, tmp_path / "r1.csv", drop_dp=3)
    s.apply_config_csv_at(mid, csv)
    assert s.source.get("config_csv@1") == csv and "config_csv" not in s.source
    assert s.csv_section_at(mid) == csv and s.csv_section_at(_mid(s, 2)) == ""
    again = Session.from_source(dict(s.source))
    assert _ports_by_region(again) == _ports_by_region(s)
    again.apply_config_csv_at(mid, None)
    assert "config_csv@1" not in again.source
    assert _ports_by_region(again) == _ports_by_region(demo)


def test_a_workspace_names_the_regions_csv_relative_to_itself(tmp_path):
    import json

    from swi3s_studio.workspace import LinkSpec, Workspace
    s = _fresh()
    os.makedirs(tmp_path / "cfg")
    csv = _csv_of_region(s, 1, tmp_path / "cfg" / "r1.csv")
    s.apply_config_csv_at(_mid(s, 1), csv)
    ws_path = str(tmp_path / "w.swi3s")
    Workspace(links=[LinkSpec(source=dict(s.source))]).save(ws_path)
    with open(ws_path, encoding="utf-8") as f:
        assert json.load(f)["links"][0]["source"]["config_csv@1"] == "cfg/r1.csv"
    os.remove(csv)
    ws = Workspace.load(ws_path)
    assert [k for _i, k, _p in ws.missing_files()] == ["config_csv@1"]


def test_a_first_region_with_no_config_on_the_wire_takes_the_csv():
    """Every later region is opened by a reconfigure, which is where the decoder folds a
    region's settings in. A capture that starts after its setup commit has none in its
    first region, so the CSV never reached the payload engine until the decode start
    applied section 0's settings itself (Decoder::run)."""
    full = Session.from_demo(_SAMPLES, phy=2, cold_start=True)
    t0 = int(full.segments[-1]["start_sample"]) + 20000
    late = full.capture.subcapture(t0, int(full.capture.clock_edges[-1]))
    s = Session(late)
    assert s.audio_count == 0, "test premise: nothing on the wire to configure from"
    cfg, _n = BusConfig.from_decoder_config(full.config_dataports_at(t0 + 40000))
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        cfg.to_csv_file(path)
        s.apply_config_csv_at(0, path)
    finally:
        os.remove(path)
    assert s.audio_count > 0
    assert {int(d) for d in np.unique(s.audio_columns()["dp"])} == {0, 1, 2, 3}


def test_the_window_imports_into_the_cursor_region_only(monkeypatch, tmp_path):
    """Decode ▸ Import Visualizer CSV on a multi-region capture goes through the region
    path with the cursor's sample, and does not prompt when the CSV fits the region."""
    from PySide6.QtWidgets import QApplication, QMessageBox
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_session(_fresh())
    s = win._session
    csv = _csv_of_region(s, 1, tmp_path / "r1.csv")
    win.cursor.set_sample(_mid(s, 1))
    calls, prompts = [], []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: prompts.append(a) or QMessageBox.No))
    monkeypatch.setattr(win, "_load_async", lambda fn, **k: calls.append(fn()))
    monkeypatch.setattr(s, "apply_config_csv",
                        lambda p: pytest.fail("the whole-capture import ran"))
    win._apply_config_csv_path(csv)
    assert not prompts and calls == [s]
    assert s.csv_sections == {1: csv}


def test_decode_menu_removes_the_cursor_regions_csv(monkeypatch, tmp_path):
    """Decode ▸ Remove … from This Region: enabled, and naming the file, only while the
    cursor's region has a CSV; it removes that region's and leaves another region's."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_session(_fresh())
    s = win._session
    csv1 = _csv_of_region(s, 1, tmp_path / "r1.csv")
    csv2 = _csv_of_region(s, 2, tmp_path / "r2.csv")
    s.apply_config_csv_at(_mid(s, 1), csv1)
    s.apply_config_csv_at(_mid(s, 2), csv2)
    monkeypatch.setattr(win, "_load_async", lambda fn, **k: fn())
    act = win._remove_region_csv_action
    win.cursor.set_sample(_mid(s, 0))
    win._update_remove_region_csv()
    assert not act.isEnabled()
    win.cursor.set_sample(_mid(s, 1))
    win._update_remove_region_csv()
    assert act.isEnabled() and "r1.csv" in act.text()
    act.trigger()
    assert s.csv_sections == {2: csv2}
    assert "config_csv@1" not in s.source and s.source["config_csv@2"] == csv2
