"""CDS 8b/10b symbol decode test.

Re-decodes the demo CDS into classified symbols and checks the framing: the
first symbol is the K.28.7 comma, the phase header is robust tokens, the manager
packet bytes are D-codes, and the count is bounded by max_symbols.

Run: python3 tests/test_symbols.py
"""
import swi3score
from swi3s_studio.ingest import transitions

KIND_INVALID, KIND_COMMA, KIND_RT, KIND_D, KIND_K = range(5)


def _symbols(n=8, maxn=60):
    return swi3score.decode_symbols(transitions.demo_capture(n).sample_source(), 16, maxn)


def test_framing():
    syms = _symbols()
    assert syms, "no symbols decoded"
    assert syms[0]["kind"] == KIND_COMMA, syms[0]
    assert syms[0]["raw"] in (0x0F8, 0x307), f"comma codeword {syms[0]['raw']:#05x}"
    kinds = {s["kind"] for s in syms}
    assert KIND_RT in kinds, "expected robust tokens (phase header)"
    assert KIND_D in kinds, "expected D-codes (manager packet bytes)"
    # The first phase is a Ping: comma, then header RT0 (GetStatus), RT15x3 (mask F/F/F).
    hdr = [s for s in syms if s["kind"] == KIND_RT][:4]
    assert hdr[0]["value"] == 0, hdr
    assert [h["value"] for h in hdr[1:4]] == [15, 15, 15], hdr


def test_bound_and_monotonic():
    syms = _symbols(maxn=25)
    assert len(syms) == 25
    starts = [s["start_sample"] for s in syms]
    assert starts == sorted(starts)
    assert all(s["end_sample"] >= s["start_sample"] for s in syms)


if __name__ == "__main__":
    test_framing(); print("ok: framing — comma, robust-token header, D-code packet")
    test_bound_and_monotonic(); print("ok: max_symbols honoured + monotonic samples")
    print("ALL SYMBOL TESTS PASSED")
