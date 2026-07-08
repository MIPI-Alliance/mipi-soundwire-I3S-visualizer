// Audio payload reconstruction engine.
//
// Drives one CDataPort per enabled dataport in lockstep with the bus (ticked
// every UI), reads the physical data line at each data bit's sample point,
// descrambles per channel (when ScramblerEn), and assembles MSB-first samples
// into values. TX_PRESENT bits are passed through the channel descrambler to
// keep its LFSR in step (they are scrambled too) but are not placed into a
// sample. Guard/Tail are not scrambled and carry no audio.

#ifndef SWI3S_CPAYLOADENGINE_H
#define SWI3S_CPAYLOADENGINE_H

#include <map>
#include <deque>
#include <vector>
#include <cstdio>
#include <LogicPublicTypes.h>

#include "CDataPort.h"
#include "CFlowControlPort.h"
#include "CDescrambler.h"
#include "CDpConfig.h"

struct AudioSample
{
    int dp = 0;
    int deviceNum = -1;
    int channel = 0;
    U64 value = 0;
    int sampleSize = 0;     // bits in the sample
    U64 index = 0;          // running per-(dp,channel) sample count
    U64 startSample = 0;
    U64 endSample = 0;
};

// A located flow-control (DRQ) bit, for annotation only (value not assembled).
struct DrqEvent
{
    int dp = 0;
    int deviceNum = -1;
    bool isSource = false;  // DRQ direction (true = peripheral/FCP drives the bus)
    U64 sample = 0;
};

// One sampled data bit (diagnostic overlay): where on the wire a data port's bit
// was read, and whether it is the MSB (start of a new sample interval).
struct BitSample
{
    U64 sample = 0;
    int deviceNum = -1;
    int dp = 0;
    int channel = 0;
    bool isStart = false;   // MSB: this bit begins a new sample interval
};

class CPayloadEngine
{
public:
    // Close the debug dump file (opened in Configure when SWI3S_DUMP_PATH is set)
    // so the FILE* isn't leaked for the engine's lifetime.
    ~CPayloadEngine() { if (mDumpFile) { std::fclose(mDumpFile); mDumpFile = nullptr; } }

    void Configure(const SwI3sConfig& cfg);
    // Set the bus column the next tick lands on (the capture's Column-0 phase, as
    // found from the differential CDS). A truncated/mid-stream capture starts partway
    // into a row; without this the payload ports assume column 0 and sample every port
    // that-many columns early. Re-applied to every port on the next Configure.
    void SetStartColumn(int col) { mStartColumn = col; }
    void Reset();
    bool Enabled() const { return !mDps.empty(); }

    // Diagnostic: collect a per-data-bit sample point for every assembled data bit
    // (off by default). Persists across reconfigurations; the accumulated points
    // are read via bitSamples().
    void SetCollectBits(bool on) { mCollectBits = on; }
    const std::vector<BitSample>& bitSamples() const { return mBitSamples; }

    // Re-anchor all dataports to a Stream Synchronization Point observed on the
    // bus: each port's row counter is set so this row is row_in_interval == 0.
    void SyncToSSP();

    // Advance every dataport by one UI. 'level' is the physical data-line bit
    // for this UI (true = high). Appends any completed samples to 'out'.
    void Tick(bool level, U64 sampleNumber, std::vector<AudioSample>& out);

private:
    struct ChannelRuntime {
        CDescrambler descram;
        U64 acc = 0;
        U64 index = 0;
        U64 startSample = 0;
    };
    struct DpRuntime {
        int dpIndex = 0;
        int deviceNum = -1;
        bool scramblerEn = false;
        int  sampleSize = 0;
        bool hasFcp = false;             // DRQ active (RxControlled / Async)
        bool isSource = false;           // peripheral drives this port onto the bus
        CDataPort port;
        CFlowControlPort fcp;
        // One runtime per channel number (0..15). Flat array (not a map) so the
        // per-bit assembly in Tick is an O(1) index, not a red-black-tree lookup in
        // the innermost decode loop. Disabled channels are simply never ticked.
        ChannelRuntime channels[16];
        // DIAGNOSTIC (SWI3S_SRC_SHIFT): delay the level<->bit association for a
        // source port by N UIs, to probe peripheral clock-to-data round-trip skew.
        struct PendingBit { bool has = false; int channel = 0; int bitInChannel = 0; bool isData = false; };
        std::deque<PendingBit> pending;
    };

    SwI3sConfig mCfg;
    std::vector<DpRuntime> mDps;
    // Source ports are sampled this many UIs later than their register column grid,
    // to compensate the peripheral's clock-to-data round-trip skew (see Configure).
    static const int kDefaultSourceShift = 0;
    int mSourceShift = kDefaultSourceShift;
    // Column the next tick lands on (capture's Column-0 phase; 0 = starts on a row
    // boundary). Applied to every port in Configure so audio shares the CDS origin.
    int mStartColumn = 0;
    // Debug dump (env SWI3S_DUMP_PATH/DP/MAX): per assembled data bit, write
    // "sampleNumber channel bitInChannel recoveredLevel" for the target DP.
    std::FILE* mDumpFile = nullptr;
    long mDumpMax = 0, mDumpCount = 0;
    int  mDumpDp = -1;
    // Diagnostic per-bit collection (SetCollectBits): accumulates across the whole
    // run; capped so a pathological capture can't exhaust memory.
    bool mCollectBits = false;
    std::vector<BitSample> mBitSamples;
    static const std::size_t kMaxBitSamples = 24000000;
};

#endif // SWI3S_CPAYLOADENGINE_H
