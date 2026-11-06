"""Devices feature: hub-depth response delay + per-device name/regmap/depth.

- C++ hub-delay decode (via the swi3score._hub_ping_test hook).
- DecoderSettings.hub_depth_overrides plumbing + Session.set_hub_depths re-decode.
- Workspace round-trip of device names / hub depths / register maps.
- The Devices dialog (name / register map / hub depth) readback.
- A hub depth outside 0..MAX_HUB_DEPTH is clamped by the session and the decode, and left
  out of a saved workspace, and each is reported.

Run: python3 -m pytest tests/test_devices.py
"""
import pathlib

import pytest
import swi3score

from swi3s_studio.model.regmap_import import PeripheralRegisterMap
from swi3s_studio.session import Session
from swi3s_studio.workspace import Workspace

pytestmark = pytest.mark.gui

_AMP = pathlib.Path(__file__).resolve().parent.parent / "data" / "regmaps" / "example_amp.json"

# Non-decreasing depths (deeper devices addressed later), within 0..5.
_DEPTHS = [0, 0, 1, 1, 2, 3, 3, 4, 4, 5, 5, 5]


def test_hub_ping_decode_recovers_delayed_tokens():
    # A Ping whose device tokens are delayed 2*depth frame rows decodes back to
    # 0..11 only when the parser is told each device's hub depth.
    assert swi3score._hub_ping_test(_DEPTHS, _DEPTHS) == list(range(12))


def test_hub_decode_blind_parser_garbles():
    # Same delayed stream, but a depth-blind parser misframes the delayed tokens.
    naive = swi3score._hub_ping_test(_DEPTHS, [0] * 12)
    assert naive != list(range(12))


def test_no_delay_is_unchanged():
    assert swi3score._hub_ping_test([0] * 12, [0] * 12) == list(range(12))


def test_decoder_settings_field():
    s = swi3score.DecoderSettings()
    s.hub_depth_overrides = [(2, 1), (3, 2)]
    assert list(s.hub_depth_overrides) == [(2, 1), (3, 2)]


def test_session_set_hub_depths_redecodes():
    sess = Session.from_demo()
    base = list(sess.commands)
    before = sess.decoder
    sess.set_hub_depths({2: 1, 3: 2})
    assert sess.hub_depths == {2: 1, 3: 2}
    assert sess.decoder is not before                # a new decoder ran with the depths
    after = sess.decoder
    sess.set_hub_depths({2: 1, 3: 2})
    assert sess.decoder is after                     # unchanged depths do not re-decode
    # Depths describe the wire. The demo has no hub delay, so declared depths misframe
    # the response tokens after device 2 and the first commit goes unconfirmed; the
    # commands up to that commit decode unchanged, and clearing the depths restores all.
    first_commit = next(i for i, c in enumerate(base) if c["command"] == "SSCR")
    hubbed = [_key(c) for c in sess.commands]
    assert hubbed[:first_commit] == [_key(c) for c in base[:first_commit]]
    assert not sess.commands[first_commit]["commit_confirmed"]
    sess.set_hub_depths({})
    assert [_key(c) for c in sess.commands] == [_key(c) for c in base]


def _key(cmd):
    return (cmd["command"], int(cmd["start_sample"]), bool(cmd["crc_valid"]))


def test_workspace_roundtrip_device_settings(tmp_path):
    pm = PeripheralRegisterMap.from_json(
        _AMP.read_text(encoding="utf-8"))
    ws = Workspace.single(
        source={"type": "demo"},
        device_names={"0": "Codec", "1": "MicL"},
        device_hub_depths={"2": 1, "3": 4},
        device_regmaps={"0": pm.to_json_dict()},
    )
    p = str(tmp_path / "ws.json")
    ws.save(p)
    back = Workspace.load(p)
    assert back.links[0].device_names == {"0": "Codec", "1": "MicL"}
    assert back.links[0].device_hub_depths == {"2": 1, "3": 4}
    pm2 = PeripheralRegisterMap.from_json_dict(back.links[0].device_regmaps["0"])
    assert len(pm2.registers) == len(pm.registers)


def test_devices_dialog():
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    pm = PeripheralRegisterMap.from_json(
        _AMP.read_text(encoding="utf-8"))
    dlg = DevicesDialog(None, range(12), {0: "Codec"}, {2: 1}, {0: pm})
    assert dlg._name_edits[0].text() == "Codec"
    assert dlg._depth_spins[2].value() == 1
    assert dlg._map_labels[0].text().startswith("example_amp")
    # Edit and accept.
    dlg._name_edits[1].setText("MicL")
    dlg._depth_spins[3].setValue(4)
    dlg._clear_map(0)
    dlg.accept()
    assert dlg.names == {0: "Codec", 1: "MicL"}
    assert dlg.hub_depths[3] == 4 and dlg.hub_depths[2] == 1
    assert dlg.regmaps.get(0) is None


def test_devices_dialog_focused_sections():
    """The Devices menu opens the dialog focused on ONE aspect. A focused dialog
    only builds that section's widgets; hidden aspects keep their passed-in values and
    accept() doesn't touch them."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    # Hub-depth-only: no name/map widgets, depths editable, names preserved on accept.
    dlg = DevicesDialog(None, range(12), {0: "Codec"}, {2: 1}, {}, sections=("depths",))
    assert dlg._name_edits == {} and dlg._map_labels == {}
    assert dlg._depth_spins[2].value() == 1
    assert "Hub Depth" in dlg.windowTitle()
    dlg._depth_spins[5].setValue(3)
    dlg.accept()
    assert dlg.hub_depths[5] == 3
    assert dlg.names == {0: "Codec"}                 # untouched by the depths-only dialog
    # Names-only: no depth spins.
    dlg2 = DevicesDialog(None, range(12), {}, {}, {}, sections=("names",))
    assert dlg2._depth_spins == {} and dlg2._map_labels == {}
    assert "Name" in dlg2.windowTitle()


def test_command_table_peripheral_register_name():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.model.registers import RegisterMap
    from swi3s_studio.ui.command_table import CommandTableModel
    rmap = RegisterMap.load()
    pm = PeripheralRegisterMap.from_json(
        _AMP.read_text(encoding="utf-8"))
    cmd = {"command": "WriteA32", "has_address": True, "address": 0x10000104,
           "device_mask": 0b1, "data": [0x2A], "has_manager_packet": True, "crc_valid": True}
    m = CommandTableModel([cmd], rmap, 1)
    assert m._register_label(cmd) == "Device-defined (Local)"     # no map yet
    m.set_peripheral_maps({0: pm})
    assert m._register_label(cmd) == "GAIN"                  # device 0's vendor map
    other = dict(cmd, device_mask=0b10)                          # different device, no map
    assert m._register_label(other) == "Device-defined (Local)"


def test_register_view_expansion_survives_rebuild():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.model.registers import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    pm = PeripheralRegisterMap.from_json(
        _AMP.read_text(encoding="utf-8"))
    rv = RegisterView(rmap)
    df = DeviceRegisterFile(rmap, 0)
    df.apply_write(0x10000104, bytes([0x2A]))
    rv.set_files({0: df})
    rv.set_peripheral_maps({0: pm})

    def periph_node():
        root = rv._tree.invisibleRootItem()
        for i in range(root.childCount()):
            if "Peripheral" in root.child(i).text(0):
                return root.child(i)
        return None

    periph_node().setExpanded(True)         # user opens the peripheral block
    rv.set_files({0: df})                   # a cursor move re-runs _rebuild
    assert periph_node().isExpanded(), "peripheral block collapsed on rebuild"


def test_register_view_compare_shows_csv_only_device():
    """Compare against a config CSV that references a device NOT in the cursor's
    current register files: set_expected must add that device to the picker
    immediately (not only after an incidental cursor move), and clear_expected must
    drop it again."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.model.registers import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    rv = RegisterView(rmap)
    df = DeviceRegisterFile(rmap, 0)
    df.apply_write(0x10000104, bytes([0x2A]))
    rv.set_files({0: df})                    # only device 0 is tracked at the cursor
    picker_devs = lambda: {rv._device.itemData(i) for i in range(rv._device.count())}
    assert picker_devs() == {0}
    # Expected config references device 3 (absent from the current files).
    rv.set_expected([(3, 0x10000104, 0x11)])
    assert 3 in picker_devs(), picker_devs()     # CSV-only device now selectable
    rv.clear_expected()
    assert picker_devs() == {0}                  # and dropped when compare is cleared


def test_a_hub_depth_outside_the_range_is_clamped_and_reported():
    """The session and the decode hold a depth in 0..MAX_HUB_DEPTH (clamped); a saved workspace
    reads only depths in that range, for devices 0..11, and says how many it dropped and the
    two rules."""
    from swi3s_studio import import_notes, workspace
    from swi3s_studio.session import MAX_HUB_DEPTH
    assert workspace.MAX_HUB_DEPTH is MAX_HUB_DEPTH and MAX_HUB_DEPTH == 5
    saved = Workspace.single(source={"type": "demo"},
                             device_hub_depths={"2": 9, "3": -1, "4": 5, "12": 1})
    with import_notes.collect() as read:
        loaded = Workspace.from_json(saved.to_json(), "w.swi3s")
    assert loaded.links[0].device_hub_depths == {"4": 5}
    assert [str(n) for n in read] == [
        "w.swi3s: Link 1: 3 hub depths not read: the device is not 0..11 or the value is not "
        "0..5"], read
    with import_notes.collect() as opened:
        built = Session.from_demo(hub_depths={1: 7})
    assert built.hub_depths == {1: 5}
    assert "device 1 hub depth 7" in " ".join(str(n) for n in opened)
    with import_notes.collect() as notes:
        sess = Session.from_demo()
        sess.set_hub_depths({2: 9, 3: -1, 4: 5})
    assert sess.hub_depths == {2: 5, 3: 0, 4: 5}
    text = " ".join(str(n) for n in notes)
    assert "device 2 hub depth 9" in text and "device 3 hub depth -1" in text, text
    assert "device 4" not in text
