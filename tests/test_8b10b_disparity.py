"""8b/10b decoder running-disparity coverage.

The decoder accepts BOTH running-disparity (RD-/RD+) forms of every one of the 256
data bytes, including the two *balanced* exceptions, 5b/6b D.07 and 3b/4b D.x.3,
whose RD+ codewords are 000111 and 0011. EncodeByte emits one form per byte, so this
test builds both forms from the standard tables directly.

Run: python3 -m pytest tests/test_8b10b_disparity.py
"""
import swi3score

# Standard IBM/ANSI 5b/6b code (abcdei), RD- form indexed by the 5-bit value.
_C5B6B_MINUS = [
    0x27, 0x1D, 0x2D, 0x31, 0x35, 0x29, 0x19, 0x38,
    0x39, 0x25, 0x15, 0x34, 0x0D, 0x2C, 0x1C, 0x17,
    0x1B, 0x23, 0x13, 0x32, 0x0B, 0x2A, 0x1A, 0x3A,
    0x33, 0x26, 0x16, 0x36, 0x0E, 0x2E, 0x1E, 0x2B,
]
# Standard 3b/4b code (fghj), RD- form indexed by the 3-bit value (y=7 is P7).
_C3B4B_MINUS = [0xB, 0x9, 0x5, 0xC, 0xD, 0xA, 0x6, 0xE]


def _popcount(v):
    return bin(v).count("1")


def _forms6(x):
    """Both legal 6b codewords for 5-bit value x. The RD+ form is the complement
    for *unbalanced* sub-blocks; balanced ones reuse the single form, except D.07
    which has two balanced alternates (111000 / 000111) chosen by disparity."""
    c = _C5B6B_MINUS[x]
    out = {c}
    if _popcount(c) != 3:
        out.add((~c) & 0x3F)
    if x == 7:
        out |= {0x38, 0x07}    # D.07 RD- / RD+ balanced alternates
    return out


def _forms4(y):
    """Both legal 4b codewords for 3-bit value y. RD+ is the complement for
    unbalanced sub-blocks; D.x.3 has the balanced alternates 1100 / 0011 and
    D.x.7 has the P7 / A7 alternates (0001/1110 plus 0111/1000)."""
    c = _C3B4B_MINUS[y]
    out = {c}
    if _popcount(c) != 2:
        out.add((~c) & 0xF)
    if y == 3:
        out |= {0xC, 0x3}      # D.x.3 RD- / RD+ balanced alternates
    if y == 7:
        out |= {0xE, 0x1, 0x7, 0x8}   # P7 / A7 alternates
    return out


def test_both_disparity_forms_decode():
    """Every byte 0..255 decodes from both its RD- and RD+ 10-bit codewords."""
    bad = []
    for value in range(256):
        x = value & 0x1F
        y = (value >> 5) & 0x7
        for six in _forms6(x):
            for four in _forms4(y):
                raw = (six << 4) | four
                if not 4 <= _popcount(raw) <= 6:
                    continue                      # two same-sign sub-blocks: illegal
                d = swi3score.decode_codeword(raw)
                if not d["valid"] or d["value"] != value:
                    bad.append((value, raw, d["valid"], d["value"]))
    assert not bad, f"{len(bad)} codewords mis-decoded, e.g. {bad[:8]}"


def test_balanced_exceptions_specifically():
    """The two balanced RD+ codewords (D.07 000111, D.x.3 0011) decode correctly."""
    # 5b/6b D.07 RD+ = 000111 (0x07); pair with a known-good fghj to make 0xE7.
    d = swi3score.decode_codeword((0x07 << 4) | 0x01)   # x=7, y=7 -> 0xE7
    assert d["valid"] and d["value"] == 0xE7, d
    # 3b/4b D.x.3 RD+ = 0011 (0x3); paired with the 6b D.0 RD+ form 011000 it is D.0.3 = 0x60.
    d = swi3score.decode_codeword((0x18B & 0x3F0) | 0x3)
    assert d["valid"] and d["value"] == 0x60, d


def test_same_sign_sub_blocks_are_not_a_legal_codeword():
    """A 6b sub-block and a 4b sub-block that both lean the same way carry 3 or 7 ones,
    which no running-disparity sequence produces. D.0.0 with both RD- sub-blocks, and its
    complement, are rejected."""
    for raw in (0x27B, (~0x27B) & 0x3FF):
        assert _popcount(raw) not in (4, 5, 6)
        assert not swi3score.decode_codeword(raw)["valid"], hex(raw)


def test_encoder_emits_only_legal_codewords_that_decode_to_their_byte():
    """Every byte's EncodeByte codeword carries 4, 5 or 6 ones and decodes back to the
    byte; D.0.0 is the byte whose two RD- sub-blocks together carry seven ones."""
    bad = []
    for value in range(256):
        raw = swi3score.encode_codeword(value)
        d = swi3score.decode_codeword(raw)
        if not 4 <= _popcount(raw) <= 6 or not d["valid"] or d["value"] != value:
            bad.append((value, hex(raw), d))
    assert not bad, f"{len(bad)} bytes encode to an illegal or mis-decoding codeword: {bad[:4]}"
    assert swi3score.encode_codeword(0x00) == 0b1001110100      # D.0.0, RD- form
