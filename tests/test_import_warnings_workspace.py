"""Import warnings from a workspace: a key this version does not read, a value of the wrong
type, a row that does not read and a saved setting that no longer applies are left out and
reported, and a reopened workspace lists them in one Import warnings window titled with the
workspace file."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from conftest import pump_loads
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from swi3s_studio import import_notes
from swi3s_studio.workspace import LinkSpec, Workspace

pytestmark = pytest.mark.gui


def _messages(notes):
    return [n.message for n in notes]


def test_a_saved_workspace_reads_back_without_warnings():
    ws = Workspace(links=[LinkSpec(source={"type": "demo"}, overlay=[[0, 1, 0x2090, 2]],
                                   device_names={"1": "Amp"}, device_hub_depths={"2": 1},
                                   device_scramblers=[[1, 2, True]], even_timing=[[1, 2]])],
                   bookmarks=[{"sample": 5, "group": "A", "index": 1, "label": "", "link": 0}],
                   view={"grid_rows": 32})
    with import_notes.collect() as notes:
        back = Workspace.from_json(ws.to_json(), "ok.swi3s")
    assert notes == []
    assert back.links[0].overlay == [[0, 1, 0x2090, 2]]


def test_a_v3_workspace_reads_without_warnings():
    v3 = {"version": 3, "source": {"type": "demo"}, "overlay": [[0, 0, 0x2090, 2]],
          "bookmarks": [10, 20], "cursor": 4, "mode": "Analysis", "device_names": {},
          "device_hub_depths": {}, "device_regmaps": {}, "device_scramblers": [], "view": {}}
    with import_notes.collect() as notes:
        Workspace.from_json(json.dumps(v3), "old.json")
    assert notes == []


@pytest.mark.parametrize("cursor", ["abc", None, [1], True, "12", 1.5, -3, 10 ** 30,
                                    float("inf"), float("nan")])
def test_a_cursor_that_is_not_a_sample_opens_at_0_with_one_note(cursor):
    d = {"active_link": 0, "cursor": cursor, "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "cur.swi3s")
    assert ws.cursor == 0
    assert _messages(notes) == ["the saved cursor is not a sample; 0 is used"]


def test_a_cursor_of_1e999_opens_at_0_with_one_note():
    text = '{"active_link": 0, "cursor": 1e999, "links": [{"source": {"type": "demo"}}]}'
    with import_notes.collect() as notes:
        ws = Workspace.from_json(text, "cur.swi3s")
    assert ws.cursor == 0
    assert _messages(notes) == ["the saved cursor is not a sample; 0 is used"]


@pytest.mark.parametrize("value", [None, "x", float("inf")])
def test_an_active_link_that_is_not_a_number_is_the_first_with_one_note(value):
    d = {"active_link": value, "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "act.swi3s")
    assert ws.active_link == 0
    assert _messages(notes) == ["the saved active Link is not a number; the first is used"]


@pytest.mark.parametrize("offset", [None, "x", 1.5, float("inf")])
def test_an_offset_that_is_not_a_whole_number_is_0_with_one_note(offset):
    d = {"links": [{"source": {"type": "demo"}, "name": "Bus", "offset_ps": offset}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "off.swi3s")
    assert ws.links[0].offset_ps == 0
    assert _messages(notes) == ["Link 1 (Bus): its saved time offset is not a whole number; "
                                "0 is used"]


@pytest.mark.parametrize("row", [[0, 1, -1, 2], [0, 1, 2 ** 32, 2], [2 ** 40, 1, 0x2090, 2],
                                 [-1, 1, 0x2090, 2], [0, 12, 0x2090, 2],
                                 [0, -1, 0x2090, 2], [0, 1, 0x2090, float("inf")]])
def test_a_register_override_the_decoder_cannot_take_is_dropped_and_reported(row):
    d = {"links": [{"source": {"type": "demo"}, "overlay": [row, [0, 1, 0x2090, 2]]}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "ovr.swi3s")
    assert ws.links[0].overlay == [[0, 1, 0x2090, 2]]
    assert len(notes) == 1 and notes[0].message.startswith("Link 1: 1 register override "
                                                           "not read")


def _bookmark(sample=7, **kw):
    return {"sample": sample, "group": "A", "index": 1, "label": "", "link": 0, **kw}


def test_each_bookmark_entry_is_read_by_its_own_form():
    mixed = [5, _bookmark(7), 9.5, float("nan"), "x", None, True]
    d = {"bookmarks": mixed, "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "bm.swi3s")
    assert ws.bookmarks == [5, _bookmark(7)]
    assert _messages(notes) == ["5 cursors with no readable sample, index or Link were not read"]
    from swi3s_studio.model.bookmarks import BookmarkSet
    assert BookmarkSet.from_json(ws.bookmarks).samples() == [5, 7]


@pytest.mark.parametrize("bad", [
    _bookmark(10 ** 30), _bookmark(-5), _bookmark(2 ** 63), _bookmark(3.5), _bookmark("7"),
    _bookmark(float("inf")), _bookmark(True), _bookmark(index=10 ** 30), _bookmark(index=1.5),
    _bookmark(index=-1), _bookmark(index="1")])
def test_a_bookmark_with_an_unreadable_sample_or_index_is_dropped_and_reported(bad):
    d = {"bookmarks": [bad, _bookmark(8)], "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "bm.swi3s")
    assert ws.bookmarks == [_bookmark(8)]
    assert _messages(notes) == ["1 cursor with no readable sample, index or Link was not read"]


@pytest.mark.parametrize("link", [1, 10 ** 30, -3, 2.5, "0", float("inf")])
def test_a_bookmark_on_a_link_the_workspace_does_not_have_is_dropped_and_reported(link):
    d = {"bookmarks": [_bookmark(link=link), _bookmark(9)],
         "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "bm.swi3s")
    assert ws.bookmarks == [_bookmark(9)]
    assert _messages(notes) == ["1 cursor on a Link the workspace does not have was not read"]


def test_the_sample_and_index_limits_are_inclusive():
    top = [_bookmark(2 ** 63 - 1), _bookmark(0, index=2 ** 31 - 1), _bookmark(1, index=0)]
    d = {"bookmarks": top, "cursor": 2 ** 63 - 1, "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "lim.swi3s")
    assert notes == [] and ws.bookmarks == top and ws.cursor == 2 ** 63 - 1
    d = {"cursor": 2 ** 63, "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        assert Workspace.from_json(json.dumps(d), "lim.swi3s").cursor == 0
    assert _messages(notes) == ["the saved cursor is not a sample; 0 is used"]


def test_the_bookmark_device_and_scrambler_limits_are_inclusive():
    """A bookmark index 2**31 - 1, device 11 and a three-entry scrambler row are kept; one
    past each is dropped and reported."""
    d = {"bookmarks": [_bookmark(1, index=2 ** 31 - 1), _bookmark(2, index=2 ** 31)],
         "links": [{"source": {"type": "demo"}, "name": "Bus",
                    "device_names": {"11": "Last", "12": "Past"},
                    "device_hub_depths": {"11": 5, "12": 1},
                    "device_scramblers": [[11, 0, True], [12, 0, True], [1, 0],
                                          [1, 0, True, "extra"]]}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "lim.swi3s")
    link = ws.links[0]
    assert ws.bookmarks == [_bookmark(1, index=2 ** 31 - 1)]
    assert link.device_names == {"11": "Last"} and link.device_hub_depths == {"11": 5}
    assert link.device_scramblers == [[11, 0, True]]
    assert _messages(notes) == [
        "Link 1 (Bus): 3 scrambler overrides not read (not [device -1..11, data port, true/false])",
        "Link 1 (Bus): 1 device name not read: the device is not 0..11 or the value is not "
        "text",
        "Link 1 (Bus): 1 hub depth not read: the device is not 0..11 or the value is not 0..5",
        "1 cursor with no readable sample, index or Link was not read"]


@pytest.mark.parametrize("key, row", [
    ("overlay", [0, 1, 0x2090, "2"]), ("overlay", [0, "1", 0x2090, 2]),
    ("overlay", [0, 1, 0x2090, 2.9]), ("overlay", [1.5, 1, 0x2090, 2]),
    ("overlay", [0, 1, 0x2090, -1]), ("overlay", [0, 1, 0x2090, 256]),
    ("overlay", [0, 1, 0x2090, 2 ** 70]), ("overlay", [0, True, 0x2090, 2]),
    ("device_scramblers", ["1", 2, True]), ("device_scramblers", [1, 2.5, True]),
    ("even_timing", ["1", 2]), ("even_timing", [1, 2.5]), ("even_timing", [12, 0]),
    ("even_timing", [1, -1])])
def test_a_row_entry_that_is_not_a_whole_json_number_in_range_is_dropped(key, row):
    """Text, a fraction, a bool and a value outside its range are not row entries, as for
    a bookmark's sample; a register value is a byte, 0..255."""
    d = {"links": [{"source": {"type": "demo"}, key: [row]}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "rows.swi3s")
    assert getattr(ws.links[0], key) == []
    assert len(notes) == 1 and "not read" in notes[0].message


def test_the_rows_kept_are_integers():
    """Whole-valued fractions read as integers and the scrambler flag stays a bool."""
    d = {"links": [{"source": {"type": "demo"},
                    "overlay": [[0, 1, 0x2090, 255], [0.0, 1, 0x2090, 2.0]],
                    "device_scramblers": [[1.0, 2, True], [3, 4, False]],
                    "even_timing": [[1.0, 2]], "device_hub_depths": {"1": 2.0}}]}
    with import_notes.collect() as notes:
        link = Workspace.from_json(json.dumps(d), "rows.swi3s").links[0]
    assert notes == []
    assert link.overlay == [[0, 1, 0x2090, 255], [0, 1, 0x2090, 2]]
    assert all(type(x) is int for row in link.overlay for x in row)
    assert link.device_scramblers == [[1, 2, True], [3, 4, False]]
    assert [[type(x) for x in row] for row in link.device_scramblers] == [[int, int, bool]] * 2
    assert link.even_timing == [[1, 2]]
    assert all(type(x) is int for x in link.even_timing[0])
    assert link.device_hub_depths == {"1": 2} and type(link.device_hub_depths["1"]) is int


@pytest.mark.parametrize("key", ["1_0", "+3", " 3", "3 ", "01", "٣", "", "-1", "1.0"])
def test_a_device_key_that_is_not_plain_decimal_text_is_dropped(key):
    d = {"links": [{"source": {"type": "demo"}, "name": "Bus",
                    "device_names": {key: "x", "2": "Amp"}}]}
    with import_notes.collect() as notes:
        link = Workspace.from_json(json.dumps(d), "keys.swi3s").links[0]
    assert link.device_names == {"2": "Amp"}
    assert _messages(notes) == ["Link 1 (Bus): 1 device name not read: the device is not "
                                "0..11 or the value is not text"]


def test_a_device_named_twice_keeps_the_last_with_a_note(tmp_path):
    """A file that names a device twice in one map, as JSON text does."""
    text = ('{"links": [{"source": {"type": "demo"}, "name": "Bus", '
            '"device_hub_depths": {"3": 1, "3": 2}}]}')
    with import_notes.collect() as notes:
        link = Workspace.from_json(text, "dup.swi3s").links[0]
    assert link.device_hub_depths == {"3": 2}
    assert _messages(notes) == ["Link 1 (Bus): 1 hub depth for a device named twice not "
                                "read; the last is used"]
    path = tmp_path / "dup.swi3s"
    path.write_text(text, encoding="utf-8")
    with import_notes.collect() as notes:
        assert Workspace.load(str(path)).links[0].device_hub_depths == {"3": 2}
    assert _messages(notes) == ["Link 1 (Bus): 1 hub depth for a device named twice not "
                                "read; the last is used"]


def test_a_device_named_as_a_number_and_as_text_keeps_the_last_with_a_note():
    d = {"source": {"type": "demo"}, "name": "Bus", "device_hub_depths": {"3": 1, 3: 2}}
    with import_notes.collect() as notes:
        link = LinkSpec.from_dict(d, "dup.swi3s")
    assert link.device_hub_depths == {"3": 2}
    assert _messages(notes) == ["Link 1 (Bus): 1 hub depth for a device named twice not "
                                "read; the last is used"]


def test_any_other_setting_named_twice_keeps_the_last_with_one_note():
    text = '{"links": [{"source": {"type": "demo"}}], "cursor": 5, "cursor": 9}'
    with import_notes.collect() as notes:
        ws = Workspace.from_json(text, "dup.swi3s")
    assert ws.cursor == 9
    assert _messages(notes) == ["1 saved setting named twice in one place; the last of each "
                                "is used: cursor"]


def test_links_past_the_workspace_limit_are_not_opened_and_noted(monkeypatch):
    from swi3s_studio import workspace
    monkeypatch.setattr(workspace, "MAX_WORKSPACE_LINKS", 3)
    at = {"links": [{"source": {"type": "demo"}}] * 3}
    with import_notes.collect() as notes:
        assert len(Workspace.from_json(json.dumps(at), "lim.swi3s").links) == 3
    assert notes == []
    over = {"links": [{"source": {"type": "demo"}}] * 5, "active_link": 4,
            "bookmarks": [{"sample": 1, "link": 4}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(over), "lim.swi3s")
    assert len(ws.links) == 3 and ws.active_link == 0 and ws.bookmarks == []
    assert _messages(notes) == [
        "2 Links past the first 3 not opened",
        "the saved active Link is one not opened; the first is used",
        "1 cursor on a Link the workspace does not have was not read"]


def test_the_workspace_link_limit_is_beyond_any_capture():
    from swi3s_studio import workspace
    assert workspace.MAX_WORKSPACE_LINKS == 64


def test_a_nameless_link_is_named_by_its_position():
    d = {"links": [{"source": {"type": "demo"}, "name": "A"},
                   {"source": {"type": "demo"}, "overlay": [[0, 1]]}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "names.swi3s")
    assert [link.name for link in ws.links] == ["A", "Link 2"]
    assert _messages(notes) == ["Link 2: 1 register override not read (not [region, device "
                                "0..11, 32-bit address, value 0..255] whole numbers)"]


@pytest.mark.parametrize("offset", [2 ** 63, -(2 ** 63), 10 ** 400])
def test_an_offset_outside_64_bits_is_0_with_one_note(offset):
    text = json.dumps({"links": [{"source": {"type": "demo"}, "name": "Bus"}]}).replace(
        '"name": "Bus"', f'"name": "Bus", "offset_ps": {offset}')
    with import_notes.collect() as notes:
        ws = Workspace.from_json(text, "off.swi3s")
    assert ws.links[0].offset_ps == 0
    assert _messages(notes) == [f"Link 1 (Bus): its saved time offset is outside "
                                f"±{2 ** 63 - 1} ps; 0 is used"]


def test_an_offset_at_the_64_bit_limit_is_kept():
    for offset in (2 ** 63 - 1, -(2 ** 63 - 1)):
        d = {"links": [{"source": {"type": "demo"}, "offset_ps": offset}]}
        with import_notes.collect() as notes:
            assert Workspace.from_json(json.dumps(d), "off.swi3s").links[0].offset_ps == offset
        assert notes == []


@pytest.mark.parametrize("version", [None, "4", [4], 4.5, True])
def test_a_format_version_that_is_not_a_whole_number_is_reported(version):
    d = {"version": version, "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        Workspace.from_json(json.dumps(d), "ver.swi3s")
    assert _messages(notes) == ["the saved format version is not a whole number; the file "
                                "is read as the current format"]


def test_a_cursor_on_a_link_that_did_not_open_is_reported_in_a_sentence():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    for n, verb in ((1, "was"), (2, "were")):
        ws = Workspace.single(source=dict(win._session.source),
                              bookmarks=[_bookmark(k + 1, link=1) for k in range(n)])
        ws.links.append(LinkSpec(source=dict(win._session.source), name="Link 2"))
        with import_notes.collect() as notes:
            win.apply_workspace(ws, loaded=[0], source="ws.swi3s")
        assert f"{n} saved cursor{'s' if n > 1 else ''} on a Link that was not opened " \
               f"{verb} not restored" in _messages(notes)


@pytest.mark.parametrize("row", [
    [0, 0, 0xFFFFFFFF, 1], [0, 11, 0, 1], [2 ** 31 - 1, 0, 0, 1]])
def test_a_register_override_at_the_limit_is_kept(row):
    d = {"links": [{"source": {"type": "demo"}, "overlay": [row]}]}
    with import_notes.collect() as notes:
        assert Workspace.from_json(json.dumps(d), "o.swi3s").links[0].overlay == [row]
    assert notes == []


@pytest.mark.parametrize("row", [[0, 0, 2 ** 32, 1], [0, 12, 0, 1], [2 ** 31, 0, 0, 1]])
def test_a_register_override_one_past_the_limit_is_dropped(row):
    d = {"links": [{"source": {"type": "demo"}, "overlay": [row]}]}
    with import_notes.collect() as notes:
        assert Workspace.from_json(json.dumps(d), "o.swi3s").links[0].overlay == []
    assert len(notes) == 1


@pytest.mark.parametrize("row", [[2 ** 40, 0, True], [12, 0, True], [-2, 0, True],
                                 [1, 2 ** 31, True], [1, -1, True], [1, 0, "on"],
                                 [1, float("inf"), True], [1, 0, 1], [1, 0, 0], [1, 0, 7]])
def test_a_scrambler_override_the_decoder_cannot_take_is_dropped_and_reported(row):
    good = [1, 2, True]
    d = {"links": [{"source": {"type": "demo"}, "device_scramblers": [row, good]}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "scr.swi3s")
    assert ws.links[0].device_scramblers == [good]
    assert _messages(notes) == ["Link 1: 1 scrambler override not read (not "
                                "[device -1..11, data port, true/false])"]


def test_a_scrambler_override_at_the_limits_is_kept():
    rows = [[-1, 0, False], [0, 0, False], [11, 2 ** 31 - 1, True]]
    d = {"links": [{"source": {"type": "demo"}, "device_scramblers": rows}]}
    with import_notes.collect() as notes:
        assert Workspace.from_json(json.dumps(d), "scr.swi3s").links[0].device_scramblers == rows
    assert notes == []


def _register_map(**field):
    return {"registers": [{"name": "R", "address": field.pop("address", "0x10"),
                           "fields": [{"name": "F", "bits": field.pop("bits", "[7:0]")}]}]}


def test_a_register_map_field_may_reach_bit_7_and_no_further():
    from swi3s_studio.model.regmap_import import PeripheralRegisterMap
    with import_notes.collect() as notes:
        ok = PeripheralRegisterMap.from_json_dict(_register_map(bits="[7:0]"), "m.json")
    assert notes == [] and len(ok.registers[0].fields) == 1
    with import_notes.collect() as notes:
        bad = PeripheralRegisterMap.from_json_dict(_register_map(bits="[8:0]"), "m.json")
    assert bad.registers[0].fields == [] and len(notes) == 1


def test_a_register_map_address_may_be_the_last_32_bit_one_and_no_further():
    from swi3s_studio.model.regmap_import import PeripheralRegisterMap
    with import_notes.collect() as notes:
        top = PeripheralRegisterMap.from_json_dict(_register_map(address="0xFFFFFFFF"), "m.json")
    assert notes == [] and [r.abs_address for r in top.registers] == [0xFFFFFFFF]
    with import_notes.collect() as notes:
        past = PeripheralRegisterMap.from_json_dict(_register_map(address="0x100000000"),
                                                    "m.json")
    assert past.registers == [] and len(notes) == 1


def test_a_failed_save_leaves_the_previous_workspace(tmp_path, monkeypatch):
    path = tmp_path / "w.swi3s"
    Workspace.single(source={"type": "demo"}, name="Good").save(str(path))
    before = path.read_text(encoding="utf-8")

    def boom(self):
        raise OSError("disk full")
    monkeypatch.setattr(Workspace, "to_json", boom)
    with pytest.raises(OSError):
        Workspace.single(source={"type": "demo"}, name="Bad").save(str(path))
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["w.swi3s"]


@pytest.mark.parametrize("fails", ["chmod", "replace"])
def test_a_save_that_fails_after_its_temporary_file_exists_removes_it(tmp_path, monkeypatch,
                                                                      fails):
    """The temporary file is written; setting its mode or moving it over the workspace then
    fails."""
    path = tmp_path / "w.swi3s"
    Workspace.single(source={"type": "demo"}, name="Good").save(str(path))
    before = path.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(os, fails, boom)
    with pytest.raises(OSError, match="disk full"):
        Workspace.single(source={"type": "demo"}, name="Bad").save(str(path))
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["w.swi3s"]


def test_a_save_syncs_the_new_file_before_it_replaces_the_old(tmp_path, monkeypatch):
    path = tmp_path / "w.swi3s"
    Workspace.single(source={"type": "demo"}, name="Old").save(str(path))
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"),
                                                     real_replace(a, b))[1])
    Workspace.single(source={"type": "demo"}, name="New").save(str(path))
    assert order == ["fsync", "replace"]
    assert Workspace.load(str(path)).links[0].name == "New"


def test_a_file_system_that_refuses_to_sync_still_saves(tmp_path, monkeypatch):
    path = tmp_path / "w.swi3s"

    def refuse(_fd):
        raise OSError("not supported")
    monkeypatch.setattr(os, "fsync", refuse)
    Workspace.single(source={"type": "demo"}, name="New").save(str(path))
    assert Workspace.load(str(path)).links[0].name == "New"


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permission bits; root writes any folder")
def test_a_new_workspace_in_a_read_only_folder_is_refused_and_leaves_nothing(tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir()
    os.chmod(folder, 0o555)
    try:
        with pytest.raises(PermissionError):
            Workspace.single(source={"type": "demo"}).save(str(folder / "w.swi3s"))
    finally:
        os.chmod(folder, 0o755)
    assert list(folder.iterdir()) == []


def test_a_workspace_with_a_byte_order_mark_opens(tmp_path):
    path = tmp_path / "bom.swi3s"
    path.write_bytes(b"\xef\xbb\xbf" + Workspace.single(
        source={"type": "demo"}, name="Marked").to_json().encode("utf-8"))
    assert Workspace.load(str(path)).links[0].name == "Marked"


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need a privilege on Windows")
def test_a_save_through_a_symbolic_link_writes_the_linked_file(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    target = shared / "w.swi3s"
    Workspace.single(source={"type": "demo"}, name="Old").save(str(target))
    os.chmod(target, 0o640)
    link = tmp_path / "current.swi3s"
    link.symlink_to(target)
    (tmp_path / "current.swi3s.tmp").write_text("keep", encoding="utf-8")
    Workspace.single(source={"type": "demo"}, name="New").save(str(link))
    assert link.is_symlink()
    assert Workspace.load(str(target)).links[0].name == "New"
    assert (os.stat(target).st_mode & 0o777) == 0o640
    assert sorted(p.name for p in shared.iterdir()) == ["w.swi3s"]
    assert (tmp_path / "current.swi3s.tmp").read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permission bits; root writes any file")
def test_a_save_refuses_a_read_only_workspace_and_leaves_it(tmp_path):
    path = tmp_path / "w.swi3s"
    Workspace.single(source={"type": "demo"}, name="Kept").save(str(path))
    os.chmod(path, 0o444)
    try:
        with pytest.raises(PermissionError):
            Workspace.single(source={"type": "demo"}, name="Over").save(str(path))
        assert Workspace.load(str(path)).links[0].name == "Kept"
        assert [p.name for p in tmp_path.iterdir()] == ["w.swi3s"]
    finally:
        os.chmod(path, 0o644)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_new_workspace_gets_the_mode_the_umask_gives_and_the_umask_is_untouched(
        tmp_path, monkeypatch):
    calls = []
    real = os.umask
    old = real(0o027)
    try:
        monkeypatch.setattr(os, "umask", lambda m: calls.append(m) or real(m))
        Workspace.single(source={"type": "demo"}).save(str(tmp_path / "new.swi3s"))
    finally:
        real(old)
    assert calls == []
    assert (os.stat(tmp_path / "new.swi3s").st_mode & 0o777) == 0o640


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permission bits; root writes any folder")
def test_a_writable_workspace_in_a_read_only_folder_is_written_in_place(tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir()
    path = folder / "w.swi3s"
    Workspace.single(source={"type": "demo"}, name="Old").save(str(path))
    os.chmod(folder, 0o555)
    try:
        Workspace.single(source={"type": "demo"}, name="New").save(str(path))
    finally:
        os.chmod(folder, 0o755)
    assert Workspace.load(str(path)).links[0].name == "New"
    assert [p.name for p in folder.iterdir()] == ["w.swi3s"]


@pytest.mark.parametrize("saved", [None, "", "  ", 7])
def test_a_link_name_that_is_not_text_or_is_blank_reads_as_the_default_with_a_note(saved):
    d = {"links": [{"source": {"type": "demo"}},
                   {"source": {"type": "demo"}, "name": saved}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "name.swi3s")
    assert [link.name for link in ws.links] == ["Link 1", "Link 2"]
    assert _messages(notes) == ["Link 2: its saved name is not text or is blank; Link 2 is "
                                "used"]


def test_an_integer_too_long_to_read_raises_value_error_naming_the_file(tmp_path):
    path = tmp_path / "long.swi3s"
    path.write_text('{"links": [{"source": {"type": "demo"}, "offset_ps": ' + "9" * 5000
                    + "}]}", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        Workspace.load(str(path))
    assert str(path) in str(e.value) and "not a readable workspace" in str(e.value)


def test_a_mode_that_is_not_text_opens_in_analysis_with_one_note():
    d = {"active_link": 0, "mode": [1], "links": [{"source": {"type": "demo"}}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "mode.swi3s")
    assert ws.mode == "Analysis"
    assert _messages(notes) == ["the saved mode is not text; Analysis is used"]


def test_unread_keys_are_listed_three_at_a_time_and_cut_to_the_quote_limit():
    from swi3s_studio.ingest.timebase import QUOTED_CHARS
    extra = {f"key{i}": 1 for i in range(5000)}
    extra["z" * 500] = 1
    d = {"active_link": 0, **extra, "links": [{"source": {"type": "demo"}, **extra}]}
    with import_notes.collect() as notes:
        Workspace.from_json(json.dumps(d), "many.swi3s")
    texts = _messages(notes)
    assert len(texts) == 2 and all(len(t) < 400 for t in texts)
    tail = ("5001 saved settings this version does not read, ignored: "
            "key0, key1, key10 and 4998 more")
    assert texts == ["Link 1: " + tail, tail]
    long = {"z" * 500: 1}
    with import_notes.collect() as notes:
        Workspace.from_json(json.dumps({"links": [{"source": {"type": "demo"}}], **long}), "x")
    assert _messages(notes)[0].endswith("ignored: " + "z" * (QUOTED_CHARS - 1) + "\u2026")


def test_what_a_workspace_cannot_read_is_dropped_and_reported():
    d = {"version": 9, "future_key": 1, "active_link": 0, "cursor": 0, "mode": "Analysis",
         "view": [], "timing": "fast", "bookmarks": [{"sample": "x"}, {"sample": 3}],
         "links": [{"source": {"type": "demo"}, "name": "Bus", "colour": "red",
                    "overlay": [[0, 0x2090, 2], [0, 1, 0x2090, 2]],
                    "device_scramblers": [[1, 2]], "even_timing": "all",
                    "device_names": {"1": "Amp", "40": "Nowhere", "2": 7},
                    "device_hub_depths": {"1": 9, "3": 2},
                    "device_regmaps": {"x": {}}, "view": 5}]}
    with import_notes.collect() as notes:
        ws = Workspace.from_json(json.dumps(d), "bad.swi3s")
    link = ws.links[0]
    assert link.overlay == [[0, 1, 0x2090, 2]]
    assert link.device_scramblers == [] and link.even_timing == []
    assert link.device_names == {"1": "Amp"} and link.device_hub_depths == {"3": 2}
    assert link.device_regmaps == {} and link.view == {}
    assert ws.view == {} and ws.timing is None and len(ws.bookmarks) == 1
    assert {n.source for n in notes} == {"bad.swi3s"}
    assert _messages(notes) == [
        "Link 1 (Bus): 1 saved setting this version does not read, ignored: colour",
        "Link 1 (Bus): 'even_timing' is not a list; not read",
        "Link 1 (Bus): 1 register override not read (not [region, device 0..11, 32-bit "
        "address, value 0..255] whole numbers)",
        "Link 1 (Bus): 1 scrambler override not read (not [device -1..11, data port, true/false])",
        "Link 1 (Bus): 2 device names not read: the device is not 0..11 or the value is "
        "not text",
        "Link 1 (Bus): 1 hub depth not read: the device is not 0..11 or the value is not "
        "0..5",
        "Link 1 (Bus): 1 register map not read: the device is not 0..11 or the value is not "
        "a map",
        "Link 1 (Bus): its saved view is not a mapping; not read",
        "1 saved setting this version does not read, ignored: future_key",
        "saved in workspace format 9, newer than the 4 this version reads: what that format "
        "adds is not restored",
        "the saved window view is not a mapping; not read",
        "the saved 'timing' settings are not a mapping; not read",
        "1 cursor with no readable sample, index or Link was not read",
    ]


def _reopen(monkeypatch, d, tmp_path, name="odd.swi3s"):
    from swi3s_studio.ui.main_window import MainWindow
    path = tmp_path / name
    path.write_text(json.dumps(d), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    win = MainWindow()
    win.open_workspace()
    pump_loads(win)
    return win


def _demo_source():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.load_demo()
    return dict(w._session.source)


def test_reopening_a_workspace_with_invalid_values_opens_one_warnings_window(
        tmp_path, monkeypatch):
    src = _demo_source()
    d = {"version": 4, "active_link": 0, "cursor": 0, "mode": "Sketching",
         "view": {"grid_rows": "lots", "commands_header": {"hidden": [], "sort": ["Nope", 0]},
                  "registers": {"device": 99}},
         "links": [{"source": src, "name": "Link 1", "overlay": [[0, 1]],
                    "stream_colors": [[0, 1, "dark", "not-a-colour"]]}]}
    win = _reopen(monkeypatch, d, tmp_path)
    assert len(win.links) == 1
    dlgs = win.import_warning_dialogs()
    assert len(dlgs) == 1 and not dlgs[0].isModal()
    assert dlgs[0].windowTitle() == "Import warnings — odd.swi3s"
    lines = dlgs[0].lines()
    assert all(line.startswith("odd.swi3s: ") for line in lines)
    for part in ("Link 1: 1 register override not read",
                 "Link 1: 1 saved stream colour could not be read and was not restored",
                 "the saved mode 'Sketching' is not one of",
                 "the saved 'grid_rows' 'lots' is not a number; 64 is used",
                 "the saved Commands sort 'Nope' is not a sortable column of the table",
                 "the saved Registers view has entries that do not read, left as the pane has "
                 "them: device 99"):
        assert any(part in line for line in lines), (part, lines)
    assert "import warning" in win._status.text()


def test_saved_window_on_off_settings_read_only_as_true_or_false(tmp_path, monkeypatch):
    """show_clock, tx_map and tx_persist that are not a JSON true or false take their
    defaults (clock shown, map and persistence off), each with a note."""
    src = _demo_source()
    d = {"version": 4, "active_link": 0, "cursor": 0, "mode": "Analysis",
         "view": {"show_clock": "no", "tx_map": 1, "tx_persist": "yes"},
         "links": [{"source": src, "name": "Link 1"}]}
    win = _reopen(monkeypatch, d, tmp_path)
    lines = win.import_warning_dialogs()[0].lines()
    for part in ("the saved 'show_clock' 'no' is not true or false; on is used",
                 "the saved 'tx_map' 1 is not true or false; off is used",
                 "the saved 'tx_persist' 'yes' is not true or false; off is used"):
        assert sum(part in line for line in lines) == 1, (part, lines)
    assert win._raw_view.show_clock and not win._tx_map and not win._tx_persist


def test_reopening_a_clean_workspace_opens_no_window(tmp_path, monkeypatch):
    src = _demo_source()
    d = json.loads(Workspace.single(source=src).to_json())
    win = _reopen(monkeypatch, d, tmp_path, "clean.swi3s")
    assert len(win.links) == 1
    assert win.import_warning_dialogs() == []


def test_a_saved_window_reapplied_from_a_round_trip_reports_nothing(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    path = str(tmp_path / "round.swi3s")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    again = MainWindow()
    again.open_workspace()
    pump_loads(again)
    assert again.import_warning_dialogs() == []


def test_reopening_a_workspace_with_infinite_and_huge_view_values_skips_each_and_opens(
        tmp_path, monkeypatch):
    src = _demo_source()
    inf, huge = float("inf"), 10 ** 30
    link_view = {"commands": {"col_widths": {"Row": huge, "Time": 1.5}},
                 "audio": {"channels": [[inf, 1, 1]], "channel_list_width": huge,
                           "decimation": [[1, 1, inf]], "x_range_s": [0, huge * huge]}}
    d = {"version": 4, "active_link": 0, "cursor": 0, "mode": "Analysis",
         "view": {"grid_rows": inf, "tx_start_row": inf, "tx_map": True,
                  "timeline_height": huge, "timeline_ps": [0, inf],
                  "pane_links": {"commands": huge, "grid": inf, "bottom": 5},
                  "samples": {"ports": [[inf, 1]]},
                  "registers": {"device": huge, "expanded": [[1], {}],
                                "col_widths": {"Name": huge}},
                  "statistics": {"collapsed": "Header"},
                  "timing_pane": {"filter": 5, "hidden": 7}},
         "bookmarks": [_bookmark(5, link=1), _bookmark(6, link=huge)],
         "links": [{"source": src, "name": "A", "view": link_view},
                   {"source": src, "name": "B", "view": link_view}]}
    shown = []
    for name in ("critical", "warning"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: shown.append(a[1:3]))
    win = _reopen(monkeypatch, d, tmp_path)
    assert shown == [] and len(win.links) == 2
    lines = win.import_warning_dialogs()[0].lines()
    # Window-wide panes are noted once; a Link's own Commands view once per Link.
    for part, count in (("the saved 'grid_rows' inf is not a number", 1),
                        ("the saved 'tx_start_row' inf is not a number", 1),
                        ("the saved timeline height", 1),
                        ("the saved timeline range", 1),
                        ("the saved Link of the grid panes is not a Link number", 1),
                        ("the saved Link of the bottom, commands panes is not a Link that "
                         "was opened", 1),
                        ("Link 1 (A): the saved Commands column widths of 'Row', 'Time'", 1),
                        ("Link 2 (B): the saved Commands column widths of 'Row', 'Time'", 1),
                        ("Samples view has entries that do not read", 1),
                        ("Registers view has entries that do not read", 1),
                        ("Statistics view has entries that do not read", 1),
                        ("Timing view has entries that do not read", 1),
                        ("Audio view has entries that do not read", 1),
                        ("1 cursor on a Link the workspace does not have was not read", 1)):
        assert sum(part in line for line in lines) == count, (part, lines)
    assert len(lines) == 14, lines
    win.join_worker_threads()


def test_a_workspace_authoring_config_keeps_a_reserved_cds_tail_width_and_reports_it_once():
    from swi3s_studio.model.bus_config import BusConfig, demo_config
    d = demo_config().to_dict()
    d["cds_tail"] = [0, 3] + [0] * 11
    with import_notes.collect() as notes:
        cfg = BusConfig.from_dict(d, "ws.swi3s")
    assert cfg.cds_tail[1] == 3
    assert [(n.source, n.message) for n in notes] == [(
        "ws.swi3s", "CDS_TailWidthPerSource: CDS_TailWidth 3 is Reserved (0..2 UI are defined) "
        "for Device 0 = 3; the value is kept as written, the grid draws a 3-UI tail and the "
        "expected registers carry 3.")]


def test_a_workspace_authoring_scalar_cds_tail_width_of_3_names_every_source():
    from swi3s_studio.model.bus_config import BusConfig, demo_config
    d = demo_config().to_dict()
    del d["cds_tail"]
    d["cds_tail_width"] = 3
    with import_notes.collect() as notes:
        cfg = BusConfig.from_dict(d, "ws.swi3s")
    assert cfg.cds_tail == [3] * 13
    assert len(notes) == 1 and "Manager = 3, Device 0 = 3, Device 1 = 3 and 10 more" \
        in notes[0].message


@pytest.mark.parametrize("tail", [0, 1, 2])
def test_a_workspace_authoring_config_reports_nothing_for_a_defined_cds_tail_width(tail):
    from swi3s_studio.model.bus_config import BusConfig, demo_config
    d = demo_config().to_dict()
    d["cds_tail"] = [tail] * 13
    with import_notes.collect() as notes:
        BusConfig.from_dict(d, "ws.swi3s")
    assert notes == []


def test_a_workspace_authoring_cds_tail_width_past_the_register_is_not_also_reserved():
    from swi3s_studio.model.bus_config import BusConfig, demo_config
    d = demo_config().to_dict()
    d["cds_tail"] = [0, 5] + [0] * 11
    with import_notes.collect() as notes:
        BusConfig.from_dict(d, "ws.swi3s")
    assert len(notes) == 1 and "does not fit its 2-bit" in notes[0].message
