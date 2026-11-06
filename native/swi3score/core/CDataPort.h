// Audio payload placement for one Data Port — a C++ port of the visualizer's
// DataPort cascade (swi3s_studio/swviz/models/dataport.py), which implements the normative
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

#include <cstdint>

enum class SwI3sSlot { Empty, Data, TxPresent, Guard0, Guard1, Tail, Drq };

struct DpEmit
{
    SwI3sSlot slot = SwI3sSlot::Empty;
    bool sampleHere = false;   // sample the data line on this UI (bit's sample point)
    int  channel = -1;         // channel number (DATA / TX_PRESENT)
    int  sampleInGroup = 0;
    int  bitInChannel = 0;     // numeric bit weight within the sample (MSB..0)
    // SampleSize field (bits - 1) the sample being transported was loaded with: its MSB
    // is bitInChannel == sampleSize. A SampleSize write that lands mid-sample takes effect
    // at the next sample, so this can differ from Config().SampleSize until then.
    int  sampleSize = 0;
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

    // Apply a committed config to a running port without re-anchoring it: the column,
    // interval phase and skipping accumulator are kept (only an SSP restarts the interval,
    // {ASW5103}). The channel set that governs transport follows these rules:
    //   * The channels sent in an interval are exactly one channel set, in channel order.
    //   * If this interval's transport has not begun (the channel walk is at its start:
    //     the commit point is an interval boundary, a row before Offset, a skipped
    //     interval, or between sub-row intervals), the new config governs this interval:
    //     a disabled channel sends nothing in it and an enabled one sends its first sample.
    //   * Otherwise the interval finishes on the channel set it began with, and the new
    //     set governs from the next interval. The specification states no rule for a
    //     commit point after transport has begun; this is the rule the decoder applies.
    // The decoder's DSCR / immediate-write reconfigure and every generator that mirrors it
    // call this one function.
    void ApplyConfigKeepingPhase(const SwI3sDpConfig& cfg, int numColumns,
                                 int skippingDenominator);

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

    // The complete mutable transport state (everything clock_tick/SyncToSSP touch;
    // config-derived fields are NOT included — they follow from Configure). Snapshot
    // it during the streaming decode so a windowed re-decode can restore the exact
    // phase at a checkpoint and tick forward, instead of re-decoding from the start.
    struct State {
        int  column, rowInInterval;
        bool intervalSkipped;
        int  skippingAccumulator;
        int  skipAccumAtBoundary;
        bool guardPending;
        int  tailRemaining;
        int  phase;                      // Phase enum value
        int  spacingSlotsRemaining, sampleInGroup, samplesInGroupRemaining,
             channelGroupBaseChannel, channelIndex, channelsInGroupRemaining,
             bitInChannel, wideBitRemaining;
        bool txpPending;
        int  sampleSizeLoaded;
        // The channel set the current interval's walk runs on (EnableCh mask and effective
        // ChannelGrouping); it becomes the configured set at the next walk start.
        std::uint32_t walkEnableCh;
        int  walkEffChannelGrouping;
    };
    State SaveState() const;
    void  RestoreState(const State& s);

    const SwI3sDpConfig& Config() const { return mCfg; }
    int RowInInterval() const { return mRowInInterval; }
    int Column() const { return mColumn; }
    bool IntervalSkipped() const { return mIntervalSkipped; }

    // Is the skipping pattern back at the top of its cycle? An SSP may only arrive where
    // the accumulated effect of the SkippingNumerator is already 0 (Section 9.1.6.2.1) —
    // the pattern repeats every Interval x SkippingDenominator Rows, not every Interval —
    // so an SSP anywhere else re-phases the skip pattern and garbles the audio from there
    // on. Always true for a port that isn't skipping.
    bool SkipPatternAtCycleStart() const
    {
        return mCfg.SkippingNumerator == 0 || mSkipAccumAtBoundary == 0;
    }

private:
    enum Phase { kActive, kSpacing, kPending };

    void initializeTransport();
    void startInterval();
    void loadChannelSet();
    void setWalkChannels(std::uint32_t enableCh, int effChannelGrouping);
    bool walkAtStart() const;
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

    // The configured channel set, derived in Configure(): EnableCh and the effective
    // channel grouping.
    std::uint32_t mCfgEnableCh = 0;
    int mCfgEffChannelGrouping = 1;
    // The channel set the walk runs on, loaded from the configured set at each walk start
    // (initializeTransport), so the per-UI hot path reads these instead of re-scanning the
    // mask: channel count, effective channel grouping, and the channel NUMBER for each
    // enabled ordinal (index -> channel; entries at and above the count are 0).
    std::uint32_t mWalkEnableCh = 0;
    int mNumChannels = 0;
    int mEffChannelGrouping = 0;
    int mChannelOfIndex[16] = {0};

    // State (mirrors DataPortState).
    int  mColumn = 0;
    int  mRowInInterval = 0;
    bool mIntervalSkipped = false;
    int  mSkippingAccumulator = 0;
    // The accumulator as it stood at the START of the current interval, BEFORE that
    // interval spent its SkippingNumerator step. This is the "accumulated effect" the
    // SSP rule is written against; the live accumulator has already moved on.
    int  mSkipAccumAtBoundary = 0;
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
    // SampleSize the bit counter was last loaded from (mBitInChannel counts down from it).
    int  mSampleSizeLoaded = 0;
};

#endif // SWI3S_CDATAPORT_H
