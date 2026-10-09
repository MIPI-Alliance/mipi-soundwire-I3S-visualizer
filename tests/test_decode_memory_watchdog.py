"""A decode is stopped when free memory runs low, and a stopped re-decode keeps the last.

The Open Capture dialog predicts the LOAD (est_peak_bytes, measured against RSS by
test_perf), but not the decode after it: its audio depends on the bus config, from 0.014
samples per UI (the flow-control demo) to 0.58 (PHY3), 0.17 on a user's capture, and near 1
for a 1-bit PDM port, at ~114 bytes each at the peak. A bound loose enough to be safe would
have predicted ~30 GB for that capture's 5 GB. So it is watched instead
(session.run_watched): free memory is read while the decoder runs, and below the floor
(saleae_sal.decode_memory_floor) the decode is stopped with Decoder.request_stop and
DecodeMemoryError says what happened. That is the protection the old 8 GiB budget cap was
for; it refused opens that would have fitted, where this stops only one that does not.
"""
import pytest

import swi3s_studio.session as S
from swi3s_studio.ingest import saleae_sal as ss
from swi3s_studio.session import DecodeMemoryError, Session


@pytest.fixture(scope="module")
def capture():
    return Session.from_demo(20000, phy=2, cold_start=True).capture   # ~10M UIs


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(S, "_WATCH_PERIOD_S", 0.001)


def _memory(monkeypatch, avail, floor):
    monkeypatch.setattr(ss, "available_memory_bytes", lambda: int(avail))
    monkeypatch.setattr(ss, "decode_memory_floor", lambda: int(floor))


def test_a_decode_is_stopped_when_free_memory_runs_low(monkeypatch, quick, capture):
    _memory(monkeypatch, avail=1 << 30, floor=2 << 30)
    with pytest.raises(DecodeMemoryError) as err:
        Session(capture)
    assert 0.0 <= err.value.progress < 0.5, "it ran on rather than stopping"
    assert "1.1 GB" in str(err.value) and "2.1 GB" in str(err.value)
    assert "From … to …" in str(err.value), "the message does not say what to do"


def test_with_memory_to_spare_it_decodes_as_before(monkeypatch, quick, capture):
    plain = Session(capture)
    _memory(monkeypatch, avail=1 << 40, floor=1 << 30)
    watched = Session(capture)
    assert watched.audio_count == plain.audio_count > 0
    assert len(watched.commands) == len(plain.commands)


def test_the_audio_copy_is_checked_before_it_is_made(monkeypatch, quick, capture):
    """Copying the audio out as columns is the largest allocation after the decode and
    cannot be stopped once begun, so it is sized first (Decoder.audio_count)."""
    floor = 1 << 30
    _memory(monkeypatch, avail=floor + 1000, floor=floor)    # never below the floor...
    with pytest.raises(DecodeMemoryError) as err:            # ...until the copy is added
        Session(capture)
    assert err.value.progress == 1.0


def test_a_stopped_re_decode_keeps_the_last_decode(monkeypatch, quick, capture):
    s = Session(capture)
    before = (len(s.commands), s.audio_count, [dict(g) for g in s.segments])
    decoder = s.decoder
    _memory(monkeypatch, avail=1 << 30, floor=2 << 30)
    with pytest.raises(DecodeMemoryError):
        s.force_column_count_at(int(s.segments[1]["start_sample"]) + 500, 12)
    assert s.decoder is decoder and not s.decoder.stopped
    assert (len(s.commands), s.audio_count, [dict(g) for g in s.segments]) == before
    assert len(s.decoder.commands()) == before[0], "the restored decoder lost its source"


def test_no_reading_means_no_watchdog(monkeypatch, capture):
    """Free memory unreadable: the floor is 0 and the decode runs unwatched (the load's
    guard still applies, at its degraded budget)."""
    monkeypatch.setattr(ss, "available_memory_bytes", lambda: 0)
    assert ss.decode_memory_floor() == 0
    assert Session(capture).audio_count > 0


def test_the_floor_is_a_tenth_of_ram_and_at_least_a_gib(monkeypatch):
    monkeypatch.setattr(ss, "available_memory_bytes", lambda: 1 << 30)
    for total, floor in ((4 << 30, 1 << 30), (64 << 30, int((64 << 30) * 0.1))):
        monkeypatch.setattr(ss, "total_memory_bytes", lambda t=total: t)
        assert ss.decode_memory_floor() == floor
