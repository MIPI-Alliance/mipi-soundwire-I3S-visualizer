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
// SetColumns().
//
// A Link brought up again restarts the clock: the decoder calls Reacquire() where the Link
// goes inactive and again at the restart, and the DLL locks afresh at the bring-up's first
// Row Sync with the nominal UI of its Safe-Lock geometry (the seeded row period, which a
// bring-up does not change, over its column count). The stretch between is skipped, not
// tracked: it has no rows, and the start sequence's link-control pulses are single-ended
// edges the loop would snap to.
//
// The clock needs at least kMinUiSamples samples per UI: a lock, re-acquisition or row
// whose UI is shorter ends the source. A stretch whose next rising edge lies more than
// kMaxCoastRows row periods past the predicted Sync1 holds no rows (every row opens on a
// rising edge), so the clock relocks at that edge without coasting through it; with no
// rising edge left and the capture running on further, the source ends.

#ifndef SWI3SCORE_DLVSAMPLESOURCE_H
#define SWI3SCORE_DLVSAMPLESOURCE_H

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <utility>
#include <vector>

#include "ISampleSource.h"
#include "core/SwI3sProtocolDefs.h"

namespace swi3score {

class DlvSampleSource final : public ISampleSource
{
public:
    // Non-owning (zero-copy): dpEdges[0..dpCount) are the sorted DP-wire transition
    // samples, viewed in place. They must outlive this object, either because the caller
    // guarantees it or because `owner` (released when this object is destroyed) keeps
    // them alive. initialDp: DP level before edge 0.
    // initialColumns: Safe-Lock column count to start with (snoop updates it later).
    // nominalUiSamples: expected UI period in samples for the INITIAL geometry (the PLL's
    // free-running estimate to acquire from). >0 seeds the loop so it locks to the once-
    // per-row reference edge instead of guessing the period from the first two rising edges
    // (which grabs an intra-row CDS/data edge when the first CDS bit is 1). 0 = auto.
    DlvSampleSource(const std::uint64_t* dpEdges, std::size_t dpCount, bool initialDp,
                    std::uint64_t sampleRateHz, int initialColumns,
                    double nominalUiSamples = 0.0,
                    std::shared_ptr<const void> owner = nullptr)
        : mOwner(std::move(owner)), mEdges(dpEdges), mEdgeN(dpCount),
          mInitialDp(initialDp), mRate(sampleRateHz),
          mColumns(validColumns(initialColumns)),
          mNominalUi(nominalUiSamples), mNominalRow(nominalUiSamples * mColumns)
    {
        init();
    }

    // Owning convenience (copies) for C++ callers/tests.
    DlvSampleSource(std::vector<std::uint64_t> dpEdges, bool initialDp,
                    std::uint64_t sampleRateHz, int initialColumns,
                    double nominalUiSamples = 0.0)
        : mEdgesOwned(std::move(dpEdges)), mEdges(mEdgesOwned.data()),
          mEdgeN(mEdgesOwned.size()), mInitialDp(initialDp), mRate(sampleRateHz),
          mColumns(validColumns(initialColumns)),
          mNominalUi(nominalUiSamples), mNominalRow(nominalUiSamples * mColumns)
    {
        init();
    }

    DlvSampleSource(const DlvSampleSource&) = delete;
    DlvSampleSource& operator=(const DlvSampleSource&) = delete;

    bool NextUi(bool& rising, bool& dataHigh, std::uint64_t& sampleNumber) override
    {
        if (!mLocked && !lock()) return false;

        // One leading "alignment" UI (mirrors MemorySampleSource): the decoder aligns to
        // the first rising edge and treats the NEXT UI as Column 0. Report the row's
        // Sync1 as that alignment edge, then start emitting real Column 0..N-1.
        if (!mAlignmentEmitted) {
            if (mEnded) return false;               // Reacquire found no row edge
            mAlignmentEmitted = true;
            rising = true;
            dataHigh = false;                       // pre-Sync1 (Sync0) level
            sampleNumber = mRowSyncs[mRow];         // the row's edge, exact
            return true;
        }

        if (mCol >= mColumns && !advanceRow()) return false;

        double mid = mRowStart + (mCol + 0.5) * mUiSamples;
        if (!(mid + 0.5 < kSampleLimit)) return false;   // past every sample number
        std::uint64_t s = toSample(mid + 0.5);
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
    // speeds up as the row subdivides into more columns, while the row rate holds), so a
    // seek cannot step uiIndex*mUiSamples from row 0. Seek binary-searches the row log
    // (mRowSyncs / mRowStartUi, recorded as the DLL runs) for the row containing `uiIndex`,
    // restores that row's start edge + the UI width the live decode used for it (mRowUi),
    // and sets the column within it. O(log rows); NextUi then produces the same (sample,
    // level) as the full decode: past the row it takes each logged row's edge and UI
    // (replayRow), so a window reaches across a re-acquisition too.
    bool CanSeek() const override { return true; }

    void Seek(std::uint64_t uiIndex) override
    {
        if (!mLocked && !lock()) return;
        mAlignmentEmitted = true;              // past the one-shot alignment UI
        mEnded = false;
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
        mRow = r;
        mRowStart = static_cast<double>(mRowSyncs[r]);
        mCol = static_cast<int>(uiIndex - mRowStartUi[r]);
        // The column count comes from the row's UI span in the log, the UI width from the
        // value the live decode used for the row. The final (still-open) row keeps the live
        // values. The row a stop cut short takes the UI it had (see Reacquired).
        if (r + 1 < mRowSyncs.size()) {
            int colsInRow = static_cast<int>(mRowStartUi[r + 1] - mRowStartUi[r]);
            if (colsInRow > 0) mColumns = colsInRow;
        }
        if (r < mRowUi.size()) mUiSamples = mRowUi[r];
        if (const Reacquired* k = reacquiredAt(r + 1)) mUiSamples = k->cutUiSamples;
    }
    // Approximate (progress only): the UIs already produced plus the rest of the capture
    // at the current UI period — the recovered one once locked, the seeded nominal one
    // before (0 when neither is known). The period changes at a geometry commit, so the
    // estimate follows it and the decoder re-reads it as the decode runs.
    std::uint64_t TotalUiCount() const override
    {
        const double ui = (mLocked && mUiSamples > 0.0) ? mUiSamples : mNominalUi;
        if (ui <= 0.0 || mLastSample == 0) return 0;
        const double at = mLocked ? mRowStart + mCol * ui : 0.0;
        const double rest = static_cast<double>(mLastSample) - at;
        return mUi + toSample(rest / ui);
    }

    // Snoop pushes a committed NumColumns change at a row boundary; the DLL re-subdivides
    // the row from here on. Ignored mid-row (columns only change at a commit SSP, which
    // lands on a row boundary in the decoder's feed loop).
    void SetColumns(int n) override
    {
        if (n < swi3s::kMinColumnCount || n > swi3s::kMaxColumnCount || n == mColumns) return;
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

    // A later bring-up (see the header comment): lock at the first rising edge at or after
    // `sample`, as lock() does at the first, at `columns` columns and the seeded row period
    // over them (the tracked one when none was seeded). The row log stays in UI order for
    // Seek: the UIs consumed since the last row edge belong to the row the stop cut short.
    // Called twice for one bring-up (where the Link goes inactive, then at the restart
    // with its geometry), the second replaces the first, which consumed no UI.
    void Reacquire(std::uint64_t sample, int columns) override
    {
        if (!mLocked && !lock()) return;
        if (columns < swi3s::kMinColumnCount || columns > swi3s::kMaxColumnCount) columns = mColumns;
        const std::size_t at = risingAtOrAfter(sample);
        const std::uint64_t edge = (at < mEdgeN) ? mEdges[at] : UINT64_MAX;
        double cutUi = mUiSamples;
        if (!mReacquired.empty() && mRowStartUi[mReacquired.back().row] >= mUi)
            cutUi = mReacquired.back().cutUiSamples;
        while (!mRowStartUi.empty() && mRowStartUi.back() >= mUi) {
            mRowStartUi.pop_back();
            mRowSyncs.pop_back();
            mRowUi.pop_back();
        }
        while (!mReacquired.empty() && mReacquired.back().row >= mRowSyncs.size())
            mReacquired.pop_back();
        mAlignmentEmitted = false;
        mEnded = (edge == UINT64_MAX);
        if (mEnded) return;
        const double row = (mNominalRow > 0.0) ? mNominalRow : mUiSamples * mColumns;
        if (!(row / columns >= kMinUiSamples)) { mEnded = true; return; }
        mColumns = columns;
        mUiSamples = row / columns;
        mRowStart = static_cast<double>(edge);
        mCol = 0;
        mRowSyncs.push_back(edge);
        mRowStartUi.push_back(mUi);
        mRowUi.push_back(mUiSamples);
        mLoggedThroughUi = mUi;
        mRow = mRowSyncs.size() - 1;
        mReacquired.push_back({mRow, mUiSamples, cutUi, columns});
    }

    // The recovered per-row Sync1 sample points (the bit-clock reference), collected as the
    // DLL runs — surfaced to the Raw Capture view to show clock recovery working.
    const std::vector<std::uint64_t>& RecoveredRowSyncs() const { return mRowSyncs; }
    double RecoveredUiSamples() const { return mUiSamples; }

    // The fewest samples per UI the recovered clock decodes (swi3score.dlv_min_ui_samples).
    static constexpr double kMinUiSamples = 1.0;

private:
    static constexpr double kMaxCoastRows = 64.0;
    static constexpr double kSampleLimit = 18446744073709551616.0;   // 2^64

    // `x` truncated to a sample number, saturating: 0 at or below 0 and for NaN,
    // UINT64_MAX at or past 2^64. Every double-to-sample conversion goes through it, since
    // converting a double outside the uint64 range is undefined and compilers differ on it.
    static std::uint64_t toSample(double x)
    {
        if (!(x > 0.0)) return 0;
        if (x >= kSampleLimit) return UINT64_MAX;
        return static_cast<std::uint64_t>(x);
    }

    // The Safe-Lock count to start with: `columns` when legal, else the cold-start count.
    static int validColumns(int columns)
    {
        return (columns >= swi3s::kMinColumnCount && columns <= swi3s::kMaxColumnCount)
                   ? columns : swi3s::kColdStartColumnCount;
    }

    // A re-acquisition (Reacquire), for a windowed decode to follow: the row it opened, the
    // UI and column count it locked with, and the UI of the row before it, which the stop
    // cut short.
    struct Reacquired {
        std::size_t row = 0;
        double uiSamples = 0.0;
        double cutUiSamples = 0.0;
        int columns = 0;
    };

    // The rising (0->1) edges, the row-sync candidates, are the edges whose post-edge
    // level is high: with DP low before edge 0 the even-indexed ones, with DP high the
    // odd-indexed ones. They are addressed in place by that parity, not copied.
    std::size_t firstRising() const { return mInitialDp ? 1u : 0u; }

    // Index of the first rising edge at or after `sample`, or >= mEdgeN when none.
    std::size_t risingAtOrAfter(std::uint64_t sample) const
    {
        std::size_t i = static_cast<std::size_t>(
            std::lower_bound(mEdges, mEdges + mEdgeN, sample) - mEdges);
        if ((i & 1u) != firstRising()) ++i;
        return i;
    }

    void init()
    {
        mLastSample = (mEdgeN == 0) ? 0 : mEdges[mEdgeN - 1];
    }

    // DP logical level at `sample`: initial level XOR parity of edges at/before it.
    // NextUi queries strictly-increasing samples, so a forward cursor (count of edges
    // <= sample) is O(1) amortized instead of an O(log m) upper_bound PER UI — this is
    // the whole DLV decode path. The two guards make it self-correcting for any sample
    // order (a Seek jumps the cursor back once, bounded), so it's a drop-in for the
    // upper_bound: mEdgeCursor lands on the first edge index > sample either way.
    bool levelAt(std::uint64_t sample) const
    {
        while (mEdgeCursor < mEdgeN && mEdges[mEdgeCursor] <= sample) ++mEdgeCursor;
        while (mEdgeCursor > 0 && mEdges[mEdgeCursor - 1] > sample) --mEdgeCursor;
        return mInitialDp ^ ((mEdgeCursor & 1u) != 0);
    }

    // Nearest rising edge to `predicted` within +/- `window` samples, or UINT64_MAX.
    std::uint64_t nearestRising(std::uint64_t predicted, std::uint64_t window) const
    {
        const std::size_t at = risingAtOrAfter(predicted);
        std::uint64_t best = UINT64_MAX, bestDist = window + 1;
        const std::size_t prev = (at >= firstRising() + 2) ? at - 2 : at;
        for (std::size_t cand : {at, prev}) {
            if (cand >= mEdgeN) continue;
            const std::uint64_t e = mEdges[cand];
            std::uint64_t d = (e > predicted) ? (e - predicted) : (predicted - e);
            if (d <= window && d < bestDist) { best = e; bestDist = d; }
        }
        return best;
    }

    // Acquire: first rising edge = Row 0's Sync1 (the reference-edge phase). Seed the UI
    // period from the nominal free-running estimate when given (robust: the aperture then
    // tracks the once-per-row reference, ignoring intra-row CDS/data edges); otherwise fall
    // back to guessing it from the first two rising edges / columns.
    bool lock()
    {
        const std::size_t r0 = firstRising();
        if (r0 >= mEdgeN) return false;
        mRowStart = static_cast<double>(mEdges[r0]);
        mRowStart0 = static_cast<double>(mEdges[r0]);   // fixed row-0 anchor for Seek()
        if (mNominalUi > 0.0) {
            mUiSamples = mNominalUi;
        } else {
            if (r0 + 2 >= mEdgeN) return false;
            mUiSamples = static_cast<double>(mEdges[r0 + 2] - mEdges[r0]) / mColumns;
        }
        if (!(mUiSamples >= kMinUiSamples) || !std::isfinite(mUiSamples)) return false;
        mRowSyncs.push_back(mEdges[r0]);
        mRowStartUi.push_back(0);          // row 0's Column 0 is source UI 0 (Seek row lookup)
        mRowUi.push_back(mUiSamples);
        mRow = 0;
        mLocked = true;
        mCol = 0;
        return true;
    }

    // End of a row: predict the next Sync1 (rowStart + columns*UI), snap to the nearest
    // observed rising edge (tracks jitter/drift; coasts on the prediction if none is within
    // half a UI — a missing Row Sync, which the spec tolerates), and refine the UI estimate.
    // A next rising edge more than kMaxCoastRows rows on is relocked to directly, with the
    // UI unchanged; none at all, with the capture running on further, ends the source, as
    // does a UI under kMinUiSamples or a row that does not advance.
    bool advanceRow()
    {
        if (mRow + 1 < mRowSyncs.size()) return replayRow(mRow + 1);
        if (!(mUiSamples >= kMinUiSamples)) return false;
        double period = mColumns * mUiSamples;
        double predicted = mRowStart + period;
        if (predicted > static_cast<double>(mLastSample) + mUiSamples) return false;
        const std::uint64_t at = toSample(predicted + 0.5);
        std::uint64_t window = toSample(mUiSamples * 0.5 + 0.5);
        std::uint64_t snap = nearestRising(at, window);
        bool relocked = false;
        std::uint64_t newStart = (snap != UINT64_MAX) ? snap : at;
        if (snap == UINT64_MAX) {
            const std::size_t next = risingAtOrAfter(at);
            const double limit = predicted + kMaxCoastRows * period;
            if (next >= mEdgeN) {
                if (static_cast<double>(mLastSample) > limit) return false;
            } else if (static_cast<double>(mEdges[next]) > limit) {
                newStart = mEdges[next];
                relocked = true;
            }
        }
        if (static_cast<double>(newStart) <= mRowStart) return false;
        // Refine UI from the measured row period (light EMA); guard against a bad snap.
        double measured = (static_cast<double>(newStart) - mRowStart) / mColumns;
        if (!relocked && measured > 0.0) mUiSamples = 0.75 * mUiSamples + 0.25 * measured;
        mRowStart = newStart;
        // Log this row's (edge, Column-0 UI) — but only the FIRST time we advance through it.
        // A windowed re-decode (Seek + NextUi, e.g. the CDS symbol viewer or the bit-sample
        // overlay) walks rows already logged; re-appending would corrupt the recovered-clock
        // marks and the Seek row lookup itself. Only genuinely new (higher-UI) rows are logged.
        if (mUi > mLoggedThroughUi || mRowSyncs.empty()) {
            mRowSyncs.push_back(newStart);
            mRowStartUi.push_back(mUi);        // source UI of this row's Column 0 (Seek row lookup)
            mRowUi.push_back(mUiSamples);      // the UI the row's samples are placed with
            mLoggedThroughUi = mUi;
        }
        mRow = mRowSyncs.size() - 1;
        mCol = 0;
        return true;
    }

    // Row `n` of the log, reached again by a windowed decode (after Seek). It opens on the
    // edge the live decode logged, with the column count the log gives it (a commit's
    // SetColumns, which nothing pushes to a replay) and the UI the live decode placed the
    // row's samples with (mRowUi). A re-acquired row takes the UI and column count it
    // locked with; the row a stop cut short takes the UI it had and ends where the
    // re-acquired row begins. A replay is the live decode exactly.
    bool replayRow(std::size_t n)
    {
        const double start = static_cast<double>(mRowSyncs[n]);
        if (const Reacquired* k = reacquiredAt(n)) {
            mUiSamples = k->uiSamples;
            mColumns = k->columns;
        } else {
            if (n + 1 < mRowStartUi.size() && !reacquiredAt(n + 1)) {
                const int cols = static_cast<int>(mRowStartUi[n + 1] - mRowStartUi[n]);
                if (cols >= swi3s::kMinColumnCount) mColumns = cols;
            }
            mUiSamples = mRowUi[n];
        }
        if (const Reacquired* k = reacquiredAt(n + 1)) {
            mUiSamples = k->cutUiSamples;
            mColumns = static_cast<int>(mRowStartUi[n + 1] - mRowStartUi[n]);
        }
        mRowStart = start;
        mRow = n;
        mCol = 0;
        return true;
    }

    // The re-acquisition that opened row `n` of the log, or null.
    const Reacquired* reacquiredAt(std::size_t n) const
    {
        auto it = std::lower_bound(mReacquired.begin(), mReacquired.end(), n,
            [](const Reacquired& k, std::size_t v) { return k.row < v; });
        return (it != mReacquired.end() && it->row == n) ? &*it : nullptr;
    }

    std::shared_ptr<const void> mOwner;     // holds the viewed buffer (zero-copy path)
    std::vector<std::uint64_t> mEdgesOwned; // empty in the zero-copy path
    const std::uint64_t* mEdges;            // DP wire transitions
    std::size_t mEdgeN;
    mutable std::size_t mEdgeCursor = 0;    // levelAt forward cursor: #edges <= last sample
    std::vector<std::uint64_t> mRowSyncs;   // recovered Sync1 samples (bit-clock reference)
    std::vector<std::uint64_t> mRowStartUi; // source UI index at each row's Column 0 (Seek)
    std::vector<double> mRowUi;             // UI width (samples) each row's samples were placed with
    std::uint64_t mLoggedThroughUi = 0;     // highest row-Column-0 UI logged (no re-append on replay)
    bool mInitialDp;
    std::uint64_t mRate;
    int mColumns;
    double mNominalUi = 0.0;      // seeded free-running UI estimate (0 = auto-acquire)
    double mNominalRow = 0.0;     // ...its row period, which a later bring-up locks with too
    std::vector<Reacquired> mReacquired;   // in row order
    std::size_t mRow = 0;         // the log row being emitted (live: the last one logged)
    bool mEnded = false;          // Reacquire found no row edge: the source has ended
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
