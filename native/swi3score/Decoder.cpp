// SWI3S PHY2 streaming decoder. See Decoder.h. Ported from SwI3sAnalyzer.cpp.

#include "Decoder.h"
#include "CColumnDetector.h"
#include "CDataPort.h"
#include "CFlowControlPort.h"
#include "C8b10bDecoder.h"
#include "CNrzsDecoder.h"
#include "CRegisterModel.h"
#include "CCommandTransportParser.h"
#include "SwI3sProtocolDefs.h"
#include "TransitionSampleSource.h"   // concrete source: de-virtualize the hot stream loop
#include "DlvSampleSource.h"          // concrete DLV source: de-virtualize its stream loop too

#include <algorithm>   // std::upper_bound (windowed checkpoint lookup)
#include <cmath>
#include <numeric>   // std::lcm (manual-SSP interval period)

namespace swi3score {

// Rows without a CRC-valid command phase before command confidence is lost and we
// declare the framing stale (re-detect the column count + re-hunt the comma). This is
// the spec's device command-confidence limit: a peripheral falls off the bus after
// 8192 Rows with no valid command addressed to it, and the Manager must issue a Ping
// at least every 4096 Rows ({ASW2601}) to hold it. Counting Rows (not UIs) makes the
// limit geometry-independent — a UI threshold would trip 8x sooner at 2 columns than
// at 16. Below this, a command-sparse audio stretch is healthy, not stale, so we must
// NOT re-hunt (which would disturb the continuously-anchored payload engine).
static const long kCommandConfidenceRows = 8192;

// UIs buffered to measure the UI rate and auto-detect the column count. Must be
// large enough to contain >= 2 command phases even when traffic is sparse and
// the column count is high (few rows per UI): a high-rate 8-column capture can
// space commands ~30k UIs apart, so 8k UIs would never see two phases. Buffering
// this many UIs (a few MB) is negligible against multi-100M-edge captures.
static const int kDetectWindow = 262144;

// DLV uses a MUCH smaller leading window: its column count is snoop-driven (forced
// from the Safe-Lock geometry, then pushed by each commit), so it needs no blind
// column detection — only a brief window to seed the UI-rate measurement. Critically,
// the recovered-clock source changes its UI spacing 4x at a commit (the PLL divider
// changes while the row reference stays constant), and that change is applied via
// SetColumns DURING the feed. Buffering the whole capture (as FBCSE can, its UI being
// constant) would freeze the source at the Safe-Lock spacing before the commit is ever
// decoded, so the operational region would replay at the stale 4-column cadence. A
// small window keeps the commit in the LIVE stream, where SetColumns takes effect on
// the very next UI. Sized well under the Safe-Lock region (thousands of rows).
static const int kDlvDetectWindow = 1024;

// Sliding window (in UIs) over which the UI/clock rate is continuously re-measured
// in feed(). The clock rate can change mid-capture without a column-count change
// (a cold-start slow preamble speeding up to operational rate), which the resync
// watchdog won't catch — so re-derive the reported rate every window and adopt it
// when it drifts >kRateDriftFrac. Small enough to track a change within a few k
// UIs, large enough to average out per-UI jitter.
static const long   kRateWindowUis  = 8192;
static const double kRateDriftFrac  = 0.02;   // 2% change -> adopt the new rate

// Bus rows between payload-engine transport-phase checkpoints (see bitSamplesInWindow).
// A windowed re-decode restores the nearest checkpoint <= its window and fast-forwards
// the schedule at most this many rows, so the cost is bounded regardless of how far the
// window is from the last commit/SSP. Small enough that the fast-forward is a few
// hundred k cheap integer clock_ticks; large enough that the checkpoints stay a few MB
// even on a 300M-UI capture (one snapshot per port is ~15 ints).
static const long   kCheckpointRows = 4096;

std::vector<SymbolRec> decodeSymbols(ISampleSource& src, int columnCount,
                                     int maxSymbols, std::uint64_t startUi,
                                     std::uint64_t rowOriginUi, long rowBase,
                                     std::uint64_t endUi, bool dlv, int cdsColumn)
{
    std::vector<SymbolRec> out;
    if (columnCount < swi3s::kMinColumnCount) columnCount = swi3s::kColdStartColumnCount;
    const bool segMode = (rowOriginUi != ~0ull);   // explicit segment row origin
    // DLV reads the CDS at Column `cdsColumn` (§12.1.10.1) as a PLAIN-NRZ bit — the sampled
    // level IS the bit; there is no NRZS reference to maintain. FBCSE reads NRZS at Column 0.
    const int cdsCol = dlv ? cdsColumn : swi3s::kCdsColumn;

    bool rising = false, dataHigh = false;
    std::uint64_t sn = 0;
    CNrzsDecoder nrzs;
    int column = 0;
    std::uint64_t curUi = 0;          // absolute source UI of the UI fetched below

    if (startUi > 0 && src.CanSeek()) {
        // Windowed re-decode: seek to the UI before the window so its level
        // seeds the NRZS reference, then decode from `startUi` (Column 0).
        src.Seek(startUi - 1);
        if (!src.NextUi(rising, dataHigh, sn)) return out;
        nrzs.Observe(dataHigh ? BIT_HIGH : BIT_LOW);
        curUi = startUi;              // the next NextUi() returns UI `startUi`
    } else {
        // From the start: align to the first rising edge; next UI is Column 0.
        bool aligned = false;
        while (src.NextUi(rising, dataHigh, sn)) {
            if (rising) { aligned = true; break; }
        }
        if (!aligned) return out;
        nrzs.Reset(BIT_LOW);
        curUi = src.UiIndex();        // Column 0 of Row 0 = the next UI's index
    }

    // Row origin: a symbol's row = rowBase + (symbolFirstBitUi - originUi)/columns.
    // In segment mode this anchors to the streaming decoder's Column 0; otherwise
    // rows count from this decode's own first Column 0 (== curUi here).
    const std::uint64_t originUi = segMode ? rowOriginUi : curUi;

    C8b10bDecoder dec;
    // Ring of the last <=10 CDS bits (sample, ui). A symbol always occupies the 10
    // bits ENDING at the bit that completes it, so its first bit is the oldest one
    // here — NOT wherever the previous symbol left off. (Stamping the symbol with
    // the post-previous-symbol bit put commas found after a hunt at the wrong, far
    // earlier row, so spacing looked irregular instead of one symbol per 10 rows.)
    std::deque<std::pair<std::uint64_t, std::uint64_t>> ring;

    while (src.NextUi(rising, dataHigh, sn)) {
        // Stop at the segment boundary: past it the bus may run at a different
        // width, and continuing with THIS segment's framing would mis-decode (and
        // send the comma hunt racing far into the next region, reporting runaway
        // rows). endUi == 0 means "no cap" (the last/only segment).
        if (endUi && curUi >= endUi) break;
        BitState level = dataHigh ? BIT_HIGH : BIT_LOW;
        if (column == cdsCol) {
            bool bit = dlv ? (level == BIT_HIGH) : nrzs.Decode(level);   // DLV: level IS the bit
            ring.emplace_back(sn, curUi);
            if (static_cast<int>(ring.size()) > swi3s::kSymbolBits) ring.pop_front();
            C8b10bDecoder::Symbol sym;
            // Require a FULL ring before stamping a symbol: it occupies kSymbolBits
            // real CDS bits, so its first bit is ring.front(). Right after a fresh or
            // seeked start the ring (and the 8b10b shift window) isn't full yet, so the
            // first "comma" can be matched partly against the decoder's zero-init
            // padding -- emitting it would both invent a spurious comma and back-date
            // its start/row to a bit too new. Skip it; the next real symbol arrives
            // with a full ring and a correct front().
            if (dec.PushBit(bit, sym) &&
                static_cast<int>(ring.size()) == swi3s::kSymbolBits) {
                SymbolRec r;
                r.startSample = ring.front().first;     // first of the symbol's 10 bits
                r.endSample = sn;
                std::uint64_t symUi = ring.front().second;
                // Signed delta: on a windowed/seeked re-decode a symbol's first-bit
                // UI can precede originUi; an unsigned (symUi - originUi) would wrap
                // to an astronomical row, so compute signed and floor at rowBase.
                long dRow = (symUi >= originUi)
                    ? static_cast<long>((symUi - originUi) / columnCount)
                    : -static_cast<long>((originUi - symUi + columnCount - 1) / columnCount);
                r.row = rowBase + dRow;
                r.raw = sym.raw;
                r.aligned = sym.aligned;
                int tok = C8b10bDecoder::RobustToken(sym.raw);
                if (sym.raw == 0x3FF)         { r.kind = 5; r.value = -1; }   // NO_RESPONSE (undriven all-ones)
                else if (sym.isComma)         { r.kind = 1; r.value = -1; }
                else if (tok >= 0)            { r.kind = 2; r.value = tok; }
                else if (sym.valid && sym.isControl) { r.kind = 4; r.value = sym.byte; }
                else if (sym.valid)           { r.kind = 3; r.value = sym.byte; }
                else                          { r.kind = 0; r.value = -1; }
                out.push_back(r);
                if (maxSymbols > 0 && static_cast<int>(out.size()) >= maxSymbols) break;
            }
        } else {
            if (!dlv) nrzs.Observe(level);            // FBCSE keeps the NRZS reference; DLV has none
        }
        ++curUi;
        if (++column == columnCount) column = 0;
    }
    return out;
}

int Decoder::firstSelectedDevice(std::uint16_t mask)
{
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i)
        if (mask & (1u << i)) return i;
    return 0;
}

// ---- windowed bit-sample reconstruction (see bitSamplesInWindow / Decoder.h) ----

void Decoder::recordReconfig(std::uint64_t ui, int startColumn, bool preservePhase)
{
    // A new config epoch (a copy of the config the engine was just Configure()d with)
    // + the event that re-applies it. Kept per-Configure (not deduped): reconfigures
    // are rare (one per confirmed commit / CSV start), so the extra copies are cheap.
    mConfigEpochs.push_back(mConfig);
    mCurrentEpoch = static_cast<int>(mConfigEpochs.size()) - 1;
    TransportEvent e;
    e.ui = ui; e.reconfigure = true; e.sync = false;
    e.epoch = mCurrentEpoch; e.startColumn = startColumn;
    e.preservePhase = preservePhase;   // replay via Reconfigure (keep survivor phase), not Configure
    mTransportEvents.push_back(e);
}

void Decoder::recordSync(std::uint64_t ui)
{
    TransportEvent e;
    e.ui = ui; e.reconfigure = false; e.sync = true;
    mTransportEvents.push_back(e);
}

void Decoder::snapshotCheckpoint(std::uint64_t ui)
{
    if (!mEngine.Enabled() || mCurrentEpoch < 0) return;
    Checkpoint cp;
    cp.ui = ui;
    cp.epoch = mCurrentEpoch;
    cp.startColumn = mEngine.StartColumn();
    cp.ports = mEngine.SaveState();
    mCheckpoints.push_back(std::move(cp));
}

void Decoder::applyTransportEvent(CPayloadEngine& engine, const TransportEvent& e,
                                  const std::vector<SwI3sConfig>& epochs)
{
    if (e.reconfigure && e.epoch >= 0 && e.epoch < static_cast<int>(epochs.size())) {
        if (e.preservePhase) {
            // DSCR / immediate single-ranked write: the live decode kept the surviving
            // ports' interval phase via Reconfigure (no start-column re-anchor), so the
            // replay must too — a full Configure here would reset row_in_interval to 0
            // and misplace an Interval>1 survivor's sample-point markers for the window.
            engine.Reconfigure(epochs[e.epoch]);
        } else {
            engine.SetStartColumn(e.startColumn);
            engine.Configure(epochs[e.epoch]);
        }
    }
    if (e.sync && engine.Enabled()) engine.SyncToSSP();
}

std::vector<BitSample> Decoder::bitSamplesInWindow(std::uint64_t startUi, std::uint64_t endUi)
{
    std::vector<BitSample> out;
    if (endUi < startUi || mCheckpoints.empty() || !mSrc.CanSeek()) return out;

    // Nearest checkpoint with ui <= startUi (checkpoints are recorded ui-ascending).
    auto it = std::upper_bound(mCheckpoints.begin(), mCheckpoints.end(), startUi,
        [](std::uint64_t v, const Checkpoint& c) { return v < c.ui; });
    if (it == mCheckpoints.begin()) return out;   // window precedes any configured port
    const Checkpoint& cp = *(it - 1);
    if (cp.epoch < 0 || cp.epoch >= static_cast<int>(mConfigEpochs.size())) return out;

    CPayloadEngine engine;
    engine.RestoreState(mConfigEpochs[cp.epoch], cp.startColumn, cp.ports);
    if (!engine.Enabled()) return out;

    std::uint64_t ui = cp.ui;
    // First transport event strictly after the checkpoint's UI (events at/before it are
    // already baked into the restored state; a checkpoint is taken right after each
    // event, so there are normally NO events in (cp.ui, startUi) — but the loops apply
    // any that appear anyway, so correctness never depends on that).
    std::size_t nextEv = 0;
    while (nextEv < mTransportEvents.size() && mTransportEvents[nextEv].ui <= cp.ui) ++nextEv;

    std::vector<AudioSample> scratch;   // discarded (schedule-only fast-forward)

    // Fast-forward the schedule (no wire read) to the window start.
    while (ui < startUi) {
        while (nextEv < mTransportEvents.size() && mTransportEvents[nextEv].ui == ui) {
            applyTransportEvent(engine, mTransportEvents[nextEv], mConfigEpochs);
            ++nextEv;
        }
        engine.TickScheduleOnly();
        ++ui;
    }

    // Window: seek so the next NextUi() returns UI `startUi`, then tick reading the
    // wire and collecting each sampled data bit's position.
    mSrc.Seek(startUi);
    engine.SetCollectBits(true);
    bool rising = false, dataHigh = false;
    std::uint64_t sn = 0;
    while (ui <= endUi && mSrc.NextUi(rising, dataHigh, sn)) {
        while (nextEv < mTransportEvents.size() && mTransportEvents[nextEv].ui == ui) {
            applyTransportEvent(engine, mTransportEvents[nextEv], mConfigEpochs);
            ++nextEv;
        }
        engine.Tick(dataHigh, sn, scratch);
        ++ui;
    }
    return engine.bitSamples();
}

void Decoder::updateRowRateFromCapture(int columnCount)
{
    if (mMeasuredUiRateHz > 0.0 && columnCount > 0)
        mConfig.RowRateKHz = (mMeasuredUiRateHz / columnCount) / 1000.0;
}

void Decoder::closeUiRateWindow()
{
    // Called once per closed window (kRateWindowUis UIs) from feed()'s inlined
    // accumulator. Compare the window's measured UI rate to the current one and
    // adopt it if it drifted (a real clock-rate change) — this catches a rate change
    // the column-count resync watchdog misses (rate changed, column count didn't),
    // e.g. a cold-start preamble at 1/4 the operational clock. Reporting-only.
    if (mRateWinLastSample > mRateWinFirstSample) {
        double avgUiSamples = static_cast<double>(mRateWinLastSample - mRateWinFirstSample)
                              / (mRateWinUiCount - 1);
        if (avgUiSamples > 0.0) {
            double rate = static_cast<double>(mSrc.SampleRateHz()) / avgUiSamples;
            if (mMeasuredUiRateHz <= 0.0 ||
                std::abs(rate - mMeasuredUiRateHz) > kRateDriftFrac * mMeasuredUiRateHz) {
                mMeasuredUiRateHz = rate;
                updateRowRateFromCapture(mColumnCount);   // keep reported row rate in step
            }
        }
    }
    mRateWinUiCount = 0;   // start a fresh window
}

void Decoder::applyScramblerOverrides()
{
    if (mSettings.scramblerOverrides.empty()) return;
    for (SwI3sDpConfig& d : mConfig.dps) {
        for (const auto& ov : mSettings.scramblerOverrides) {
            if (std::get<0>(ov) == d.deviceNum && std::get<1>(ov) == d.dpNumber)
                d.ScramblerEn = (std::get<2>(ov) != 0);
        }
    }
}

void Decoder::applyRegisterOverrides(CRegisterModel& regs, int sectionIndex) const
{
    if (mSettings.registerOverrides.empty()) return;
    for (const auto& ov : mSettings.registerOverrides) {
        if (std::get<0>(ov) != sectionIndex) continue;
        // Force only the addressed rank of this one register — never commit every
        // group (which would promote all OTHER staged _NEXT values to _CURR).
        regs.ForceOverride(std::get<1>(ov), std::get<2>(ov),
                           static_cast<U8>(std::get<3>(ov) & 0xFF));
    }
}

int Decoder::forcedColumnsForSection(int sectionIndex) const
{
    // Bounds-check here rather than trusting the caller: the count reaches us from an
    // imported (unvalidated) config CSV via the UI, and an out-of-range width would be
    // used as a modulus in the per-UI framing (mColumn == mColumnCount never true for
    // <= 0 — the row would never wrap). Odd counts are NOT rejected: IsLegalPhy2ColumnCount
    // is a PHY2 rule, and this override also serves PHY1/PHY3.
    for (const auto& fc : mSettings.forcedColumnSections)
        if (fc.first == sectionIndex &&
            fc.second >= swi3s::kMinColumnCount && fc.second <= swi3s::kMaxColumnCount)
            return fc.second;
    return 0;
}

// Do two configs have the same ENABLED-port set + geometry the payload engine cares
// about? Used to skip a reconfigure (and its config epoch) when a write didn't actually
// change anything — e.g. a single-ranked register rewritten with the same value.
static bool sameEnabledConfig(const SwI3sConfig& a, const SwI3sConfig& b)
{
    if (a.columnCount() != b.columnCount()) return false;
    if (a.SkippingDenominator != b.SkippingDenominator) return false;
    auto enabled = [](const SwI3sConfig& c) {
        std::vector<const SwI3sDpConfig*> v;
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0) v.push_back(&d);
        return v;
    };
    std::vector<const SwI3sDpConfig*> va = enabled(a), vb = enabled(b);
    if (va.size() != vb.size()) return false;
    for (size_t i = 0; i < va.size(); ++i)
        if (!va[i]->sameTransport(*vb[i])) return false;
    return true;
}

bool Decoder::maybeConfigureFromSnoop(int columnCount, int sectionIndex, bool preservePhase)
{
    SwI3sConfig snooped;
    bool buildOk;
    if (mSettings.registerOverrides.empty()) {
        buildOk = mRegisters.BuildConfig(snooped);
    } else {
        // Fold section-scoped what-if overrides into a COPY of the live register
        // model (never the live one — force-committing it would taint later real
        // commits, Risk R2). CRegisterModel holds only std::maps, so this is cheap
        // and only happens at (infrequent) reconfigure points.
        CRegisterModel snoop = mRegisters;
        applyRegisterOverrides(snoop, sectionIndex);
        buildOk = snoop.BuildConfig(snooped);
    }
    // BuildConfig returns false when NO dataport is enabled. Before the engine is up that
    // just means "nothing to configure yet" — bail. But once ports are running, a false
    // result means the LAST enabled port was just disabled: reconfigure to an EMPTY port
    // set (drop it) rather than keeping the stale config. (Regression: disabling the last
    // port left it decoding to the end of the capture — the Dev6 DP3 bug.)
    if (!buildOk && !mEngine.Enabled()) return false;
    snooped.NumColumns = columnCount - 1;
    snooped.RowRateKHz = mConfig.RowRateKHz;   // preserve capture-derived rate
    // Skip if the effective config is unchanged — a single-ranked write that rewrote a
    // register with the same value must NOT record a spurious config epoch (which would
    // desync the windowed re-decode from the full collect).
    if (mEngine.Enabled() && sameEnabledConfig(mConfig, snooped)) return false;
    mConfig = snooped;
    applyScramblerOverrides();
    if (preservePhase) mEngine.Reconfigure(mConfig);   // DSCR: keep survivors' timing
    else               mEngine.Configure(mConfig);     // full (re)build + re-Initialize
    return true;
}

void Decoder::recordCommand(const SwI3sCommand& cmd)
{
    CommandRec r;
    r.command = cmd.CommandName();
    r.phase = cmd.PhaseName();
    r.deviceMask = cmd.deviceMask;
    r.hasManagerPacket = cmd.hasManagerPacket;
    r.packetLength = cmd.packetLength;
    r.opcode = cmd.opcode;
    r.hasAddress = cmd.hasAddress;
    r.address = cmd.address;
    r.data.assign(cmd.data.begin(), cmd.data.end());
    r.crcValid = cmd.crcValid;
    r.peripheralResponse = cmd.peripheralResponse;
    r.managerResponse = cmd.managerResponse;
    r.isCommit = cmd.isCommit;
    r.commitConfirmed = cmd.commitConfirmed;
    r.hasSyncPoint = cmd.hasSyncPoint;
    r.rowDelay = cmd.rowDelay;
    r.groupMask = cmd.groupMask;
    r.hasPingStatus = cmd.hasPingStatus;
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i) r.pingStatus[i] = cmd.pingStatus[i];
    r.hasReadData = cmd.hasReadData;
    r.readData.assign(cmd.readData.begin(), cmd.readData.end());
    r.readDataCrcValid = cmd.readDataCrcValid;
    // Bus row + start sample of the command's SPM comma — the SAME row the CDS
    // symbol viewer shows for that comma, so the command table and symbol table
    // line up. The trail records (sample -> row) for every CDS bit; cmd.startSample
    // is the comma's LAST bit, and the comma (a kSymbolBits-long K.28.7) begins
    // kSymbolBits-1 CDS bits earlier. That earlier bit is the Row Sync Point (the
    // rising edge at the start of the row / Column 0), so report THAT as the
    // command's start — the cursor then lands on the row-sync rising edge and
    // start_sample matches the comma symbol's start_sample. Remember the bits we
    // pop so we can recover the first bit's sample. Falls back to the completion
    // row / last-bit sample if the start bit isn't in the trail (e.g. after resync).
    long startRow = static_cast<long>(mRowCounter);
    std::uint64_t startSample = cmd.startSample;
    std::deque<std::pair<std::uint64_t, long>> popped;       // recent bits before the last
    while (!mCdsBitRows.empty() && mCdsBitRows.front().first < cmd.startSample) {
        popped.push_back(mCdsBitRows.front());
        if (static_cast<int>(popped.size()) > swi3s::kSymbolBits) popped.pop_front();
        mCdsBitRows.pop_front();
    }
    if (!mCdsBitRows.empty() && mCdsBitRows.front().first == cmd.startSample) {
        startRow = mCdsBitRows.front().second - (swi3s::kSymbolBits - 1);
        if (startRow < 0) startRow = 0;
        for (const auto& e : popped)                          // comma's first bit == the RSP
            if (e.second == startRow) { startSample = e.first; break; }
    }
    r.busRow = startRow;
    r.startSample = startSample;
    r.endSample = cmd.endSample;
    mCommands.push_back(std::move(r));
}

std::vector<GridCell> layoutGrid(const SwI3sConfig& cfg, int columnCount, int rows,
                                 bool dlv, int cdsColumn)
{
    std::vector<GridCell> out;
    int cols = (columnCount > 0) ? columnCount : swi3s::kColdStartColumnCount;
    // CDS column: Column 0 for FBCSE; CDS_HorizontalStart (e.g. 2) for DLV, whose
    // Column 0 is Sync1 (S1) and last column is Sync0 (S0) instead. S0/S1 use the
    // grid-only system slot ints shared with grid_view.py (_S0=7, _S1=8).
    const int cdsCol = dlv ? cdsColumn : swi3s::kCdsColumn;
    constexpr int kGridSlotS0 = 7, kGridSlotS1 = 8;

    // Build a placement port per enabled data port.
    std::vector<CDataPort> ports;
    std::vector<int> portDp;
    std::vector<int> portDev;
    std::vector<bool> portScram;
    std::vector<int> portMode;
    // Parallel Flow Control Port per data port: the FCP places the DRQ slot (plus
    // its guard/tail) for RxControlled / Async ports, an independent bus source. Only
    // ticked for drq-enabled ports; the rest carry a default (never-emitting) FCP so
    // the vectors stay index-aligned with `ports`.
    std::vector<CFlowControlPort> fcps;
    std::vector<bool> portHasFcp;
    for (const SwI3sDpConfig& dc : cfg.dps) {
        if (dc.Enabled && dc.numChannels() > 0) {
            CDataPort p;
            p.Configure(dc, cols, cfg.SkippingDenominator);
            p.Initialize();
            // NOTE: do NOT call SyncToSSP() here. This is a config→grid layout
            // from row 0; SyncToSSP re-anchors a port to a mid-capture SSP and
            // resets the skipping accumulator, which shifts the skip phase and
            // makes skipped-interval configs disagree with the verified
            // swi3s_grid_dump / the visualizer model. Configure+Initialize is
            // exactly what the placement cross-check harness uses.
            ports.push_back(p);
            portDp.push_back(dc.dpNumber >= 0 ? dc.dpNumber : (int)portDp.size());
            portDev.push_back(dc.deviceNum);
            portScram.push_back(dc.ScramblerEn);
            portMode.push_back(dc.PortMode);
            CFlowControlPort fcp;
            if (dc.drqEnabled()) {
                fcp.Configure(dc, cols);
                fcp.Initialize();
            }
            fcps.push_back(fcp);
            portHasFcp.push_back(dc.drqEnabled());
        }
    }

    // Per-port last data-bit identity, so wide-bit *held* replay columns (which
    // clock_tick reports with channel = -1, the held value being re-driven, not
    // re-sampled) carry the same channel/sample/bit as the bit they hold — the
    // grid renderer then merges and labels them like the visualizer.
    std::vector<int> lastCh(ports.size(), -1), lastSamp(ports.size(), 0), lastBit(ports.size(), 0);
    std::vector<DpEmit> emits(ports.size());   // reused each UI (fully overwritten below), not
    //                                            re-allocated per (row, col) — see the tick loop.
    out.reserve((size_t)rows * cols);
    for (int r = 0; r < rows; ++r) {
        for (int c = 0; c < cols; ++c) {
            bool isCds = (c == cdsCol);
            // Tick EVERY port each UI (incl. Column 0) to keep them in lockstep.
            for (size_t i = 0; i < ports.size(); ++i)
                emits[i] = ports[i].clock_tick();
            if (dlv && (c == 0 || c == cols - 1)) {
                // DLV framing: Sync1 opens the row at Column 0, Sync0 closes it at the
                // last column — full-height system cells (Manager-owned, device = -1).
                GridCell sc;
                sc.row = r; sc.col = c;
                sc.slot = (c == 0) ? kGridSlotS1 : kGridSlotS0;
                sc.isSource = true;
                out.push_back(sc);
            }
            if (isCds) {
                // Column 0 is the Control Data Stream — one full-height cell that
                // anchors the row count. We still run the data-emit loop below so
                // a misconfigured data port writing into Column 0 surfaces as an
                // overlapping cell (the clash detector flags it against the CDS,
                // matching the visualizer). Data ports normally start after it.
                GridCell cell;
                cell.row = r; cell.col = c; cell.isCds = true;
                out.push_back(cell);
            }
            // Multi-emit: push a cell for EVERY port that drives/samples this UI
            // (so a source + sink sharing the slot both appear). Empty UIs emit
            // nothing, keeping the grid sparse.
            for (size_t i = 0; i < ports.size(); ++i) {
                const DpEmit& e = emits[i];
                if (e.slot == SwI3sSlot::Empty) continue;
                GridCell cell;
                cell.row = r; cell.col = c;
                cell.slot = (int)e.slot;
                cell.dp = portDp[i];
                cell.device = portDev[i];
                int ch = e.channel, samp = e.sampleInGroup, bit = e.bitInChannel;
                if (e.slot == SwI3sSlot::Data && e.channel < 0) {
                    ch = lastCh[i]; samp = lastSamp[i]; bit = lastBit[i];   // wide-bit held column
                } else if (e.slot == SwI3sSlot::Data) {
                    lastCh[i] = ch; lastSamp[i] = samp; lastBit[i] = bit;
                }
                cell.channel = (e.slot == SwI3sSlot::Data) ? ch : e.channel;
                cell.sampleHere = e.sampleHere;
                cell.isSource = e.isSource;
                cell.sample = samp;
                cell.bit = bit;
                cell.scramblerEn = portScram[i];
                cell.portMode = portMode[i];
                out.push_back(cell);
            }
            // Flow Control Port emission: place the DRQ slot (and its guard/tail) for
            // each drq-enabled port, an independent bus source parallel to the data
            // port. Ticked in lockstep every UI. The DRQ column (FCP_HorizontalStart)
            // is disjoint from the data window, so these are distinct cells; the grid
            // renderer draws them like the visualizer's FCP row.
            for (size_t i = 0; i < ports.size(); ++i) {
                if (!portHasFcp[i]) continue;
                FcpEmit fe = fcps[i].clock_tick(ports[i].IntervalSkipped());
                if (fe.slot == SwI3sSlot::Empty) continue;
                // Wide-bit occupancy follows direction (as for data bits): a SOURCE DRQ
                // DRIVES every UI of the wide bit, but a SINK DRQ SAMPLES only the last
                // UI — so a sink's earlier (non-sample-point) read UIs are sparse and get
                // no cell (matches the visualizer). Guard/Tail (source-only) always place.
                if (fe.slot == SwI3sSlot::Drq && !fe.isSource && !fe.drqSamplePoint)
                    continue;
                GridCell cell;
                cell.row = r; cell.col = c;
                cell.slot = (int)fe.slot;
                cell.dp = portDp[i];
                cell.device = portDev[i];
                cell.channel = -1;                 // DRQ carries no channel
                cell.sampleHere = fe.drqSamplePoint;
                cell.isSource = fe.isSource;
                out.push_back(cell);
            }
        }
    }
    return out;
}

std::vector<GridCell> gridFromRegisters(const std::vector<std::tuple<int, std::uint32_t, std::uint8_t>>& writes,
                                        int fallbackColumns, int rows, int forceColumns,
                                        bool dlv, int cdsColumn)
{
    CRegisterModel regs;
    for (const auto& w : writes) {
        int dev = std::get<0>(w);
        SwI3sCommand c;
        c.clear();
        c.phase = swi3s::kPhaseWrite;
        c.opcode = swi3s::kOpWriteA32;
        c.deviceMask = static_cast<std::uint16_t>(1u << dev);
        c.hasAddress = true;
        c.address = std::get<1>(w);
        c.data = { std::get<2>(w) };
        regs.OnCommand(c);
    }
    regs.Commit(0xFF);   // what-if: commit every group

    SwI3sConfig cfg;
    regs.BuildConfig(cfg);
    int cols = regs.ColumnCount();
    if (cols < swi3s::kMinColumnCount) cols = fallbackColumns;
    if (cols < swi3s::kMinColumnCount) cols = swi3s::kColdStartColumnCount;
    // A positive forceColumns overrides the register-derived width: a cold-start
    // capture configures its final (e.g. 8-col) registers up front, so to show the
    // grid that was PHYSICALLY on the wire in an earlier (e.g. 2-col) segment the
    // caller passes that segment's column count here.
    if (forceColumns >= swi3s::kMinColumnCount) cols = forceColumns;
    cfg.NumColumns = cols - 1;
    return layoutGrid(cfg, cols, rows, dlv, cdsColumn);
}

// Replay decoded write/commit commands through the real register model, faithfully
// (dual-ranked _NEXT staging + commit-group promotion), then fold in what-if register
// overrides AFTER the wire's traffic (ForceOverride: each forces just its own register,
// both ranks, without promoting other staged values). This is the single as-of-cursor
// register build shared by the bus grid AND the register-map export, so they can't
// disagree. `overrides` are (device, address, value), pre-filtered to the cursor's
// config section by the caller.
static CRegisterModel buildModelFromCommands(
    const std::vector<CommandReplay>& cmds,
    const std::vector<std::tuple<int, std::uint32_t, int>>& overrides)
{
    CRegisterModel regs;
    for (const auto& c : cmds) {
        if (c.isWrite) {
            SwI3sCommand w;
            w.clear();
            w.phase = swi3s::kPhaseWrite;
            w.opcode = swi3s::kOpWriteA32;
            w.deviceMask = c.deviceMask;
            w.hasAddress = true;
            w.address = c.address;
            w.data = c.data;
            regs.OnCommand(w);
        } else if (c.isCommit && c.commitConfirmed) {
            regs.Commit(static_cast<std::uint8_t>(c.groupMask & 0xFF));
        }
    }
    if (!overrides.empty()) {
        for (const auto& ov : overrides)
            regs.ForceOverride(std::get<0>(ov), std::get<1>(ov),
                               static_cast<std::uint8_t>(std::get<2>(ov) & 0xFF));
    }
    return regs;
}

std::vector<GridCell> gridFromCommands(const std::vector<CommandReplay>& cmds,
                                       int rows, int forceColumns,
                                       const std::vector<std::tuple<int, std::uint32_t, int>>& overrides,
                                       bool dlv, int cdsColumn)
{
    CRegisterModel regs = buildModelFromCommands(cmds, overrides);
    SwI3sConfig cfg;
    regs.BuildConfig(cfg);
    int cols = regs.ColumnCount();
    if (cols < swi3s::kMinColumnCount) cols = swi3s::kColdStartColumnCount;
    if (forceColumns >= swi3s::kMinColumnCount) cols = forceColumns;
    cfg.NumColumns = cols - 1;
    return layoutGrid(cfg, cols, rows, dlv, cdsColumn);
}

// The decoded CONFIG as-of a command replay (same model build as gridFromCommands, minus
// the layout) — so the visualizer-CSV export can reflect the geometry in effect at the
// cursor's segment, not always the final operational config.
SwI3sConfig configFromCommands(const std::vector<CommandReplay>& cmds, int forceColumns,
                               const std::vector<std::tuple<int, std::uint32_t, int>>& overrides)
{
    CRegisterModel regs = buildModelFromCommands(cmds, overrides);
    SwI3sConfig cfg;
    regs.BuildConfig(cfg);
    int cols = regs.ColumnCount();
    if (cols < swi3s::kMinColumnCount) cols = swi3s::kColdStartColumnCount;
    if (forceColumns >= swi3s::kMinColumnCount) cols = forceColumns;
    cfg.NumColumns = cols - 1;
    return cfg;
}

// As-of-cursor register state (device, addr, cur, has_cur, cur_src, next, has_next,
// next_src) from the SAME model build the grid uses, PLUS bus reads overlaid — the
// register map's value + provenance source, so register map, grid, and audio share one
// decode authority. Reads are applied AFTER the write/commit/override fold (last-op-wins,
// matching the display's read overlay) and ONLY here — gridFromCommands never applies
// reads, so a read colours the register map without perturbing the grid/audio config.
std::vector<std::tuple<int, std::uint32_t, std::uint8_t, bool, std::uint8_t,
                       std::uint8_t, bool, std::uint8_t>>
registersFromCommands(const std::vector<CommandReplay>& cmds,
                      const std::vector<std::tuple<int, std::uint32_t, int>>& overrides)
{
    // Single CHRONOLOGICAL pass so the LAST access to a register wins (a write after a
    // read reads WRITTEN, a read after a write reads READ). Reads are display-only —
    // ApplyRead updates only THIS model's display banks and never feeds BuildConfig, and
    // this model is separate from the grid/audio one (gridFromCommands), so interleaving
    // reads here can't perturb the decoded config. (The old two-pass — all writes, then
    // all reads — made a read always win, so a write after a read stayed purple.)
    CRegisterModel regs;
    for (const auto& c : cmds) {
        if (c.isWrite) {
            SwI3sCommand w;
            w.clear();
            w.phase = swi3s::kPhaseWrite;
            w.opcode = swi3s::kOpWriteA32;
            w.deviceMask = c.deviceMask;
            w.hasAddress = true;
            w.address = c.address;
            w.data = c.data;
            regs.OnCommand(w);
        } else if (c.isCommit && c.commitConfirmed) {
            regs.Commit(static_cast<std::uint8_t>(c.groupMask & 0xFF));
        } else if (c.isRead) {
            std::uint32_t addr = c.address;
            for (std::uint8_t value : c.data) {
                for (int dev = 0; dev < swi3s::kMaxPeripherals; ++dev)
                    if (c.deviceMask & (1u << dev))
                        regs.ApplyRead(dev, addr, value);
                ++addr;
            }
        }
    }
    if (!overrides.empty()) {
        for (const auto& ov : overrides)
            regs.ForceOverride(std::get<0>(ov), std::get<1>(ov),
                               static_cast<std::uint8_t>(std::get<2>(ov) & 0xFF));
    }
    return regs.Snapshot();
}

std::vector<GridCell> gridFromCsv(const std::string& csvPath, int rows)
{
    SwI3sConfig cfg;
    std::string err;
    if (!cfg.LoadCsv(csvPath, err)) return {};
    // Mirror the decode path's frame geometry so an AUTHORED CSV places like a decoded
    // capture: a PHY3 (DLV) config frames S1 at column 0 and reads the CDS at column 2
    // (§12.1.10.1); FBCSE keeps the CDS at column 0. FBCSE (the common authoring case)
    // is unaffected — dlv stays false.
    const bool dlv = cfg.PHY3Enabled;
    const int cdsColumn = dlv ? swi3s::kDlvCdsColumn : swi3s::kCdsColumn;
    return layoutGrid(cfg, cfg.columnCount(), rows, dlv, cdsColumn);
}

std::vector<std::tuple<int, std::uint32_t, int>> registersFromConfig(const SwI3sConfig& cfg)
{
    using swi3s::reg::DpAddr;
    namespace r = swi3s::reg;
    std::vector<std::tuple<int, std::uint32_t, int>> out;

    auto emit = [&](int dev, std::uint32_t addr, int val) {
        out.emplace_back(dev, addr, val & 0xFF);
    };

    // Bus geometry lives on each device that owns an enabled dataport (it is
    // identical across the link; BuildConfig reads it from a representative).
    bool deviceSeen[swi3s::kMaxPeripherals] = {false};
    for (const SwI3sDpConfig& d : cfg.dps) {
        if (d.Enabled && d.numChannels() > 0 && d.deviceNum >= 0 &&
            d.deviceNum < swi3s::kMaxPeripherals)
            deviceSeen[d.deviceNum] = true;
    }
    for (int dev = 0; dev < swi3s::kMaxPeripherals; ++dev) {
        if (!deviceSeen[dev]) continue;
        emit(dev, r::kNumColumns_Next, cfg.NumColumns & 0x1F);
        if (cfg.SkippingDenominator != 1) {
            emit(dev, r::kSkippingDenomLo, cfg.SkippingDenominator & 0xFF);
            emit(dev, r::kSkippingDenomHi, (cfg.SkippingDenominator >> 8) & 0xF);
        }
    }

    // Per dataport: invert decodeDp's exact bit packing (see CRegisterModel.cpp).
    for (const SwI3sDpConfig& d : cfg.dps) {
        if (!(d.Enabled && d.numChannels() > 0 && d.deviceNum >= 0)) continue;
        int dev = d.deviceNum;
        int n = d.dpNumber >= 0 ? d.dpNumber : 0;
        emit(dev, DpAddr(n, r::kDpSampleSizeGrouping),
             ((d.SampleGrouping & 0x7) << 5) | (d.SampleSize & 0x1F));
        emit(dev, DpAddr(n, r::kDpScramDirMode),
             ((d.ScramblerEn ? 1 : 0) << 3) | ((d.PortDirection ? 1 : 0) << 2) | (d.PortMode & 0x3));
        emit(dev, DpAddr(n, r::kDpSkipNumLo), d.SkippingNumerator & 0xFF);
        emit(dev, DpAddr(n, r::kDpSkipNumHi), (d.SkippingNumerator >> 8) & 0xF);
        emit(dev, DpAddr(n, r::kDpFlowMode),
             (d.FlowMode & 0x3) | ((d.FlowControlDelay & 0x1) << 7));   // 0x0E: [1:0] mode, [7] FCP delay
        emit(dev, DpAddr(n, r::kDpBitWidthHStart_N),
             ((d.BitWidth & 0x3) << 6) | (d.HorizontalStart & 0x1F));
        emit(dev, DpAddr(n, r::kDpEnableCh0HCount_N),
             ((d.EnableCh & 0x1) ? 0x80 : 0) | (d.HorizontalCount & 0x1F));
        emit(dev, DpAddr(n, r::kDpTailSubSpacing_N),
             ((d.TailWidth & 0x3) << 6) | ((d.SubRowInterval ? 1 : 0) << 5) | (d.Spacing & 0xF));
        emit(dev, DpAddr(n, r::kDpOffIntLo_N),
             ((d.Offset & 0xF) << 4) | (d.Interval & 0xF));
        emit(dev, DpAddr(n, r::kDpIntHi_N), (d.Interval >> 4) & 0xFF);
        emit(dev, DpAddr(n, r::kDpOffHi_N), (d.Offset >> 4) & 0xFF);
        emit(dev, DpAddr(n, r::kDpChannelGrouping_N), d.ChannelGrouping & 0xF);
        emit(dev, DpAddr(n, r::kDpGuard_N),
             ((d.GuardEnable ? 1 : 0) << 1) | (d.GuardPolarity ? 1 : 0));
        emit(dev, DpAddr(n, r::kDpEnableCh1_7_N), d.EnableCh & 0xFE);     // ch1..7 at bits 1..7
        emit(dev, DpAddr(n, r::kDpEnableCh8_15_N), (d.EnableCh >> 8) & 0xFF);
        emit(dev, DpAddr(n, r::kDpFcpBwHStart_N),
             ((d.FCP_BitWidth & 0x3) << 6) | (d.FCP_HorizontalStart & 0x1F));
        emit(dev, DpAddr(n, r::kDpFcpTail_N), (d.FCP_TailWidth & 0x3) << 6);
        emit(dev, DpAddr(n, r::kDpFcpOffLo_N), (d.FCP_Offset & 0xF) << 4);
        emit(dev, DpAddr(n, r::kDpFcpOffHi_N), (d.FCP_Offset >> 4) & 0xFF);
        emit(dev, DpAddr(n, r::kDpFcpGuard_N),
             ((d.FCP_GuardEnable ? 1 : 0) << 1) | (d.FCP_GuardPolarity ? 1 : 0));
    }
    return out;
}

std::vector<std::tuple<int, std::uint32_t, int>> registersFromCsv(const std::string& csvPath)
{
    SwI3sConfig cfg;
    std::string err;
    if (!cfg.LoadCsv(csvPath, err)) return {};
    return registersFromConfig(cfg);
}

std::vector<std::tuple<int, std::uint32_t, int>> Decoder::configRegisters() const
{
    return registersFromConfig(mConfig);
}

std::vector<GridCell> Decoder::gridLayout(int rows) const
{
    return layoutGrid(mConfig, (mColumnCount > 0) ? mColumnCount : swi3s::kColdStartColumnCount, rows,
                      mSettings.dlv, mSettings.cdsHorizontalStart);
}

std::vector<std::tuple<int, int, double>> Decoder::audioSampleRates() const
{
    std::vector<std::tuple<int, int, double>> out;
    for (const SwI3sDpConfig& dc : mConfig.dps) {
        if (dc.Enabled && dc.numChannels() > 0) {
            int dp = dc.dpNumber >= 0 ? dc.dpNumber : 0;
            out.emplace_back(dc.deviceNum, dp, mConfig.SampleRateHz(dc));
        }
    }
    return out;
}

void Decoder::run()
{
    // Decode progress (see Decoder.h progressUis()/totalUis()): reset the counter and
    // fix the total for THIS run before any feed() call. TotalUiCount() is 0 for a
    // source that can't report one up front (pure streaming) — progress() then reports
    // 0.0 rather than a bogus percentage; progressUis() still counts up for an n-of-N
    // (N unknown) readout.
    mProgressUis = 0;
    mTotalUis = mSrc.TotalUiCount();

    mConfig = SwI3sConfig();
    mHaveCsv = false;
    if (!mSettings.configCsvPath.empty()) {
        std::string err;
        mHaveCsv = mConfig.LoadCsv(mSettings.configCsvPath, err);
    }
    mDecodeAudio = mSettings.decodeAudio;
    mEngine.SetCollectBits(mSettings.collectBitSamples);   // persists across reconfigures
    mEngine.SetBitSampleStart(mSettings.bitSampleStart);   // windowed collection
    // Windowed-decode reconstruction log (transport events + phase checkpoints), rebuilt
    // fresh each run so bitSamplesInWindow replays THIS decode's engine timeline.
    mConfigEpochs.clear();
    mTransportEvents.clear();
    mCheckpoints.clear();
    mCurrentEpoch = -1;
    if (mDecodeAudio && mHaveCsv && mConfig.anyAudioEnabled()) {
        // Prepare the config now; the engine itself is Configure()d below once the
        // Column-0 phase is known, so the payload ports start on the right column.
        applyScramblerOverrides();
    }

    // Align to Column 0: advance to the first rising clock edge (that UI is the
    // alignment edge; the next UI read is Column 0 of Row 0).
    bool rising = false, dataHigh = false;
    std::uint64_t sn = 0;
    bool aligned = false;
    while (mSrc.NextUi(rising, dataHigh, sn)) {
        if (rising) { aligned = true; break; }
    }
    if (!aligned) return;
    std::uint64_t segStartUi = mSrc.UiIndex();   // source UI of this segment's Column 0

    // Collect a bounded leading window to measure the UI rate and auto-detect the
    // column count (replaces the plugin's history/rewind; memory stays bounded).
    // DLV streams live after a tiny seed window (see kDlvDetectWindow) so a mid-stream
    // divider change (SetColumns) isn't frozen out by a whole-capture buffer.
    const int kWindow = mSettings.dlv ? kDlvDetectWindow : kDetectWindow;
    std::vector<bool> levels;
    std::vector<std::uint64_t> samples;
    levels.reserve(kWindow);
    samples.reserve(kWindow);
    std::uint64_t firstSample = 0, lastSample = 0;
    bool haveFirst = false;
    while (static_cast<int>(levels.size()) < kWindow &&
           mSrc.NextUi(rising, dataHigh, sn)) {
        if (!haveFirst) { firstSample = sn; haveFirst = true; }
        lastSample = sn;
        levels.push_back(dataHigh);
        samples.push_back(sn);
    }
    if (levels.empty()) return;

    int uiCount = static_cast<int>(levels.size());
    if (lastSample > firstSample && uiCount > 0) {
        double avgUiSamples = static_cast<double>(lastSample - firstSample) / uiCount;
        if (avgUiSamples > 0.0)
            mMeasuredUiRateHz = static_cast<double>(mSrc.SampleRateHz()) / avgUiSamples;
    }

    int phaseOffset = 0;
    // A region-scoped forced width for section 0 outranks every other source (snoop,
    // CSV, blind detect) — it is the user pinning the geometry of the region they are
    // looking at. Sections 1+ are pinned as they're anchored (see feed / resync).
    const int forcedSec0 = forcedColumnsForSection(0);
    if (mSettings.dlv) {
        // DLV: the DlvSampleSource recovers the clock and aligns Column 0 to Sync1, so
        // trust its phase (no blind column detection — that reads FBCSE edge cadence).
        // forcedColumnCount seeds the Safe-Lock count; snoop drives the rest.
        mColumnCount = forcedSec0 ? forcedSec0
                     : (mSettings.forcedColumnCount >= swi3s::kMinColumnCount)
                       ? mSettings.forcedColumnCount : swi3s::kColdStartColumnCount;
        phaseOffset = 0;
        // The recovered-clock source subdivides each row by the column count, so a
        // pinned width has to reach it too or the DLL keeps framing at the old one.
        if (forcedSec0) mSrc.SetColumns(forcedSec0);
    } else if (forcedSec0 != 0) {
        mColumnCount = forcedSec0;
        phaseOffset = CColumnDetector::BestOffset(levels, mColumnCount);
    } else if (mSettings.forcedColumnCount != 0) {
        mColumnCount = mSettings.forcedColumnCount;
        // Count is known, but Column 0's phase still must be found (>2 columns
        // put several columns on rising edges) or the CDS decode misaligns.
        phaseOffset = CColumnDetector::BestOffset(levels, mColumnCount);
    } else if (mHaveCsv) {
        mColumnCount = mConfig.columnCount();
        phaseOffset = CColumnDetector::BestOffset(levels, mColumnCount);
    } else {
        int c = CColumnDetector::Detect(levels, &phaseOffset);
        mColumnCount = (c != 0) ? c : swi3s::kColdStartColumnCount;
    }

    if (mConfig.RowRateKHz <= 0.0) updateRowRateFromCapture(mColumnCount);

    // Seed the audio-output vector's capacity to roughly one bus row's worth of samples
    // per row (mAudio is appended to per decoded sample in the feed loop, never cleared).
    // Bounded by ROW count (mTotalUis/mColumnCount), not UI count, so it can't
    // over-allocate for a sparse/PCM stream; a denser (e.g. 1-bit PDM) stream simply grows
    // from here — either way this removes ~log2(rows) reallocation+copy passes over a
    // multi-million-element vector. Skipped for a streaming source of unknown length.
    if (mTotalUis > 0 && mColumnCount > 0)
        mAudio.reserve(mTotalUis / static_cast<std::uint64_t>(mColumnCount));

    mNrzs.Reset(BIT_LOW);
    mParser.Reset();
    // Hub depths are config (survive parser Reset), so apply once here.
    {
        int depths[swi3s::kMaxPeripherals] = {0};
        for (const auto& ov : mSettings.hubDepthOverrides) {
            int dev = ov.first, depth = ov.second;
            if (dev >= 0 && dev < swi3s::kMaxPeripherals) depths[dev] = depth;
        }
        mParser.SetHubDepths(depths);
    }
    mRegisters.Reset();
    // Align Column 0 to the detected phase offset: with >2 columns, the
    // rising-edge alignment can start mid-row, so step mColumn so that window
    // index `phaseOffset` is treated as Column 0.
    mColumn = (mColumnCount - phaseOffset % mColumnCount) % mColumnCount;
    // Payload ports must share the CDS Column-0 phase: a truncated/mid-stream capture
    // starts partway into a row (phaseOffset != 0), and CDS re-derives Column 0 from
    // its differential code — but the payload is sampled as an absolute level, so
    // without this the ports assume column 0 and read every port phaseOffset columns
    // early. Re-anchor the engine (configured above at start column 0) to mColumn.
    // (CSV path only; the auto path (re)configures at row boundaries where column 0
    // is already correct.)
    if (mDecodeAudio && mHaveCsv && mConfig.anyAudioEnabled()) {
        mEngine.SetStartColumn(mColumn);
        mEngine.Configure(mConfig);
        recordReconfig(segStartUi, mColumn);   // first config epoch (windowed decode)
    }
    mRowCounter = 0;
    mPendingSspRow = -1;
    mPendingSspCommit = false;
    mPendingSspSyncPoint = false;
    mPendingImmediateReconfig = false;
    mPendingSspCmdIndex = -1;
    // Manual SSP: reduce the chosen bus row modulo the LCM of the enabled ports'
    // interval periods (Interval + 1 rows each), so we re-anchor at the FIRST
    // congruent row near the decode start — making the chosen row (and every period
    // from it) row_in_interval == 0 across the whole capture, not just after it.
    mSspSyncRow = -1;
    if (mSettings.sspRow >= 0 && mEngine.Enabled()) {
        long period = 1;
        for (const SwI3sDpConfig& dc : mConfig.dps)
            // Skip a malformed negative Interval (Interval+1 <= 0): an applied config
            // CSV is unvalidated, and std::lcm(period, 0) == 0 would make the modulo
            // below divide by zero (SIGFPE).
            if (dc.Enabled && dc.numChannels() > 0 && dc.Interval >= 0)
                period = std::lcm(period, static_cast<long>(dc.Interval) + 1);
        if (period < 1) period = 1;          // defensive: never modulo by 0
        mSspSyncRow = ((mSettings.sspRow % period) + period) % period;
        // A reduced row of 0 means the SSP falls on the decode-start row. The feed()
        // re-anchor only fires on a row *wrap* (mRowCounter >= 1), so anchor row 0
        // explicitly here rather than relying on Initialize's coincident row_in_interval
        // == 0. Consume it so feed() doesn't look for row 0 again.
        if (mSspSyncRow == 0) {
            mEngine.SyncToSSP();
            recordSync(segStartUi);            // row-0 manual SSP re-anchor (windowed decode)
            mSspSyncRow = -1;
        }
    }
    mRowsSinceCommand = 0;
    mNeedResync = false;
    mCdsBitRows.clear();   // rows reset to 0 here, so old (sample->row) trail is stale
    mRowSyncSamples.clear();   // per-run row-start (RSP) samples; reset with the framing
    mSegments.clear();
    mSegmentPending = true;   // first Column-0 CDS bit anchors segment 0
    // Snapshot the initial engine phase (if a CSV config enabled ports) so a window
    // right after the start has a checkpoint to restore from.
    snapshotCheckpoint(segStartUi);

    // Feed the buffered window, then stream the remainder of the capture. If the
    // framing goes stale for a long run (a bus reconfiguration / cold restart we
    // never saw the commit for, e.g. 2 -> 8 columns), re-detect the column count
    // on a fresh window and re-hunt the comma, then continue. `srcUi` (the
    // absolute source UI of each fed UI) anchors segment row numbering.
    for (std::size_t i = 0; i < levels.size(); ++i)
        feed(levels[i] ? BIT_HIGH : BIT_LOW, samples[i], segStartUi + i);
    // Stream the remainder. Dispatch once to a source-typed drainer: for the concrete
    // TransitionSampleSource (the only source real captures use) this de-virtualizes
    // NextUi() and lets the tight inner loop inline — ~millions of virtual calls
    // otherwise. Any other ISampleSource (MemorySampleSource in tests) uses the
    // base-typed path with identical behaviour.
    if (auto* ts = dynamic_cast<TransitionSampleSource*>(&mSrc))
        streamRemainder(*ts);
    else if (auto* ds = dynamic_cast<DlvSampleSource*>(&mSrc))
        streamRemainder(*ds);            // PHY3: de-virtualize the recovered-clock NextUi too
    else
        streamRemainder(mSrc);
}

// Templated so the concrete source's NextUi()/UiIndex() inline (no vtable in the
// per-UI loop). Behaviour is identical to the base-typed walk it replaces.
template <class Src>
void Decoder::streamRemainder(Src& src)
{
    bool rising = false, dataHigh = false;
    std::uint64_t sn = 0;
    while (src.NextUi(rising, dataHigh, sn)) {
        feed(dataHigh ? BIT_HIGH : BIT_LOW, sn, src.UiIndex() - 1);
        if (mNeedResync)
            resync();
    }
}

// Re-detect the column count on the next window and reset framing. Registers are
// kept (config persists across the gap and the snoop will refine it); only the
// bit-level framing (NRZS phase, comma hunt, column/row counters) is reset.
// Column 0 begins on a rising clock edge, so re-align there first (as run() does).
void Decoder::resync()
{
    bool rising = false, dataHigh = false;
    std::uint64_t sn = 0;
    bool aligned = false;
    while (mSrc.NextUi(rising, dataHigh, sn)) {
        if (rising) { aligned = true; break; }
    }
    if (!aligned) { mNeedResync = false; return; }
    std::uint64_t segStartUi = mSrc.UiIndex();   // source UI of the new segment's Column 0

    // The rising edge is the alignment UI; Column 0 is the NEXT UI (matching
    // run()). Start the window there, not at the alignment edge, or the framing
    // is shifted by one UI and Column 0 lands on indices the offset search skips.
    const int kWindow = mSettings.dlv ? kDlvDetectWindow : kDetectWindow;
    std::vector<bool> levels;
    std::vector<std::uint64_t> samples;
    levels.reserve(kWindow);
    samples.reserve(kWindow);
    while (static_cast<int>(levels.size()) < kWindow &&
           mSrc.NextUi(rising, dataHigh, sn)) {
        levels.push_back(dataHigh);
        samples.push_back(sn);
    }
    if (levels.empty()) { mNeedResync = false; return; }

    // Re-measure the UI rate from this window: the clock rate can change across a
    // reconfiguration, so reusing the initial rate would mis-report the row rate
    // and audio sample rates for the new phase.
    if (samples.size() > 1 && samples.back() > samples.front()) {
        double avgUiSamples =
            static_cast<double>(samples.back() - samples.front()) / (samples.size() - 1);
        if (avgUiSamples > 0.0)
            mMeasuredUiRateHz = static_cast<double>(mSrc.SampleRateHz()) / avgUiSamples;
    }

    int phaseOffset = 0;
    int c = CColumnDetector::Detect(levels, &phaseOffset);
    // A pin on the section this resync would open outranks the blind detector — that
    // detector mis-locking is one of the two reasons to pin a region in the first place.
    if (int pin = forcedColumnsForSection(static_cast<int>(mSegments.size())))
        c = pin;
    if (c >= swi3s::kMinColumnCount && c != mColumnCount) {
        // Genuine reconfiguration (e.g. 2 -> 8 columns): adopt the new geometry
        // and rebuild the payload engine for it.
        mColumnCount = c;
        updateRowRateFromCapture(mColumnCount);
        // Seed the rebuilt engine with this segment's Column-0 phase (the resync
        // re-aligned to a rising edge and re-detected, so it can start mid-row too),
        // matching the mColumn set below.
        mEngine.SetStartColumn((mColumnCount - phaseOffset % mColumnCount) % mColumnCount);
        // Reconfiguration → a NEW section is about to be anchored (mSegmentPending
        // is set below); it will take the next segment index.
        maybeConfigureFromSnoop(mColumnCount, static_cast<int>(mSegments.size()));
        // Record the new config epoch + phase so a windowed re-decode can start here.
        recordReconfig(segStartUi, (mColumnCount - phaseOffset % mColumnCount) % mColumnCount);
        snapshotCheckpoint(segStartUi);
    } else if (c < swi3s::kMinColumnCount) {
        phaseOffset = 0;   // detection failed; keep prior count, restart at col 0
    }
    // If the column count is unchanged, this is just a comma re-hunt after a
    // command-sparse stretch (common during long audio payload). Do NOT reconfigure
    // the engine: that would reset its SSP-anchored interval counter and garble the
    // audio. The engine ticks continuously and stays anchored across the re-hunt.

    mNrzs.Reset(BIT_LOW);
    mParser.Reset();
    mColumn = (mColumnCount - phaseOffset % mColumnCount) % mColumnCount;
    // Bus rows stay CONTINUOUS across the resync (do NOT reset mRowCounter): the
    // new segment's rows carry on from where the old one left off, so the command
    // table and symbol viewer share one monotonic row scale rather than each
    // segment restarting at 0 (which made row numbers ambiguous).
    mPendingSspRow = -1;
    mPendingSspCommit = false;
    mPendingSspSyncPoint = false;
    mPendingImmediateReconfig = false;
    mPendingSspCmdIndex = -1;
    mRowsSinceCommand = 0;
    mNeedResync = false;
    mCdsBitRows.clear();      // sample->row trail: framing changed, old entries stale
    mSegmentPending = true;   // next Column-0 CDS bit anchors the new segment

    for (std::size_t i = 0; i < levels.size(); ++i)
        feed(levels[i] ? BIT_HIGH : BIT_LOW, samples[i], segStartUi + i);
}

void Decoder::feed(BitState level, std::uint64_t sampleNumber, std::uint64_t srcUi)
{
    // Decode progress (see Decoder.h progressUis()): one UI, one increment. Plain
    // member, no lock/atomic — negligible overhead next to the rest of feed(), and a
    // coarse cross-thread progress readout doesn't need anything stronger (see the
    // accessor's comment).
    ++mProgressUis;

    // DLV: record every Column-0 UI's sample as a Row Sync Point, from the decoder's
    // OWN framing (mColumn is re-aligned on resync), so the recovered-clock RSP marks
    // sit on the true S0→S1 row boundaries — the sample source's internal row counter
    // can diverge after a re-hunt and drift off the real edges. FBCSE marks come from
    // clock_edges (one edge per UI), so it doesn't need this.
    if (mSettings.dlv && mColumn == 0)
        mRowSyncSamples.push_back(sampleNumber);

    // Continuously track the forwarded-clock rate so a mid-capture clock-rate
    // change (without a column-count change) is reflected in the reported row /
    // audio sample rates — the resync watchdog only fires on framing loss. The
    // accumulate fast-path is inlined here (2 stores per edge); only the once-per-
    // window measurement calls out to closeUiRateWindow().
    if (mRateWinUiCount == 0) {
        mRateWinFirstSample = sampleNumber;
        mRateWinLastSample = sampleNumber;
        mRateWinUiCount = 1;
    } else {
        mRateWinLastSample = sampleNumber;
        if (++mRateWinUiCount >= kRateWindowUis)
            closeUiRateWindow();
    }

    // Framing-health tracking for auto re-sync now runs per-Row at the row wrap
    // below (command confidence is a Row count), so nothing to do per-UI here.

    // CDS column: FBCSE = Column 0 (NRZS-coded); DLV = CDS_HorizontalStart (plain NRZ,
    // the mid-UI level IS the bit — no NRZS). Sync1 (Col 0) / Sync0 (last col) framing in
    // DLV carries no CDS and no port, so it's simply not the CDS column and no port claims it.
    const int cdsColumn = mSettings.dlv ? mSettings.cdsHorizontalStart : swi3s::kCdsColumn;
    if (mColumn == cdsColumn) {
        bool bit = mSettings.dlv ? (level == BIT_HIGH) : mNrzs.Decode(level);
        // Anchor the current segment at its first real Column-0 CDS bit: record
        // (source UI, sample, column count, cumulative row base) so the symbol
        // viewer can window into this segment and compute matching row numbers as
        // rowBase + (symbolUi - startUi) / columnCount. Anchoring at an actual
        // Column-0 bit makes that exact regardless of the phase-offset alignment.
        if (mSegmentPending) {
            mSegments.push_back({srcUi, sampleNumber, mColumnCount,
                                 static_cast<long>(mRowCounter)});
            mSegmentPending = false;
        }
        // Record this CDS bit's row so a completed command can be back-dated to
        // its starting comma's row (see recordCommand). Bounded: the interior +
        // spacer bits are popped as each command resolves, but cap it anyway so a
        // command that never completes (garbled stretch) can't grow it forever.
        mCdsBitRows.emplace_back(sampleNumber, static_cast<long>(mRowCounter));
        if (mCdsBitRows.size() > 1u << 20) mCdsBitRows.pop_front();
        if (mParser.PushCdsBit(bit, sampleNumber)) {
            const SwI3sCommand& cmd = mParser.Command();
            // Record every completed phase for the command table (r.crcValid lets
            // the UI flag a bad phase); but only MUTATE snooped state on a good CRC.
            recordCommand(cmd);

            // Only a CRC-valid phase proves both that the framing is healthy and
            // that the decoded fields are trustworthy. A phase decoded at the wrong
            // column count still parses, but with a bad CRC -- applying its
            // (garbled) writes or commit would corrupt the snooped register /
            // geometry state, and letting a bad CRC reset the watchdog would stop a
            // reconfiguration (e.g. 2 -> 8 columns) from ever triggering a re-sync.
            // So gate every state mutation on crcValid; a run of bad CRCs is exactly
            // what the watchdog needs to see to re-sync.
            if (cmd.crcValid) {
                mRowsSinceCommand = 0;
                mRegisters.OnCommand(cmd);

                // A single-ranked WriteA32 to a data-port register takes committed effect
                // IMMEDIATELY (no commit) — e.g. PortControl (ScramblerEn/PortMode/Direction),
                // FlowMode, SampleSizeGrouping. Reconfigure the engine at the next row
                // boundary so the decode tracks the register state however it was set, not
                // only at a commit. (Dual-ranked writes stage and take effect at their commit.)
                if (cmd.phase == swi3s::kPhaseWrite && cmd.opcode == swi3s::kOpWriteA32 &&
                    cmd.hasAddress && cmd.address >= swi3s::reg::kDpBase &&
                    mRegisters.WriteIsImmediate(cmd.address))
                    mPendingImmediateReconfig = true;

                // ERROR CHECK (see CommandRec::enablechCurrError): a WriteA32 that touches an
                // EnableCh _CURR byte (0xC1/0xD0/0xD1 within a DP block) writes the channel-
                // enable register's committed rank directly, bypassing the normal _NEXT ->
                // _CURR promotion at an SSP/Commit. Per SWI3S v1.1, EnableCh is dual-ranked
                // for exactly this reason: an immediate change to which channels transport is
                // only safe when the port's Interval is 1 Row (every row transports, so there
                // is no mid-interval phase for the channel set to change under). Flag it
                // whenever the addressed port's (committed) Interval register != 0. A single
                // WriteA32 can span several consecutive registers (CRegisterModel::OnCommand
                // increments the address per byte), so check every byte's address, not just
                // the command's base address. Mirrors OnCommand's own DeviceMaskValid() guard —
                // a malformed multicast Write can't be attributed to one device's Interval.
                if (cmd.phase == swi3s::kPhaseWrite && cmd.opcode == swi3s::kOpWriteA32 &&
                    cmd.hasAddress && cmd.DeviceMaskValid()) {
                    int dev = firstSelectedDevice(cmd.deviceMask);
                    for (std::size_t bi = 0; bi < cmd.data.size(); ++bi) {
                        U32 a = cmd.address + static_cast<U32>(bi);
                        if (!swi3s::reg::InDpBlock(a)) continue;
                        U32 off = swi3s::reg::DpOffsetOf(a);
                        if (!swi3s::reg::IsEnableChCurrOffset(off)) continue;
                        int dp = swi3s::reg::DpIndexOf(a);
                        if (mRegisters.CurrentInterval(dev, dp) != 0) {
                            mCommands.back().enablechCurrError = true;
                            break;   // flagged; no need to scan the rest of the write
                        }
                        // This byte's port has Interval == 1 Row (safe), but a LATER
                        // byte can span a DIFFERENT port's EnableCh_CURR whose Interval
                        // != 0 — so keep scanning rather than breaking on the first hit.
                    }
                }

                // A confirmed commit (SSCR or DSCR) is SYNCED: it takes effect at its
                // Row_Delay row, not immediately — so defer the register commit + engine
                // reconfigure to that row (below), the same as an SSP. The only
                // difference is a DSCR does NOT re-anchor the SSP (see mPendingSspSyncPoint).
                // An SSPA (Announce with sync point, not a commit) re-anchors only.
                bool willCommit = cmd.isCommit && cmd.commitConfirmed;
                bool sspaReanchor = cmd.hasSyncPoint && !cmd.isCommit;
                if ((willCommit || sspaReanchor) &&
                    cmd.rowDelay >= swi3s::kSspRowDelayMin &&
                    cmd.rowDelay <= swi3s::kSspRowDelayMax) {
                    int spo = mHaveCsv ? swi3s::kSspDefaultSyncPointOffset
                                       : mRegisters.SyncPointOffset(firstSelectedDevice(cmd.deviceMask));
                    int delay = static_cast<int>(cmd.rowDelay) - spo;
                    if (delay < 0) delay = 0;
                    mPendingSspRow = static_cast<long>(mRowCounter) + 1 + delay;
                    mPendingSspCommit = willCommit;
                    mPendingSspSyncPoint = cmd.hasSyncPoint;   // DSCR (false) commits but no re-anchor
                    mPendingSspGroup = cmd.groupMask;
                    // Remember this commit's command so its effectiveRow can be filled
                    // in when the SSP fires (the authoritative commit point). recordCommand
                    // just appended it, so it's the last element. SSPA re-anchors aren't
                    // commits -> no marker.
                    mPendingSspCmdIndex = willCommit
                        ? static_cast<long>(mCommands.size()) - 1 : -1;
                }
            }
        }
    } else {
        if (!mSettings.dlv) mNrzs.Observe(level);   // DLV has no NRZS reference to maintain
    }

    if (mEngine.Enabled()) {
        bool payloadBit = (level == BIT_HIGH);
        // Append decoded samples straight into mAudio (Tick appends, never clears) —
        // no per-sample scratch→AudioRec copy.
        mEngine.Tick(payloadBit, sampleNumber, mAudio);
    }

    if (++mColumn == mColumnCount) {
        mColumn = 0;
        ++mRowCounter;
        // Framing health for auto re-sync (see header). Command confidence is lost after
        // kCommandConfidenceRows Rows with no CRC-valid command, so count Rows here (reset
        // on a good command below). Auto mode only: forced/CSV column counts are
        // authoritative and must not be overridden.
        if (mSettings.forcedColumnCount == 0 && !mHaveCsv &&
            ++mRowsSinceCommand > kCommandConfidenceRows)
            mNeedResync = true;
        // Absolute source UI of Column 0 of the row we're entering — the UI at which a
        // re-anchor/reconfigure done here takes effect on the payload engine (the next
        // tick). Used to key the windowed-decode transport events + checkpoints.
        const std::uint64_t evUi = srcUi + 1;
        // Manual SSP: re-anchor the payload engine at the chosen row (reduced to the
        // first congruent row near the start). This row becomes row_in_interval == 0.
        if (mSspSyncRow >= 0 && static_cast<long>(mRowCounter) == mSspSyncRow) {
            if (mEngine.Enabled()) { mEngine.SyncToSSP(); recordSync(evUi); snapshotCheckpoint(evUi); }
            mSspSyncRow = -1;                 // once (the phase then holds for the capture)
        }
        if (mPendingSspRow >= 0 && static_cast<long>(mRowCounter) >= mPendingSspRow) {
            if (mPendingSspCommit) {
                // The commit takes effect NOW, at the SSP row. Record it on the command
                // as the authoritative commit point (the UI reads this instead of
                // re-estimating the SSP and drifting from the decode).
                if (mPendingSspCmdIndex >= 0 &&
                    mPendingSspCmdIndex < static_cast<long>(mCommands.size()))
                    mCommands[mPendingSspCmdIndex].effectiveRow = mPendingSspRow;
                mRegisters.Commit(mPendingSspGroup);
                mParser.SetShortProtocolSpacer(mRegisters.ShortProtocolSpacer());
                if (!mHaveCsv && mDecodeAudio) {
                    int newCols = mRegisters.ColumnCount();
                    // A region-scoped pin on the section this commit opens outranks the
                    // snooped width (the wire may never have carried the real geometry).
                    // Resolve it against the index the new section will take, which is
                    // what the width change itself decides — so compute the candidate
                    // width first, then the index, then let a pin override.
                    bool willOpenSection = (newCols >= swi3s::kMinColumnCount &&
                                            newCols != mColumnCount);
                    int pin = forcedColumnsForSection(
                        willOpenSection ? static_cast<int>(mSegments.size())
                                        : std::max(0, static_cast<int>(mSegments.size()) - 1));
                    if (pin != 0) newCols = pin;
                    bool colsChanged = (newCols >= swi3s::kMinColumnCount &&
                                        newCols != mColumnCount);
                    if (colsChanged) {
                        // A reconfiguration (e.g. Safe-Lock-2 → 8 columns) changes
                        // the bus geometry: anchor a NEW segment at the next
                        // Column-0 bit so the grid/timeline see the width-over-time
                        // (the old width applied before this commit, the new one
                        // after) instead of one segment mislabelled with the start
                        // width while column_count reports only the latest.
                        mColumnCount = newCols;
                        mSegmentPending = true;
                        // DLV: the recovered-clock source subdivides each row by the
                        // column count, so push the new count (fires at a row boundary,
                        // where mColumn just wrapped). No-op for the forwarded-clock source.
                        mSrc.SetColumns(newCols);
                    }
                    updateRowRateFromCapture(mColumnCount);
                    // Section index being (re)configured: the pending new segment
                    // (width changed) takes the next index; otherwise it's the
                    // current segment (a same-width DP-config commit).
                    int sectionIndex = mSegmentPending
                        ? static_cast<int>(mSegments.size())
                        : static_cast<int>(mSegments.size()) - 1;
                    if (sectionIndex < 0) sectionIndex = 0;
                    // This reconfigure fires at a row boundary (mColumn just wrapped
                    // to 0), so the new ports start exactly on Column 0. A DSCR (no SSP,
                    // same geometry) preserves surviving ports' phase; anything that
                    // re-anchors or changes the column grid does a full (re)build.
                    mEngine.SetStartColumn(0);
                    bool preservePhase = !mPendingSspSyncPoint && !colsChanged;
                    if (maybeConfigureFromSnoop(mColumnCount, sectionIndex, preservePhase))
                        recordReconfig(evUi, 0, preservePhase);   // new config epoch (windowed decode)
                    mPendingImmediateReconfig = false;   // this commit already reconciled
                }
            }
            // Re-anchor the ports' interval timing only for events that generate an SSP
            // (SSCR / SSPA). A DSCR is synced but must NOT reset the SSP, so it skips this.
            if (mEngine.Enabled() && mPendingSspSyncPoint) { mEngine.SyncToSSP(); recordSync(evUi); }
            // Checkpoint the post-re-anchor phase so a windowed re-decode can restore it
            // (every commit/SSP gets one, bounding fast-forward regardless of spacing).
            if (mEngine.Enabled()) snapshotCheckpoint(evUi);
            mPendingSspRow = -1;
            mPendingSspCommit = false;
            mPendingSspSyncPoint = false;
            mPendingSspCmdIndex = -1;
        }
        // Immediate (single-ranked) config write: reflect it in the engine now, phase-
        // preserving (no SSP re-anchor), so the register state drives the decode however
        // it was set. A commit firing this row already rebuilt and cleared the flag.
        if (mPendingImmediateReconfig) {
            mPendingImmediateReconfig = false;
            if (!mHaveCsv && mDecodeAudio) {
                updateRowRateFromCapture(mColumnCount);
                int sectionIndex = static_cast<int>(mSegments.size()) - 1;
                if (sectionIndex < 0) sectionIndex = 0;
                mEngine.SetStartColumn(0);
                if (maybeConfigureFromSnoop(mColumnCount, sectionIndex, /*preservePhase=*/true)) {
                    recordReconfig(evUi, 0, /*preservePhase=*/true);
                    if (mEngine.Enabled()) snapshotCheckpoint(evUi);
                }
            }
        }
        // Periodic transport-phase checkpoint so a windowed re-decode never fast-forwards
        // more than kCheckpointRows rows even across a long commit/SSP-free stretch. Taken
        // AFTER any re-anchor above so it reflects the post-event phase.
        if (mEngine.Enabled() && (mRowCounter % kCheckpointRows) == 0)
            snapshotCheckpoint(evUi);
    }
}

} // namespace swi3score
