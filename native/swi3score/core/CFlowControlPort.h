// Flow Control Port (DRQ) slot placement — a C++ port of the visualizer's
// FlowControlPort (swi3s_studio/swviz/models/flow_control_port.py). It is an independent peer
// of CDataPort on the bus: ticked once per UI, it emits the DRQ slot (plus
// guard/tail for a source DRQ) in RxControlled / Async flow modes.
//
// DRQ direction is inverted relative to the parent data port: a SINK data port
// implies a SOURCE DRQ (the FCP drives the bus), and a SOURCE data port implies
// a SINK DRQ (the FCP samples the bus). DRQ payload is scrambled like audio, but
// for annotation we only locate the slot, not its value.

#ifndef SWI3S_CFLOWCONTROLPORT_H
#define SWI3S_CFLOWCONTROLPORT_H

#include "CDpConfig.h"
#include "CDataPort.h"   // for SwI3sSlot

struct FcpEmit
{
    SwI3sSlot slot = SwI3sSlot::Empty;   // Drq / Guard0 / Guard1 / Tail / Empty
    bool drqSamplePoint = false;         // the single UI to annotate as a DRQ event
    bool isSource = false;               // DRQ direction (true = FCP drives the bus)
};

class CFlowControlPort
{
public:
    CFlowControlPort() = default;

    void Configure(const SwI3sDpConfig& dpCfg, int numColumns);
    // Initialize the DRQ placement state. 'startColumn' is the bus column the FIRST tick
    // lands on, wrapped modulo the column count as CDataPort::Initialize wraps it;
    // 'rowInInterval' is the row of the interval that tick is in. FCP_HorizontalStart and
    // FCP_Offset are absolute positions, so both must match the parent data port's phase.
    void Initialize(int startColumn = 0, int rowInInterval = 0);

    // Advance one UI. 'dpIntervalSkipped' is the parent data port's
    // interval-skipped status for this interval (the FCP gates DRQ on it).
    FcpEmit clock_tick(bool dpIntervalSkipped);

private:
    void startInterval();
    void advanceColumn();
    void advanceRow();
    void armDrqRepeat();
    void armGuardTail();

    SwI3sDpConfig mCfg;     // parent data port config (holds FCP_* + FlowMode/dir)
    int  mNumColumns = 2;

    int  mColumn = 0;
    int  mRowInInterval = 0;
    bool mGuardPending = false;
    int  mTailRemaining = 0;
    bool mDrqSent = false;
    int  mWideBitRemaining = 0;
};

#endif // SWI3S_CFLOWCONTROLPORT_H
