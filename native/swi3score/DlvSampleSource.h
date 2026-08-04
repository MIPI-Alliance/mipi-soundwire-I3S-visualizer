// PHY3 (DLV) sample source: recovers the bit clock from a differential capture and
// yields per-UI samples like the forwarded-clock TransitionSampleSource, so the shared
// decode core is reused unchanged above the bit level.
//
// DLV has no forwarded clock. DP and DN are a complementary differential pair carrying
// ONE logical signal (1 = DP high / DN low); the capture stores the DP wire's edges
// (DN is its exact complement, so the DP wire alone determines the logical level). Each
// row begins with Sync1 (Column 0, logical 1) — the Sync0->Sync1 rising edge at the row
// boundary is the Row Sync Point — and ends with Sync0 (logical 0), so every row starts
// with a clean 0->1 edge. A virtual DLL locks to that once-per-row rising edge to recover
// the row period, subdivides it into `columns` equal UIs, and samples each UI at its
// MIDDLE (§12.2.5 {ASW4803}), unlike FBCSE which samples at the UI edge.
//
// The column count comes from the snoop (authoritative): the source starts at the
// Safe-Lock-4 count and the decoder pushes each committed NumColumns change via
// SetColumns(). A future "join an operational bus" mode can instead seed the initial
// count + CDS position from waveform detection / a Visualizer CSV — the DLL is unchanged.

#ifndef SWI3SCORE_DLVSAMPLESOURCE_H
#define SWI3SCORE_DLVSAMPLESOURCE_H

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

#include "ISampleSource.h"
#include "core/SwI3sProtocolDefs.h"

namespace swi3score {

class DlvSampleSource final : public ISampleSource
{
public:
    // dpEdges: sorted DP-wire transition samples. initialDp: DP level before edge 0.
    // initialColumns: Safe-Lock column count to start with (snoop updates it later).
    // nominalUiSamples: expected UI period in samples for the INITIAL geometry (the PLL's
    // free-running estimate to acquire from). >0 seeds the loop so it locks to the once-
    // per-row reference edge instead of guessing the period from the first two rising edges
    // (which grabs an intra-row CDS/data edge when the first CDS bit is 1). 0 = auto (legacy).
    DlvSampleSource(std::vector<std::uint64_t> dpEdges, bool initialDp,
                    std::uint64_t sampleRateHz, int initialColumns,
                    double nominalUiSamples = 0.0)
        : mEdges(std::move(dpEdges)), mInitialDp(initialDp), mRate(sampleRateHz),
          mColumns(initialColumns >= swi3s::kMinColumnCount ? initialColumns : swi3s::kColdStartColumnCount),
          mNominalUi(nominalUiSamples)
    {
        // Precompute the 0->1 (rising) edge samples the DLL locks to. With DP low before
        // edge 0, even-indexed edges are rising; with DP high, odd-indexed. (A rising
        // edge is a transition whose post-edge level is high.)
        const std::size_t firstRising = mInitialDp ? 1u : 0u;
        for (std::size_t i = firstRising; i < mEdges.size(); i += 2)
            mRising.push_back(mEdges[i]);
        mLastSample = mEdges.empty() ? 0 : mEdges.back();
    }

    bool NextUi(bool& rising, bool& dataHigh, std::uint64_t& sampleNumber) override
    {
        if (!mLocked && !lock()) return false;

        // One leading "alignment" UI (mirrors MemorySampleSource): the decoder aligns to
        // the first rising edge and treats the NEXT UI as Column 0. Report the row's
        // Sync1 as that alignment edge, then start emitting real Column 0..N-1.
        if (!mAlignmentEmitted) {
            mAlignmentEmitted = true;
            rising = true;
            dataHigh = false;                       // pre-Sync1 (Sync0) level
            sampleNumber = mRowStart;
            return true;
        }

        if (mCol >= mColumns && !advanceRow()) return false;

        double mid = mRowStart + (mCol + 0.5) * mUiSamples;
        std::uint64_t s = (mid <= 0.0) ? 0 : static_cast<std::uint64_t>(mid + 0.5);
        if (s > mLastSample) return false;          // ran past the capture
        rising = (mCol == 0);                        // Column 0 = Sync1 (row start)
        dataHigh = levelAt(s);                       // mid-UI differential level = the bit
        sampleNumber = s;
        ++mCol;
        ++mUi;
        return true;
    }

    std::uint64_t SampleRateHz() const override { return mRate; }
    std::uint64_t UiIndex() const override { return mUi; }

    // Random access for the windowed bit-sample decode. The recovered clock is NOT a
    // single uniform grid: the UI period changes when the geometry commits (the bit clock
    // speeds up as the row subdivides into more columns, while the row rate holds). So we
    // can't jump by uiIndex*mUiSamples from row 0 — that ignores the wider Safe-Lock UIs.
    // Instead each row's (start edge, start UI) was recorded as the DLL ran (mRowSyncs /
    // mRowStartUi), so we binary-search the row containing `uiIndex`, restore that row's
    // start edge + per-row UI width, and set the column within it. O(log rows), and NextUi
    // resumes producing the same (sample, level) the full decode did.
    bool CanSeek() const override { return true; }

    void Seek(std::uint64_t uiIndex) override
    {
        if (!mLocked && !lock()) return;
        mAlignmentEmitted = true;              // past the one-shot alignment UI
        mUi = uiIndex;
        if (mRowStartUi.empty()) {             // no per-row log (shouldn't happen post-lock)
            int cols = (mColumns >= swi3s::kMinColumnCount) ? mColumns : swi3s::kColdStartColumnCount;
            int col = static_cast<int>(uiIndex % static_cast<std::uint64_t>(cols));
            mCol = col;
            mRowStart = mRowStart0 + static_cast<double>(uiIndex - col) * mUiSamples;
            return;
        }
        // Row r = last row whose start UI is <= uiIndex.
        auto it = std::upper_bound(mRowStartUi.begin(), mRowStartUi.end(), uiIndex);
        std::size_t r = (it == mRowStartUi.begin()) ? 0
                                                    : static_cast<std::size_t>((it - mRowStartUi.begin()) - 1);
        mRowStart = static_cast<double>(mRowSyncs[r]);
        mCol = static_cast<int>(uiIndex - mRowStartUi[r]);
        // Per-row UI width and column count from the recorded row spans (row rate constant,
        // so this row's period / its column count = that row's UI). Fall back to the live
        // values for the final (still-open) row.
        if (r + 1 < mRowSyncs.size()) {
            int colsInRow = static_cast<int>(mRowStartUi[r + 1] - mRowStartUi[r]);
            if (colsInRow > 0) {
                mColumns = colsInRow;
                mUiSamples = static_cast<double>(mRowSyncs[r + 1] - mRowSyncs[r]) / colsInRow;
            }
        }
    }
    std::uint64_t TotalUiCount() const override
    {
        // Approximate (progress only): whole-capture UIs at the recovered rate.
        if (mUiSamples <= 0.0 || mLastSample == 0) return 0;
        return static_cast<std::uint64_t>(mLastSample / mUiSamples);
    }

    // Snoop pushes a committed NumColumns change at a row boundary; the DLL re-subdivides
    // the row from here on. Ignored mid-row (columns only change at a commit SSP, which
    // lands on a row boundary in the decoder's feed loop).
    void SetColumns(int n) override
    {
        if (n < swi3s::kMinColumnCount || n == mColumns) return;
        // The decoder pushes a column change at a row boundary (it fires the commit when
        // its own column counter wraps), so the current row is fully emitted and mCol sits
        // at the old column count, awaiting the next row. The row RATE (reference) is
        // constant, so the row period is invariant across the change — rescale the recovered
        // UI by oldColumns/newColumns (= row_period/columns) so the bit clock speeds up
        // (e.g. 4 -> 16 cols). Then re-arm mCol at the NEW count so the next NextUi advances
        // to the next Sync1 and restarts at Column 0 under the new geometry; leaving mCol
        // mid-row would reinterpret the just-finished row with more columns and slip the
        // decoder's Column-0 phase by (newCols - mCol) UIs.
        const bool atRowBoundary = (mCol >= mColumns);
        if (mUiSamples > 0.0 && mColumns > 0)
            mUiSamples = mUiSamples * static_cast<double>(mColumns) / static_cast<double>(n);
        mColumns = n;
        if (atRowBoundary) mCol = n;   // force advanceRow() on the next NextUi (fresh Column 0)
    }

    // The recovered per-row Sync1 sample points (the bit-clock reference), collected as the
    // DLL runs — surfaced to the Raw Capture view to show clock recovery working.
    const std::vector<std::uint64_t>& RecoveredRowSyncs() const { return mRowSyncs; }
    double RecoveredUiSamples() const { return mUiSamples; }

private:
    // DP logical level at `sample`: initial level XOR parity of edges at/before it.
    // NextUi queries strictly-increasing samples, so a forward cursor (count of edges
    // <= sample) is O(1) amortized instead of an O(log m) upper_bound PER UI — this is
    // the whole DLV decode path. The two guards make it self-correcting for any sample
    // order (a Seek jumps the cursor back once, bounded), so it's a drop-in for the
    // upper_bound: mEdgeCursor lands on the first edge index > sample either way.
    bool levelAt(std::uint64_t sample) const
    {
        while (mEdgeCursor < mEdges.size() && mEdges[mEdgeCursor] <= sample) ++mEdgeCursor;
        while (mEdgeCursor > 0 && mEdges[mEdgeCursor - 1] > sample) --mEdgeCursor;
        return mInitialDp ^ ((mEdgeCursor & 1u) != 0);
    }

    // Nearest rising edge to `predicted` within +/- `window` samples, or UINT64_MAX.
    std::uint64_t nearestRising(std::uint64_t predicted, std::uint64_t window) const
    {
        auto it = std::lower_bound(mRising.begin(), mRising.end(), predicted);
        std::uint64_t best = UINT64_MAX, bestDist = window + 1;
        for (auto cand : {it, (it == mRising.begin() ? it : std::prev(it))}) {
            if (cand == mRising.end()) continue;
            std::uint64_t d = (*cand > predicted) ? (*cand - predicted) : (predicted - *cand);
            if (d <= window && d < bestDist) { best = *cand; bestDist = d; }
        }
        return best;
    }

    // Acquire: first rising edge = Row 0's Sync1 (the reference-edge phase). Seed the UI
    // period from the nominal free-running estimate when given (robust: the aperture then
    // tracks the once-per-row reference, ignoring intra-row CDS/data edges); otherwise fall
    // back to guessing it from the first two rising edges / columns.
    bool lock()
    {
        if (mRising.empty()) return false;
        mRowStart = mRising[0];
        mRowStart0 = static_cast<double>(mRising[0]);   // fixed row-0 anchor for Seek()
        if (mNominalUi > 0.0) {
            mUiSamples = mNominalUi;
        } else {
            if (mRising.size() < 2) return false;
            mUiSamples = static_cast<double>(mRising[1] - mRising[0]) / mColumns;
        }
        if (mUiSamples <= 0.0) return false;
        mRowSyncs.push_back(mRowStart);
        mRowStartUi.push_back(0);          // row 0's Column 0 is source UI 0 (Seek row lookup)
        mLocked = true;
        mCol = 0;
        return true;
    }

    // End of a row: predict the next Sync1 (rowStart + columns*UI), snap to the nearest
    // observed rising edge (tracks jitter/drift; coasts on the prediction if none is within
    // half a UI — a missing Row Sync, which the spec tolerates), and refine the UI estimate.
    bool advanceRow()
    {
        double period = mColumns * mUiSamples;
        double predicted = mRowStart + period;
        if (predicted > static_cast<double>(mLastSample) + mUiSamples) return false;
        std::uint64_t window = static_cast<std::uint64_t>(mUiSamples * 0.5 + 0.5);
        std::uint64_t snap = nearestRising(static_cast<std::uint64_t>(predicted + 0.5), window);
        std::uint64_t newStart = (snap != UINT64_MAX) ? snap
                                                      : static_cast<std::uint64_t>(predicted + 0.5);
        // Refine UI from the measured row period (light EMA); guard against a bad snap.
        double measured = static_cast<double>(newStart - mRowStart) / mColumns;
        if (measured > 0.0) mUiSamples = 0.75 * mUiSamples + 0.25 * measured;
        mRowStart = newStart;
        // Log this row's (edge, Column-0 UI) — but only the FIRST time we advance through it.
        // A windowed re-decode (Seek + NextUi, e.g. the CDS symbol viewer or the bit-sample
        // overlay) walks rows already logged; re-appending would corrupt the recovered-clock
        // marks and the Seek row lookup itself. Only genuinely new (higher-UI) rows are logged.
        if (mUi > mLoggedThroughUi || mRowSyncs.empty()) {
            mRowSyncs.push_back(mRowStart);
            mRowStartUi.push_back(mUi);        // source UI of this row's Column 0 (Seek row lookup)
            mLoggedThroughUi = mUi;
        }
        mCol = 0;
        return true;
    }

    std::vector<std::uint64_t> mEdges;      // DP wire transitions
    std::vector<std::uint64_t> mRising;     // 0->1 edges (row-sync candidates)
    mutable std::size_t mEdgeCursor = 0;    // levelAt forward cursor: #edges <= last sample
    std::vector<std::uint64_t> mRowSyncs;   // recovered Sync1 samples (bit-clock reference)
    std::vector<std::uint64_t> mRowStartUi; // source UI index at each row's Column 0 (Seek)
    std::uint64_t mLoggedThroughUi = 0;     // highest row-Column-0 UI logged (no re-append on replay)
    bool mInitialDp;
    std::uint64_t mRate;
    int mColumns;
    double mNominalUi = 0.0;      // seeded free-running UI estimate (0 = auto-acquire)
    std::uint64_t mLastSample = 0;

    bool mLocked = false;
    bool mAlignmentEmitted = false;
    double mRowStart = 0.0;
    double mRowStart0 = 0.0;      // row-0 Sync1 sample (Seek's fixed anchor)
    double mUiSamples = 0.0;
    int mCol = 0;
    std::uint64_t mUi = 0;
};

} // namespace swi3score

#endif // SWI3SCORE_DLVSAMPLESOURCE_H
