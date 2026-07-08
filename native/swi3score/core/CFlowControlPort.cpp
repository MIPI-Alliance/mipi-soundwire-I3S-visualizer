// Flow Control Port (DRQ) placement. Port of flow_control_port.py. See header.

#include "CFlowControlPort.h"

void CFlowControlPort::Configure(const SwI3sDpConfig& dpCfg, int numColumns)
{
    mCfg = dpCfg;
    mNumColumns = numColumns;
}

void CFlowControlPort::Initialize()
{
    mColumn = 0;
    mRowInInterval = 0;
    mGuardPending = false;
    mTailRemaining = 0;
    startInterval();
}

void CFlowControlPort::startInterval()
{
    mDrqSent = false;
    mWideBitRemaining = 0;
}

FcpEmit CFlowControlPort::clock_tick(bool dpIntervalSkipped)
{
    FcpEmit e;
    // DRQ direction is inverted vs the parent DP: SINK DP -> SOURCE DRQ.
    bool drqIsSource = !mCfg.isSource();
    e.isSource = drqIsSource;

    // 1. Wide-bit replay of a DRQ already triggered.
    if (mDrqSent && mWideBitRemaining > 0) {
        e.slot = SwI3sSlot::Drq;            // the DRQ bit still occupies the bus
        if (drqIsSource) {
            // source DRQ held every replay UI (held value), no new sample point
        } else if (mWideBitRemaining == 1) {
            e.drqSamplePoint = true;        // wide sink DRQ samples on the last replay UI
        }
        --mWideBitRemaining;
        advanceColumn();
        return e;
    }

    // 2. Fresh DRQ trigger at (FCP_HorizontalStart, FCP_Offset).
    if (mCfg.drqEnabled() && !dpIntervalSkipped && !mDrqSent &&
        mRowInInterval == mCfg.FCP_Offset && mColumn == mCfg.FCP_HorizontalStart) {
        e.slot = SwI3sSlot::Drq;            // first UI of the DRQ bit
        if (drqIsSource) {
            e.drqSamplePoint = true;        // source DRQ drives now
        } else if (mCfg.FCP_BitWidth == 0) {
            e.drqSamplePoint = true;        // narrow sink DRQ samples now
        }
        armDrqRepeat();
        advanceColumn();
        return e;
    }

    // 3. Drain guard/tail (source DRQ only).
    if (mGuardPending) {
        e.slot = mCfg.FCP_GuardPolarity ? SwI3sSlot::Guard1 : SwI3sSlot::Guard0;
        mGuardPending = false;
    } else if (mTailRemaining > 0) {
        e.slot = SwI3sSlot::Tail;
        --mTailRemaining;
    }
    advanceColumn();
    return e;
}

void CFlowControlPort::advanceColumn()
{
    if (++mColumn >= mNumColumns) {
        advanceRow();
    }
}

void CFlowControlPort::advanceRow()
{
    mColumn = 0;
    mGuardPending = false;
    mTailRemaining = 0;
    mWideBitRemaining = 0;   // an in-progress DRQ replay terminates at row edge
    ++mRowInInterval;
    if (mRowInInterval > mCfg.Interval) {
        mRowInInterval = 0;
        startInterval();
    }
}

void CFlowControlPort::armDrqRepeat()
{
    mDrqSent = true;
    armGuardTail();
    mWideBitRemaining = mCfg.FCP_BitWidth;
}

void CFlowControlPort::armGuardTail()
{
    mGuardPending = false;
    mTailRemaining = 0;
    if (mCfg.isSource()) {
        return;             // source DP -> sink DRQ: no guard/tail
    }
    if (mCfg.FCP_GuardEnable) {
        mGuardPending = true;
    }
    mTailRemaining = mCfg.FCP_TailWidth;
}
