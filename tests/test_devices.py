"""Devices feature: hub-depth response delay + per-device name/regmap/depth.

- C++ hub-delay decode (via the swi3score._hub_ping_test hook).
- DecoderSettings.hub_depth_overrides plumbing + Session.set_hub_depths re-decode.
- Workspace round-trip of device names / hub depths / register maps.
- The Devices dialog (name / register map / hub depth) readback.

Run: python3 -m pytest tests/test_devices.py
"""
import os
import tempfile

import swi3score

from swi3s_studio.model.regmap_import import PeripheralRegisterMap
from swi3s_studio.session import Session
from swi3s_studio.workspace import Workspace

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
    n0 = len(sess.commands)
    sess.set_hub_depths({2: 1, 3: 2})
    assert sess.hub_depths == {2: 1, 3: 2}
    # The demo has no hubbed responses, so the command set is unchanged.
    assert len(sess.commands) == n0


def test_workspace_roundtrip_device_settings():
    pm = PeripheralRegisterMap.from_json(
        open(os.path.join("data", "regmaps", "example_amp.json"), encoding="utf-8").read())
    ws = Workspace(
        source={"type": "demo"},
        device_names={"0": "Codec", "1": "MicL"},
        device_hub_depths={"2": 1, "3": 4},
        device_regmaps={"0": pm.to_json_dict()},
    )
    p = os.path.join(tempfile.mkdtemp(), "ws.json")
    ws.save(p)
    back = Workspace.load(p)
    assert back.device_names == {"0": "Codec", "1": "MicL"}
    assert back.device_hub_depths == {"2": 1, "3": 4}
    pm2 = PeripheralRegisterMap.from_json_dict(back.device_regmaps["0"])
    assert len(pm2.registers) == len(pm.registers)


def test_devices_dialog():
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    pm = PeripheralRegisterMap.from_json(
        open(os.path.join("data", "regmaps", "example_amp.json"), encoding="utf-8").read())
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
    """The File ▸ Devices menu opens the dialog focused on ONE aspect. A focused dialog
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
        open(os.path.join("data", "regmaps", "example_amp.json"), encoding="utf-8").read())
    cmd = {"command": "WriteA32", "has_address": True, "address": 0x10000007,
           "device_mask": 0b1, "data": [0x2A], "has_manager_packet": True, "crc_valid": True}
    m = CommandTableModel([cmd], rmap, 1)
    assert m._register_label(cmd) == "Device-defined (Local)"     # no map yet
    m.set_peripheral_maps({0: pm})
    assert m._register_label(cmd) == "AMP_LEVEL"                  # device 0's vendor map
    other = dict(cmd, device_mask=0b10)                          # different device, no map
    assert m._register_label(other) == "Device-defined (Local)"


def test_register_view_expansion_survives_rebuild():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.model.registers import DeviceRegisterFile, RegisterMap
    from swi3s_studio.ui.register_view import RegisterView
    rmap = RegisterMap.load()
    pm = PeripheralRegisterMap.from_json(
        open(os.path.join("data", "regmaps", "example_amp.json"), encoding="utf-8").read())
    rv = RegisterView(rmap)
    df = DeviceRegisterFile(rmap, 0)
    df.apply_write(0x10000007, bytes([0x2A]))
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
    df.apply_write(0x10000007, bytes([0x2A]))
    rv.set_files({0: df})                    # only device 0 is tracked at the cursor
    picker_devs = lambda: {rv._device.itemData(i) for i in range(rv._device.count())}
    assert picker_devs() == {0}
    # Expected config references device 3 (absent from the current files).
    rv.set_expected([(3, 0x10000007, 0x11)])
    assert 3 in picker_devs(), picker_devs()     # CSV-only device now selectable
    rv.clear_expected()
    assert picker_devs() == {0}                  # and dropped when compare is cleared
