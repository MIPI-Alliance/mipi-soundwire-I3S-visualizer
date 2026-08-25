"""PHY-agnostic invariants that must hold for EVERY audio-mode PHY.

Most of the PHY3 (DLV) bugs we hit were the same shape: shared Session/decoder code
that was correct for FBCSE (PHY2) but silently wrong for DLV — the sparse-clock_edges
UI↔sample mapping threw off row/column/cursor math, the recovered-clock RSP marks were
non-periodic, and the windowed bit-sample overlay drew nothing because the DLV source
wasn't seekable. None of it was caught because tests exercised one PHY, used tiny demos
that never reached the deep/reconfigured regions, and asserted outputs in isolation
rather than cross-view CONSISTENCY.

This module runs one set of structural invariants against every synthesizable PHY, on a
demo long enough to span the Safe-Lock→audio reconfigure and reach a deep region. Any
future PHY (PHY1 when synthesized, or a real-capture path) gets the same coverage by
adding it to `PHYS`. The invariants are deliberately PHY-independent — they assert the
relationships the UI relies on, not PHY-specific values — so they catch "works for one
PHY, broken for another" regressions.

Run: PYTHONPATH=. python3 -m pytest tests/test_phy_invariants.py
"""
import numpy as np
import pytest

from swi3s_studio.session import Session

# Audio-mode PHYs that Session.from_demo can synthesize. All three inherit every invariant
# below. PHY1 (FBCSE-slow) was excluded on the premise that it "isn't built yet"; it has been
# since 3.0.11, and adding it here passed 26 of 27 immediately — the one failure was an
# assertion pinning the 4-port demo shape, not a PHY invariant (see test_audio_streams_present).
PHYS = [1, 2, 3]
_DEMO_SAMPLES = 1500          # spans the 4-col Safe-Lock → 16-col audio reconfigure and
#                               leaves a deep region for drift/seek coverage.

# The port set each demo synthesizes. NOT an invariant — demo shape, which is precisely why
# hardcoding one set here excluded a whole PHY from the 26 checks that ARE invariant.
_DEMO_STREAMS = {
    1: [(0, 0), (0, 1), (0, 2)],                    # PHY1: 4-col bus, three ports
    2: [(0, 0), (0, 1), (0, 2), (0, 3)],
    3: [(0, 0), (0, 1), (0, 2), (0, 3)],
}


@pytest.fixture(scope="module", params=PHYS, ids=[f"phy{p}" for p in PHYS])
def sess(request):
    return Session.from_demo(_DEMO_SAMPLES, cold_start=True, phy=request.param)


def _audio_points(s: Session, n: int = 40) -> np.ndarray:
    """`n` sample points spread across the audio region (post bring-up)."""
    a, end = int(s.audio_start_sample) + 1000, int(s.audio_end_sample) - 1000
    return np.linspace(a, max(a + 1, end), n).astype(np.int64)


def _row_samples(s: Session) -> float:
    return s.sample_rate_hz / (s.row_rate_khz * 1000.0)


def test_ui_and_row_mapping_monotonic(sess):
    """`_ui_for_sample` and `bus_row_for_sample` are monotonic non-decreasing in the
    sample number — the property the DLV sparse-clock_edges mapping violated (it
    under-counted UIs, so a later sample mapped to an earlier row)."""
    pts = _audio_points(sess)
    uis = np.array([sess._ui_for_sample(int(x)) for x in pts])
    rows = np.array([sess.bus_row_for_sample(int(x)) for x in pts])
    assert np.all(np.diff(uis) >= 0), "UI index not monotonic in sample"
    assert np.all(np.diff(rows) >= 0), "bus row not monotonic in sample"


def test_batch_and_scalar_row_numbering_agree(sess):
    """The vectorised `bus_rows_for_samples` matches the scalar `bus_row_for_sample`
    for every point — they must not diverge (the audio pane uses the batch path, the
    Timeline/commands the scalar one). The analyzer samples off-multiple of the UI, so a
    query point can land exactly on a clock edge where the two paths' side conventions
    (closing-edge vs RSP) legitimately differ by one row; anything larger is a real bug."""
    pts = _audio_points(sess)
    batch = np.asarray(sess.bus_rows_for_samples(pts))
    scalar = np.array([sess.bus_row_for_sample(int(x)) for x in pts])
    assert np.abs(batch - scalar).max() <= 1, (batch - scalar).tolist()


def test_row_sample_round_trip_stays_in_row(sess):
    """sample → row → sample lands at/before the sample and inside the SAME row — so a
    clicked command/commit anchors on its own row's Row Sync Point. (The DLV inverse
    `_sample_at_bus_row` used to round-trip to a totally different time.)"""
    for x in _audio_points(sess):
        row = sess.bus_row_for_sample(int(x))
        back = sess._sample_at_bus_row(row)
        assert back <= int(x), (back, int(x))              # RSP anchor is at/ before the sample
        assert sess.bus_row_for_sample(back) == row        # and maps back to the same row


def test_cursor_geometry_consistent_across_views(sess):
    """The column count a cursor sees must agree across the three ways the UI derives it:
    the Bus Grid's sample-based `column_count_at`, the segment `_segment_for_sample` falls
    in, AND the segment its BUS ROW falls in (row_base ranges — the Timeline/commands
    path). This is the exact disagreement behind "Timeline shows 4col, Capture shows
    16col" for one cursor: a wrong bus row put the cursor in the 4-col segment by row
    while the grid read 16-col by sample."""
    segs = sess.segments

    def cols_for_row(row: int) -> int:
        seg = segs[0]
        for s in segs:                              # segments are row_base-ascending
            if int(s["row_base"]) <= row:
                seg = s
            else:
                break
        return int(seg["column_count"])

    for x in _audio_points(sess):
        by_sample = sess.column_count_at(int(x))
        by_segment = int(sess._segment_for_sample(int(x))["column_count"])
        by_row = cols_for_row(sess.bus_row_for_sample(int(x)))
        assert by_sample == by_segment == by_row, (int(x), by_sample, by_segment, by_row)


def test_rsp_marks_are_periodic_one_per_row(sess):
    """The Raw-view Row Sync Point marks recur once per row at a near-uniform row period
    within a constant-width region — not the ragged/vanishing marks the DLV sparse-edge
    indexing produced. The analyzer samples off-multiple of the UI, so a constant row
    period quantises to two adjacent sample counts (e.g. 325/326) — the ±1 jitter a real
    capture shows — hence a spread of at most one sample, not exactly zero."""
    row = _row_samples(sess)
    mid = int(sess.audio_start_sample) + int(0.5 * (sess.audio_end_sample - sess.audio_start_sample))
    lo, hi = mid, mid + int(30 * row)
    marks = np.asarray(sess.cds_column_samples(lo, hi)).astype(np.int64)
    assert marks.size >= 8, marks.size
    gaps = np.diff(marks)
    assert gaps.size and (gaps.max() - gaps.min()) <= 1, gaps.tolist()   # near-uniform (±1 jitter)
    assert abs(int(gaps[0]) - round(row)) <= 2                            # == the row period


def test_windowed_bit_samples_resolve_and_are_consistent(sess):
    """The on-demand windowed bit-sample overlay (Show Samples) resolves in the audio
    region for EVERY PHY, everywhere in the capture, and a sub-window is exactly the
    super-window's bits clipped to it. DLV drew nothing because its sample source wasn't
    seekable; a mid-capture-only test would also have missed a deep-region seek failure.
    """
    row = _row_samples(sess)
    a, end = int(sess.audio_start_sample), int(sess.audio_end_sample)
    mid = a + int(0.5 * (end - a))
    lo, hi = mid, mid + int(30 * row)
    sub_hi = lo + int(12 * row)
    sub = np.sort(sess.port_bit_marks(lo, sub_hi)["sample"].astype(np.int64))
    assert sub.size > 0, "no bit samples resolved in the audio region"
    # Every bit a sub-window query returns must appear at the SAME position in a wider
    # window's decode — proves the seek/position is exact and stable across windows.
    # (Subset, not equality: the windowed decode yields whole UIs, so a query's last UI
    # can carry a mid-UI sample just past the exact hi bound — a boundary bit, not drift.)
    wu = sess._port_bit_window_uis(lo, hi)
    allb = sess.decoder.bit_samples_window(wu[0], wu[1])
    allsamp = set(np.asarray(allb["sample"], dtype=np.int64).tolist())
    assert set(sub.tolist()) <= allsamp, "sub-window bit samples not a subset of the wider window"
    # A window far into the capture also resolves (seek reaches deep regions).
    deep_lo = a + int(0.85 * (end - a))
    deep = sess.port_bit_marks(deep_lo, deep_lo + int(30 * row))
    assert deep["sample"].size > 0, "deep-region bit samples did not resolve (seek failed)"


def test_row_math_matches_rsp_ground_truth(sess):
    """Cross-check the row math against an INDEPENDENT oracle — the RSP marks
    (`cds_column_samples`, derived from the recovered clock / edges, not from the row
    formulas). Each RSP is a true Column-0 row opener, so: it round-trips exactly
    (row → sample → same RSP), consecutive RSPs advance the row by exactly 1, and a
    sample just after an RSP shares that RSP's row. Regression: the row math anchored on
    the segment's CDS-column UI without adding the CDS offset, so every DLV row came out
    one low and the cursor landed 2 UIs off — self-consistent (so round-trip/consistency
    tests passed) but wrong against this external ground truth."""
    row = _row_samples(sess)
    a = int(sess.audio_start_sample) + int(5 * row)
    marks = np.asarray(sess.cds_column_samples(a, a + int(12 * row))).astype(np.int64)
    assert marks.size >= 8, marks.size
    for m in marks:
        r = sess.bus_row_for_sample(int(m))
        assert sess._sample_at_bus_row(r) == int(m)              # round-trips to the SAME RSP
        assert sess.bus_row_for_sample(int(m) + 2) == r          # RSP opens its row (side='right')
    assert np.all(np.diff([sess.bus_row_for_sample(int(m)) for m in marks]) == 1)  # one row per RSP


@pytest.mark.parametrize("phy", PHYS, ids=[f"phy{p}" for p in PHYS])
def test_audio_streams_present(phy):
    """Every PHY decodes its demo's full port set, and every stream carries samples — a
    guard that a shared decode path didn't regress audio for one PHY while another worked.

    Parametrized rather than taking the shared `sess`, because the expected set is per-PHY
    and a Session does not record which PHY built it. The previous version hardcoded the
    4-port PHY2/PHY3 shape, which is what kept PHY1 out of this file entirely.

    Also asserts each stream is NON-EMPTY, which the set comparison alone never did: a port
    that decoded zero samples still appeared in `audio_streams()` and passed."""
    sess = Session.from_demo(_DEMO_SAMPLES, cold_start=True, phy=phy)
    assert sess.audio_streams() == _DEMO_STREAMS[phy]
    counts = {}
    for a in sess.audio:                   # the Session's copy (decoder's is released)
        counts[(a["device"], a["dp"])] = counts.get((a["device"], a["dp"]), 0) + 1
    empty = [k for k in _DEMO_STREAMS[phy] if not counts.get(k)]
    assert not empty, f"phy{phy}: stream(s) present but carrying no samples: {empty}"


@pytest.mark.parametrize("phy", PHYS, ids=[f"phy{p}" for p in PHYS])
def test_redecode_preserves_phy_and_audio(phy):
    """An in-place re-decode (scrambler / hub / register what-if) must keep the SAME PHY
    framing and audio. Regression: `_redecode` rebuilt the source as forwarded-clock
    FBCSE, so a scrambler override on a DLV session silently reverted it — wrong segments
    and corrupt audio — while `is_dlv` still reported True. (Own session, not the shared
    fixture, since it re-decodes in place.)"""
    s = Session.from_demo(_DEMO_SAMPLES, cold_start=True, phy=phy)
    is_dlv, segs, n_audio = s.is_dlv, [x["column_count"] for x in s.segments], s.audio_count
    n_rows = s.recovered_clock()[0].size if is_dlv else -1
    s.set_scrambler_overrides({(0, 0): True})       # forces a re-decode
    # Re-decode must invalidate the recovered-clock/RSP caches so they can't serve a
    # column-changing re-decode's stale marks.
    assert s._rsp_analysis_cache is None and s._rec_syncs_cache is None
    assert s.is_dlv == is_dlv
    assert [x["column_count"] for x in s.segments] == segs
    assert s.audio_count == n_audio
    if is_dlv:                                        # recovered clock survives the re-decode
        assert s.recovered_clock()[0].size == n_rows
        assert np.asarray(s.decoder.row_sync_samples()).size == n_rows

