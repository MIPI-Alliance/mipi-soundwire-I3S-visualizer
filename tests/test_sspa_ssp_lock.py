"""SSPA-derived SSP lock for partial captures, applied backwards.

A partial capture starts after the bus was configured, so the setup `WriteA32`/commit
sequence — and with it the Stream Sync Point — is not on the wire. Port geometry can be
supplied with an imported config CSV, but the SSP cannot: a port with Interval > 1
transports on 1 of N rows and the CSV doesn't say which, so the audio decodes at an
arbitrary phase (only 1/N rows land right).

An SSPA in the capture announces a real SSP, so it can supply that anchor. The decoder
already re-anchors when it *reaches* one — but that only fixes rows from the SSPA
onward; everything before it still decodes at the arbitrary phase. Feeding the row
through the manual-SSP path reduces it modulo the LCM of the ports' interval periods, so
the phase holds from row 0 and the WHOLE capture decodes cleanly. Measured on the sliced
demo below: the pre-SSPA region goes from about -14 dB tone SNR to about +38 dB.
"""
import os
import tempfile

import numpy as np
import pytest

from swi3s_studio.ingest.capture import Capture
from swi3s_studio.model.bus_config import BusConfig
from swi3s_studio.session import Session

_SAMPLES = 6000          # deep enough that the 16-col region holds an SSPA


def _tone_snr(a) -> float:
    """Single-tone SNR (dB): dominant FFT bin vs the rest. A correctly-phased PCM stream
    reads well above 0; one decoded at the wrong interval phase collapses below it."""
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


def _slice_from(cap: Capture, t0: int) -> Capture:
    """The capture from sample `t0` onward, re-based to 0 — a synthetic partial capture.

    NOTE the field order: Capture is (clock_edges, data_edges, initial_clock,
    initial_data, sample_rate_hz). Passing clock/data the other way round decodes as
    noise, which is easy to mistake for a decoder bug."""
    ce = np.ascontiguousarray(cap.clock_edges[cap.clock_edges >= t0] - t0)
    de = np.ascontiguousarray(cap.data_edges[cap.data_edges >= t0] - t0)
    ic = bool(cap.initial_clock) ^ (int(np.searchsorted(cap.clock_edges, t0)) & 1)
    idd = bool(cap.initial_data) ^ (int(np.searchsorted(cap.data_edges, t0)) & 1)
    return Capture(ce, de, ic, idd, cap.sample_rate_hz)


@pytest.fixture(scope="module")
def full():
    return Session.from_demo(_SAMPLES, phy=2, cold_start=True)


@pytest.fixture(scope="module")
def config_csv(full):
    """The demo's own 16-col config, exported as a Visualizer CSV — what a user would
    import to give a partial capture its geometry."""
    seg = full.segments[-1]
    cfg, _n = BusConfig.from_decoder_config(
        full.config_dataports_at(int(seg["start_sample"]) + 60000))
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    cfg.to_csv_file(path)
    yield path
    os.remove(path)


@pytest.fixture(scope="module")
def partial_capture(full):
    """The 16-col audio region from 20 000 samples in — no commit, but an SSPA later on."""
    seg = full.segments[-1]
    return _slice_from(full.capture, int(seg["start_sample"]) + 20000)


def _pcm_streams(store):
    for dev, dp in store.streams():
        for ch in store.channels(dev, dp):
            if store.native_sample_bits(dev, dp, ch) != 1:      # skip PDM density streams
                yield dev, dp, ch


def test_partial_capture_has_an_sspa_but_no_commit(partial_capture, config_csv):
    """Precondition for everything below: this really is the partial-capture case —
    a sync-point SSPA is present and no confirmed commit is."""
    s = Session(partial_capture, config_csv=config_csv)
    assert any(c.get("command") == "SSPA" for c in s.commands), "no SSPA in the slice"
    assert not any(c.get("is_commit") and c.get("commit_confirmed") for c in s.commands), \
        "slice still contains a commit — it isn't a partial capture"


def test_sspa_ssp_row_is_derived_from_the_command_end(partial_capture, config_csv):
    """The announced SSP is Row_Delay rows after the phase ENDS. Deriving it from
    `bus_row` (the phase START) would be short by the command's length."""
    s = Session(partial_capture, config_csv=config_csv)
    sspa = next(c for c in s.commands if c.get("command") == "SSPA")
    expected = s.bus_row_for_sample(int(sspa["end_sample"])) + 1 + int(sspa["row_delay"])
    assert s.sspa_ssp_row() == expected
    assert s.sspa_ssp_row() > int(sspa["bus_row"]), "must be past the phase start"


def test_partial_capture_auto_locks_from_the_sspa(partial_capture, config_csv):
    s = Session(partial_capture, config_csv=config_csv)
    assert s.ssp_locked_from_sspa is True
    assert s.ssp_row == s.sspa_ssp_row()


def test_lock_applies_backwards_as_well_as_forwards(partial_capture, config_csv):
    """THE point: with the SSPA anchor reduced modulo the interval period, the region
    BEFORE the first SSPA decodes as cleanly as the region after it."""
    s = Session(partial_capture, config_csv=config_csv)
    store = s.audio_store()
    checked = 0
    for dev, dp, ch in _pcm_streams(store):
        a = store.samples(dev, dp, ch)
        half = len(a) // 2
        before, after = _tone_snr(a[:half]), _tone_snr(a[half:])
        assert before > 20.0, f"dev{dev} dp{dp} ch{ch}: pre-SSPA SNR {before:.1f} dB"
        assert after > 20.0, f"dev{dev} dp{dp} ch{ch}: post-SSPA SNR {after:.1f} dB"
        checked += 1
    assert checked > 0, "no PCM stream checked"


def test_without_the_lock_the_pre_sspa_region_is_garbled(partial_capture, config_csv):
    """Pins the bug the lock fixes, so the test above can't pass vacuously (e.g. if the
    slice happened to start already in phase). Forcing SSP auto (-1) restores the
    decoder's own behaviour: it re-anchors only when it REACHES the SSPA, so the region
    before it is at the arbitrary phase while the region after is clean."""
    s = Session(partial_capture, config_csv=config_csv)
    assert s.ssp_locked_from_sspa, "precondition: the lock must have fired"
    s.set_ssp_row(-1)                     # explicit auto -> no derived anchor
    assert not s.ssp_locked_from_sspa
    store = s.audio_store()
    split = []
    for dev, dp, ch in _pcm_streams(store):
        a = store.samples(dev, dp, ch)
        half = len(a) // 2
        split.append((_tone_snr(a[:half]), _tone_snr(a[half:])))
    assert split, "no PCM stream checked"
    assert all(before < 10.0 for before, _after in split), \
        f"pre-SSPA region should be garbled without the lock: {split}"
    assert any(after > 20.0 for _before, after in split), \
        f"post-SSPA region should still be clean (decoder re-anchors there): {split}"


def test_a_full_capture_does_not_auto_lock(full):
    """A capture with its setup commit on the wire already has an SSP; deriving one from
    an SSPA would be redundant and would cost a second decode on every open."""
    assert full.ssp_locked_from_sspa is False
    assert full.ssp_row == -1


def test_an_explicit_ssp_row_is_not_overridden(partial_capture, config_csv):
    """A user (or a reopened workspace) that pinned a row must keep it — the derived
    anchor is a fallback, not a preference."""
    s = Session(partial_capture, config_csv=config_csv, ssp_row=7)
    assert s.ssp_row == 7
    assert s.ssp_locked_from_sspa is False


def test_derived_row_is_not_persisted(partial_capture, config_csv):
    """The anchor is derived, so it must NOT be written into the source descriptor:
    a reopen re-derives it (self-healing if the capture or CSV changes) rather than
    pinning a value that then looks user-chosen."""
    s = Session(partial_capture, config_csv=config_csv)
    assert s.ssp_locked_from_sspa
    assert s.source.get("ssp_row") is None


def test_no_lock_without_an_imposed_geometry(partial_capture):
    """With no config CSV the partial capture has no port geometry at all, so there is
    no audio to re-phase and nothing to derive."""
    s = Session(partial_capture)
    assert s.ssp_locked_from_sspa is False
    assert s.ssp_row == -1


def test_apply_config_csv_triggers_the_lock(partial_capture, config_csv):
    """The CSV is what gives a partial capture its geometry, so importing one LATER must
    also trigger the derivation — not just supplying it at open."""
    s = Session(partial_capture)
    assert s.ssp_locked_from_sspa is False
    s.apply_config_csv(config_csv)
    assert s.ssp_locked_from_sspa is True
    store = s.audio_store()
    for dev, dp, ch in _pcm_streams(store):
        a = store.samples(dev, dp, ch)
        assert _tone_snr(a[:len(a) // 2]) > 20.0
