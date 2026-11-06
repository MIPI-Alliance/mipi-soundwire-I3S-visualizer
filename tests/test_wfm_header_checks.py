"""wfm.read_wfm and wfm.read_header: the sample interval must be a positive number giving
at most the maximum rate, and the label is the file's base name under either path
separator.

Every .wfm is synthetic (test_analog_import._write_synthetic_wfm).

Run: python3 -m pytest tests/test_wfm_header_checks.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from test_analog_import import _square_wave_codes, _write_synthetic_wfm

from swi3s_studio.ingest import wfm


@pytest.mark.parametrize("dt", [0.0, -1e-9, float("nan"), float("inf")])
def test_a_sample_interval_that_is_not_positive_is_refused(tmp_path, dt):
    p = str(tmp_path / "bad.wfm")
    _write_synthetic_wfm(p, _square_wave_codes(400, 8), dt=dt)
    with pytest.raises(ValueError, match="sample interval"):
        wfm.read_wfm(p)
    with pytest.raises(ValueError, match="bad.wfm: .wfm sample interval"):
        wfm.read_header(p)


@pytest.mark.parametrize("dt", [1e-300, 1e-320, 5e-16])
def test_a_sample_interval_giving_a_rate_beyond_the_maximum_is_refused(tmp_path, dt):
    p = str(tmp_path / "fast.wfm")
    _write_synthetic_wfm(p, _square_wave_codes(400, 8), dt=dt)
    for read in (wfm.read_wfm, wfm.read_header):
        with pytest.raises(ValueError, match="fast.wfm: .wfm sample interval .* above"):
            read(p)


def test_the_finest_accepted_sample_interval_is_read(tmp_path):
    p = str(tmp_path / "ok.wfm")
    _write_synthetic_wfm(p, _square_wave_codes(400, 8), dt=1e-15)
    assert wfm.read_header(p)["sample_rate_hz"] == pytest.approx(1e15)
    assert wfm.read_wfm(p)["sample_rate_hz"] == pytest.approx(1e15)


def test_the_label_is_the_base_name_under_either_separator(tmp_path):
    p = str(tmp_path / "ch1.wfm")
    _write_synthetic_wfm(p, _square_wave_codes(400, 8), dt=1e-9)
    assert wfm.read_wfm(p)["label"] == "ch1.wfm"
    assert wfm._base_name("C:\\caps\\ch1.wfm") == "ch1.wfm"
    assert wfm._base_name("/caps/a/ch1.wfm") == "ch1.wfm"
    assert wfm._base_name("ch1.wfm") == "ch1.wfm"


@pytest.mark.parametrize("dt", [-1e-9, float("nan"), float("inf"), 1e-320])
def test_the_probe_does_not_offer_a_pair_whose_interval_the_loader_refuses(tmp_path, dt):
    from swi3s_studio.ingest import probe
    paths = []
    for name in ("a.wfm", "b.wfm"):
        p = str(tmp_path / name)
        _write_synthetic_wfm(p, _square_wave_codes(400, 8), dt=dt)
        paths.append(p)
    got = probe.probe(paths)
    assert got.sample_rate_hz is None and got.duration_s is None
    assert got.cost_all_bytes is None
    with pytest.raises(ValueError, match="sample interval"):
        wfm.read_wfm_pair(*paths)


def test_the_probe_reports_the_rate_and_length_of_a_pair_the_loader_reads(tmp_path):
    from swi3s_studio.ingest import probe
    paths = []
    for name in ("a.wfm", "b.wfm"):
        p = str(tmp_path / name)
        _write_synthetic_wfm(p, _square_wave_codes(400, 8), dt=1e-9)
        paths.append(p)
    got = probe.probe(paths)
    assert got.sample_rate_hz == pytest.approx(1e9)
    assert got.duration_s == pytest.approx(400e-9)
