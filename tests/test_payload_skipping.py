"""Payload Interval Skipping (SWI3S Section 14.1.10) through the decode.

A skipping port programs a transport Interval FASTER than its sample rate and then leaves
some of those intervals empty, so the average rate comes out right — 44.1 kHz carried on
48 kHz intervals with Numerator 13 / Denominator 160, for instance. Which intervals are
empty follows a fractional accumulator: each interval adds N, and an interval whose
accumulator reaches D is skipped (D subtracted). Source and sink run the identical
accumulator, re-initialized by every SSP, so they agree without signalling.

`make_skipping_levels` carries a RAMP — sample n has value n — rather than a sine, because
the failure being guarded against is a decode that skips the WRONG interval. That reads an
idle interval as data and discards the sample really transported next door, which a sine
hides (it still looks like audio) and a ramp cannot (it stops counting). The generator
Initialize()s its ports and never SyncToSSP()s them, so the wire's skip phase comes from
the path the placement cross-check pins against the normative Python model — the decode's
SSP re-anchor has to AGREE with that rather than share a mistake with it.
"""
import swi3score

from swi3s_studio.analysis import errors

# The fixture's bus: 8 columns at a 24.576 MHz UI rate (98.304 MHz capture, 4 samples/UI).
_RATE_HZ, _SAMPLES_PER_UI = 98_304_000, 4
# 32-Row intervals on an 8-column bus -> 3.072 MRows/s / 32 = 96 kHz of transport
# opportunities, of which (D - N) / D actually carry a sample.
_INTERVAL_HZ = 96_000.0


def _decode(samples=200, numerator=13, denominator=160, misaligned_sspa=False):
    levels = swi3score.make_skipping_levels(samples, numerator, denominator, misaligned_sspa)
    src = swi3score.MemorySampleSource(levels, _RATE_HZ, _SAMPLES_PER_UI)
    dec = swi3score.Decoder(src, swi3score.DecoderSettings())
    dec.run()
    return dec


def _ramp(dec):
    """Decoded values in transport order."""
    return [a["value"] for a in sorted(dec.audio(), key=lambda a: a["start_sample"])]


def _prefix_len(values):
    """How many leading samples count up 0, 1, 2, ... — i.e. how far the decode held."""
    return next((i for i, v in enumerate(values) if v != i), len(values))


def test_skipping_decodes_every_transported_sample_and_no_others():
    """The whole feature, end to end: with N=13/D=160 the decode must return the ramp the
    device put on the wire, unbroken.

    REGRESSION: CDataPort::SyncToSSP cleared the skipping accumulator without starting the
    interval that begins at the SSP, so that interval never spent its `A += Numerator`
    step and every later skip decision landed ONE INTERVAL LATE. The decode read the
    interval the device had skipped (idle bus) and skipped the next one — dropping a real
    sample per skip. This broke at sample 12 (the first skip), returning 0x3333, the idle
    zebra read as a sample."""
    values = _ramp(_decode())
    assert len(values) >= 200, f"only {len(values)} samples decoded"
    assert _prefix_len(values) == len(values), (
        f"ramp broke at sample {_prefix_len(values)}: "
        f"got {values[_prefix_len(values)]}, want {_prefix_len(values)}")


def test_the_skipped_intervals_are_the_ones_the_accumulator_names():
    """Not just the right VALUES but the right INTERVALS: consecutive samples sit one
    interval apart except across a skip, where they sit two. So the doubled gaps must
    number N per D intervals — 13 skips per 160 here, i.e. 13 per 147 samples."""
    dec = _decode(samples=400)
    starts = [a["start_sample"] for a in sorted(dec.audio(), key=lambda a: a["start_sample"])]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    one = min(gaps)
    skipped = [g for g in gaps if g > 1.5 * one]
    assert all(abs(g - 2 * one) < 0.1 * one for g in skipped), \
        f"a gap that is neither one interval nor two: {sorted(set(gaps))}"
    # 13 skips per 147 transported samples, +-1 for where the capture happens to end.
    want = len(gaps) * 13 / 147
    assert abs(len(skipped) - want) <= 2, \
        f"{len(skipped)} skipped intervals in {len(gaps)}; want ~{want:.0f}"


def test_a_zero_numerator_transports_every_interval():
    """Numerator 0 means no skipping whatever the Denominator says, so the identity case
    must decode the same ramp with EVERY interval carrying a sample — one interval's gap
    throughout. Guards the skipping branch against altering a plain port."""
    dec = _decode(samples=120, numerator=0)
    values = _ramp(dec)
    assert _prefix_len(values) == len(values), f"ramp broke at {_prefix_len(values)}"
    starts = [a["start_sample"] for a in sorted(dec.audio(), key=lambda a: a["start_sample"])]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert max(gaps) < 1.5 * min(gaps), f"an interval was skipped with Numerator 0: {max(gaps)}"


def test_the_reported_sample_rate_is_the_skipped_rate_not_the_interval_rate():
    """A skipping port's audio rate is the interval rate scaled by (D - N) / D — 88.2 kHz
    out of 96 kHz of opportunities here. Reporting the raw interval rate would mislabel
    every WAV export and rate readout by the skip fraction."""
    rates = _decode().audio_sample_rates()
    assert list(rates) == [(0, 0)], f"unexpected ports: {list(rates)}"
    got = rates[(0, 0)]
    want = _INTERVAL_HZ * (160 - 13) / 160
    # The rate is derived from the MEASURED UI rate, which drifts a little across the
    # capture, so compare in relative terms — the skip fraction is a 8.1 % effect, orders
    # above this tolerance.
    assert abs(got - want) / want < 5e-4, f"reported {got} Hz, want {want} Hz"


def test_an_ssp_on_the_pattern_boundary_is_not_flagged_and_does_not_disturb_the_phase():
    """The fixture's periodic SSPA lands where the accumulated skipping is back at 0 — a
    legal SSP — so re-anchoring must be a no-op: no error, and the ramp runs through it.

    This is also what pins the pattern period. The fixture places its SSPAs on multiples of
    the enabled ports' pattern period (SwI3sConfig::patternRows — Interval x
    SkippingDenominator for a skipping port, 32 x 160 = 5120 Rows here). Were that period
    still one Interval, as it was before, the SSPA would land part-way through the skip
    pattern and this test would fail on both counts."""
    dec = _decode(samples=400)
    assert any(c["command"] == "SSPA" for c in dec.commands()), "fixture emitted no SSPA"
    flagged = [c["command"] for c in dec.commands() if c.get("unexpected_ssp_error")]
    assert not flagged, f"a pattern-aligned SSP was flagged: {flagged}"
    values = _ramp(dec)
    assert _prefix_len(values) == len(values), f"the SSPA moved the phase at {_prefix_len(values)}"


def test_an_ssp_part_way_through_the_skip_pattern_is_reported():
    """A skipping port's pattern repeats every Interval x SkippingDenominator Rows, not
    every Interval, so an SSP on a merely interval-aligned row restarts the pattern
    part-way through and re-phases which intervals transport. Section 9.1.6.2.1 calls that
    an unexpected SSP; the decode has to say so rather than silently garble the audio it
    then reports."""
    dec = _decode(samples=400, misaligned_sspa=True)
    flagged = [c for c in dec.commands() if c.get("unexpected_ssp_error")]
    assert [c["command"] for c in flagged] == ["SSPA"], \
        f"expected the SSPA flagged, got {[c['command'] for c in flagged]}"
    assert errors.command_error(flagged[0]) == "SSP mid skip pattern (accumulated skipping != 0)"
    assert errors.is_error(flagged[0])
    # And the flag is earned: the audio really does stop tracking at the misplaced SSP.
    values = _ramp(dec)
    assert _prefix_len(values) < len(values), \
        "a mid-pattern SSP was flagged but the audio was unaffected — check the fixture"
