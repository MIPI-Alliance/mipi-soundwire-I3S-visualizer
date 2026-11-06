"""Wire-level checks the core's own encoder cannot make.

Every command and scrambled payload the demo plans are produced by the same CRC and
scrambler classes the decode uses, so a wrong polynomial, seed or tap is self-consistent
and passes every round-trip test. These tests read the planned wire levels back and
compare them with independent Python models of the CRC-16 and the descrambler, and check
that a windowed DLV replay lands on exactly the sample points of the full decode, over
exactly the source-UI range it was asked for (every bit of a row inside the range, and no
bit of a row outside it).

The descrambler's No-Toggle rule is checked on a port whose wire has runs that set it off,
and the demo's 8b/10b encoder is compared with the standard code tables for every byte."""
import numpy as np
import swi3score

from swi3s_studio.ingest import transitions

_RATE = 98_304_000
_SAMPLES_PER_UI = 4


def _crc16(data, poly=0xA2EB):
    """CRC-16: polynomial 0xA2EB, init 0, MSB-first, no reflection, no final XOR."""
    crc = 0
    for byte in data:
        for i in range(7, -1, -1):
            top = ((crc >> 15) & 1) ^ ((byte >> i) & 1)
            crc = (crc << 1) & 0xFFFF
            if top:
                crc ^= poly
    return crc


def _descramble(received, seed=0x154, no_toggle=16):
    """Self-synchronising 9-bit descrambler (taps Q4, Q0) with the No-Toggle rule: after
    `no_toggle` received bits each equal to the one nine before, the next bit is inverted."""
    q, run, out = seed, 0, []
    for r in received:
        tap = ((q >> 4) ^ q) & 1
        out.append(r ^ tap ^ (1 if run == no_toggle else 0))
        toggled = r ^ ((q >> 8) & 1)
        q = ((q >> 1) | (r << 8)) & 0x1FF
        if run == no_toggle:
            run = 0 if toggled else 1
        else:
            run = 0 if toggled else run + 1
    return out


def _demo(samples=40, **settings):
    levels = swi3score.make_demo_levels(samples)
    st = swi3score.DecoderSettings()
    for k, v in settings.items():
        setattr(st, k, v)
    dec = swi3score.Decoder(swi3score.MemorySampleSource(levels, _RATE, _SAMPLES_PER_UI), st)
    dec.run()
    return levels, dec


def test_the_crc_worked_vector():
    assert _crc16([0x80, 0x35]) == 0xCC89
    assert _crc16([0x80, 0x35], poly=0xA2EA) != 0xCC89


def test_the_demo_wire_carries_the_independent_crc_of_every_early_write():
    """Read the Safe-Lock CDS bits back off the planned levels (NRZS, one CDS column per
    row) and compare each WriteA32's two CRC bytes with the independent CRC-16."""
    levels, dec = _demo()
    first = dec.segments()[0]
    assert first["column_count"] == 2 and first["cds_column"] == 0
    cds = [int(v) for v in levels[0::2]]
    bits = [1 ^ cds[i] ^ cds[i - 1] for i in range(1, len(cds))]

    def byte_at(symbol):
        raw = 0
        for b in bits[symbol:symbol + 10]:
            raw = (raw << 1) | b
        out = swi3score.decode_codeword(raw)
        assert out["valid"] and not out["is_control"]
        return out["value"]

    writes = [c for c in dec.commands()
              if c["command"] == "WriteA32" and c["start_sample"] < first["start_sample"] + 30000
              and c["start_sample"] < dec.segments()[1]["start_sample"]]
    assert len(writes) >= 5
    for c in writes:
        row = c["bus_row"] - 1                       # the comma's index in `bits`
        assert swi3score.decode_codeword(
            sum(b << (9 - i) for i, b in enumerate(bits[row:row + 10])))["is_comma"]
        n = c["packet_length"]
        packet = [byte_at(row + 10 * (7 + i)) for i in range(n)]
        assert packet[1:5] == list(c["address"].to_bytes(4, "big"))
        assert bytes(packet[5:]) == c["data"]
        wire_crc = (byte_at(row + 10 * (7 + n)) << 8) | byte_at(row + 10 * (8 + n))
        assert wire_crc == _crc16(packet)
        assert c["crc_valid"]


def test_a_scrambled_port_descrambles_with_the_specs_lfsr():
    """The wire levels of a scrambled demo port, run through an independent descrambler
    (seed 0b101010100, taps Q4 ^ Q0), give the audio the decoder reports."""
    levels, dec = _demo(collect_bit_samples=True)
    bits, audio = dec.bit_samples(), dec.audio()
    dp = 1
    samples = bits["sample"][bits["dp"] == dp]
    wire = [int(levels[int(s) // _SAMPLES_PER_UI - 2]) for s in samples]
    values = [a["value"] for a in audio if a["dp"] == dp]
    n = min(len(values), len(wire) // 16, 20)
    assert n >= 10
    out = _descramble(wire[:16 * n])
    words = [sum(out[16 * i + j] << (15 - j) for j in range(16)) for i in range(n)]
    assert words == values[:n]
    assert _descramble(wire[:16 * n], seed=0x155) != out


def test_a_scrambled_channel_keeps_its_lfsr_across_an_sscr():
    """A channel enabled before and after an SSCR, scrambled both times, is not reseeded:
    one independent descrambler run over every bit of the demo's scrambled dp1, across
    its 8 -> 16 column SSCR, gives every sample the decoder reports, and a descrambler
    reseeded at that SSCR does not."""
    levels, dec = _demo(300, collect_bit_samples=True)
    bits, audio = dec.bit_samples(), dec.audio()
    dp = 1
    samples = bits["sample"][bits["dp"] == dp]
    wire = [int(levels[int(s) // _SAMPLES_PER_UI - 2]) for s in samples]
    values = [a["value"] for a in audio if a["dp"] == dp]
    assert len(wire) == 16 * len(values)
    sscr = dec.segments()[-1]
    assert sscr["column_count"] == 16
    cut = int(np.searchsorted(samples, sscr["start_sample"]))
    assert 0 < cut < len(wire) and cut % 16 == 0, "test premise: dp1 runs on both sides"

    def words(out):
        return [sum(out[16 * i + j] << (15 - j) for j in range(16))
                for i in range(len(out) // 16)]

    assert words(_descramble(wire)) == values
    reseeded = words(_descramble(wire[:cut]) + _descramble(wire[cut:]))
    assert reseeded[cut // 16] != values[cut // 16]


def test_the_no_toggle_rule_inverts_the_bit_after_sixteen_unchanged_bits():
    """The demo's dp0 is sent unscrambled, and its wire holds runs that set the No-Toggle
    detector off. Decoded with ScramblerEn forced on, every value equals the independent
    descrambler with a 16-bit limit, and a 17-bit limit gives different values."""
    levels, dec = _demo(300, collect_bit_samples=True)
    device = int(dec.bit_samples()["device"][dec.bit_samples()["dp"] == 0][0])
    levels, dec = _demo(300, collect_bit_samples=True, scrambler_overrides=[(device, 0, 1)])
    bits, audio = dec.bit_samples(), dec.audio()
    on_dp0 = bits["dp"] == 0
    assert set(int(c) for c in bits["channel"][on_dp0]) == {0}, "test premise: one channel"
    wire = [int(levels[int(s) // _SAMPLES_PER_UI - 2]) for s in bits["sample"][on_dp0]]
    values = [a["value"] for a in audio if a["dp"] == 0]
    assert len(wire) == 16 * len(values) > 0

    def words(out):
        return [sum(out[16 * i + j] << (15 - j) for j in range(16)) for i in range(len(values))]

    assert words(_descramble(wire)) == values
    assert words(_descramble(wire, no_toggle=17)) != values


# Standard 8b/10b data code tables: 5b/6b abcdei and 3b/4b fghj, (RD-, RD+) per value.
_6B = [(0b100111, 0b011000), (0b011101, 0b100010), (0b101101, 0b010010), (0b110001,) * 2,
       (0b110101, 0b001010), (0b101001,) * 2, (0b011001,) * 2, (0b111000, 0b000111),
       (0b111001, 0b000110), (0b100101,) * 2, (0b010101,) * 2, (0b110100,) * 2,
       (0b001101,) * 2, (0b101100,) * 2, (0b011100,) * 2, (0b010111, 0b101000),
       (0b011011, 0b100100), (0b100011,) * 2, (0b010011,) * 2, (0b110010,) * 2,
       (0b001011,) * 2, (0b101010,) * 2, (0b011010,) * 2, (0b111010, 0b000101),
       (0b110011, 0b001100), (0b100110,) * 2, (0b010110,) * 2, (0b110110, 0b001001),
       (0b001110,) * 2, (0b101110, 0b010001), (0b011110, 0b100001), (0b101011, 0b010100)]
_4B = [(0b1011, 0b0100), (0b1001,) * 2, (0b0101,) * 2, (0b1100, 0b0011),
       (0b1101, 0b0010), (0b1010,) * 2, (0b0110,) * 2, (0b1110, 0b0001)]
_A7 = (0b0111, 0b1000)


def _encode_rd_minus(value):
    """The standard 10-bit codeword of data byte `value` sent at running disparity -1."""
    x, y = value & 0x1F, value >> 5
    six = _6B[x][0]
    rd = 1 if bin(six).count("1") == 4 else 0            # RD after the 6b sub-block
    if y == 7 and ((rd == 0 and x in (17, 18, 20)) or (rd == 1 and x in (11, 13, 14))):
        four = _A7[rd]
    else:
        four = _4B[y][rd]
    return (six << 4) | four


def test_the_demo_encoder_matches_the_standard_8b10b_tables():
    bad = [(v, format(swi3score.encode_codeword(v), "010b"), format(_encode_rd_minus(v), "010b"))
           for v in range(256) if swi3score.encode_codeword(v) != _encode_rd_minus(v)]
    assert not bad, bad[:8]


def _dlv_decode():
    cap = transitions.demo_capture(400, phy=3)
    nominal = cap.sample_rate_hz / (3_072_000.0 * 4)
    src = swi3score.DlvSampleSource(cap.clock_edges.astype(np.uint64),
                                    bool(cap.initial_clock), int(cap.sample_rate_hz), 4, nominal)
    st = swi3score.DecoderSettings()
    st.dlv = True
    st.cds_horizontal_start = 2
    st.forced_column_count = 4
    st.collect_bit_samples = True
    dec = swi3score.Decoder(src, st)
    dec.run()
    return dec


def test_a_windowed_dlv_replay_places_every_bit_on_the_full_decodes_sample():
    dec = _dlv_decode()
    full = np.sort(np.asarray(dec.bit_samples()["sample"], dtype=np.int64))
    assert full.size > 2000
    rows = np.asarray(dec.row_sync_samples(), dtype=np.int64)
    segments = dec.segments()

    def ui_at(sample):
        """The source UI of Column 0 of the row holding `sample`."""
        row = int(np.searchsorted(rows, sample))
        seg = [g for g in segments if g["row_base"] <= row][-1]
        return int(seg["start_ui"] + (row - seg["row_base"]) * seg["column_count"])

    def row_span(row):
        """The first and last source UI of bus row `row`."""
        seg = [g for g in segments if g["row_base"] <= row][-1]
        first = int(seg["start_ui"] + (row - seg["row_base"]) * seg["column_count"])
        return first, first + seg["column_count"] - 1

    # Each full-decode bit's row: the last Row Sync Point at or before its sample opens it.
    spans = np.array([row_span(int(r)) for r in np.searchsorted(rows, full, side="right") - 1])
    span = int(full[-1] - full[0])
    for k in range(1, 27):
        # A window is addressed by source UI: 1500 UIs from the row at this point of the span.
        lo = ui_at(int(full[0]) + span * k // 28)
        hi = lo + 1500
        window = np.sort(np.asarray(dec.bit_samples_window(lo, hi)["sample"], dtype=np.int64))
        assert window.size > 0, k
        live = full[(full >= window[0]) & (full <= window[-1])]
        assert np.array_equal(window, live), (k, int(np.abs(window.size - live.size)))
        # The window's extent is the requested UI range: every bit of a row inside it is in
        # the window, and every bit in the window is of a row that overlaps it.
        inside = full[(spans[:, 0] >= lo) & (spans[:, 1] <= hi)]
        overlapping = full[(spans[:, 1] >= lo) & (spans[:, 0] <= hi)]
        assert np.isin(inside, window).all(), (k, int((~np.isin(inside, window)).sum()))
        assert np.isin(window, overlapping).all(), k
