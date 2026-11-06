"""Export tests: command CSV (headless) and bus-grid SVG/PNG (offscreen Qt).

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_export.py
"""
import csv
import os
import pathlib
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from swi3s_studio import decode_capture
from swi3s_studio.export import write_commands_csv
from swi3s_studio.ingest import transitions
from swi3s_studio.model import RegisterMap

pytestmark = pytest.mark.gui


def test_commands_csv():
    res = decode_capture(transitions.demo_capture(8))
    rmap = RegisterMap.load()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cmds.csv")
        write_commands_csv(res.commands, path, rmap)
        with open(path) as f:
            rows = list(csv.DictReader(f))
    assert len(rows) == len(res.commands)
    # First WriteA32 resolves its register label, and the demo is error-free.
    w = next(r for r in rows if r["command"] == "WriteA32")
    assert "NumColumns" in w["register"] or "DP" in w["register"]
    assert all(r["error"] == "" for r in rows)


def test_grid_export_png_svg():
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.main_window import MainWindow

    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()
    with tempfile.TemporaryDirectory() as d:
        png = win._grid_view.export_image(os.path.join(d, "g.png"))
        svg = win._grid_view.export_image(os.path.join(d, "g.svg"))
        assert os.path.getsize(png) > 0
        assert os.path.getsize(svg) > 0
        head = pathlib.Path(svg).read_bytes()[:256]
        assert head.lstrip().startswith(b"<?xml") or b"svg" in head


def test_the_links_export_merges_every_link_in_global_time(tmp_path):
    import csv

    from swi3s_studio.export import write_links_commands_csv
    a = [{"start_sample": 10, "end_sample": 11, "command": "Ping"},
         {"start_sample": 30, "end_sample": 31, "command": "Write"}]
    b = [{"start_sample": 5, "end_sample": 6, "command": "Read"}]
    path = str(tmp_path / "all.csv")
    write_links_commands_csv([("Link 1", a, lambda s: s * 1000),
                              ("Amp", b, lambda s: s * 1000 + 15_000)], path)
    rows = list(csv.reader(pathlib.Path(path).read_text(encoding="utf-8").splitlines()))
    assert rows[0][:3] == ["link", "start_ps", "start_sample"]
    # Amp's sample 5 is at 20 ns: after Link 1's 10 ns, before its 30 ns.
    assert [(r[0], r[1], r[4]) for r in rows[1:]] == [
        ("Link 1", "10000", "Ping"), ("Amp", "20000", "Read"), ("Link 1", "30000", "Write")]


def test_a_reads_returned_bytes_reach_the_data_column(tmp_path):
    cmds = [{"command": "ReadA32", "has_read_data": True, "read_data": b"\x04"},
            {"command": "WriteA32", "data": b"\x01\x02"}]
    path = str(tmp_path / "c.csv")
    write_commands_csv(cmds, path)
    rows = list(csv.DictReader(pathlib.Path(path).read_text(encoding="utf-8").splitlines()))
    assert [r["data_hex"] for r in rows] == ["04", "01 02"]


def test_a_link_name_is_never_written_as_a_spreadsheet_formula(tmp_path):
    from swi3s_studio.export import write_links_commands_csv
    cmd = [{"start_sample": 1, "end_sample": 2, "command": "Ping"}]
    path = str(tmp_path / "l.csv")
    write_links_commands_csv([('=HYPERLINK("http://x","a")', cmd, lambda s: s),
                              ("@x", cmd, lambda s: s), ("Amp", cmd, lambda s: s)], path)
    rows = list(csv.reader(pathlib.Path(path).read_text(encoding="utf-8").splitlines()))
    names = [r[0] for r in rows[1:]]
    assert names == ["'=HYPERLINK(\"http://x\",\"a\")", "'@x", "Amp"]


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_every_formula_leading_character_is_quoted(lead):
    from swi3s_studio.export.commands_csv import _text_cell
    assert _text_cell(f"{lead}cmd") == f"'{lead}cmd"


@pytest.mark.parametrize("name", ["Amp", "a=b", "x-y", "1+1", "a@b", " lead", ""])
def test_a_name_without_a_leading_formula_character_is_left_alone(name):
    from swi3s_studio.export.commands_csv import _text_cell
    assert _text_cell(name) == name


def test_the_commands_csv_and_the_table_show_the_same_payload():
    from swi3s_studio.analysis.responses import command_payload
    from swi3s_studio.export import commands_csv
    assert commands_csv.command_payload is command_payload
    assert command_payload({"has_read_data": True, "read_data": b"\x04", "data": b"\x01"}) == b"\x04"
    assert command_payload({"has_read_data": True, "read_data": b"", "data": b"\x01"}) == b"\x01"
    assert command_payload({"data": b"\x01\x02"}) == b"\x01\x02"
    assert command_payload({}) == b""


def test_the_commands_csv_rows_carry_devices_crc_and_register_rank(tmp_path):
    def cmd(address, mask, crc_valid, start):
        return {"command": "WriteA32", "phase": "", "has_address": True, "address": address,
                "data": b"\x03", "device_mask": mask, "has_manager_packet": True,
                "crc_valid": crc_valid, "start_sample": start, "end_sample": start + 5}
    cmds = [cmd(0x1081, 1 << 11, True, 10), cmd(0x10C1, 0b101, False, 20),
            {"command": "Ping", "phase": "GetStatus", "device_mask": 0, "start_sample": 30,
             "end_sample": 31, "peripheral_response": -1}]
    path = str(tmp_path / "c.csv")
    write_commands_csv(cmds, path, RegisterMap.load())
    rows = list(csv.reader(pathlib.Path(path).read_text(encoding="utf-8").splitlines()))
    assert rows[1] == ["10", "15", "WriteA32", "", "11", "0x00001081", "SLC.NumColumns[NEXT]",
                       "03", "OK", "", ""]
    assert rows[2] == ["20", "25", "WriteA32", "", "0;2", "0x000010C1", "SLC.NumColumns[CURR]",
                       "03", "BAD", "", "CRC error"]
    assert rows[3][4] == "-" and rows[3][8] == ""


def test_a_data_port_register_label_names_its_port(tmp_path):
    """A data-port register is labelled `DP.DP<n>.<name>` with its port number, so the CSV says
    which port a write reached."""
    def cmd(address):
        return {"command": "WriteA32", "phase": "", "has_address": True, "address": address,
                "data": b"\x01", "device_mask": 1, "has_manager_packet": True,
                "crc_valid": True, "start_sample": 0, "end_sample": 1}
    path = str(tmp_path / "c.csv")
    write_commands_csv([cmd(0x2000), cmd(0x2300)], path, RegisterMap.load())
    rows = list(csv.reader(pathlib.Path(path).read_text(encoding="utf-8").splitlines()))
    assert [r[6] for r in rows[1:]] == ["DP.DP0.IntStat", "DP.DP3.IntStat"]


@pytest.mark.parametrize("links", [False, True])
def test_a_failed_commands_export_keeps_the_previous_file(tmp_path, monkeypatch, links):
    from swi3s_studio.export import write_links_commands_csv
    cmds = [{"start_sample": 1, "end_sample": 2, "command": "Ping"}]
    path = tmp_path / "commands.csv"
    path.write_text("previous", encoding="utf-8")

    def export():
        if links:
            write_links_commands_csv([("Link 1", cmds, lambda s: s)], str(path))
        else:
            write_commands_csv(cmds, str(path))

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            export()
    assert path.read_text(encoding="utf-8") == "previous"
    assert [p.name for p in tmp_path.iterdir()] == ["commands.csv"]
    export()
    assert "Ping" in path.read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir()] == ["commands.csv"]
