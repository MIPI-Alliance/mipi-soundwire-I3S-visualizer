// Data Port placement cascade. Faithful C++ port of dataport.py. See header.

#include "CDataPort.h"

void CDataPort::Configure(const SwI3sDpConfig& cfg, int numColumns, int skippingDenominator)
{
    mCfg = cfg;
    mNumColumns = numColumns;
    mSkippingDenominator = skippingDenominator;
    // Precompute the EnableCh-derived values once (the per-UI hot path reads these
    // instead of re-scanning the 16-bit mask on every tick — see clock_tick).
    mNumChannels = 0;
    for (int i = 0; i < 16; ++i)
        if (cfg.EnableCh & (1u << i)) mChannelOfIndex[mNumChannels++] = i;
    mEffChannelGrouping = (cfg.ChannelGrouping == 0) ? mNumChannels : cfg.ChannelGrouping;
    // A channel group can't hold more channels than are enabled. Clamp here so the
    // per-UI channel walk (advanceChannel) can never index mChannelOfIndex[16] past
    // its mNumChannels populated entries -- a config CSV can supply an unbounded
    // ChannelGrouping (the CSV path doesn't mask it, unlike the &0xF register path).
    if (mEffChannelGrouping > mNumChannels) mEffChannelGrouping = mNumChannels;
    // Keep it >= 1 so initializeTransport's `mEffChannelGrouping - 1` can't go
    // negative when no channels are enabled (mNumChannels == 0). Such a port never
    // ticks a data slot (inWindow requires base < nch), but keep the state sane.
    if (mEffChannelGrouping < 1) mEffChannelGrouping = 1;
}

void CDataPort::initializeTransport()
{
    mPhase = kActive;
    mSpacingSlotsRemaining = 0;
    mSampleInGroup = 0;
    mSamplesInGroupRemaining = mCfg.SampleGrouping;
    mChannelGroupBaseChannel = 0;
    mChannelIndex = 0;
    mChannelsInGroupRemaining = mEffChannelGrouping - 1;
    mBitInChannel = mCfg.SampleSize;
    mWideBitRemaining = mCfg.BitWidth;
    mTxpPending = mCfg.txpEnabled();
}

void CDataPort::Initialize(int startColumn)
{
    // Seed the column to the capture's Column-0 phase (0 for a capture that begins
    // on a row boundary). Only the horizontal position is phased here; the interval
    // (vertical) phase is row_in_interval, which the SSP anchors separately.
    mColumn = ((startColumn % mNumColumns) + mNumColumns) % mNumColumns;
    mRowInInterval = 0;
    mIntervalSkipped = false;
    mSkippingAccumulator = 0;
    mSkipAccumAtBoundary = 0;
    mGuardPending = false;
    mTailRemaining = 0;
    initializeTransport();
    startInterval();
}

void CDataPort::SyncToSSP()
{
    // The SSP defines row_in_interval == 0 and clears accumulated skipping
    // (Section 9.1.6.2.1). The interval that BEGINS at the SSP is then started like
    // any other: it spends the accumulator's first step (A += Numerator) and takes
    // its own skip decision, exactly as Initialize() does. Clearing the accumulator
    // WITHOUT starting the interval left that step unspent, so every later decision
    // landed one interval late -- the decode read the interval the device had
    // skipped (idle bus, sample of 0) and skipped the next one, dropping the sample
    // that was really transported there.
    mRowInInterval = 0;
    mSkippingAccumulator = 0;
    mGuardPending = false;
    mTailRemaining = 0;
    startInterval();
}

CDataPort::State CDataPort::SaveState() const
{
    return { mColumn, mRowInInterval, mIntervalSkipped, mSkippingAccumulator,
             mSkipAccumAtBoundary,
             mGuardPending, mTailRemaining, static_cast<int>(mPhase),
             mSpacingSlotsRemaining, mSampleInGroup, mSamplesInGroupRemaining,
             mChannelGroupBaseChannel, mChannelIndex, mChannelsInGroupRemaining,
             mBitInChannel, mWideBitRemaining, mTxpPending };
}

void CDataPort::RestoreState(const State& s)
{
    mColumn = s.column;
    mRowInInterval = s.rowInInterval;
    mIntervalSkipped = s.intervalSkipped;
    mSkippingAccumulator = s.skippingAccumulator;
    mSkipAccumAtBoundary = s.skipAccumAtBoundary;
    mGuardPending = s.guardPending;
    mTailRemaining = s.tailRemaining;
    mPhase = static_cast<Phase>(s.phase);
    mSpacingSlotsRemaining = s.spacingSlotsRemaining;
    mSampleInGroup = s.sampleInGroup;
    mSamplesInGroupRemaining = s.samplesInGroupRemaining;
    mChannelGroupBaseChannel = s.channelGroupBaseChannel;
    mChannelIndex = s.channelIndex;
    mChannelsInGroupRemaining = s.channelsInGroupRemaining;
    mBitInChannel = s.bitInChannel;
    mWideBitRemaining = s.wideBitRemaining;
    mTxpPending = s.txpPending;
}

bool CDataPort::advanceSkippingAccumulator()
{
    if (mCfg.SkippingNumerator == 0) {
        return false;
    }
    mSkippingAccumulator += mCfg.SkippingNumerator;
    if (mSkippingAccumulator < mSkippingDenominator) {
        return false;
    }
    mSkippingAccumulator -= mSkippingDenominator;
    return true;
}

void CDataPort::startInterval()
{
    mSkipAccumAtBoundary = mSkippingAccumulator;
    mIntervalSkipped = advanceSkippingAccumulator();
    initializeTransport();
}

bool CDataPort::computeFresh() const
{
    // The sample point is the LAST UI of a wide bit for BOTH directions (a sniffer
    // always sinks), so the "fresh transport" first-bit sample lands at
    // mWideBitRemaining == 0 regardless of PortDirection -- matching emitPoint.
    return mChannelGroupBaseChannel == 0
        && mChannelIndex == 0
        && mSampleInGroup == 0
        && mBitInChannel == mCfg.SampleSize
        && mSamplesInGroupRemaining == mCfg.SampleGrouping
        && mWideBitRemaining == 0
        && mTxpPending == mCfg.txpEnabled();
}

DpEmit CDataPort::clock_tick()
{
    DpEmit e;
    e.isSource = mCfg.isSource();

    const int nch = mNumChannels;
    bool inWindow =
        nch > 0
        && !mIntervalSkipped
        && mChannelGroupBaseChannel < nch
        && mRowInInterval >= mCfg.Offset
        && mColumn >= mCfg.HorizontalStart
        && mColumn <= mCfg.horizontalEnd();

    if (inWindow) {
        if (mPhase == kSpacing) {
            if (--mSpacingSlotsRemaining == 0) {
                mPhase = kActive;
            }
            // fall through to guard/tail drain
        } else if (mPhase == kActive) {
            // KEY INSIGHT: a bus SNIFFER is always a sink. Whether the peripheral
            // is sourcing data to the manager or sinking it from the manager, WE
            // sample it off the wire as an observer -- so for a wide bit we sample at
            // the LAST UI (the settled value), like a sink, REGARDLESS of
            // PortDirection. (Sampling a source's FIRST UI would read the unsettled
            // leading edge of the wide bit; this is only invisible for BitWidth==0,
            // where first UI == last UI.) Separately, peripheral-SOURCED data arrives
            // ~1 UI late (clock-to-data round-trip skew), so it must be sampled one
            // column later than its register HorizontalStart -- see CPayloadEngine's
            // source sample-shift.
            bool emitPoint = (mWideBitRemaining == 0);

            SwI3sSlot kind = mTxpPending ? SwI3sSlot::TxPresent : SwI3sSlot::Data;
            e.slot = kind;
            // Bit IDENTITY (channel / sample / bit) is reported on EVERY UI of the
            // (possibly wide) bit — mChannelIndex/mBitInChannel don't change until the
            // wide bit completes — so both columns of a wide bit carry the same bit and
            // the grid merges the pair *within its row* into `C{ch}B{bit} xN`. This is
            // independent of where the sample point falls (the last UI), which
            // previously left a wide sink bit's other column labelled with the
            // neighbouring bit (and a phantom leading held column).
            e.channel = mChannelOfIndex[mChannelIndex];
            e.sampleInGroup = mSampleInGroup;
            e.bitInChannel = (kind == SwI3sSlot::TxPresent) ? 0 : mBitInChannel;
            if (emitPoint) {
                e.sampleHere = true;        // the settled sample UI (sniffer marker)
                e.freshTransport = computeFresh();
            }

            advanceWideBit();
            armGuardTail();
            advanceColumn();
            return e;
        }
    }

    e = popGuardTail();
    e.isSource = mCfg.isSource();
    advanceColumn();
    return e;
}

void CDataPort::advanceColumn()
{
    if (++mColumn >= mNumColumns) {
        advanceRow();
    }
}

void CDataPort::advanceRow()
{
    mColumn = 0;
    mGuardPending = false;
    mTailRemaining = 0;

    // Channel-group spacing is a WITHIN-row gap and must not survive the row.
    // Spacing only counts down inside the transport window, so a window that
    // closes before the countdown finishes would otherwise carry the remainder
    // into the next row and eat its first in-window UI — the next row's data then
    // starts one column late (and alternates, as the error re-accrues). The 1.74
    // visualizer cleared this explicitly when the column passed
    // HorizontalStart+HorizontalCount (its `done_with_row` latch, which also set
    // channel_group_is_spacing = 0); the v2/v3 rewrite folded that latch into the
    // in-window predicate and dropped the side effect. Verified against 1.74 over
    // 768 register combinations: without this, 180 of them mis-place data.
    if (mPhase == kSpacing) {
        mPhase = kActive;
        mSpacingSlotsRemaining = 0;
    }

    if (mPhase == kPending && mChannelGroupBaseChannel < mNumChannels) {
        mPhase = kActive;
    }

    ++mRowInInterval;
    if (mRowInInterval > mCfg.Interval) {
        mRowInInterval = 0;
        startInterval();
    }
}

void CDataPort::advanceWideBit()
{
    if (mWideBitRemaining == 0) {
        mWideBitRemaining = mCfg.BitWidth;
        advanceBitInChannel();
    } else {
        --mWideBitRemaining;
    }
}

void CDataPort::advanceBitInChannel()
{
    if (mTxpPending) {
        mTxpPending = false;
        return;
    }
    if (mBitInChannel == 0) {
        mBitInChannel = mCfg.SampleSize;
        advanceChannel();
    } else {
        --mBitInChannel;
    }
}

void CDataPort::advanceChannel()
{
    mTxpPending = mCfg.txpEnabled();
    if (mChannelsInGroupRemaining == 0) {
        mChannelIndex = mChannelGroupBaseChannel;
        // Reset to THIS group's channel count, not the nominal grouping. The trailing
        // group is PARTIAL when the enabled-channel count isn't a multiple of
        // ChannelGrouping (3 channels in groups of 2 leaves a group of 1). Resetting
        // to the full mEffChannelGrouping made that last group transport as if it
        // were full, so the port emitted one extra DATA cell per interval and walked
        // mChannelIndex past the enabled channels — the interval carried
        // channels x samples x bits + 1 slots (7 where 3x2x1 = 6). Mirrors the same
        // clamp in advanceChannelGroup (and in swviz's _advance_channel, which had
        // it; this is what made the two engines disagree).
        int groupChannels = mNumChannels - mChannelGroupBaseChannel;
        if (groupChannels > mEffChannelGrouping) groupChannels = mEffChannelGrouping;
        mChannelsInGroupRemaining = (groupChannels > 1) ? groupChannels - 1 : 0;
        advanceSample();
    } else {
        ++mChannelIndex;
        --mChannelsInGroupRemaining;
    }
}

void CDataPort::advanceSample()
{
    if (mSamplesInGroupRemaining == 0) {
        mSampleInGroup = 0;
        mSamplesInGroupRemaining = mCfg.SampleGrouping;
        advanceChannelGroup();
    } else {
        ++mSampleInGroup;
        --mSamplesInGroupRemaining;
    }
}

void CDataPort::advanceChannelGroup()
{
    const int ecg = mEffChannelGrouping;
    const int nch = mNumChannels;
    mChannelGroupBaseChannel += ecg;
    bool complete = mChannelGroupBaseChannel >= nch;

    if (complete) {
        if (mCfg.SubRowInterval) {
            initializeTransport();
        } else {
            mPhase = kPending;
            return;
        }
    } else {
        int remaining = nch - mChannelGroupBaseChannel;
        if (remaining > ecg) remaining = ecg;
        mChannelsInGroupRemaining = remaining - 1;
        mChannelIndex = mChannelGroupBaseChannel;
    }

    if (mCfg.Spacing != 0) {
        mSpacingSlotsRemaining = mCfg.Spacing - 1;
        mPhase = (mCfg.Spacing > 1) ? kSpacing : kActive;
    } else {
        mPhase = kPending;
    }
}

DpEmit CDataPort::popGuardTail()
{
    DpEmit e;
    if (mGuardPending) {
        e.slot = mCfg.GuardPolarity ? SwI3sSlot::Guard1 : SwI3sSlot::Guard0;
        mGuardPending = false;
    } else if (mTailRemaining > 0) {
        e.slot = SwI3sSlot::Tail;
        --mTailRemaining;
    }
    return e;
}

void CDataPort::armGuardTail()
{
    mGuardPending = false;
    mTailRemaining = 0;
    if (!mCfg.isSource()) {
        return;
    }
    if (mCfg.GuardEnable) {
        mGuardPending = true;
    }
    mTailRemaining = mCfg.TailWidth;
}
