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
    // --- Flow control (ABI 7) -------------------------------------------------
    // The owning port's flow mode (0 NORMAL, 1 TX_CONTROLLED, 2 RX_CONTROLLED,
    // 3 ASYNC). NORMAL samples always transport; in the flow-controlled modes a
    // sample is only emitted when its TX_PRESENT bit read 1 (a used transport
    // opportunity) — so the emitted stream is already the de-jittered audio.
    int flowMode = 0;
    // For RX_CONTROLLED/ASYNC, the capture sample where the DRQ that requested
    // this data was observed on the bus (0 = none / not a DRQ-gated sample).
    // Populated when the FCP is re-driven during decode; lets the UI/tests tie a
    // transported sample back to its handshake. Reserved-but-populated so the
    // audio return shape is final for the whole flow-control feature.
    U64 drqSample = 0;
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
    // Phase-preserving reconfigure for a DSCR (a synced commit that changes config but
    // does NOT re-anchor the SSP): drop ports no longer enabled, add newly enabled ones
    // (Initialized fresh), and carry surviving (device,dp) ports over with their interval
    // phase + descrambler state INTACT. Unlike Configure (which rebuilds and re-Initializes
    // EVERY port), so a mid-stream enable/disable doesn't glitch the ports that stayed up.
    void Reconfigure(const SwI3sConfig& cfg);
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
    // Only collect bit points at capture sample >= `s` (windowed collection, so the
    // bounded buffer lands where it's viewed instead of filling from sample 0).
    void SetBitSampleStart(U64 s) { mBitSampleStart = s; }
    const std::vector<BitSample>& bitSamples() const { return mBitSamples; }

    // Re-anchor all dataports to a Stream Synchronization Point observed on the
    // bus: each port's row counter is set so this row is row_in_interval == 0.
    void SyncToSSP();

    // The column the next tick lands on (last SetStartColumn), so a checkpoint can
    // record it and a windowed re-decode can rebuild the same port phase.
    int StartColumn() const { return mStartColumn; }

    // Snapshot / restore the transport phase of every port (NOT the descrambler
    // LFSRs — bit SAMPLE POINTS are level-independent, so a windowed re-decode that
    // only needs where bits are sampled can restore the schedule and skip values).
    // RestoreState re-Configures for `cfg` (rebuilding the same enabled-port set in
    // the same order the states were saved) then overwrites each port's phase.
    std::vector<CDataPort::State> SaveState() const;
    void RestoreState(const SwI3sConfig& cfg, int startColumn,
                      const std::vector<CDataPort::State>& ports);

    // Advance every dataport by one UI WITHOUT reading a data level, descrambling,
    // or collecting — just the placement schedule. Used to fast-forward a windowed
    // re-decode from a checkpoint to the window start (the schedule is what locates
    // sample points; values are irrelevant there).
    void TickScheduleOnly();

    // Advance every dataport by one UI. 'level' is the physical data-line bit
    // for this UI (true = high). Appends any completed samples to 'out'.
    void Tick(bool level, U64 sampleNumber, std::vector<AudioSample>& out);

    // Flow-control handshake validation (SWI3S §14.2.2 {ASW5205}). Accumulated across
    // the whole decode: how many transported samples were checked against their
    // Earlier_FCP_DRQ, and how many violated the mode's rule (RX: TxPresent must EQUAL
    // Earlier_DRQ; ASYNC: TxPresent=1 requires Earlier_DRQ=1). 0 fails = a clean
    // handshake. Only RxControlled/Async ports contribute.
    struct FlowControlStats {
        U64 checked = 0;    // transport opportunities validated against a DRQ
        U64 fails = 0;      // {ASW5205} violations (unrequested / withheld data)
        U64 drqBits = 0;    // DRQ bits observed on the bus
    };
    const FlowControlStats& flowControlStats() const { return mFcStats; }

private:
    struct ChannelRuntime {
        CDescrambler descram;
        U64 acc = 0;
        U64 index = 0;
        U64 startSample = 0;
        // Flow control: validity of the sample currently being assembled, latched
        // from the TX_PRESENT bit that precedes each channel's sample. Defaults
        // true so NORMAL-mode ports (no TX_PRESENT bit) always emit. A sample
        // whose TX_PRESENT read 0 is an unused transport opportunity — assembled
        // (to keep the descrambler in step) but NOT emitted, so audio() carries
        // only the de-jittered used samples.
        bool txpValid = true;
    };
    struct DpRuntime {
        int dpIndex = 0;
        int deviceNum = -1;
        bool scramblerEn = false;
        int  sampleSize = 0;
        SwI3sDpConfig cfg;               // the config this runtime was built from (so a
                                         // mid-stream reconfigure can tell if it changed)
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
        // Flow control (RxControlled / Async): the DRQ bit read at each FCP sample
        // point, oldest..newest, one per non-skipped interval. The handshake pipeline
        // looks back d = FlowControlDelay+1 intervals for Earlier_FCP_DRQ (SWI3S
        // Flow control (RxControlled / Async): DRQ bits read at the FCP sample point,
        // indexed by NON-SKIPPED interval number so the handshake lookback is robust
        // to whether the DRQ column falls before or after the data window within an
        // interval. `drqHistory[i - drqBase]` is interval i's DRQ; the window is bounded
        // (only the last d+2 intervals are ever read). `txpInterval` counts validated
        // TxPresent intervals so Earlier_FCP_DRQ = interval (txpInterval - d). Startup
        // assumes DRQ=1 for the first d intervals (SWI3S §14.2.2 Table 156).
        std::deque<char> drqHistory;
        U64 drqBase = 0;               // interval index of drqHistory.front()
        U64 drqCount = 0;              // total DRQ intervals seen (next interval index)
        U64 txpInterval = 0;           // validated TxPresent intervals so far
        U64 lastDrqSample = 0;         // capture sample of the most recent DRQ bit
    };

    SwI3sConfig mCfg;
    std::vector<DpRuntime> mDps;
    // Build one runtime for an enabled dataport (Configure + Initialize at mStartColumn),
    // shared by Configure and Reconfigure. `idx` seeds dpIndex only when the config lacks
    // an explicit dpNumber.
    DpRuntime buildRuntime(const SwI3sDpConfig& dc, int idx) const;
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
    U64  mBitSampleStart = 0;      // windowed collection: skip bit points before this sample
    std::vector<BitSample> mBitSamples;
    static const std::size_t kMaxBitSamples = 24000000;
    FlowControlStats mFcStats;     // DRQ<->TxPresent handshake validation accumulators
};

#endif // SWI3S_CPAYLOADENGINE_H
