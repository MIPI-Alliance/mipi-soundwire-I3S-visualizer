"""Headless tests for _PcmSource, the pull-mode QAudioSink source behind
glitch-free playback and the 50 ms stop-fade. No audio device is required: we
drive readData() directly and check the byte stream it would hand the sink.

Verifies the three properties the fade redesign depends on:
  1. It reproduces the PCM exactly, then emits silence forever (never a short
     read -> the sink can't underrun or hit end-of-data).
  2. A partial block at the clip end is filled with silence (still full-length).
  3. stop_fade() applies a smooth, monotonic 1->0 ramp that reaches true zero by
     `fade_frames` and stays there.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.audio_view import _PcmSource

_app = QApplication.instance() or QApplication([])
NCH, SB = 2, 2          # stereo int16
BPF = NCH * SB


def _read_frames(src, nframes):
    """Read exactly nframes frames as an (nframes, NCH) int16 array, across as many
    readData calls as the source chooses to split them into."""
    out = bytearray()
    want = nframes
    while want > 0:
        chunk = src.readData(want * BPF)
        if not chunk:
            break
        out += bytes(chunk)
        want -= len(chunk) // BPF
    return np.frombuffer(bytes(out), dtype=np.int16).reshape(-1, NCH)


def test_reproduces_pcm_then_silence():
    n = 100
    frames = np.empty((n, NCH), dtype=np.int16)
    frames[:, 0] = np.arange(n)
    frames[:, 1] = 1000 + np.arange(n)
    src = _PcmSource(frames.tobytes(), NCH, SB, fade_frames=10)
    assert src.total_frames == n

    got = _read_frames(src, n + 40)            # read past the end
    assert np.array_equal(got[:n], frames), "PCM not reproduced exactly"
    assert np.array_equal(got[n:], np.zeros((40, NCH), np.int16)), "tail not silent"


def test_partial_block_at_end_is_padded():
    n = 5
    frames = np.full((n, NCH), 7, dtype=np.int16)
    src = _PcmSource(frames.tobytes(), NCH, SB, fade_frames=4)
    # One oversized read that straddles the end: real frames then silence, full len.
    got = np.frombuffer(bytes(src.readData(20 * BPF)), dtype=np.int16).reshape(-1, NCH)
    assert len(got) == 20
    assert np.array_equal(got[:n], frames)
    assert np.array_equal(got[n:], np.zeros((20 - n, NCH), np.int16))


def test_stop_fade_is_monotonic_and_hits_zero():
    n, ff = 200, 10
    frames = np.full((n, NCH), 1000, dtype=np.int16)
    src = _PcmSource(frames.tobytes(), NCH, SB, fade_frames=ff)
    src.stop_fade()                            # fade from frame 0
    got = _read_frames(src, ff + 20)
    col = got[:, 0].astype(int)
    assert col[0] == 1000, "fade should start at unity"
    assert np.all(np.diff(col[: ff + 1]) <= 0), "ramp must be non-increasing"
    assert np.all(col[ff:] == 0), "must be silent by fade_frames and stay silent"


def test_stop_fade_is_idempotent():
    src = _PcmSource(np.zeros((50, NCH), np.int16).tobytes(), NCH, SB, fade_frames=8)
    _read_frames(src, 5)
    src.stop_fade()
    at = src.fade_at
    src.stop_fade()                            # second call must not move the ramp start
    assert src.fade_at == at == 5


if __name__ == "__main__":
    test_reproduces_pcm_then_silence()
    test_partial_block_at_end_is_padded()
    test_stop_fade_is_monotonic_and_hits_zero()
    test_stop_fade_is_idempotent()
    print("AUDIO SOURCE TESTS PASSED")
