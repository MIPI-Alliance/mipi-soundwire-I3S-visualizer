"""Import warnings from register maps: the XML importers' ImportReport, a stored normalized
map read from JSON, the spec map's JSON, and the Devices dialog, which shows the warnings
of a map it binds in the Import warnings window."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from swi3s_studio import import_notes
from swi3s_studio.model.registers import DEFAULT_REGISTERS_JSON, RegisterMap
from swi3s_studio.model.regmap_import import (
    PeripheralRegisterMap,
    import_text,
    library_dir,
)

pytestmark = pytest.mark.gui

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
        "register map 'amp': 1 field with a bits spec that is not [hi:lo] within 7..0 dropped (C.g)",
        "register map 'amp': 1 unparseable reset read as unknown (B)",
        "register map 'amp': 1 value label(s) with an unparseable value or one outside the "
        "field's width dropped",
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
        {"name": "BITS", "offset": "0x01", "fields": [
            {"name": "b", "bits": "0..3"}, {"name": "inv", "bits": "[3:7]"},
            {"name": "wide", "bits": "[9:8]"}]},
        {"name": "ARR", "offset": "0x02-0x03", "array": {"kind": "words"}, "fields": []}]}]}
    path = tmp_path / "regs.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with import_notes.collect() as notes:
        RegisterMap.load(str(path))
    assert {n.source for n in notes} == {"regs.json"}
    assert _messages(notes) == [
        "1 register(s) with an offset that is neither a number nor a range skipped: "
        "SLC.NOOFF",
        "3 field(s) with an unreadable bits spec skipped: SLC.BITS.b, SLC.BITS.inv, "
        "SLC.BITS.wide",
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


def test_an_unrecognised_access_is_reported_in_both_formats_and_kept_as_written():
    bits = _CLEAN_BITS.replace('name="mode[1]" access="RW"', 'name="mode[1]" access="R"')
    pmap, report = import_text(bits, "acc")
    assert ("1 register(s) or field(s) with access 'R', which is not RW, RO, WO or RW1C; "
            "kept as written: CTRL.mode[1]") in report.warnings
    assert {f.name: f.access for f in pmap.registers[0].fields}["mode"] == "R"
    fields = _FIELDS_XML.replace("<Access>R/W</Access>", "<Access>Read-Only</Access>")
    _pmap, report = import_text(fields, "acc")
    assert ("1 register(s) or field(s) with access 'Read-Only', which is not RW, RO, WO or "
            "RW1C; kept as written: R") in report.warnings


def test_the_access_spellings_of_the_spec_and_of_vendor_tools_are_read_without_a_warning():
    for text, canon in (("R/W", "RW"), ("rw", "RW"), ("R/O", "RO"), ("W/O", "WO"),
                        ("RW1C", "RW1C"), ("R/W1C", "RW1C")):
        pmap, report = import_text(
            _CLEAN_BITS.replace('access="RW"', f'access="{text}"'), "acc")
        assert report.warnings == [], text
        assert {f.access for f in pmap.registers[0].fields} == {canon}, text


def test_a_blank_access_is_not_a_value_to_report():
    bits = _CLEAN_BITS.replace(' access="RW"', "")
    _pmap, report = import_text(bits, "acc")
    assert report.warnings == []


def test_a_doctype_is_skipped_and_malformed_xml_is_a_value_error_naming_the_file_and_line():
    from swi3s_studio.model.regmap_import import BitListXmlImporter
    for doctype in ('<!DOCTYPE Map SYSTEM "map.dtd">',
                    '<!DOCTYPE Map [ <!ENTITY e "x"> <!ELEMENT register ANY> ]>'):
        text = '<?xml version="1.0"?>\n' + doctype + "\n" + _CLEAN_BITS
        pmap, report = import_text(text, "dt")
        assert pmap.registers[0].name == "CTRL" and report.warnings == []
    broken = "<?xml version=\"1.0\"?>\n" + _CLEAN_BITS.replace("</register>", "</registr>")
    with pytest.raises(ValueError, match=r"^broken: not well-formed XML \(mismatched tag, line 6\)"):
        import_text(broken, "broken")
    with pytest.raises(ValueError, match="^not well-formed XML"):
        BitListXmlImporter().parse("<register", "")


def test_a_file_full_of_unterminated_doctypes_is_read_in_linear_time():
    """The inputs are a hundred thousand declarations, so a quadratic scan would not finish."""
    from swi3s_studio.model.regmap_import import _xml_tolerant_parse
    for hostile in ("<!DOCTYPE " * 100_000, "<!DOCTYPE [" * 100_000,
                    "<!DOCTYPE a [ " + "<!DOCTYPE " * 100_000):
        text = _CLEAN_BITS + hostile
        assert len(text) > 900_000
        with pytest.raises(ValueError, match="not well-formed XML"):
            _xml_tolerant_parse(text, "hostile")
    ok = _xml_tolerant_parse('<!DOCTYPE m [ <!ENTITY e "a>b"> ]><register name="R"/>', "ok")
    assert [e.tag for e in ok] == ["register"]


def test_a_mixed_access_warning_clips_the_access_text_it_echoes():
    text = ('<register name="CTRL" address="0x10000100">'
            f'<bit position="1" name="x[1]" access="{"Q" * 5000}" />'
            '<bit position="0" name="x[0]" access="RW" /></register>\n')
    _pmap, report = import_text(text, "mixed")
    mixed = [w for w in report.warnings if "mixed access" in w]
    assert len(mixed) == 1 and len(mixed[0]) < 300
    assert "Q" * 40 + "…" in mixed[0] and "Q" * 41 not in mixed[0]


def _bit_register(name, address, reset=None):
    tail = "" if reset is None else f'<reset hex="{reset}" />'
    return (f'<register name="{name}" address="{address}">'
            f'<bit position="0" name="b" access="RW" />{tail}</register>\n')


def test_a_bit_list_register_beyond_32_bits_is_skipped_with_a_warning():
    text = _CLEAN_BITS + _bit_register("FAR", "0x110000000") + _bit_register("NEG", "-1")
    pmap, report = import_text(text, "far")
    assert [r.name for r in pmap.registers] == ["CTRL"]
    far = [w for w in report.warnings if "outside 0..0xFFFFFFFF" in w]
    assert len(far) == 2 and "'FAR'" in far[0] and "'NEG'" in far[1]


def test_a_register_field_xml_register_beyond_32_bits_is_skipped_with_a_warning():
    text = _FIELDS_XML.replace("0x10000000", "0x110000000")
    pmap, report = import_text(text, "far")
    assert pmap.registers == []
    assert any("outside 0..0xFFFFFFFF" in w for w in report.warnings)


def test_a_bit_list_reset_outside_a_byte_is_reported_and_read_as_no_reset():
    text = (_bit_register("BIG", "0x10000000", "0x1FF") + _bit_register("NEG", "0x10000001", "-1")
            + _bit_register("OK", "0x10000002", "0xFF"))
    pmap, report = import_text(text, "reset")
    resets = {r.name: r.reset for r in pmap.registers}
    assert resets["BIG"] is None and resets["NEG"] is None and resets["OK"] == 0xFF
    msgs = [w for w in report.warnings if "outside one byte" in w]
    assert len(msgs) == 2 and "'BIG'" in msgs[0] and "'NEG'" in msgs[1]
    again = PeripheralRegisterMap.from_json(pmap.to_json(), "reset.json")
    assert [r.reset for r in again.registers] == [r.reset for r in pmap.registers]


def test_a_field_default_wider_than_its_field_is_reported():
    text = """<Registers><Register><Name>R</Name><Address>0x10000000</Address>
<DefaultValue>0x0</DefaultValue></Register></Registers>
<Fields><Field><Name>ONE</Name><Address>R[0]</Address><DefaultValue>0x3</DefaultValue></Field>
<Field><Name>TWO</Name><Address>R[2:1]</Address><DefaultValue>0x3</DefaultValue></Field></Fields>"""
    pmap, report = import_text(text, "wide")
    wide = [w for w in report.warnings if "bits above the field's" in w]
    assert wide == ["field 'ONE': default '0x3' has bits above the field's 1-bit width — dropped"]
    assert {f.name: f.reset for f in pmap.registers[0].fields} == {"ONE": 1, "TWO": 3}


def test_a_register_field_xml_field_past_the_register_width_has_no_dead_warning():
    text = """<Registers><Register><Name>R</Name><Address>0x10000000</Address>
<DefaultValue>0x1</DefaultValue></Register></Registers>
<Fields><Field><Name>TOP</Name><Address>R[63:60]</Address></Field></Fields>"""
    pmap, report = import_text(text, "top")
    assert len(pmap.registers) == 8
    assert not any("reaches bit" in w for w in report.warnings)


def test_a_huge_bit_index_or_reset_in_a_stored_map_is_reported_not_raised():
    doc = {"_meta": {"name": "big"},
           "registers": [
               {"name": "A", "address": "0x10000000", "reset": "0x" + "F" * 5000,
                "fields": [{"name": "huge", "bits": "[" + "9" * 5000 + "]"},
                           {"name": "wide", "bits": "[1:0]", "reset": "0x4"},
                           {"name": "hex", "bits": "[7:4]", "reset": "0x" + "F" * 5000},
                           {"name": "ok", "bits": "[3:2]", "reset": "0x3",
                            "enum": {"0x3": "three", "0x4": "past the field",
                                     "0x" + "F" * 5000: "huge"}}]},
               {"name": "B", "address": "0x10000001", "reset": -1, "fields": []}]}
    with import_notes.collect() as notes:
        pmap = PeripheralRegisterMap.from_json_dict(doc, "big.json")
    msgs = _messages(notes)
    assert any("1 field with a bits spec" in m and "A.huge" in m for m in msgs)
    wide = [m for m in msgs if "outside the register's byte or the field's width" in m]
    assert len(wide) == 1 and wide[0].startswith("register map 'big': 4 resets")
    a = pmap.registers[0]
    assert a.reset is None and pmap.registers[1].reset is None
    assert {f.name: f.reset for f in a.fields} == {"wide": None, "hex": None, "ok": 3}
    assert a.fields[-1].enum == {3: "three"}
    assert any("2 value label(s)" in m for m in msgs)
    assert len(pmap.to_json()) < 5000


def test_a_register_index_name_with_a_huge_index_is_a_plain_name():
    name = "bit[" + "9" * 5000 + "]"
    text = (f'<register name="R" address="0x10000000"><bit position="0" name="{name}" />'
            f'<reset hex="0" /></register>')
    pmap, _report = import_text(text, "idx")
    assert pmap.registers[0].fields[0].name == name


def test_importers_list_a_few_warnings_of_a_kind_and_count_the_rest():
    n = 20_000
    text = "".join(f'<register name="R{i}"><bit position="0" name="b" /></register>\n'
                   for i in range(n))
    _pmap, report = import_text(text, "many")
    with import_notes.collect() as notes:
        report.report_notes("many.xml")
    missing = [w for w in report.warnings if "missing/invalid address" in w]
    assert len(missing) == 3
    assert f"{n - 3} more missing address warning(s) not listed" in report.warnings
    assert len(notes) == len(report.warnings) < 12


def test_a_note_raised_twice_is_collected_once_in_constant_time():
    with import_notes.collect() as notes:
        for i in range(30_000):
            import_notes.warn("f", f"note {i % 15_000}")
    assert len(notes) == 15_000
    with import_notes.collect() as outer:
        import_notes.warn("f", "a")
        with import_notes.collect() as inner:
            import_notes.warn("f", "a")
        import_notes.warn("f", "a")
    assert len(outer) == 1 and len(inner) == 1
