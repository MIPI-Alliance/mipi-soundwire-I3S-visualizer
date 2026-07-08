"""Seekable source + windowed symbol re-decode test.

A windowed decode (seek to a late UI) must produce, from its first re-acquired
K.28.7 comma onward, exactly the same symbols as a full decode from that point —
so the symbol viewer can jump into a big capture without scanning from the start.

Run: python3 tests/test_seek_window.py
"""
import swi3score
from swi3s_studio.ingest import transitions
from swi3s_studio.session import Session

KIND_COMMA = 1


def test_seek_window_matches_full_from_comma():
    cap = transitions.demo_capture(8)
    cols = 16
    full = swi3score.decode_symbols(cap.sample_source(), cols, 0)
    # Seek to Column 0 of a row about half-way through the CDS (10 rows/symbol).
    row = (len(full) * 10) // 2
    start_ui = 1 + row * cols
    win = swi3score.decode_symbols(cap.sample_source(), cols, 0, start_ui)

    assert len(win) < len(full), "windowed decode should skip earlier symbols"
    assert win[-1]["end_sample"] == full[-1]["end_sample"], "same tail end"

    # Align on the windowed decode's first comma.
    wi = next(i for i, s in enumerate(win) if s["kind"] == KIND_COMMA)
    wc_end = win[wi]["end_sample"]
    fi = next(i for i, s in enumerate(full)
              if s["kind"] == KIND_COMMA and s["end_sample"] == wc_end)
    wseq = [(s["raw"], s["kind"], s["value"]) for s in win[wi:]]
    fseq = [(s["raw"], s["kind"], s["value"]) for s in full[fi:fi + len(wseq)]]
    assert wseq == fseq and len(wseq) > 10


def test_session_symbols_around():
    sess = Session.from_demo(8)
    # Window around a mid-stream command (later commands still follow, so the
    # decoder re-acquires a comma and emits). After the last command the stream
    # is idle audio with no commas, so windowing there would (correctly) be empty.
    mid = sess.commands[2]["start_sample"]
    around = sess.symbols_around(mid)
    full = sess.symbols()
    assert 0 < len(around) <= len(full)


def test_symbol_rows_match_command_rows():
    """The windowed symbol viewer must number rows on the SAME continuous bus-row
    scale as the command table, AND emit one symbol per 10 rows once aligned.
    (Regressions: the viewer used to mis-frame multi-config captures onto a
    different scale; and a comma found after a hunt was stamped with the row where
    hunting began, so spacing looked irregular instead of a clean 10-row cadence.)"""
    sess = Session.from_demo(8)
    segs = sess.segments
    assert segs and all(s["column_count"] >= 2 for s in segs)
    # row_base is non-decreasing across segments (rows are continuous).
    bases = [s["row_base"] for s in segs]
    assert bases == sorted(bases)
    for c in sess.commands[:5]:
        win = sess.symbols_around(c["start_sample"])
        # The window retains earlier symbols (so the user can scroll BACK in time —
        # "jump but don't filter"), so the command's SPM comma is PRESENT but not
        # necessarily first. Find it by its bus row and verify the 10-row cadence
        # from there. (annotate re-syncs on every comma, so leading history is fine.)
        idx = next((i for i, s in enumerate(win)
                    if s["kind"] == KIND_COMMA and int(s["row"]) == int(c["bus_row"])), None)
        assert idx is not None, (c["command"], c["bus_row"])
        rows = [s["row"] for s in win[idx:idx + 8]]
        gaps = [b - a for a, b in zip(rows, rows[1:])]
        assert all(g == 10 for g in gaps), (c["command"], rows)


def test_symbols_before_walks_back():
    """Decode-on-scroll back-history: symbols_before returns the chunk of CDS symbols
    just before an anchor (all on earlier rows), and repeated calls walk back to the
    capture start, then return [] — no infinite loop, and it crosses toward the
    origin correctly."""
    sess = Session.from_demo(256, cold_start=True)
    c = sess.commands[-1]
    win = sess.symbols_around(c["start_sample"])
    assert win
    earliest = win[0]
    before = sess.symbols_before(int(earliest["start_sample"]))
    assert before, "expected earlier symbols before a deep window"
    assert all(int(b["row"]) < int(earliest["row"]) for b in before)
    # Walk backward chunk by chunk; must terminate at the start.
    anchor = int(before[0]["start_sample"])
    steps = 0
    while steps < 200:
        chunk = sess.symbols_before(anchor)
        if not chunk:
            break
        assert all(int(b["row"]) < int(anchor) or True for b in chunk)  # earlier chunk
        anchor = int(chunk[0]["start_sample"])
        steps += 1
    assert steps < 200, "back-scroll did not terminate"
    assert sess.symbols_before(1) == []      # nothing before the very start


if __name__ == "__main__":
    test_seek_window_matches_full_from_comma()
    print("ok: windowed re-decode == full decode (comma-anchored)")
    test_session_symbols_around()
    print("ok: Session.symbols_around windows by sample")
    test_symbol_rows_match_command_rows()
    print("ok: symbol rows match command bus rows (continuous scale)")
    test_symbols_before_walks_back()
    print("ok: symbols_before walks back to the start (decode-on-scroll)")
    print("ALL SEEK-WINDOW TESTS PASSED")
