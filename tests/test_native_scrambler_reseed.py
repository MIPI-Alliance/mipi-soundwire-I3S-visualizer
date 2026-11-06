"""A scrambler is seeded when its channel or FCP is enabled, or when its scrambler enable
bit turns on, and continues otherwise.

`make_channel_reseed_levels` runs one scrambled two-channel port through seven phases:
channel 0 runs throughout, channel 1 is disabled and re-enabled across SSCRs and then
across DSCRs, and single-ranked 0x0B writes then clear and set ScramblerEn. The wire bits
are read back by decoding with ScramblerEn forced off, and an independent descrambler run
over them per channel, seeded where the rule seeds, must give every value the decoder
reports. At each re-enable and where ScramblerEn turns on, the decoded sample equals a
descrambler started from the seed there and differs from one that continues; at each
SSCR and DSCR the surviving channel 0 equals the continuing descrambler and differs from
one reseeded there. Across two full re-anchors (Warm Start restarts) with ScramblerEn
cleared in the section between, the second re-anchor seeds the channel.

`make_fcp_reseed_levels` runs a scrambled FCP across an SSCR, a DSCR, and single-ranked
0x0E writes that remove and re-enable it and then clear and set FCP_ScramblerEn. The
decoder's DRQ bits are seen through the RX_CONTROLLED handshake check: a wire seeded at
each FCP enable and where FCP_ScramblerEn turns on passes every check, and a wire seeded
anywhere else, or not at one of those, fails some.
"""
import pytest
import swi3score
from test_native_decoder_run import _sine
from test_native_reference_vectors import _descramble

_RATE = 98_304_000
_SAMPLES_PER_UI = 4
_K = 32                       # intervals per phase of the channel fixture
_OFF, _ON = 5, 6              # its phases with ScramblerEn cleared, then set again
_SS = 15                      # SampleSize field: 16-bit samples


def _decode(levels, **settings):
    st = swi3score.DecoderSettings()
    for k, v in settings.items():
        setattr(st, k, v)
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, _RATE, _SAMPLES_PER_UI), st)
    dec.run()
    return dec


def _by_channel(dec):
    out: dict[int, list[int]] = {}
    for a in sorted(dec.audio(), key=lambda a: (a["channel"], a["index"])):
        out.setdefault(a["channel"], []).append(a["value"])
    return out


def _bits(word):
    return [(word >> b) & 1 for b in range(_SS, -1, -1)]          # MSB first, as sent


def _word(bits):
    return sum(bit << (_SS - i) for i, bit in enumerate(bits))


def _descramble_words(raw, seeds):
    """Descramble one channel's raw sample words, seeding before each index in `seeds`."""
    out, cuts = [], sorted(set(seeds) | {len(raw)})
    for start, stop in zip(cuts, cuts[1:]):
        flat = [b for w in raw[start:stop] for b in _bits(w)]
        rec = _descramble(flat)
        out += [_word(rec[16 * i:16 * i + 16]) for i in range(stop - start)]
    return out


@pytest.fixture(scope="module")
def channel_decode():
    """Decoded words and raw wire words per channel; channel 1's re-enable indices; and
    per channel the indices of the first samples with ScramblerEn cleared and set again."""
    levels = swi3score.make_channel_reseed_levels(_K)
    plain_dec = _decode(levels)
    plain = _by_channel(plain_dec)
    raw = _by_channel(_decode(levels, scrambler_overrides=[(0, 0, False)]))
    assert sorted(plain) == sorted(raw) == [0, 1]
    assert len(plain[0]) == len(raw[0]) == 7 * _K, "channel 0 runs through all seven phases"
    assert len(plain[1]) == len(raw[1])
    # Channel 0 sends one sample per interval; a gap in channel 1 is a disabled stretch,
    # and the sample after it is channel 1's first after a re-enable.
    starts = {ch: [a["start_sample"] for a in sorted(plain_dec.audio(), key=lambda a: a["index"])
                   if a["channel"] == ch] for ch in (0, 1)}
    interval = starts[0][1] - starts[0][0]
    reenabled = [i for i in range(1, len(starts[1]))
                 if starts[1][i] - starts[1][i - 1] > interval]
    assert len(reenabled) == 2, "channel 1 is re-enabled once by an SSCR, once by a DSCR"
    # Channel 1 follows channel 0 in each interval, so its first sample of a phase is the
    # first that starts after channel 0's.
    toggles = {0: (_OFF * _K, _ON * _K)}
    toggles[1] = tuple(next(i for i, t in enumerate(starts[1]) if t > starts[0][k * _K])
                       for k in (_OFF, _ON))
    return plain, raw, reenabled, toggles


def _model(raw, seeds, toggle):
    """The rule's decode of one channel: descrambled, seeded at index 0, each of `seeds` and
    the ScramblerEn set; the words between the clear and the set pass as sent."""
    off, on = toggle
    return (_descramble_words(raw[:off], [0] + seeds) + raw[off:on]
            + _descramble_words(raw[on:], [0]))


def test_each_channel_decodes_to_its_generated_sine(channel_decode):
    plain = channel_decode[0]
    for ch, values in plain.items():
        assert values == [_sine(ch, i, _SS) for i in range(len(values))], f"ch{ch}"


def test_a_descrambler_seeded_at_each_channel_enable_reproduces_the_decode(channel_decode):
    plain, raw, reenabled, toggles = channel_decode
    assert raw[0] != plain[0] and raw[1] != plain[1], "test premise: the wire is scrambled"
    assert _model(raw[0], [], toggles[0]) == plain[0]
    assert _model(raw[1], reenabled, toggles[1]) == plain[1]


@pytest.mark.parametrize("ch", [0, 1])
def test_scrambler_en_turning_on_seeds_the_channel(channel_decode, ch):
    """With ScramblerEn cleared the channel decodes as sent; the immediate 0x0B write that
    sets it again seeds the channel's descrambler, rather than resuming the LFSR it had
    when ScramblerEn was cleared. Both writes are flagged as programming errors."""
    plain, raw, reenabled, toggles = channel_decode
    off, on = toggles[ch]
    assert plain[ch][off:on] == raw[ch][off:on]
    seeded = _descramble_words(raw[ch][on:], [0])[0]
    resumed = _descramble_words(raw[ch][:off] + raw[ch][on:on + 1],
                                [0] + (reenabled if ch else []))[-1]
    assert seeded != resumed, "test premise: the seed and the resumed LFSR differ here"
    assert plain[ch][on] == seeded


def test_scrambler_en_writes_are_flagged_as_programming_errors():
    dec = _decode(swi3score.make_channel_reseed_levels(_K))
    writes = [c for c in dec.commands()
              if c["command"] == "WriteA32" and c.get("address") == 0x200B]
    assert [c["running_port_write_error"] for c in writes] == [True, True]


@pytest.mark.parametrize("enable", [0, 1], ids=["after_sscr", "after_dscr"])
def test_a_re_enabled_channel_is_reseeded(channel_decode, enable):
    """Channel 1's first sample after its re-enable (by the third SSCR, then by the second
    DSCR) equals a descrambler started from the seed there, not one that continues."""
    plain, raw, reenabled, _ = channel_decode
    i = reenabled[enable]
    seeded = _descramble_words(raw[1][i:], [0])[0]
    continued = _descramble_words(raw[1][:i + 1], [0])[i]
    assert seeded != continued, "test premise: the seed and the running LFSR differ here"
    assert plain[1][i] == seeded


@pytest.mark.parametrize("phase", [1, 2, 3, 4],
                         ids=["sscr_disable", "sscr_enable", "dscr_disable", "dscr_enable"])
def test_a_surviving_channel_continues_its_lfsr(channel_decode, phase):
    """Channel 0 stays enabled through every commit: its first sample after each one equals
    the continuing descrambler and differs from one reseeded there."""
    plain, raw, _, _ = channel_decode
    i = phase * _K
    continued = _descramble_words(raw[0][:i + 1], [0])[i]
    reseeded = _descramble_words(raw[0][i:], [0])[0]
    assert continued != reseeded, "test premise: the seed and the running LFSR differ here"
    assert plain[0][i] == continued


def test_scrambler_en_off_then_on_across_two_re_anchors_seeds_the_channel():
    """Two Warm Start restarts on interval boundaries of phase 0, each a full re-anchor of
    the port, with a register override clearing ScramblerEn in the section between them:
    that section decodes as sent, and the second re-anchor, where ScramblerEn is set again,
    seeds channel 0's descrambler rather than resuming the LFSR it ran before the first."""
    levels = swi3score.make_channel_reseed_levels(_K)
    clean = _decode(levels)
    seg = clean.segments()[-1]
    ssp = next(c for c in clean.commands() if c["command"] == "SSCR")["effective_row"]

    def sample(row):
        return (seg["start_ui"] + (row - seg["row_base"]) * 8) * _SAMPLES_PER_UI

    first, second = sample(ssp + 8 * 64), sample(ssp + 16 * 64)
    restarts = [(first, 8, False, first), (second, 8, False, second)]
    off = [(len(clean.segments()), 0, 0x200B, 0x00)]        # the section between: ScramblerEn 0

    def channel0(**more):
        d = _decode(levels, restart_points=restarts, register_overrides=off, **more)
        assert [g["restart"] for g in d.segments()][-2:] == [True, True]
        return sorted((a for a in d.audio() if a["channel"] == 0), key=lambda a: a["index"])

    plain = channel0()
    raw = [a["value"] for a in channel0(scrambler_overrides=[(0, 0, False)])]
    values = [a["value"] for a in plain]
    i = next(k for k, a in enumerate(plain) if a["start_sample"] >= first)
    j = next(k for k, a in enumerate(plain) if a["start_sample"] >= second)
    assert values[i:j] == raw[i:j]
    seeded = _descramble_words(raw[j:j + 8], [0])
    resumed = _descramble_words(raw[:i] + raw[j:j + 1], [0])[-1]
    assert resumed != seeded[0], "test premise: the seed and the resumed LFSR differ here"
    assert values[j:j + 8] == seeded


def _fcp_decode(generator_seed):
    return _decode(swi3score.make_fcp_reseed_levels(96, generator_seed=generator_seed))


def test_a_fcp_seeded_at_each_enable_passes_every_handshake_check():
    dec = _fcp_decode("enable")
    stats = dec.flow_control_stats()
    # Checked from the second SSCR: two RX_CONTROLLED phases, the TX_CONTROLLED one
    # unchecked, then three RX_CONTROLLED phases.
    assert stats["checked"] == 5 * 96
    assert stats["fails"] == 0
    flow_writes = [c for c in dec.commands()
                   if c["command"] == "WriteA32" and c.get("address") == 0x200E]
    assert [c["running_port_write_error"] for c in flow_writes] == [False] + [True] * 4, \
        "the four 0x0E writes on the running port are flagged as programming errors"


@pytest.mark.parametrize("generator_seed", ["sscr", "dscr", "interval", "resume", "continue"])
def test_a_fcp_seeded_off_its_enable_fails_the_handshake(generator_seed):
    """The decode's DRQ scrambler continues across the SSCR, the DSCR and every interval,
    and is seeded at the FCP's re-enable and where FCP_ScramblerEn turns on with FlowMode
    unchanged: a wire seeded at any of the first three, not where FCP_ScramblerEn turns on
    ("resume"), or not at the re-enable either ("continue"), decodes to DRQ bits the
    TX_PRESENT bits contradict."""
    assert _fcp_decode(generator_seed).flow_control_stats()["fails"] > 0


def test_an_unknown_generator_seed_is_rejected():
    with pytest.raises(ValueError):
        swi3score.make_fcp_reseed_levels(64, generator_seed="sometimes")
