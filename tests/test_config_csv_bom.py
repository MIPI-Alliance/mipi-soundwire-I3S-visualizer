"""A config CSV saved with a UTF-8 byte-order mark reads as the same file without one.

Run: python3 -m pytest tests/test_config_csv_bom.py
"""
import os

from swi3s_studio import import_notes
from swi3s_studio.ingest import visualizer_csv

_LEGACY = "Columns per Row (2-32),8\nRow Rate [kHz],3072\n"


def _upgraded(path):
    with import_notes.collect() as notes:
        out = visualizer_csv.normalized_path(str(path))
    try:
        with open(out, encoding="utf-8") as f:
            return f.read(), [str(n) for n in notes]
    finally:
        if out != str(path):
            os.remove(out)


def test_a_legacy_file_with_a_bom_keeps_its_first_field(tmp_path):
    plain = tmp_path / "plain.csv"
    plain.write_text(_LEGACY, encoding="utf-8")
    bom = tmp_path / "bom.csv"
    bom.write_text(_LEGACY, encoding="utf-8-sig")
    text, _ = _upgraded(plain)
    got, notes = _upgraded(bom)
    assert got == text
    assert "NumColumns_REG,7" in got
    assert not any("not recognised" in n for n in notes)


def test_read_legacy_csv_drops_the_bom(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text(_LEGACY, encoding="utf-8-sig")
    assert next(iter(visualizer_csv.read_legacy_csv(p))) == "Columns per Row"


def _current_text():
    from swi3s_studio.model.bus_config import demo_config
    rows = demo_config().to_csv().splitlines()
    assert rows[0].startswith("AppVersion")
    text = "\n".join(rows[1:]) + "\n"                  # first row NumColumns_REG
    return text.replace("NumColumns_REG,15", "NumColumns_REG,9", 1)


def test_a_current_format_file_with_a_bom_reads_as_without_one_in_both_engines(tmp_path):
    import swi3score

    from swi3s_studio.model import viz_engine
    text = _current_text()
    assert text.startswith("NumColumns_REG,9")
    plain = tmp_path / "plain.csv"
    plain.write_text(text, encoding="utf-8")
    bom = tmp_path / "bom.csv"
    bom.write_text(text, encoding="utf-8-sig")
    assert bom.read_bytes().startswith(b"\xef\xbb\xbf")
    with import_notes.collect() as notes:
        out = visualizer_csv.normalized_path(str(bom))
        visualizer_csv.report_csv_warnings(out, str(bom))
    assert not notes
    assert swi3score.registers_from_csv(out) == swi3score.registers_from_csv(str(plain))
    assert swi3score.grid_from_csv(out) == swi3score.grid_from_csv(str(plain))
    assert viz_engine.model_json(out) == viz_engine.model_json(str(plain))
    assert max(c["col"] for c in swi3score.grid_from_csv(out)) < 10     # NumColumns_REG,9 read


def test_a_current_format_file_is_returned_unchanged(tmp_path):
    plain = tmp_path / "plain.csv"
    plain.write_text(_current_text(), encoding="utf-8")
    assert visualizer_csv.normalized_path(str(plain)) == str(plain)
