"""api.decode removes the upgraded copy of a config CSV it makes: after a decode, and when
reporting the CSV's warnings or the decode itself raises.

Run: python3 -m pytest tests/test_api_decode_temp.py
"""
import pytest
import swi3score

from swi3s_studio import api, decode, import_notes
from swi3s_studio.ingest import visualizer_csv


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """The temp directory the upgraded copy is written to, empty."""
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(d))
    return d


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "legacy.csv"
    path.write_text("Columns per Row (2-32),8\nRow Rate [kHz],3072\n", encoding="utf-8")
    return str(path)


def _source():
    return swi3score.MemorySampleSource(swi3score.make_demo_levels(4), 98_304_000, 4)


@pytest.fixture
def made(monkeypatch):
    """The paths visualizer_csv.normalized_path returns during the test."""
    paths = []
    real = visualizer_csv.normalized_path

    def normalized(path):
        paths.append(real(path))
        return paths[-1]
    monkeypatch.setattr(visualizer_csv, "normalized_path", normalized)
    return paths


def test_decode_leaves_no_upgraded_config_behind(scratch, legacy, made):
    with import_notes.collect():
        res = decode(_source(), config_csv=legacy)
    assert res.column_count > 0
    assert made and made[0] != legacy                # an upgraded copy was made
    assert made[0].startswith(str(scratch))          # in the temp directory watched here
    assert list(scratch.iterdir()) == []


class _Raised(Exception):
    pass


def _raise(*a, **k):
    raise _Raised


@pytest.mark.parametrize("where", ["warnings", "decoder", "run"])
def test_a_decode_that_raises_leaves_no_upgraded_config_behind(scratch, legacy, monkeypatch,
                                                                made, where):
    if where == "warnings":
        monkeypatch.setattr(visualizer_csv, "report_csv_warnings", _raise)
    elif where == "decoder":
        monkeypatch.setattr(api.swi3score, "Decoder", _raise)
    else:
        class Decoder:
            def __init__(self, *a):
                pass
            run = _raise
        monkeypatch.setattr(api.swi3score, "Decoder", Decoder)
    with import_notes.collect(), pytest.raises(_Raised):
        decode(_source(), config_csv=legacy)
    assert made and made[0] != legacy                # an upgraded copy was made
    assert list(scratch.iterdir()) == []
