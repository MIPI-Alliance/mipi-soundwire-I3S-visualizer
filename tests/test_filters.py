"""The audio high-pass against SciPy: design, steady-state start, and zero-phase filtering.

`swi3s_studio.dsp.filters` follows scipy.signal step for step without depending on it.
These compare it with outputs SciPy produced for a fixed synthetic signal over corners
from 1 Hz to just under Nyquist, stored in tests/fixtures/highpass_reference.json.
Regenerate them (SciPy required, and only with a reviewed diff):

    python3 tests/test_filters.py --regen
"""
import json
import os
import sys

import numpy as np
import pytest

from swi3s_studio.dsp import filters

_REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                    "highpass_reference.json")
# (rate Hz, corner Hz): the UI's range ends, a typical glitch hunt, and the Nyquist limit at
# a low rate.
_CASES = [(48_000, 1.0), (48_000, 20.0), (48_000, 1_000.0), (48_000, 20_000.0),
          (8_000, 3_990.0), (16_000, 50.0)]
_N = 600
# Agreement to 1 part in 1e8. At a 1 Hz corner at 48 kHz the poles sit within 1.3e-4 of the
# unit circle, so rounding differences between two correct implementations grow by about
# that factor; measured, they reach 1.1e-9 of full scale there and at most 1e-11 at the
# other corners. On a 16-bit stream 1e-8 of full scale is a three-thousandth of one step.
_TOL = 1e-8


def _signal(n=_N):
    """DC, a slow drift, two tones, a step and a one-sample click: something for every
    corner to remove or keep. A formula, not a random draw, so it does not depend on an RNG."""
    t = np.arange(n, dtype=np.float64)
    x = 1200.0 + 0.8 * t + 3000.0 * np.sin(2 * np.pi * t / 97.0) + 700.0 * np.sin(t * 1.9)
    x[n // 3:] += 2500.0
    x[n // 2] += 9000.0
    return x


def _regen():
    from scipy import signal
    out = []
    for fs, fc in _CASES:
        sos = signal.butter(filters.ORDER, fc, "highpass", fs=fs, output="sos")
        pad = min(filters.highpass_padlen(sos, fc, fs), _N - 1)
        out.append({"rate": fs, "cutoff": fc, "sos": sos.tolist(),
                    "zi": signal.sosfilt_zi(sos).tolist(),
                    "y": signal.sosfiltfilt(sos, _signal()).tolist(),
                    "padlen": pad,
                    "y_highpass": signal.sosfiltfilt(sos, _signal(), padlen=pad).tolist()})
    with open(_REF, "w", encoding="utf-8") as f:
        json.dump({"scipy": __import__("scipy").__version__, "n": _N, "cases": out}, f,
                  indent=1)
        f.write("\n")
    print(f"wrote {_REF}: {len(out)} cases")


def _cases():
    with open(_REF, encoding="utf-8") as f:
        return json.load(f)["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['rate']}Hz-{c['cutoff']}Hz")
def test_the_design_is_scipys_butterworth(case):
    sos = filters.butter_highpass_sos(case["cutoff"], case["rate"])
    np.testing.assert_allclose(sos, case["sos"], rtol=1e-10, atol=1e-13)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['rate']}Hz-{c['cutoff']}Hz")
def test_the_initial_state_is_scipys(case):
    np.testing.assert_allclose(filters.sosfilt_zi(np.array(case["sos"])), case["zi"],
                               rtol=_TOL, atol=1e-12)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['rate']}Hz-{c['cutoff']}Hz")
def test_the_zero_phase_filter_is_scipys_sosfiltfilt(case):
    y = filters.sosfiltfilt(np.array(case["sos"]), _signal())
    scale = float(np.max(np.abs(case["y"])))
    np.testing.assert_allclose(y, case["y"], rtol=0, atol=_TOL * scale)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['rate']}Hz-{c['cutoff']}Hz")
def test_the_high_pass_is_scipys_sosfiltfilt_with_its_longer_padding(case):
    sos = np.array(case["sos"])
    assert min(filters.highpass_padlen(sos, case["cutoff"], case["rate"]), _N - 1) == \
        case["padlen"]
    y = filters.highpass(_signal(), case["cutoff"], case["rate"])
    scale = float(np.max(np.abs(case["y_highpass"])))
    np.testing.assert_allclose(y, case["y_highpass"], rtol=0, atol=_TOL * scale)


def test_the_longer_padding_keeps_the_ends_of_a_stream_clean():
    """No false glitch at the start and end from an offset or a drift: a tone on both comes
    out as the tone, right up to the first and last sample. SciPy's default padding leaves
    hundreds of counts of transient there. (The tone starts and ends on a zero crossing: a
    stream that ends far from its own level still shows the filter's edge response for a
    few periods of the corner, with any padding, which docs/USER_GUIDE.md says.)"""
    fs, t = 48_000.0, np.arange(96_001)
    tone = 1000.0 * np.sin(2 * np.pi * 1000.0 * t / fs)
    x = 4000.0 + tone + 0.5 * t
    assert np.max(np.abs(filters.highpass(x, 20.0, fs) - tone)) < 1.0
    sos = filters.butter_highpass_sos(20.0, fs)
    assert np.max(np.abs(filters.sosfiltfilt(sos, x) - tone)) > 100.0     # SciPy's default


def test_the_corner_is_3_db_down_and_the_stopband_falls_at_24_db_an_octave():
    """A 4th-order Butterworth applied twice: -6 dB at the corner (-3 dB each pass), and
    -48 dB per octave well below it (-24 dB each pass)."""
    fs, fc = 48_000.0, 1_000.0
    sos = filters.butter_highpass_sos(fc, fs)

    def gain_db(f):
        z = np.exp(1j * 2 * np.pi * f / fs)
        h = np.prod([(r[0] + r[1] / z + r[2] / z ** 2) / (r[3] + r[4] / z + r[5] / z ** 2)
                     for r in sos])
        return 20 * np.log10(abs(h) ** 2)              # forward and backward
    assert gain_db(fc) == pytest.approx(-6.02, abs=0.02)
    assert gain_db(10_000.0) == pytest.approx(0.0, abs=0.01)
    assert gain_db(100.0) - gain_db(200.0) == pytest.approx(-48.0, abs=0.5)


def test_zero_phase_leaves_a_click_where_it_happened():
    """The reason for filtfilt: the click is not moved later in time."""
    x = np.zeros(4000)
    x[2000] = 1.0
    y = filters.highpass(x, 200.0, 48_000.0)
    assert int(np.argmax(np.abs(y))) == 2000
    np.testing.assert_allclose(y[2000 - 300:2000], y[2001:2301][::-1], atol=1e-12)


def test_dc_is_removed():
    y = filters.highpass(np.full(5000, 12345.0), 20.0, 48_000.0)
    assert np.max(np.abs(y)) < 1e-6


def test_a_stream_shorter_than_the_padding_is_still_filtered():
    """SciPy refuses one no longer than its edge extension; this shrinks the extension."""
    sos = filters.butter_highpass_sos(1_000.0, 48_000.0)
    y = filters.sosfiltfilt(sos, np.array([1.0, 5.0, -2.0, 7.0]))
    assert y.shape == (4,) and np.all(np.isfinite(y))
    np.testing.assert_array_equal(filters.sosfiltfilt(sos, np.array([3.0])), [3.0])
    assert filters.sosfiltfilt(sos, np.zeros(0)).shape == (0,)


def test_the_corner_range_follows_the_rate():
    assert filters.max_cutoff_hz(48_000.0) == 20_000.0
    assert filters.max_cutoff_hz(8_000.0) == pytest.approx(3_992.0)
    with pytest.raises(ValueError):
        filters.butter_highpass_sos(4_000.0, 8_000.0)
    with pytest.raises(ValueError):
        filters.butter_highpass_sos(1_000.0, 48_000.0, order=3)


def test_the_native_cascade_matches_the_textbook_recursion():
    """swi3score.sosfilt against transposed direct form II written out in Python."""
    rng = np.random.default_rng(7)
    sos = filters.butter_highpass_sos(300.0, 16_000.0)
    x = rng.standard_normal(257)
    zi = rng.standard_normal((2, 2))
    y, zf = filters.sosfilt(sos, x, zi)
    want = x.copy()
    state = zi.copy()
    for s, (b0, b1, b2, _a0, a1, a2) in enumerate(sos):
        z0, z1 = state[s]
        for i, v in enumerate(want):
            out = b0 * v + z0
            z0, z1 = b1 * v - a1 * out + z1, b2 * v - a2 * out
            want[i] = out
        state[s] = z0, z1
    np.testing.assert_allclose(y, want, rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(zf, state, rtol=1e-12, atol=1e-12)


def test_the_native_cascade_refuses_malformed_input():
    import swi3score
    with pytest.raises(ValueError):
        swi3score.sosfilt(np.zeros((1, 5)), np.zeros(3), np.zeros((1, 2)))
    with pytest.raises(ValueError):
        swi3score.sosfilt(np.zeros((1, 6)), np.zeros(3), np.zeros((2, 2)))
    with pytest.raises(ValueError):
        swi3score.sosfilt(np.array([[1.0, 0, 0, 2.0, 0, 0]]), np.zeros(3), np.zeros((1, 2)))


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen()
    else:
        print(__doc__)
