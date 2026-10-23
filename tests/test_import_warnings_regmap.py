"""Import warnings from register maps: the XML importers' ImportReport, a stored normalized
map read from JSON, the spec map's JSON, and the Devices dialog, which shows the warnings
of a map it binds in the Import warnings window."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from swi3s_studio import import_notes
from swi3s_studio.model.registers import DEFAULT_REGISTERS_JSON, RegisterMap
from swi3s_studio.model.regmap_import import (
    PeripheralRegisterMap,
    import_text,
    library_dir,
)

_CLEAN_BITS = """<register name="CTRL" address="0x10000100">
  <bit position="1" name="mode[1]" access="RW" />
  <bit position="0" name="mode[0]" access="RW" />
  <reset hex="0x01" />
</register>
"""

_ODD_BITS = """<register name="CTRL" address="0x10000100" page="2">
  <bit position="1" name="mode[1]" access="RW" />
  <bit position="1" name="other" access="RW" />
  <note>free text</note>
  <reset hex="zz" />
</register>
"""

_FIELDS_XML = """<Registers><Register><Name>R</Name><Address>0x10000000</Address>
<DefaultValue>0x0</DefaultValue><Access>R/W</Access></Register></Registers>
<Fields>
<Field><Name>LOW</Name><Address>R[0:3]</Address>
  <Values><Value Value="0x1"><Description>one</Description></Value>
          <Value Value="nope"><Description>bad</Description></Value></Values></Field>
</Fields>
"""


def _messages(notes):
    return [n.message for n in notes]


def test_a_clean_bit_list_map_reports_nothing():
    _pmap, report = import_text(_CLEAN_BITS, "clean")
    assert report.warnings == []
    with import_notes.collect() as notes:
        report.report_notes("clean.xml")
    assert notes == []


def test_bit_list_attributes_children_resets_and_repeated_bits_are_reported():
    _pmap, report = import_text(_ODD_BITS, "odd")
    text = "\n".join(report.warnings)
    assert "'CTRL': bit position 1 is listed twice — both kept" in text
    assert "dropped register attributes and elements (not modelled): <note>, page" in text
    assert "1 register(s) with an unparseable <reset hex> read as no reset: CTRL" in text
    with import_notes.collect() as notes:
        report.report_notes("odd.xml")
    assert {n.source for n in notes} == {"odd.xml"}
    assert _messages(notes) == report.warnings


def test_register_field_xml_reports_swapped_bits_and_unread_value_labels():
    _pmap, report = import_text(_FIELDS_XML, "fields")
    assert "1 field(s) written low bit first read as [hi:lo]: LOW" in report.warnings
    assert ("1 value label(s) with no parseable Value or no Description dropped"
            in report.warnings)


def test_a_stored_map_reports_what_it_could_not_read():
    doc = {"_meta": {"schema": "swi3s-studio-regmap/1", "name": "amp"},
           "registers": [
               {"name": "A", "address": "0x10000000", "reset": "0x00",
                "fields": [{"name": "f", "bits": "[7:0]", "enum": {"0x1": "on", "x": "?"}}]},
               {"name": "B", "address": "0x10000000", "reset": "weird", "fields": []},
               {"name": "NOADDR", "address": "here", "fields": []},
               {"name": "C", "address": "0x10000001",
                "fields": [{"name": "g", "bits": "7..0"}]}]}
    with import_notes.collect() as notes:
        pmap = PeripheralRegisterMap.from_json_dict(doc, "ws.swi3s")
    assert [r.name for r in pmap.registers] == ["A", "B", "C"]
    assert {n.source for n in notes} == {"ws.swi3s"}
    assert _messages(notes) == [
        "register map 'amp': 1 register with a missing or unparseable address skipped "
        "(NOADDR)",
        "register map 'amp': 1 duplicate address: the later register is used "
        "(0x10000000 ('A' and 'B'))",
        "register map 'amp': 1 field with an unparseable bits spec dropped (C.g)",
        "register map 'amp': 1 unparseable reset read as unknown (B)",
        "register map 'amp': 1 value label(s) with an unparseable value dropped",
    ]


def test_the_bundled_example_map_reads_without_warnings():
    path = os.path.join(library_dir(), "example_amp.json")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with import_notes.collect() as notes:
        PeripheralRegisterMap.from_json(text, "example_amp.json")
    assert notes == []


def test_the_spec_map_reads_without_warnings_and_a_damaged_one_says_what_it_skipped(tmp_path):
    with import_notes.collect() as notes:
        RegisterMap.load(DEFAULT_REGISTERS_JSON)
    assert notes == []
    doc = {"blocks": [{"name": "SLC", "registers": [
        {"name": "OK", "offset": "0x00", "fields": [{"name": "a", "bits": "[0]"}]},
        {"name": "NOOFF", "offset": "soon", "fields": []},
        {"name": "BITS", "offset": "0x01", "fields": [{"name": "b", "bits": "0..3"}]},
        {"name": "ARR", "offset": "0x02-0x03", "array": {"kind": "words"}, "fields": []}]}]}
    path = tmp_path / "regs.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with import_notes.collect() as notes:
        RegisterMap.load(str(path))
    assert {n.source for n in notes} == {"regs.json"}
    assert _messages(notes) == [
        "1 register(s) with an offset that is neither a number nor a range skipped: "
        "SLC.NOOFF",
        "1 field(s) with an unreadable bits spec skipped: SLC.BITS.b",
        "1 array row(s) of an unknown kind kept as one register: SLC.ARR",
    ]


def test_binding_a_map_with_warnings_from_the_devices_dialog_opens_the_window(
        tmp_path, monkeypatch):
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.regmap_import_dialog import RegisterMapImportDialog
    QApplication.instance() or QApplication([])
    path = tmp_path / "odd.xml"
    path.write_text(_ODD_BITS, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))

    def import_map(dlg):
        dlg._browse()
        dlg.accept()
        return QDialog.Accepted

    def devices(dlg):
        dlg._set_map(3)
        dlg.accept()
        return QDialog.Accepted
    monkeypatch.setattr(RegisterMapImportDialog, "exec", import_map)
    monkeypatch.setattr(DevicesDialog, "exec", devices)
    win = MainWindow()
    win.load_demo()
    win.manage_register_maps()
    assert win._session.peripheral_maps[3].name == "odd"
    dlgs = win.import_warning_dialogs()
    assert len(dlgs) == 1 and not dlgs[0].isModal()
    assert dlgs[0].windowTitle() == "Import warnings — odd.xml"
    assert "odd.xml: 'CTRL': bit position 1 is listed twice — both kept" in dlgs[0].lines()


def test_a_map_imported_then_cancelled_shows_nothing(tmp_path, monkeypatch):
    from swi3s_studio.ui.devices_dialog import DevicesDialog
    from swi3s_studio.ui.main_window import MainWindow
    from swi3s_studio.ui.regmap_import_dialog import RegisterMapImportDialog
    QApplication.instance() or QApplication([])
    path = tmp_path / "odd.xml"
    path.write_text(_ODD_BITS, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(RegisterMapImportDialog, "exec",
                        lambda dlg: (dlg._browse(), dlg.accept(), QDialog.Accepted)[-1])
    monkeypatch.setattr(DevicesDialog, "exec",
                        lambda dlg: (dlg._set_map(3), QDialog.Rejected)[-1])
    win = MainWindow()
    win.load_demo()
    win.manage_register_maps()
    assert win.import_warning_dialogs() == []
