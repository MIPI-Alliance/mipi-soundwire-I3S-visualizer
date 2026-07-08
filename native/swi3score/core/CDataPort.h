// Audio payload placement for one Data Port — a C++ port of the visualizer's
// DataPort cascade (src/models/dataport.py), which implements the normative
// §14.2.5 "Payload Visualizer" algorithm.
//
// clock_tick() advances the port by one UI and returns what that UI carries
// for this port (DATA / TX_PRESENT / GUARD / TAIL / nothing) plus, at a data
// bit's sample point, the (channel, sample, bit) identity so the engine can
// place the descrambled bit. The port tracks its own column/row, so the engine
// just ticks it once per UI in lockstep with the bus.

#ifndef SWI3S_CDATAPORT_H
#define SWI3S_CDATAPORT_H

#include "CDpConfig.h"

enum class SwI3sSlot { Empty, Data, TxPresent, Guard0, Guard1, Tail, Drq };

struct DpEmit
{
    SwI3sSlot slot = SwI3sSlot::Empty;
    bool sampleHere = false;   // sample the data line on this UI (bit's sample point)
    int  channel = -1;         // channel number (DATA / TX_PRESENT)
    int  sampleInGroup = 0;
    int  bitInChannel = 0;     // numeric bit weight within the sample (MSB..0)
    bool freshTransport = false;
    bool isSource = false;
};

class CDataPort
{
public:
    CDataPort() = default;

    // Bind configuration. 'numColumns' and 'skippingDenominator' are the
    // shared bus geometry / interface registers.
    void Configure(const SwI3sDpConfig& cfg, int numColumns, int skippingDenominator);

    // Initialize transport state. 'startColumn' is the bus column the FIRST tick
    // lands on (default 0). A truncated/mid-stream capture starts partway into a
    // row: the decoder finds Column 0's phase from the (differential) CDS, and the
    // payload ports must share that same origin or they sample the wrong columns.
    void Initialize(int startColumn = 0);
    DpEmit clock_tick();

    // Re-anchor to a Stream Synchronization Point: row_in_interval becomes 0 and
    // the skipping accumulator resets (Section 9.1.6.2.1). Column position is
    // unchanged (the SSP lands on a row boundary).
    void SyncToSSP();

    const SwI3sDpConfig& Config() const { return mCfg; }
    int RowInInterval() const { return mRowInInterval; }
    int Column() const { return mColumn; }
    bool IntervalSkipped() const { return mIntervalSkipped; }

private:
    enum Phase { kActive, kSpacing, kPending };

    void initializeTransport();
    void startInterval();
    bool advanceSkippingAccumulator();
    void advanceColumn();
    void advanceRow();
    void advanceWideBit();
    void advanceBitInChannel();
    void advanceChannel();
    void advanceSample();
    void advanceChannelGroup();
    DpEmit popGuardTail();
    void armGuardTail();
    bool computeFresh() const;

    SwI3sDpConfig mCfg;
    int mNumColumns = 2;
    int mSkippingDenominator = 1;

    // Derived config, precomputed once in Configure() so the per-UI hot path doesn't
    // re-scan the EnableCh bitmask every tick: channel count, effective channel
    // grouping, and channel NUMBER for each enabled ordinal (index -> channel).
    int mNumChannels = 0;
    int mEffChannelGrouping = 0;
    int mChannelOfIndex[16] = {0};

    // State (mirrors DataPortState).
    int  mColumn = 0;
    int  mRowInInterval = 0;
    bool mIntervalSkipped = false;
    int  mSkippingAccumulator = 0;
    bool mGuardPending = false;
    int  mTailRemaining = 0;

    Phase mPhase = kActive;
    int  mSpacingSlotsRemaining = 0;
    int  mSampleInGroup = 0;
    int  mSamplesInGroupRemaining = 0;
    int  mChannelGroupBaseChannel = 0;
    int  mChannelIndex = 0;
    int  mChannelsInGroupRemaining = 0;
    int  mBitInChannel = 0;
    int  mWideBitRemaining = 0;
    bool mTxpPending = false;
};

#endif // SWI3S_CDATAPORT_H
