// Capture-backed sample source: yields UIs by walking forwarded-clock and data
// transition lists (the compact edge representation a .sal / digital CSV decodes
// to). Python parses the file into two sorted transition arrays + initial states
// and hands them here; the per-UI walk runs in C++ at native speed, so the hot
// loop never crosses the Python boundary.
//
// Storage is a NON-OWNING view (pointer + size): the binding constructs it
// straight from NumPy buffers and keeps them alive via pybind11 keep_alive, so
// even multi-GB edge arrays are referenced zero-copy. An owning constructor is
// provided for C++ use/tests.
//
// Semantics mirror the plugin's CBitstreamDecoder: each clock edge bounds one UI;
// the data line is sampled JUST BEFORE the edge (sample S-1), since data changes
// shortly after a clock edge.

#ifndef SWI3SCORE_TRANSITIONSAMPLESOURCE_H
#define SWI3SCORE_TRANSITIONSAMPLESOURCE_H

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

#include "ISampleSource.h"

namespace swi3score {

class TransitionSampleSource final : public ISampleSource
{
public:
    // Non-owning (zero-copy): caller guarantees the arrays outlive this object.
    TransitionSampleSource(const std::uint64_t* clockEdges, std::size_t clockCount,
                           const std::uint64_t* dataEdges, std::size_t dataCount,
                           bool initialClock, bool initialData,
                           std::uint64_t sampleRateHz)
        : mClock(clockEdges), mClockN(clockCount),
          mData(dataEdges), mDataN(dataCount),
          mInitialClock(initialClock), mInitialData(initialData),
          mClockState(initialClock), mDataState(initialData), mRate(sampleRateHz) {}

    // Owning convenience (copies) for C++ callers/tests.
    TransitionSampleSource(std::vector<std::uint64_t> clockEdges,
                           std::vector<std::uint64_t> dataEdges,
                           bool initialClock, bool initialData,
                           std::uint64_t sampleRateHz)
        : mClockOwned(std::move(clockEdges)), mDataOwned(std::move(dataEdges)),
          mClock(mClockOwned.data()), mClockN(mClockOwned.size()),
          mData(mDataOwned.data()), mDataN(mDataOwned.size()),
          mInitialClock(initialClock), mInitialData(initialData),
          mClockState(initialClock), mDataState(initialData), mRate(sampleRateHz) {}

    bool NextUi(bool& rising, bool& dataHigh, std::uint64_t& sampleNumber) override
    {
        if (mCi >= mClockN) return false;
        std::uint64_t s = mClock[mCi++];
        mClockState = !mClockState;
        rising = mClockState;                       // toggled TO high => rising edge

        std::uint64_t x = (s > 0) ? (s - 1) : 0;    // sample just before the edge
        while (mDi < mDataN && mData[mDi] <= x) { mDataState = !mDataState; ++mDi; }
        dataHigh = mDataState;
        sampleNumber = s;
        return true;
    }

    std::uint64_t SampleRateHz() const override { return mRate; }

    bool CanSeek() const override { return true; }

    std::uint64_t UiIndex() const override { return mCi; }

    // Known up front: the clock-edge list IS the full UI count for a seekable capture.
    std::uint64_t TotalUiCount() const override { return mClockN; }

    // Jump so the next NextUi() returns UI `uiIndex`. O(log n): clock state by
    // parity, data state by binary search of the data edges.
    void Seek(std::uint64_t uiIndex) override
    {
        if (uiIndex > mClockN) uiIndex = mClockN;
        mCi = static_cast<std::size_t>(uiIndex);
        mClockState = mInitialClock ^ ((uiIndex & 1ull) != 0);
        if (uiIndex == 0) {
            mDi = 0;
            mDataState = mInitialData;
            return;
        }
        std::uint64_t prev = mClock[uiIndex - 1];
        std::uint64_t prevRead = (prev > 0) ? (prev - 1) : 0;
        mDi = static_cast<std::size_t>(
            std::upper_bound(mData, mData + mDataN, prevRead) - mData);
        mDataState = mInitialData ^ ((mDi & 1u) != 0);
    }

private:
    std::vector<std::uint64_t> mClockOwned;   // empty in the zero-copy path
    std::vector<std::uint64_t> mDataOwned;
    const std::uint64_t* mClock;
    std::size_t mClockN;
    const std::uint64_t* mData;
    std::size_t mDataN;
    bool mInitialClock;
    bool mInitialData;
    bool mClockState;
    bool mDataState;
    std::uint64_t mRate;
    std::size_t mCi = 0;
    std::size_t mDi = 0;
};

} // namespace swi3score

#endif // SWI3SCORE_TRANSITIONSAMPLESOURCE_H
