"""Payload interval skipping in the demo captures (Section 14.1.10).

The PHY1/PHY2/PHY3 demos carry DP0 at 44.1 kHz on 48 kHz transport opportunities: 13 of every
160 intervals are skipped. 44100/48000 == 147/160 exactly, and 160 is the smallest denominator
that expresses it, so the rate is exact rather than approximated.

WHY DP0 AND NOT DP1. DP0 runs unscrambled. A mis-phased skip then surfaces as a wrong sample
VALUE, directly readable against the generator's own sine; on the scrambled port it would surface
as descrambler noise, which looks like a broken decode without saying where. That is the same
reasoning MakeSkippingLevels documents for its own fixture.

The value check below is the load-bearing one. A count can be right while the phase is wrong —
skip interval k instead of k+1 and you still transport 147 of 160, but you read an idle interval
as a sample and drop a real one. The sine is indexed in SAMPLES, so the decoded stream must equal
the generated sequence element for element, with no gap, across every SSP and geometry change.
"""
from __future__ import annotations

import numpy as np
import pytest
from conftest import (
    DEMO_SKIP_DENOMINATOR,
    DEMO_SKIP_NUMERATOR,
    demo_skipped_rate_hz,
    demo_transported_samples,
)

from swi3s_studio.session import Session

_SAMPLES = 1500          # >> 160, so several full skip cycles occur
_PHYS = [1, 2, 3]


@pytest.fixture(scope="module")
def sessions():
    return {phy: Session.from_demo(_SAMPLES, cold_start=True, phy=phy) for phy in _PHYS}


def _generated_sine(n: int, tone_index: int, sample_size: int = 15) -> np.ndarray:
    """Demo.cpp's sineSample, as a signed array: one cycle per 64 SAMPLES."""
    amp = (1 << sample_size) * 0.45
    idx = np.arange(n)
    v = np.rint(amp * np.sin(2 * np.pi * (tone_index + 1) * idx / 64.0)).astype(np.int64)
    v &= (1 << (sample_size + 1)) - 1
    sign = 1 << sample_size
    return np.where(v & sign, v - (1 << (sample_size + 1)), v).astype(np.int64)


def test_the_fraction_is_exact():
    """Not an approximation, and not a choice with slack in it: 147/160 is in lowest terms, so
    any other denominator is either a multiple of 160 or cannot express 44.1/48 at all."""
    kept = DEMO_SKIP_DENOMINATOR - DEMO_SKIP_NUMERATOR
    assert 48_000 * kept / DEMO_SKIP_DENOMINATOR == 44_100.0
    assert np.gcd(kept, DEMO_SKIP_DENOMINATOR) == 1, "147/160 must be in lowest terms"


@pytest.mark.parametrize("phy", _PHYS)
def test_dp0_reports_44100_and_dp1_still_reports_48000(sessions, phy):
    """The rate comes from the registers (RowRate / Interval / skipping), so this is the
    decoder agreeing with what the demo wrote — not a measurement."""
    rates = sessions[phy].decoder.audio_sample_rates()
    assert abs(rates[(0, 0)] - demo_skipped_rate_hz()) < 1.0, rates[(0, 0)]
    assert abs(rates[(0, 1)] - 48_000.0) < 1.0, rates[(0, 1)]


@pytest.mark.parametrize("phy", _PHYS)
def test_dp0_transports_fewer_samples_than_dp1(sessions, phy):
    """The observable consequence: the same number of opportunities, fewer transported."""
    store = sessions[phy].audio_store()
    dp0 = store.samples(0, 0, next(iter(store.channels(0, 0))))
    dp1 = store.samples(0, 1, next(iter(store.channels(0, 1))))
    lo, hi = demo_transported_samples(_SAMPLES)
    assert lo <= len(dp0) <= hi, f"dp0 {len(dp0)}, expected {lo}..{hi}"
    assert len(dp1) == _SAMPLES, f"dp1 {len(dp1)} — the non-skipping port must lose nothing"
    assert len(dp0) < len(dp1)


@pytest.mark.parametrize("phy", _PHYS)
def test_every_transported_value_matches_the_generated_sine(sessions, phy):
    """THE assertion. A skipped interval must advance nothing: the decoded stream equals the
    generated sequence element for element. Skip the wrong interval and the very first
    misplacement reads an idle interval (value 0, or the previous sample held) where a real
    sample was, and every element after it is offset.

    Exact equality, not an SNR floor — these are integers off a deterministic generator, and a
    tolerance would hide precisely the one-sample slip this exists to catch."""
    store = sessions[phy].audio_store()
    ch = next(iter(store.channels(0, 0)))
    got = np.asarray(store.samples(0, 0, ch), dtype=np.int64)
    assert got.size > 4 * DEMO_SKIP_DENOMINATOR, (
        f"only {got.size} samples — fewer than four skip cycles, so a phase error late in the "
        "pattern could not show up")
    want = _generated_sine(got.size, tone_index=0 * 2 + ch)
    bad = int(np.count_nonzero(got != want))
    assert bad == 0, (
        f"phy{phy} dp0: {bad}/{got.size} decoded samples differ from the generated sine — "
        f"first at index {int(np.argmax(got != want))}. A wrong skip PHASE, not a wrong count.")


@pytest.mark.parametrize("phy", _PHYS)
def test_the_demo_decodes_without_an_unexpected_ssp(sessions, phy):
    """A skipping port's legal SSP rows are Interval x SkippingDenominator apart, not Interval
    (Section 9.1.6.2.1). The demo's SSPA scheduler already multiplies by the denominator; if it
    did not, an SSPA would land part-way through the pattern and the decoder would flag it."""
    s = sessions[phy]
    flagged = [c for c in s.commands if c.get("unexpected_ssp") or c.get("unexpectedSsp")]
    assert not flagged, f"phy{phy}: {len(flagged)} command(s) flagged an unexpected SSP"
    errs = [c for c in s.commands if c.get("error")]
    assert not errs, f"phy{phy}: {len(errs)} command(s) decoded with an error"


def test_the_flow_control_demo_does_not_skip():
    """Left alone deliberately: its ports gate transport on DRQ/SourceReady, so skipping would
    confound two independent reasons for an interval to carry nothing."""
    s = Session.from_demo(400, phy=2, variant="flow_control")
    rates = s.decoder.audio_sample_rates()
    assert rates, "flow-control demo reported no audio streams"
    for (dev, dp), hz in rates.items():
        assert abs(hz - demo_skipped_rate_hz()) > 1.0, (
            f"flow dev{dev} dp{dp} reports {hz:.1f} Hz — the flow demo must not skip")
