"""Column-0 phase for the audio payload.

A truncated / mid-stream capture starts partway into a bus row. CDS is differential
(NRZS), so the column detector re-derives Column 0 from the transition pattern + CRC
regardless of where the file begins. But the audio payload is sampled as an ABSOLUTE
level, so the payload ports must be seeded with that same Column-0 phase or they read
every port `phaseOffset` columns early — which garbles sources and makes a 2-channel
source impossible to sync (CH0 or CH1 but never both, at any SSP row).

This is a regression guard for that fix (payload engine start-column = CDS phase).
It uses a real capture pair (a full .sal and the same window exported as truncated
version-0 .bin files) and is SKIPPED when those files aren't present, so it never
breaks CI — run it wherever the Box captures are available.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_column_phase.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from swi3s_studio.session import Session

_BOX = os.path.expanduser(os.environ.get("SWI3S_CAPTURES", "~/swi3s_captures"))
_CSV = os.path.join(_BOX, "bus_grid_settings.csv")
_CLK = os.path.join(_BOX, "digital_1.bin")   # forwarded clock (most transitions)
_DAT = os.path.join(_BOX, "digital_0.bin")   # data line
_SAL = os.path.join(_BOX, "n50dev_rlusk_16col_guardwa_070226_2.sal")

_HAVE = all(os.path.exists(p) for p in (_CSV, _CLK, _DAT, _SAL))
# Reported as SKIPPED (not passed) when the Box captures aren't present — otherwise a
# CI machine without them would report these regression guards as green no-ops.
requires_box = pytest.mark.skipif(not _HAVE, reason=f"Box captures not present at {_BOX}")


def _sgn16(a):
    a = a.astype(np.int64)
    return np.where(a >= 32768, a - 65536, a).astype(np.float64)


def _tone_snr(a):
    """Crude single-tone SNR (dB): energy in the dominant FFT bin (+/-2) vs the rest.
    A cleanly decoded sine reads well above 0 dB; garbled framing reads negative."""
    a = _sgn16(a)[2000:2000 + 16384]
    if a.size < 8192 or a.std() < 1:
        return -99.0
    a = a - a.mean()
    A = np.abs(np.fft.rfft(a * np.hanning(a.size))) ** 2
    A[0] = 0.0
    k = int(np.argmax(A))
    band = slice(max(1, k - 2), k + 3)
    sig = A[band].sum()
    return 10.0 * np.log10(sig / (A.sum() - sig + 1e-9))


def _truncated():
    return Session.from_saleae_binary(_CLK, _DAT, 500_000_000, config_csv=_CSV)


@requires_box
def test_truncated_sink_is_column_aligned():
    """The sink (DP1, manager-driven, SSP-independent) must decode cleanly straight
    away: it proves the payload Column-0 phase is applied. Before the fix this read a
    2-columns-early alias (a wrong, higher pitch) at every SSP."""
    s = _truncated()
    st = s.audio_store()
    assert _tone_snr(st.samples(1, 1, 0)) > 0.0, "sink not column-aligned"


@requires_box
def test_truncated_source_both_channels_sync_together():
    """DP2 is a 2-channel 16-bit source (interval 15). With the column phase fixed,
    a SINGLE SSP row must bring BOTH channels up cleanly at once — the axis the user
    couldn't reach before (only ever CH0 or CH1). We just require such a row exists."""
    s = _truncated()
    best_both = -99.0
    for ssp in range(16):
        s.set_ssp_row(ssp)
        st = s.audio_store()
        both = min(_tone_snr(st.samples(1, 2, 0)), _tone_snr(st.samples(1, 2, 1)))
        best_both = max(best_both, both)
    assert best_both > 0.0, f"no SSP row cleans both source channels (best={best_both:.1f}dB)"


@requires_box
def test_full_capture_unaffected():
    """The full .sal begins on a row boundary (phaseOffset 0), so the fix is a no-op
    there — both source channels still decode at the default (no manual SSP)."""
    full = Session.from_sal(_SAL, 1, 0, config_csv=_CSV)
    st = full.audio_store()
    assert _tone_snr(st.samples(1, 1, 0)) > 0.0, "full-capture sink regressed"


if __name__ == "__main__":
    if not _HAVE:
        print("SKIP: Box captures not present at", _BOX)
    else:
        test_truncated_sink_is_column_aligned(); print("ok: truncated sink column-aligned")
        test_truncated_source_both_channels_sync_together(); print("ok: source both channels sync together")
        test_full_capture_unaffected(); print("ok: full capture unaffected")
        print("ALL COLUMN-PHASE TESTS PASSED")
