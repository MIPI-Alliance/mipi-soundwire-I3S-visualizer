"""api.decode removes the upgraded copy of a config CSV it makes: after a decode, and when
reporting the CSV's warnings or the decode itself raises. api.decode_capture and a closed
Session leave none either; a fork and its Session share the copy until both are closed.

Run: python3 -m pytest tests/test_api_decode_temp.py
"""
import os

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


def _capture():
    from swi3s_studio.session import Session
    return Session.from_demo(48, phy=2, cold_start=True).capture


def _session(legacy):
    from swi3s_studio.session import Session
    with import_notes.collect():
        return Session(_capture(), config_csv=legacy)


def test_decode_capture_leaves_no_upgraded_config_behind(scratch, legacy, made):
    cap = _capture()
    with import_notes.collect():
        res = api.decode_capture(cap, config_csv=legacy)
    assert res.column_count > 0
    assert made and made[0] != legacy and made[0].startswith(str(scratch))
    assert list(scratch.iterdir()) == []


def test_a_closed_session_leaves_no_upgraded_config_behind(scratch, legacy, made):
    s = _session(legacy)
    assert [p.name for p in scratch.iterdir()] == [os.path.basename(made[0])]
    s.close()
    s.close()
    assert list(scratch.iterdir()) == []


def test_a_fork_shares_its_sessions_upgraded_config_until_both_are_closed(scratch, legacy,
                                                                           made):
    """A fork that is closed leaves the copy its Session reads; a Session closed while a
    fork of it is open leaves the copy to that fork, which removes it when it is closed."""
    s = _session(legacy)
    s.fork().close()
    assert os.path.exists(made[0])
    fork = s.fork()
    s.close()
    assert os.path.exists(made[0])
    fork.close()
    assert list(scratch.iterdir()) == []


def test_an_adopted_fork_leaves_its_upgraded_config_to_the_session(scratch, legacy, made,
                                                                    tmp_path):
    """A fork that imposes another legacy CSV and is adopted hands its copy to the Session,
    which removes it when closed."""
    s = _session(legacy)
    fork = s.fork()
    other = tmp_path / "other.csv"
    other.write_text("Columns per Row (2-32),8\nRow Rate [kHz],3072\n", encoding="utf-8")
    with import_notes.collect():
        fork.apply_config_csv(str(other))
    assert not os.path.exists(made[0]) and os.path.exists(made[-1])
    s.adopt(fork)
    s.close()
    assert list(scratch.iterdir()) == []
