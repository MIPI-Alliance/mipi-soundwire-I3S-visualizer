"""A DSCR that changes a running port's channel set applies it from its commit point.

SSCR and DSCR generate the same Commit operation (SWI3S v1.1 §4.9.4), which takes effect at
the commit point's Row Sync Point (§9.1.13). A DSCR generates no SSP, so the port keeps its
interval phase and skipping accumulator. Rules:

- The channels sent in an interval are exactly the enabled set, in channel order.
- A commit point on an interval boundary (before that interval's transport begins) applies
  the new set to that interval: a disabled channel sends nothing in it, and an enabled
  channel sends its first sample in it.
- A commit point after the interval's transport has begun leaves that interval on the set
  it began with; the new set applies from the next interval. The specification states no
  rule for this case; this is the rule the decoder applies.
- A channel the DSCR enables is seeded at its enable; a surviving channel's scrambler
  continues.

`make_dscr_channel_set_levels` builds one scrambled 16-bit PCM port (Columns 1-7 of an
8-column bus, 64-row Interval, a sample of each channel per interval in its first rows)
whose DSCR commit point is `samples_per_channel` intervals after the SSCR's SSP, plus
`row_in_interval` rows.
"""
import pytest
import swi3score
from test_native_decoder_run import _sine
from test_native_scrambler_reseed import _by_channel, _decode, _descramble_words

_N = 16                        # intervals before the commit point
_SS = 15                       # SampleSize field: 16-bit samples
_INTERVAL = 64 * 8 * 4         # capture samples per interval: 64 rows x 8 columns x 4 / UI


def _channels(mask):
    return [ch for ch in range(3) if mask & (1 << ch)]


def _per_interval(dec):
    """The channels of the samples starting in each interval, in transport order, keyed by
    the interval's number from the SSCR's SSP."""
    audio = sorted(dec.audio(), key=lambda a: a["start_sample"])
    # Channel 0 is enabled first; its first sample's MSB is Column 1 of the SSP's row.
    origin = min(a["start_sample"] for a in audio if a["channel"] == 0) - 4
    out: dict[int, list[int]] = {}
    for a in audio:
        out.setdefault((a["start_sample"] - origin) // _INTERVAL, []).append(a["channel"])
    return out


def _run(from_mask, to_mask, row_in_interval=0):
    dec = _decode(swi3score.make_dscr_channel_set_levels(_N, from_mask, to_mask,
                                                         row_in_interval))
    commits = [c for c in dec.commands() if c.get("is_commit") and c.get("commit_confirmed")]
    assert [c["command"] for c in commits] == ["SSCR", "DSCR"]
    assert not any(c.get("unexpected_ssp_error") for c in dec.commands())
    return dec


def _assert_values(dec):
    for ch, values in _by_channel(dec).items():
        assert values == [_sine(ch, i, _SS) for i in range(len(values))], f"ch{ch}"


@pytest.mark.parametrize("from_mask,to_mask", [(0x3, 0x1), (0x1, 0x3), (0x5, 0x7), (0x3, 0x2)],
                         ids=["disable_ch1", "enable_ch1", "enable_middle_ch1",
                              "disable_ch0"])
def test_a_boundary_commit_applies_the_new_channel_set_to_its_interval(from_mask, to_mask):
    """Each interval before the commit point carries exactly the old set, and each from the
    commit point's interval on exactly the new set, each channel once, in channel order."""
    dec = _run(from_mask, to_mask)
    per = _per_interval(dec)
    for k in range(_N):
        assert per.get(k) == _channels(from_mask), (k, per.get(k))
    for k in range(_N, 2 * _N):
        assert per.get(k) == _channels(to_mask), (k, per.get(k))
    for ch in _channels(to_mask & ~from_mask):           # enabled: index 0 in the commit interval
        assert sorted(a["index"] for a in dec.audio() if a["channel"] == ch) == \
            list(range(_N)), ch
    _assert_values(dec)


@pytest.mark.parametrize("from_mask,to_mask,row", [(0x1, 0x3, 3), (0x1, 0x3, 32),
                                                    (0x3, 0x2, 3), (0x3, 0x1, 3),
                                                    (0x5, 0x7, 32)],
                         ids=["enable_after_transport", "enable_after_transport_end",
                              "disable_ch0_in_flight", "disable_ch1_in_flight",
                              "enable_middle_after_transport_end"])
def test_a_mid_interval_commit_applies_the_new_set_from_the_next_interval(from_mask, to_mask,
                                                                          row):
    """The commit point lands after the interval's transport has begun (Row 3 is during it,
    Row 32 after it): that interval carries the old set, and the next intervals the new."""
    dec = _run(from_mask, to_mask, row)
    per = _per_interval(dec)
    for k in range(_N + 1):
        assert per.get(k) == _channels(from_mask), (k, per.get(k))
    for k in range(_N + 1, 2 * _N):
        assert per.get(k) == _channels(to_mask), (k, per.get(k))
    _assert_values(dec)


@pytest.mark.parametrize("row", [0, 3])
def test_a_dscr_enabled_channel_is_seeded_and_a_survivor_continues(row):
    """With the wire read back unscrambled, a descrambler seeded at channel 1's first sample
    reproduces its decode, and channel 0's decode is one descrambler running across the
    commit point, which differs from one reseeded there."""
    levels = swi3score.make_dscr_channel_set_levels(_N, 0x1, 0x3, row)
    plain = _by_channel(_decode(levels))
    raw = _by_channel(_decode(levels, scrambler_overrides=[(0, 0, False)]))
    assert raw[1] != plain[1] and raw[0] != plain[0], "test premise: the wire is scrambled"
    assert _descramble_words(raw[1], [0]) == plain[1]
    assert _descramble_words(raw[0], [0]) == plain[0]
    first_after = _N + (1 if row else 0)
    reseeded = _descramble_words(raw[0][first_after:], [0])[0]
    assert reseeded != plain[0][first_after], "test premise: a reseed would differ here"


@pytest.mark.parametrize("from_mask,to_mask", [(0x3, 0x2), (0x1, 0x3)],
                         ids=["disable_ch0", "enable_ch1"])
def test_a_windowed_re_decode_matches_the_full_decode_across_a_mid_interval_commit(
        from_mask, to_mask):
    """A windowed bit-sample re-decode that starts at the DSCR's checkpoint restores the
    channel set the interval in progress runs on, so its sample points equal the full
    decode's."""
    import numpy as np

    from swi3s_studio.ingest import transitions
    from swi3s_studio.session import Session
    levels = swi3score.make_dscr_channel_set_levels(_N, from_mask, to_mask, 3)
    cap = transitions.build_capture_from_levels(levels)
    st = swi3score.DecoderSettings(); st.collect_bit_samples = True
    full = swi3score.Decoder(cap.sample_source(), st); full.run()
    bits = full.bit_samples()
    sample = np.asarray(bits["sample"], np.int64)
    channel = np.asarray(bits["channel"], np.int32)
    # From Row 3 of the commit interval (just after the commit point) to two intervals on.
    # Channel 0 sends in every interval up to the commit interval, so its bit N * 16 is the
    # MSB at Column 1 of that interval's first Row.
    ch0 = np.sort(sample[channel == 0])
    interval = int(ch0[_N * 16] - ch0[(_N - 1) * 16])
    lo = int(ch0[_N * 16]) + 3 * interval // 64
    hi = lo + 2 * interval
    win = Session(cap).port_bit_marks(lo, hi, max_marks=1_000_000)

    def in_window(samples, channels):
        samples = np.asarray(samples, np.int64)
        keep = (samples >= lo) & (samples <= hi)
        order = np.argsort(samples[keep], kind="stable")
        return samples[keep][order], np.asarray(channels, np.int32)[keep][order]

    want_s, want_c = in_window(sample, channel)
    got_s, got_c = in_window(win["sample"], win["channel"])
    assert want_s.size > 32
    assert np.array_equal(got_s, want_s) and np.array_equal(got_c, want_c)
