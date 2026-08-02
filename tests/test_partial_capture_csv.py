"""Join-late partial captures: no config on the wire, geometry supplied by a CSV.

This is the workflow a user hits with a real capture started after the bus was already
running: the setup `WriteA32`s and the commit happened before recording began, so the
decoder has nothing to snoop. Port geometry comes from an imported Visualizer config
CSV instead, and (since 3.0.11) the SSP is recovered from an SSPA.

Existing `test_config_csv.py` coverage applies a CSV to a FULL demo — where the config
commands are still on the wire, so the decoder would have found them anyway. That
exercises the CSV plumbing but never the join-late case: nothing failed if the CSV was
quietly ignored. These tests build the real thing with `Capture.subcapture`, starting
inside the 16-column audio region, and assert the capture is genuinely configless first.
"""
import os
import tempfile

import numpy as np
import pytest

from swi3s_studio.model.bus_config import BusConfig
from swi3s_studio.session import Session

_SAMPLES = 6000          # deep enough that the 16-col region holds an SSPA


def _tone_snr(a) -> float:
    a = np.asarray(a, dtype=np.float64)
    if a.size < 512:
        return -99.0
    a = a - a.mean()
    w = np.hanning(min(4096, a.size))
    A = np.abs(np.fft.rfft(a[:w.size] * w)) ** 2
    A[0] = 0.0
    k = int(np.argmax(A))
    sig = A[max(1, k - 2):k + 3].sum()
    return 10.0 * np.log10(sig / (A.sum() - sig + 1e-9))


@pytest.fixture(scope="module")
def full():
    return Session.from_demo(_SAMPLES, phy=2, cold_start=True)


@pytest.fixture(scope="module")
def joined_late(full):
    """The capture as if recording started mid-audio: everything from 20 000 samples
    into the 16-column region onward, rebased to 0. Uses the app's own
    Capture.subcapture — the same code path Export Capture uses for a range."""
    t0 = int(full.segments[-1]["start_sample"]) + 20000
    end = int(full.capture.clock_edges[-1])
    return full.capture.subcapture(t0, end)


@pytest.fixture(scope="module")
def config_csv(full):
    """The demo's own operational config, exported as a Visualizer CSV — exactly what a
    user would hand the Analyzer for a capture whose setup they missed."""
    t0 = int(full.segments[-1]["start_sample"]) + 60000
    cfg, _n = BusConfig.from_decoder_config(full.config_dataports_at(t0))
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    cfg.to_csv_file(path)
    yield path
    os.remove(path)


def _pcm(store):
    for dev, dp in store.streams():
        for ch in store.channels(dev, dp):
            if store.native_sample_bits(dev, dp, ch) != 1:
                yield dev, dp, ch


def test_the_slice_really_has_no_config(joined_late):
    """Precondition. If the slice still carried the setup writes or the commit, every
    test below would pass with the CSV doing nothing."""
    s = Session(joined_late)
    assert not [c for c in s.commands if c.get("command") == "WriteA32"], \
        "slice still contains config writes — not a join-late capture"
    assert not [c for c in s.commands if c.get("is_commit")], \
        "slice still contains a commit"


def test_without_a_csv_there_is_no_audio(joined_late):
    """Nothing on the wire says which columns carry which port, so no stream decodes.
    This is the state the CSV import exists to rescue."""
    s = Session(joined_late)
    assert s.audio_store().streams() == []


def test_csv_at_open_recovers_every_stream(joined_late, config_csv):
    """Supplying the config at open reconstructs all four data ports."""
    s = Session(joined_late, config_csv=config_csv)
    store = s.audio_store()
    assert len(store.streams()) == 4, store.streams()


def test_csv_applied_later_recovers_every_stream(joined_late, config_csv):
    """The other route: open the capture bare, then File ▸ Import Visualizer CSV."""
    s = Session(joined_late)
    assert s.audio_store().streams() == []
    s.apply_config_csv(config_csv)
    assert len(s.audio_store().streams()) == 4


def test_recovered_audio_is_clean_end_to_end(joined_late, config_csv):
    """The point of the whole workflow: the PCM streams decode as clean tones across the
    WHOLE slice, not just the part after the SSPA (the SSP lock is what buys the
    pre-SSPA half — see test_sspa_ssp_lock.py)."""
    s = Session(joined_late, config_csv=config_csv)
    store = s.audio_store()
    checked = 0
    for dev, dp, ch in _pcm(store):
        a = store.samples(dev, dp, ch)
        half = len(a) // 2
        assert _tone_snr(a[:half]) > 20.0, f"dev{dev} dp{dp} ch{ch}: first half garbled"
        assert _tone_snr(a[half:]) > 20.0, f"dev{dev} dp{dp} ch{ch}: second half garbled"
        checked += 1
    assert checked >= 2, f"expected at least two PCM streams, checked {checked}"


def test_geometry_matches_the_original_capture(joined_late, config_csv, full):
    """The recovered decode must agree with the full capture it was cut from: same
    column count, same set of streams."""
    s = Session(joined_late, config_csv=config_csv)
    assert s.column_count == full.column_count
    assert set(s.audio_store().streams()) == set(full.audio_store().streams())


def test_recovered_audio_matches_the_full_capture(joined_late, config_csv, full):
    """Strongest form: sample VALUES from the join-late decode must match the same
    stream decoded from the complete capture. Compares a window from the middle of the
    slice against the corresponding window of the original, which pins the phase as well
    as the geometry."""
    s = Session(joined_late, config_csv=config_csv)
    part, whole = s.audio_store(), full.audio_store()
    compared = 0
    for dev, dp, ch in _pcm(part):
        a = np.asarray(part.samples(dev, dp, ch))
        b = np.asarray(whole.samples(dev, dp, ch))
        if a.size < 600 or b.size < a.size:
            continue
        # The slice starts partway through, so align by finding a's mid-window in b.
        probe = a[a.size // 2: a.size // 2 + 256]
        tail = b[-(a.size + 8):]
        hit = None
        for off in range(0, tail.size - probe.size):
            if np.array_equal(tail[off:off + probe.size], probe):
                hit = off
                break
        assert hit is not None, f"dev{dev} dp{dp} ch{ch}: recovered samples not found in the original"
        compared += 1
    assert compared >= 2, f"compared only {compared} streams"


def test_workspace_round_trip_keeps_the_recovery(joined_late, config_csv):
    """A join-late session is only useful if it reopens the same way: the CSV path is
    persisted, and the SSP is re-derived from the SSPA on load."""
    import json


    s = Session(joined_late, config_csv=config_csv)
    assert s.ssp_locked_from_sspa
    before = {k: len(s.audio_store().samples(*k)) for k in s.audio_store()._channels}

    # Only the descriptor round-trips; the capture itself is supplied by the caller in
    # this synthetic case, so rebuild the session the same way and re-apply the source.
    src = json.loads(json.dumps(s.source))
    assert src.get("config_csv") == config_csv
    s2 = Session(joined_late, config_csv=src["config_csv"])
    assert s2.ssp_locked_from_sspa
    assert {k: len(s2.audio_store().samples(*k)) for k in s2.audio_store()._channels} == before
