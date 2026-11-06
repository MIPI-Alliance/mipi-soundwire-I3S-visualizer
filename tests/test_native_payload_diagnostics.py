"""The payload engine's two diagnostics, both read from the environment when a decode
configures its ports (CPayloadEngine::Configure):

* SWI3S_SRC_SHIFT=N reads every bit of a peripheral-sourced port with the level N UIs
  later, leaving manager-driven (sink) ports as they are. The windowed re-decode keeps the
  same shift queue, so it agrees with the full decode.
* SWI3S_DUMP_PATH writes one line per assembled data bit, "<sample> <dp> <channel> <bit>
  <level>", for the port SWI3S_DUMP_DP (every port when unset), up to SWI3S_DUMP_MAX lines.

Every input is synthetic: the flow-control demo, whose devices 2 and 3 are source ports,
built with a fixed 4 samples per UI so a shift of N UIs is 4N samples.
"""
import gc

import numpy as np
import pytest
import swi3score

from swi3s_studio.ingest.transitions import demo_capture

_SPU = 4
_RATE = 98_304_000
_SOURCES = {2, 3}
_SINKS = {0, 1}
_ENV = ("SWI3S_SRC_SHIFT", "SWI3S_DUMP_PATH", "SWI3S_DUMP_DP", "SWI3S_DUMP_MAX")


@pytest.fixture(scope="module")
def capture():
    return demo_capture(16, variant="flow_control", samples_per_ui=_SPU, sample_rate_hz=_RATE)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def _decode(cap):
    src = swi3score.TransitionSampleSource(cap.clock_edges, cap.data_edges,
                                           bool(cap.initial_clock), bool(cap.initial_data),
                                           cap.sample_rate_hz)
    settings = swi3score.DecoderSettings()
    settings.collect_bit_samples = True
    d = swi3score.Decoder(src, settings)
    d.run()
    return d


def _by_device(bits, device):
    return bits["sample"][bits["device"] == device].astype(np.int64)


def test_the_ports_the_shift_applies_to_are_the_source_ports(capture):
    ports = [p for p in _decode(capture).config_dataports()["dataports"] if p["enabled"]]
    assert {p["device_number"] for p in ports if not p["port_direction"]} == _SOURCES
    assert {p["device_number"] for p in ports if p["port_direction"]} == _SINKS


@pytest.mark.parametrize("shift", [1, 3])
def test_a_source_shift_reads_each_source_bit_that_many_uis_later(capture, monkeypatch, shift):
    plain = _decode(capture).bit_samples()
    monkeypatch.setenv("SWI3S_SRC_SHIFT", str(shift))
    shifted = _decode(capture).bit_samples()
    for dev in _SINKS:
        assert np.array_equal(_by_device(shifted, dev), _by_device(plain, dev)), dev
    for dev in _SOURCES:
        a, b = _by_device(plain, dev), _by_device(shifted, dev)
        assert a.size > 100, dev
        n = min(a.size, b.size)
        assert a.size - n <= 1, (dev, a.size, b.size)
        assert np.all(b[:n] - a[:n] == shift * _SPU), (dev, np.unique(b[:n] - a[:n]))


def test_a_shift_of_zero_is_the_default_decode(capture, monkeypatch):
    plain = _decode(capture)
    monkeypatch.setenv("SWI3S_SRC_SHIFT", "0")
    zero = _decode(capture)
    assert np.array_equal(zero.bit_samples()["sample"], plain.bit_samples()["sample"])
    assert zero.audio() == plain.audio()


def test_the_windowed_decode_keeps_the_source_shift(capture, monkeypatch):
    monkeypatch.setenv("SWI3S_SRC_SHIFT", "3")
    d = _decode(capture)
    full = d.bit_samples()
    mid = int(np.median(full["sample"]))                 # a UI index is a clock-edge index
    lo = int(np.searchsorted(capture.clock_edges.astype(np.int64), mid))
    hi = lo + 3000
    window = d.bit_samples_window(lo, hi)
    assert window["sample"].size > 100
    w_lo, w_hi = int(window["sample"].min()), int(window["sample"].max())
    inside = (full["sample"] >= w_lo) & (full["sample"] <= w_hi)

    def key(bits, mask):
        return sorted(zip(bits["sample"][mask].tolist(), bits["device"][mask].tolist(),
                          bits["channel"][mask].tolist()))
    assert key(window, np.ones(window["sample"].size, bool)) == key(full, inside)
    assert set(window["device"].tolist()) & _SOURCES


def _dump(capture, monkeypatch, tmp_path, **env):
    path = tmp_path / "dump.txt"
    monkeypatch.setenv("SWI3S_DUMP_PATH", str(path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    d = _decode(capture)
    bits, audio = d.bit_samples(), d.audio()
    del d
    gc.collect()                               # the engine closes the dump file
    rows = [tuple(int(x) for x in line.split()) for line in path.read_text().splitlines()]
    return rows, bits, audio


def test_the_bit_dump_lists_the_bits_assembled_up_to_its_limit(capture, monkeypatch, tmp_path):
    rows, bits, audio = _dump(capture, monkeypatch, tmp_path, SWI3S_DUMP_MAX="64")
    assert len(rows) == 64
    assert [(r[0], r[1], r[2]) for r in rows] == list(zip(
        bits["sample"][:64].tolist(), bits["dp"][:64].tolist(), bits["channel"][:64].tolist()))
    # The levels are the recovered (descrambled) bits: the first whole sample a sink
    # port assembles from them is that port's first decoded sample.
    device_at = dict(zip(bits["sample"].tolist(), bits["device"].tolist()))
    first = rows[0]
    assert device_at[first[0]] in _SINKS and first[3] == 15     # the MSB of a 16-bit sample
    own = [r for r in rows if device_at[r[0]] == device_at[first[0]] and r[2] == first[2]][:16]
    assert [r[3] for r in own] == list(range(15, -1, -1))
    value = sum(r[4] << r[3] for r in own)
    sample = next(a for a in audio if a["device"] == device_at[first[0]]
                  and a["channel"] == first[2])
    assert value == sample["value"]


def test_the_bit_dump_lists_only_the_port_it_names(capture, monkeypatch, tmp_path):
    rows, _bits, _audio = _dump(capture, monkeypatch, tmp_path, SWI3S_DUMP_DP="0",
                                SWI3S_DUMP_MAX="10")
    assert len(rows) == 10 and {r[1] for r in rows} == {0}
    rows, _bits, _audio = _dump(capture, monkeypatch, tmp_path, SWI3S_DUMP_DP="5")
    assert rows == []
