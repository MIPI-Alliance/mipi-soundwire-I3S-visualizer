// Abstract source of decoded UIs for the SWI3S decode core.
//
// The Saleae plugin pulls UIs from AnalyzerChannelData via CBitstreamDecoder;
// for SWI3S Studio the same pipeline is driven from an ISampleSource backed by
// a .sal/CSV reader. Each NextUi() advances to the next forwarded-clock edge
// (one edge per UI) and reports the data-line level for the UI it ends, plus the
// absolute sample number, exactly as the plugin's bit decoder did. Returns false
// at end of capture.

#ifndef SWI3SCORE_ISAMPLESOURCE_H
#define SWI3SCORE_ISAMPLESOURCE_H

#include <cstdint>
#include <utility>
#include <vector>

namespace swi3score {

class ISampleSource
{
public:
    virtual ~ISampleSource() = default;

    // Advance one UI. 'rising' = the clock edge that bounds this UI was rising;
    // 'dataHigh' = data-line level for this UI; 'sampleNumber' = absolute sample.
    virtual bool NextUi(bool& rising, bool& dataHigh, std::uint64_t& sampleNumber) = 0;

    // Capture sample rate (Hz), used to derive UI / row rate.
    virtual std::uint64_t SampleRateHz() const = 0;

    // Optional random access: a seekable source can jump to absolute UI index
    // `uiIndex` so the next NextUi() returns that UI. Enables windowed re-decode
    // (e.g. the symbol viewer) without scanning from the start.
    virtual bool CanSeek() const { return false; }
    virtual void Seek(std::uint64_t /*uiIndex*/) {}

    // Absolute index of the UI that the NEXT NextUi() will return (i.e. the count
    // of UIs already consumed). Lets the decoder stamp segment boundaries with an
    // absolute UI so windowed re-decodes (the symbol viewer) align row numbering.
    virtual std::uint64_t UiIndex() const { return 0; }
};

// In-memory source for tests and the synthetic demo: replays an absolute
// per-UI data-level plan. It emits one leading rising "alignment" UI (data low)
// so that, after the decoder aligns to the first rising edge, plan element 0 is
// Column 0 of Row 0 -- matching the plugin's alignToColumnZero semantics.
class MemorySampleSource : public ISampleSource
{
public:
    MemorySampleSource(std::vector<bool> levels, std::uint64_t sampleRateHz,
                       std::uint32_t samplesPerUi = 4)
        : mLevels(std::move(levels)), mRate(sampleRateHz), mSpu(samplesPerUi) {}

    bool NextUi(bool& rising, bool& dataHigh, std::uint64_t& sampleNumber) override
    {
        if (mIdx > mLevels.size()) return false;   // 1 leading UI + N plan levels
        mSample += mSpu;
        rising = (mIdx % 2 == 0);                   // DDR: alternate; UI 0 rising
        dataHigh = (mIdx == 0) ? false : mLevels[mIdx - 1];
        sampleNumber = mSample;
        ++mIdx;
        return true;
    }

    std::uint64_t SampleRateHz() const override { return mRate; }

    std::uint64_t UiIndex() const override { return mIdx; }

private:
    std::vector<bool> mLevels;
    std::uint64_t mRate;
    std::uint32_t mSpu;
    std::size_t mIdx = 0;
    std::uint64_t mSample = 0;
};

} // namespace swi3score

#endif // SWI3SCORE_ISAMPLESOURCE_H
