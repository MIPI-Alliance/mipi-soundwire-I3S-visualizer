// Streaming SWI3S PHY2 decoder over an ISampleSource, with the pipeline of the
// analyzer plugin's SwI3sAnalyzer::WorkerThread and no SDK dependency: align to
// Column 0, measure the UI rate + auto-detect the column count over a bounded
// leading window, then run the NRZS -> 8b/10b -> command transport ->
// register-snoop -> SSP/commit -> payload pipeline, collecting lightweight result
// records.

#ifndef SWI3SCORE_DECODER_H
#define SWI3SCORE_DECODER_H

#include <atomic>
#include <cstdint>
#include <deque>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "ISampleSource.h"
#include "CCommandTransportParser.h"
#include "CRegisterModel.h"
#include "CPayloadEngine.h"
#include "CDpConfig.h"
#include "CNrzsDecoder.h"

namespace swi3score {

struct DecoderSettings
{
    int  forcedColumnCount = 0;   // 0 = auto-detect from the bitstream; a value outside
                                  // [kMinColumnCount, kMaxColumnCount] also auto-detects
    bool decodeAudio = true;
    // PHY3 (DLV) mode: the source recovers the clock (mid-UI sampling) and the CDS is a
    // plain-NRZ bit at `cdsHorizontalStart` (no NRZS decode). Column 0 = Sync1 and the
    // last column = Sync0 are framing (no port claims them). forcedColumnCount seeds the
    // Safe-Lock count; snoop drives changes. FBCSE (default) leaves dlv=false.
    bool dlv = false;
    int  cdsHorizontalStart = 0;  // CDS column for DLV (e.g. 2 in Safe-Lock-4); 0 = FBCSE
    std::string configCsvPath;    // optional visualizer CSV (mid-stream mode)
    // Per-dataport scrambler override: (device, dp, on). Present entries force
    // ScramblerEn for that (device, dp) regardless of the snooped/CSV config;
    // absent entries keep the decoded value. Lets the UI correct a mis-snooped
    // ScramblerEn (descrambling raw data — or not descrambling scrambled data —
    // turns the payload into noise).
    std::vector<std::tuple<int, int, int>> scramblerOverrides;
    // Per-device hub depth (device, depth 0..5): a peripheral behind N hub
    // levels has its response delayed by 2*N frame rows. Absent devices = 0.
    std::vector<std::pair<int, int>> hubDepthOverrides;
    // Diagnostic: collect a per-data-bit sample point (sample, device, dp, channel,
    // is-sample-start) for the Raw Capture overlay. Off by default — it stores one
    // record per assembled data bit, which is sampleSize x the audio-value count.
    bool collectBitSamples = false;
    // Windowed bit-sample collection: only store bit points at capture sample >=
    // this. A dense capture has far more data bits than the kMaxBitSamples cap, so
    // collecting from sample 0 fills the cap before later ports — starting near the
    // region being viewed keeps the (bounded) collection where it's needed. 0 = start.
    std::uint64_t bitSampleStart = 0;
    // What-if register overrides: (section_index, device, address, value). Forced
    // into the register model for the matching config SECTION only, so the SAME
    // override the grid + register map show also drives the audio decode. Section-
    // scoped because a capture can change config (column count) mid-stream; keyed by
    // the section's index in segments() (stable — overrides don't move wire-detected
    // section boundaries). Applied like a write + commit-all (see applyRegisterOverrides).
    std::vector<std::tuple<int, int, std::uint32_t, int>> registerOverrides;
    // Region-scoped forced column count: (section_index, column_count) pairs. Pins ONE
    // config section's bus geometry, overriding both the snooped NumColumns and the blind
    // column detector for that section only. Section index keys segments(), exactly like
    // registerOverrides. Unlike the global forcedColumnCount it leaves the other
    // geometries of a capture that reconfigures mid-stream (a cold start's
    // Safe-Lock-2 -> 8 -> 16) alone. Applied as the section is anchored, so the section
    // RECORDS the forced width and the decode frames at it from there.
    std::vector<std::pair<int, int>> forcedColumnSections;
    // Sections whose payload config is built from their registerOverrides ALONE, not from
    // the snooped registers on top: a config CSV imported into that region. The CSV is the
    // region's whole config, so a port the wire enabled and the CSV does not name must
    // not decode there — the CSV writes no registers for a port it leaves out, so folding
    // it over the snoop could not express that. Framing and command parsing still follow
    // the wire (mRegisters is untouched); only the config handed to the payload engine
    // changes. Section index keys segments(), like registerOverrides.
    std::vector<int> importedConfigSections;
    // Later link bring-ups (the first is where the decode starts): (sample, column count,
    // cold, inactive from) per start sequence the Link-Control decoder found after a
    // stopped clock, in any order (sorted on load; points at one sample merge, cold if
    // either is). `sample` is the bring-up's first audio-mode clock edge. From `inactive
    // from` (the first sample after the clock stopped; 0 = none) up to `sample` the Link
    // is not in audio mode: those UIs are not decoded, so the old geometry emits no audio
    // and no commands from the start sequence, and every data port stops there.
    // At `sample` the decode restarts as at the start of the capture: it re-aligns to the
    // next rising clock edge, frames at `column count` (the Safe-Lock count, 2..32; out of
    // range, a Cold Start frames at the cold-start count and a Warm Start keeps the
    // current one) and opens a new segment, its payload engine reconfigured on the new
    // Column-0 phase. A Cold Start (`cold`) resets the bus: the register model returns to
    // its reset values and every port stays stopped until configured again. A Warm Start
    // keeps the registers, so its ports resume on the new phase.
    // A config CSV is the capture's configuration through every bring-up: a restart keeps
    // the CSV's column count and ports (a Cold Start still resets the snooped registers,
    // which command parsing reads). A forcedColumnSections pin on the section a restart
    // opens outranks both. With `dlv` the recovered clock locks again at the first Row Sync
    // at or after `sample`, with the nominal UI of `column count`, and is not run through
    // the inactive stretch (ISampleSource::Reacquire); the column count is chosen as for
    // FBCSE except that a config CSV's is not taken, as run() does not take it at the first
    // bring-up.
    std::vector<std::tuple<std::uint64_t, int, bool, std::uint64_t>> restartPoints;
    // Clock pauses with no bring-up after them: the first clock edge after each stop. Audio
    // mode never pauses its clock, so the analyzer lost samples or the capture was joined
    // there, and the bus may or may not carry on in phase. At each the decode checks the
    // Column-0 phase of a fresh window at the current column count: in phase, it decodes on
    // through as if nothing happened; out of phase (UIs were lost), it re-frames there, its
    // payload engine reconfigured on the new phase, in a new segment that is no restart
    // (geometry and registers kept). Any order; a restart point at the same sample wins.
    // FBCSE only: ignored when `dlv`. A DLV pause is a stretch with no row edges, and after
    // it the first rising edge need not open a row, so its row phase would have to be found
    // from the edges with no bring-up to anchor it.
    std::vector<std::uint64_t> resumePoints;
    // Manual SSP: force the payload engine's Stream Sync Point (row_in_interval == 0)
    // to bus row `sspRow`. -1 = off (auto: anchor at decode start / snooped commit).
    // A post-commit capture never carries the SSPA/SSCR that sets the SSP, so ports
    // with interval > 1 decode at an arbitrary phase (1/N rows correct, rest garbled);
    // this lets the user pick the row and re-decode until the audio is clean. Any bus
    // row — the phase (row mod interval) is anchored from the start, so the whole
    // capture (before and after the row) decodes to that SSP.
    long sspRow = -1;
};

// Decoded command phase.
struct CommandRec
{
    std::string command;          // CommandName() e.g. "WriteA32", "Ping", "SSCR"
    std::string phase;            // PhaseName()
    std::uint16_t deviceMask = 0;
    bool hasManagerPacket = false;
    std::uint8_t packetLength = 0;   // Manager Packet bytes (phase-header field)
    std::uint8_t opcode = 0;
    bool hasAddress = false;
    std::uint32_t address = 0;
    std::vector<std::uint8_t> data;
    bool crcValid = false;
    int peripheralResponse = -1;
    int managerResponse = -1;
    bool isCommit = false;
    bool commitConfirmed = false;
    bool hasSyncPoint = false;        // SSPA/SSCR generate a Stream Sync Point
    std::uint8_t rowDelay = 0;        // SSP Row_Delay (anchors the payload interval)
    std::uint16_t groupMask = 0;
    bool hasPingStatus = false;
    int pingStatus[12] = {};          // default-initialised
    bool hasReadData = false;
    std::vector<std::uint8_t> readData;
    bool readDataCrcValid = false;
    // ERROR: this WriteA32 targeted an EnableCh _CURR (0xC1/0xD0/0xD1) register address —
    // a direct immediate-effect write to the dual-ranked channel-enable register — while
    // the addressed port's committed Interval != 0 (Interval > 1 Row). Per SWI3S v1.1,
    // EnableCh is dual-ranked and normally promoted _NEXT -> _CURR at an SSP; a direct
    // _CURR write bypasses that and is only safe when every row transports (Interval ==
    // 1 Row, i.e. the Interval register == 0) — otherwise it can land mid-interval and
    // desynchronize payload framing. See Decoder::feed's EnableCh_CURR check.
    bool enablechCurrError = false;
    // ERROR: this command generated an SSP (SSCR / SSPA) that landed while a Payload
    // Interval Skipping port was PART-WAY through its skip pattern — the accumulated
    // effect of the SkippingNumerator was not already 0, which Section 9.1.6.2.1 calls an
    // unexpected SSP. Legal SSP rows for a skipping port are Interval x
    // SkippingDenominator Rows apart, not Interval Rows; an SSP anywhere else restarts the
    // pattern and shifts which intervals transport from there on. See Decoder::feed.
    bool unexpectedSspError = false;
    long busRow = 0;             // SWI3S bus row (Column-0 index) at phase start (SPM); continuous across segments
    // For a confirmed sync-point commit: the bus row where it actually TAKES EFFECT
    // (its SSP = command-end row + 1 + Row_Delay - SyncPointOffset), filled in by the
    // streaming decode when the commit fires. -1 = not a fired sync-point commit. This
    // is the authoritative commit point; the UI uses it instead of re-estimating.
    long effectiveRow = -1;
    std::uint64_t startSample = 0;
    std::uint64_t endSample = 0;
};

// One reconstructed audio sample. Identical to the payload engine's AudioSample, so
// it's an alias — the engine appends decoded samples straight into the decoder's
// output vector (no per-sample scratch→record copy across tens of millions of samples).
using AudioRec = AudioSample;

// One cell of the 2D bus grid (for the bus-grid view). 'slot' is the SwI3sSlot
// enum value; 'isCds' marks Column 0 (the Control Data Stream). The grid is
// MULTI-EMIT: a (row,col) carrying both a source transport and a sink transport
// yields two cells (distinguished by 'isSource'), so the renderer can draw the
// source half (top) and sink half (bottom) like the visualizer. 'sample'/'bit'
// are the in-group sample ordinal and the numeric bit weight, for the in-cell
// "S<sample>C<channel>" labels.
struct GridCell
{
    int row = 0;
    int col = 0;
    int dp = -1;
    int device = -1;         // owning peripheral (Device Mask bit), -1 if none
    int channel = -1;
    int slot = 0;            // SwI3sSlot: 0 Empty,1 Data,2 TxPresent,3/4 Guard,5 Tail,6 Drq
    bool sampleHere = false;
    bool isCds = false;
    bool isSource = false;   // true = source half (top), false = sink half (bottom)
    int sample = 0;          // sample-in-group ordinal (for the S<sample> label)
    int bit = 0;             // numeric bit weight within the sample (MSB..0)
    bool scramblerEn = false;  // owning data port's ScramblerEn (scrambler indicator)
    int portMode = 0;        // owning data port's PortMode (0 normal, 2 ones, 3 zeros)
};

// One decoded CDS 8b/10b symbol (for the symbol viewer).
struct SymbolRec
{
    std::uint64_t startSample = 0;
    std::uint64_t endSample = 0;
    long row = 0;                // SWI3S bus row (Column-0 index) of the symbol start
    std::uint16_t raw = 0;       // 10-bit codeword (a@bit9 .. j@bit0)
    int kind = 0;                // 0 invalid, 1 comma, 2 robust token, 3 D-code, 4 K-code
    int value = -1;              // D/K byte, or robust-token number, else -1
    bool aligned = false;
};

// One decode "segment": a stretch of the capture decoded at a single column
// count. A reconfiguration (e.g. cold-start 2 columns -> commit -> 8 columns)
// triggers a resync that starts a new segment, and so does a later link bring-up
// (DecoderSettings::restartPoints). Bus rows are continuous across
// segments (rowBase is the cumulative row at the segment's first Column-0 UI),
// so the symbol viewer can window into any segment with matching row numbers.
struct SegmentRec
{
    std::uint64_t startUi = 0;       // absolute source UI of this segment's Column 0, Row `rowBase`
    std::uint64_t startSample = 0;   // sample at that boundary
    int           columnCount = 2;
    long          rowBase = 0;        // cumulative bus row at the segment start
    bool          restart = false;    // opened by a later link bring-up (restartPoints)
    bool          coldStart = false;  // ...and that bring-up was a Cold Start
};

class Decoder
{
public:
    Decoder(ISampleSource& src, const DecoderSettings& settings)
        : mSrc(src), mSettings(settings) {}

    void run();

    const std::vector<CommandRec>& commands() const { return mCommands; }
    const std::vector<AudioRec>&   audio() const { return mAudio; }

    // Has releaseAudio() already dropped the samples? (See below — callers check this
    // rather than inferring "no audio" from an empty audio().)
    bool audioReleased() const { return mAudioReleased; }

    // Drop the decoded audio samples and hand their memory back (72 bytes per sample).
    //
    // Call once the Python layer has copied this vector into NumPy columns (once per
    // decode): every consumer past that point — audio store, playback, the plots, PDM
    // decode, WAV export — reads the columns, and a re-decode builds a new Decoder.
    //
    // Irreversible for this Decoder: audio() / audio_columns() afterwards is a bug in the
    // caller, and the bindings report it rather than answer with an empty set, which would
    // be indistinguishable from a capture that carried no audio.
    void releaseAudio()
    {
        mAudio.clear();
        mAudio.shrink_to_fit();     // clear() alone keeps the capacity
        mAudioReleased = true;
    }
    int columnCount() const { return mColumnCount; }
    double rowRateKHz() const { return mConfig.RowRateKHz; }
    double measuredUiRateHz() const { return mMeasuredUiRateHz; }
    const SwI3sConfig& config() const { return mConfig; }
    const std::vector<SegmentRec>& segments() const { return mSegments; }
    // Row Sync Point samples: the capture sample of every Column-0 UI (each row's
    // opening edge — the CDS Column-0 rising edge in FBCSE, the S0→S1 Sync1 edge in
    // DLV). Recorded from the decoder's OWN framing (mColumn==0), so they are the
    // authoritative, correctly-tracked row boundaries even across resyncs — unlike the
    // DLV sample source's internal row counter, which can diverge after a re-hunt.
    const std::vector<std::uint64_t>& rowSyncSamples() const { return mRowSyncSamples; }
    // Per-data-bit sample points (populated only when settings.collectBitSamples).
    const std::vector<BitSample>& bitSamples() const { return mEngine.bitSamples(); }

    // Flow-control DRQ<->TxPresent handshake validation results (SWI3S §14.2.2
    // {ASW5205}), accumulated over the decode. checked/fails/drqBits; 0 fails = clean.
    const CPayloadEngine::FlowControlStats& flowControlStats() const {
        return mEngine.flowControlStats();
    }

    // Windowed per-data-bit sample points for the source-UI range [startUi, endUi],
    // WITHOUT re-decoding the whole capture. The transport schedule (which UI carries
    // each bit's sample point) is fully determined by the config + SSP phase, so this
    // restores the nearest transport checkpoint recorded during run(), fast-forwards
    // the schedule to startUi (bounded by the checkpoint interval), then ticks the
    // window reading the wire to stamp each sampled bit. No 8b/10b, descramble, or
    // command parse — the Raw Capture overlay needs only the sample POSITIONS. Empty
    // when the source can't seek, no ports were configured before the window, or run()
    // hasn't been called. Requires settings.decodeAudio (else no ports/checkpoints).
    std::vector<BitSample> bitSamplesInWindow(std::uint64_t startUi, std::uint64_t endUi);

    // The decode authority's final committed register state (device, address, value),
    // for parity-checking the display register model — see CRegisterModel::CommittedSnapshot.
    std::vector<std::tuple<int, std::uint32_t, std::uint8_t>> committedRegisters() const {
        return mRegisters.CommittedSnapshot();
    }

    // Lay out the bus grid for the final (committed) config: tick the placement
    // cascade over `rows` rows and return the slot of each cell. Reuses the one
    // placement engine (CDataPort) rather than reimplementing §14.2.5.
    std::vector<GridCell> gridLayout(int rows) const;

    // Per-enabled-dataport audio sample rate (Hz), via the verified
    // SwI3sConfig::SampleRateHz formula. Tuples of (device, dp index, rate).
    std::vector<std::tuple<int, int, double>> audioSampleRates() const;

    // The decoder's EFFECTIVE config encoded as (device, address, value) register
    // writes (auto-detected column count + snoop + SSP commits) — the config that
    // actually produced the grid, complete even for cold-start-preloaded setups
    // the bus never re-wrote. Used as the baseline for the config comparison.
    std::vector<std::tuple<int, std::uint32_t, int>> configRegisters() const;

    // ---- decode progress (poll from another thread while run() is in flight) ----
    // run() is blocking and typically driven from a worker thread (bindings.cpp releases
    // the GIL across it) so the GUI thread can poll these for a progress dialog. Both are
    // plain uint64 members incremented/set from run()'s thread; reads from another thread
    // are safe for this coarse a readout WITHOUT a lock — each is written with a single
    // aligned store (so a concurrent read never sees a torn value), progressUis() is
    // monotonically non-decreasing for the life of one run(), and a stale/one-UI-behind
    // read only makes the reported percentage briefly lag. Do not add finer
    // synchronization here; that would defeat the point
    // (a per-UI atomic/lock would cost more than the decode step itself).
    std::uint64_t progressUis() const { return mProgressUis; }
    // Total UI count for this run, or 0 if the source can't report one (pure streaming)
    // — callers should treat 0 as "unknown", not "100% done". Set from the source's
    // TotalUiCount() before the main feed loop starts, then re-read every 64k UIs and at
    // the end of run(), never below progressUis(). A source with an exact count (a
    // forwarded-clock capture) reports the same value each time; a recovered-clock (DLV)
    // source reports an estimate that follows its current UI period.
    std::uint64_t totalUis() const { return mTotalUis; }
    // Convenience: progressUis()/totalUis() clamped to [0,1], or 0.0 if totalUis() is
    // unknown (0).
    double progress() const
    {
        if (mTotalUis == 0) return 0.0;
        const double f = static_cast<double>(mProgressUis) / static_cast<double>(mTotalUis);
        return (f < 1.0) ? f : 1.0;
    }
    // Ask an in-flight run() to stop, from another thread: it returns within ~64k UIs,
    // with what it decoded so far, and stopped() says so. For a caller watching memory
    // while the decode grows (the audio it produces cannot be predicted before decoding).
    // Atomic, unlike the progress counters, because this one changes control flow.
    void requestStop() { mStopRequested.store(true, std::memory_order_relaxed); }
    bool stopped() const { return mStopped; }

private:
    void feed(BitState level, std::uint64_t sampleNumber, std::uint64_t srcUi);
    // Stream the capture remainder after the leading window. Templated on the source
    // type so the concrete TransitionSampleSource de-virtualizes NextUi()/UiIndex()
    // in the per-UI hot loop; defined in Decoder.cpp (both instantiations used there).
    template <class Src> void streamRemainder(Src& src);
    template <class Src> void refreshTotalUis(const Src& src);
    void resync();   // re-detect column count + re-hunt comma after framing loss
    // Where a window read or an edge hunt stopped short: at the UI that reached the next
    // restart point (DecoderSettings::restartPoints), a falling or a rising edge, or not
    // at all (the window filled, the edge was found, or the source ended).
    enum class Crossing { None, Falling, Rising };
    // One restart point, normalized on load (see loadRestarts).
    struct Restart {
        std::uint64_t sample = 0;         // the bring-up's first audio-mode clock edge
        int           columns = 0;        // its Safe-Lock count (validated in restartAt)
        bool          cold = false;
        std::uint64_t inactiveFrom = 0;   // <= sample; == sample when there is no stretch
        bool          resume = false;     // a clock pause (resumePoints), not a bring-up
    };
    // The pending restarts from `restartPoints` (and, for FBCSE, `resumePoints`), later
    // than `after` (the first alignment edge): sorted stably by sample, points at one
    // sample merged (cold if either is), each inactive stretch clamped to begin after the
    // previous restart.
    void loadRestarts(std::uint64_t after);
    // The next sample the per-UI loops leave the fast path at: the next restart's
    // inactiveFrom (every UI from there to its sample takes the slow path, a few dozen in
    // a start sequence), or UINT64_MAX when none is pending.
    void updateNextStop();
    // Read the next UI to decode. Returns false at the end of the source (`crossing`
    // None) or at a UI at or past the next restart point (`crossing` says whether it was
    // a rising edge); that UI is the restart's. UIs in the inactive stretch before it are
    // consumed without being decoded (goInactive).
    bool readUi(bool& rising, bool& dataHigh, std::uint64_t& sn, Crossing& crossing);
    // The Link stopped at source UI `ui` (the first UI past the next restart's
    // inactiveFrom): from here to the restart nothing is decoded. A recovered clock (DLV)
    // is re-acquired at the restart at once, so the stretch is not read at all.
    void goInactive(std::uint64_t ui);
    // Stop every data port where the Link went inactive: an empty payload config, recorded
    // as a transport event at that UI so a windowed bit-sample replay stops there too.
    // Called as the restart is taken, once every UI before the stretch has been fed (a
    // window read can run past the stretch before its UIs are fed).
    void stopPorts();
    // Read UIs to the next rising clock edge (Column 0 opens on one). `aligned` says
    // whether it was found; if not, the return value says whether a restart point was.
    Crossing alignToRising(bool& aligned);
    // A later link bring-up (DecoderSettings::restartPoints): reset the registers on a
    // Cold Start and mark the segment it opens, then re-align, re-frame, reconfigure the
    // payload engine on the new Column-0 phase and feed a fresh window. `rising` is
    // whether the UI that crossed the restart sample is a rising edge. The Cold Start's
    // reset and the segment marks apply even when the window is empty, because the NEXT
    // restart point follows within it; the return value is that one's crossing, if any.
    Crossing restartAt(const Restart& r, bool rising);
    // The column count restart `r` frames at, opening segment `section`: a pin on that
    // section, else (FBCSE) a config CSV's, else the bring-up's Safe-Lock count (2..32),
    // else a Cold Start's cold-start count; a Warm Start with none keeps the current one.
    int restartColumns(const Restart& r, int section) const;
    // A clock pause (DecoderSettings::resumePoints): read to the next rising edge and a
    // fresh window, then either feed it all on in the current framing (in phase) or
    // re-frame on the window's Column-0 phase. `rising` is whether the UI that crossed the
    // pause (mCross*) is a rising edge. Returns the next restart point's crossing, if any.
    Crossing resumeAt(bool rising);
    // Take the restart points from `crossing` on, each in turn (one's window can reach the
    // next).
    void takeRestarts(Crossing crossing);
    // Read up to `n` UIs into levels/samples through readUi, stopping at a UI at or past
    // the next restart point, which is the restart's and not the window's. Every window
    // read goes through here: the leading one is 262144 UIs, which a short first session
    // fits inside whole.
    Crossing readWindow(int n, std::vector<bool>& levels, std::vector<std::uint64_t>& samples);
    // The bit-level framing reset shared by run(), resync() and restartAt(): NRZS phase,
    // comma hunt, Column-0 phase `startColumn`, pending commits, the watchdog count, and a
    // new segment anchored at the next Column-0 CDS bit. Bus rows carry on.
    void resetFraming(int startColumn);
    // Measure the UI rate from a window's UI samples (span / intervals), if it has two.
    void measureUiRate(const std::vector<std::uint64_t>& samples);
    void recordCommand(const SwI3sCommand& cmd);
    // Rebuild the payload config from the current register snoop. preservePhase uses the
    // engine's phase-preserving Reconfigure (for a DSCR — keep survivors' timing) instead
    // of a full Configure (which re-Initializes every port, for the SSP-anchored paths).
    // Returns true if the effective (enabled-port) config actually CHANGED and the engine
    // was (re)configured; false if it was unchanged (so the caller skips a spurious config
    // epoch — e.g. a single-ranked write that rewrote a register with the same value).
    // `force` reconfigures an unchanged config too: a restart puts every port on the new
    // Column-0 phase.
    bool maybeConfigureFromSnoop(int columnCount, int sectionIndex, bool preservePhase = false,
                                 bool force = false);
    void applyScramblerOverrides();   // force per-(device,dp) ScramblerEn from settings
    // Fold what-if register overrides for config section `sectionIndex` into `regs`
    // (ForceWrite + Commit-all), so the snooped config the audio engine uses matches
    // the grid/register-map for that section. No-op if none match.
    void applyRegisterOverrides(CRegisterModel& regs, int sectionIndex) const;
    // Region-scoped forced column count for `sectionIndex`, or 0 if none/out of range
    // (see DecoderSettings::forcedColumnSections).
    int forcedColumnsForSection(int sectionIndex) const;
    // DecoderSettings::forcedColumnCount, or 0 (auto-detect) if out of range.
    int forcedColumnCount() const;
    void updateRowRateFromCapture(int columnCount);
    static int firstSelectedDevice(std::uint16_t mask);

    // ---- windowed bit-sample reconstruction (see bitSamplesInWindow) ----
    // A transport-phase event recorded during run(): a payload-engine (re)Configure or
    // a SyncToSSP re-anchor, keyed by the absolute source UI at which it takes effect
    // (Column 0 of its row). Replaying this exact sequence over the payload engine
    // reproduces the schedule bit-for-bit, so a windowed re-decode can apply events
    // that fall inside its window (a mid-window commit/SSP) instead of re-parsing them.
    struct TransportEvent {
        std::uint64_t ui = 0;
        bool reconfigure = false;   // Configure(mConfigEpochs[epoch]) with startColumn
        bool sync = false;          // SyncToSSP() (after the reconfigure, when both)
        bool preservePhase = false; // reconfigure via phase-preserving Reconfigure (DSCR /
                                    //   immediate write) rather than a full Configure, so the
                                    //   windowed replay matches the live decode's survivor phase
        int  epoch = -1;            // index into mConfigEpochs (reconfigure only)
        int  startColumn = 0;
    };
    // A periodic snapshot of the payload engine's transport phase, so a windowed
    // re-decode restores the nearest one <= its window and ticks forward a bounded
    // distance rather than from the capture start.
    struct Checkpoint {
        std::uint64_t ui = 0;
        int epoch = -1;             // config epoch active at the snapshot
        int startColumn = 0;
        std::vector<CDataPort::State> ports;
    };
    void recordReconfig(std::uint64_t ui, int startColumn, bool preservePhase = false);
    void recordSync(std::uint64_t ui);                        // SSP re-anchor event
    void snapshotCheckpoint(std::uint64_t ui);                // engine phase snapshot
    static void applyTransportEvent(CPayloadEngine& engine, const TransportEvent& e,
                                    const std::vector<SwI3sConfig>& epochs);

    ISampleSource&  mSrc;
    DecoderSettings mSettings;

    CNrzsDecoder            mNrzs;
    CCommandTransportParser mParser;
    CRegisterModel          mRegisters;
    CPayloadEngine          mEngine;
    SwI3sConfig             mConfig;
    bool   mHaveCsv = false;
    bool   mDecodeAudio = true;
    int    mColumnCount = 2;
    int    mColumn = 0;
    std::uint64_t mRowCounter = 0;
    long   mPendingSspRow = -1;
    bool   mPendingSspCommit = false;
    // Whether the pending event generates a Stream Sync Point (SSCR / SSPA re-anchor
    // the ports' interval timing). A DSCR is a synced commit too, but does NOT re-anchor
    // the SSP — so it commits + reconfigures at its row without a SyncToSSP.
    bool   mPendingSspSyncPoint = false;
    std::uint8_t mPendingSspGroup = 0;
    std::uint16_t mPendingSspDevices = 0;   // the pending commit's Device Mask
    // An immediate (single-ranked) config write changed the committed register state with
    // no commit; reconfigure the payload engine at the next row boundary so the audio /
    // samples decode reflects the register state however it was set (not only at commits).
    bool   mPendingImmediateReconfig = false;
    long   mPendingSspCmdIndex = -1;   // index into mCommands of the pending commit, so
                                       // its effectiveRow can be back-filled when the SSP fires
    // Manual-SSP re-anchor row (settings.sspRow reduced modulo the port-interval LCM,
    // so it anchors near the decode start and phases the whole capture). -1 = off/done.
    long   mSspSyncRow = -1;
    double mMeasuredUiRateHz = 0.0;

    // Decode progress (see progressUis()/totalUis()/progress()): a plain counter bumped
    // once per UI in feed() (a single increment — negligible next to the decode work it
    // sits beside) and a total read from the source's TotalUiCount() (0 if the source
    // can't report one) at the start of run() and refreshed as totalUis() describes. No
    // lock: see progressUis()'s comment for why a coarse cross-thread read is fine here.
    std::uint64_t mProgressUis = 0;
    std::uint64_t mTotalUis = 0;
    std::atomic<bool> mStopRequested{false};
    bool mStopped = false;           // run() returned early because of requestStop()

    // Continuous UI-rate tracking: the forwarded-clock rate can change mid-capture
    // (e.g. a cold-start slow preamble that speeds up 4x to operational rate) WITHOUT
    // a column-count change, so the resync watchdog won't catch it. Sample the UI
    // period over a sliding window and re-derive the rate (and the reported row rate
    // + audio sample rates) when it drifts past a threshold. Decode framing is per
    // clock-edge and unaffected; this only keeps the *reported* rates honest.
    std::uint64_t mRateWinFirstSample = 0;   // first UI sample of the current window
    std::uint64_t mRateWinLastSample = 0;    // most recent UI sample
    long          mRateWinUiCount = 0;       // UIs accumulated in the window
    void closeUiRateWindow();   // once-per-window rate measurement (fast path inlined in feed)

    // Auto re-sync: the bus can be reconfigured (e.g. cold-start 2 columns ->
    // commit -> 8 columns) after an idle gap we never decode the commit across.
    // Command confidence is lost after 8192 Rows without a CRC-valid command (the
    // spec's confidence limit; the Manager must Ping at least every 4096 Rows,
    // {ASW2601}), so re-detect the column count on a fresh window and re-hunt the
    // comma only once that many Rows have elapsed command-free.
    long   mRowsSinceCommand = 0;
    bool   mNeedResync = false;

    // Rolling (CDS-bit sample -> bus row) trail, so a completed command can be
    // tagged with the row of its FIRST CDS bit (its SPM comma) rather than the
    // row where the phase finished — the latter is hundreds of rows later and
    // wouldn't line up with the symbol viewer or the command's start_sample.
    std::deque<std::pair<std::uint64_t, long>> mCdsBitRows;

    // Every Column-0 UI's sample (row-start / Row Sync Point), from the decoder's own
    // framing — see rowSyncSamples().
    std::vector<std::uint64_t> mRowSyncSamples;

    // Decode segments (one per column count or link restart); bus rows are continuous
    // across them.
    std::vector<SegmentRec> mSegments;
    bool mSegmentPending = false;   // anchor the next segment on the next Column-0 CDS bit
    bool mSegmentRestart = false;   // ...and mark it as opened by a later bring-up
    bool mSegmentColdStart = false; // ...a Cold Start
    std::vector<Restart> mRestarts;   // pending, sorted (loadRestarts)
    std::size_t mNextRestart = 0;
    std::uint64_t mNextStop = UINT64_MAX;   // see updateNextStop
    bool mInactive = false;         // between a restart's inactiveFrom and its sample
    std::uint64_t mInactiveUi = 0;  // source UI where that stretch began (stopPorts)
    // The UI that crossed the next restart point, read but not fed (readUi and the hot
    // loop set them): resumeAt feeds it when the bus carried on in phase.
    bool          mCrossHigh = false;
    std::uint64_t mCrossSample = 0;
    std::uint64_t mCrossUi = 0;

    // Windowed-decode reconstruction state, recorded during run() (see the structs
    // above / bitSamplesInWindow). mConfigEpochs holds each distinct payload config the
    // engine was Configure()d with; events + checkpoints reference it by index.
    std::vector<SwI3sConfig>  mConfigEpochs;
    std::vector<TransportEvent> mTransportEvents;
    std::vector<Checkpoint>   mCheckpoints;
    int mCurrentEpoch = -1;

    std::vector<CommandRec> mCommands;
    std::vector<AudioRec>   mAudio;
    bool                    mAudioReleased = false;   // see releaseAudio()
};

// Re-decode the Control Data Stream into classified 8b/10b symbols over a fresh
// source (the symbol viewer's data source). Decodes up to `maxSymbols` (0 =
// unbounded). If `startUi` > 0 and the source is seekable, it jumps there first
// (windowed re-decode); pass a Column-0-aligned UI index (1 + k*columnCount).
//
// Bus-row numbering: by default rows count from this decode's own Column-0
// origin. To make rows match the streaming decoder across a multi-segment
// capture, pass the containing segment's `rowOriginUi` (its Column-0, Row
// `rowBase` UI) and `rowBase`; each symbol's row is then
// rowBase + (symbolUi - rowOriginUi) / columnCount. Leave rowOriginUi at the
// sentinel (default) for the standalone, single-segment behaviour.
std::vector<SymbolRec> decodeSymbols(ISampleSource& src, int columnCount,
                                     int maxSymbols = 0, std::uint64_t startUi = 0,
                                     std::uint64_t rowOriginUi = ~0ull, long rowBase = 0,
                                     std::uint64_t endUi = 0, bool dlv = false,
                                     int cdsColumn = swi3s::kCdsColumn);

// Lay out the 2D bus grid for a config (shared by the live decode and what-if).
// PHY3 (DLV) differs from FBCSE framing: the CDS bit sits at `cdsColumn`
// (CDS_HorizontalStart, e.g. 2 for Safe-Lock-4) instead of Column 0, and every
// row opens with Sync1 (S1, Column 0) and closes with Sync0 (S0, last column) —
// pass `dlv=true` to emit those system cells. FBCSE keeps CDS at Column 0, no S0/S1.
std::vector<GridCell> layoutGrid(const SwI3sConfig& cfg, int columnCount, int rows,
                                 bool dlv = false, int cdsColumn = swi3s::kCdsColumn);

// What-if: lay out the grid from a set of (device, address, value) register
// writes — build a CRegisterModel, commit all groups, BuildConfig, layout.
// forceColumns >= kMinColumnCount overrides the register-derived column count
// (e.g. to draw an earlier cold-start segment's physical width); 0 = derive.
std::vector<GridCell> gridFromRegisters(
    const std::vector<std::tuple<int, std::uint32_t, std::uint8_t>>& writes,
    int fallbackColumns, int rows, int forceColumns = 0,
    bool dlv = false, int cdsColumn = swi3s::kCdsColumn);

// Lay out the grid from a visualizer CSV config (the "expected" config), for the
// config-vs-decoded comparison.
std::vector<GridCell> gridFromCsv(const std::string& csvPath, int rows);

// One decoded command for an as-of-cursor grid replay: a WriteA32 (isWrite, with
// deviceMask/address/data) or a commit (isCommit, commitConfirmed, groupMask).
// Replaying these in order through gridFromCommands honours dual-ranked _NEXT
// staging — a _NEXT write only takes effect on a confirmed commit of its group —
// unlike gridFromRegisters which force-commits a flat write set.
struct CommandReplay
{
    bool isWrite = false;
    bool isCommit = false;
    bool isRead = false;             // a CRC-valid ReadA32: reveals live register bytes
    bool commitConfirmed = false;
    std::uint16_t deviceMask = 0;
    std::uint32_t address = 0;
    std::vector<std::uint8_t> data;
    std::uint16_t groupMask = 0;
};

// Lay out the as-of-cursor grid by replaying decoded write/commit commands through
// the real register model (faithful _NEXT/_CURR staging + commit-group promotion).
// forceColumns >= kMinColumnCount overrides the derived width; 0 = derive.
// `overrides` are what-if forced writes (device, address, value) folded in after the
// replay (ForceWrite + Commit-all) — the SAME application the audio decode uses, so
// grid and audio agree. Already filtered to the cursor's config section by the caller.
std::vector<GridCell> gridFromCommands(const std::vector<CommandReplay>& cmds,
                                       int rows, int forceColumns = 0,
                                       const std::vector<std::tuple<int, std::uint32_t, int>>& overrides = {},
                                       bool dlv = false, int cdsColumn = swi3s::kCdsColumn);

// The decoded config as-of a command replay (same model build as gridFromCommands,
// without the layout) — the config in effect at the cursor's segment, for exporting the
// bus grid you're viewing as a visualizer CSV rather than always the final config.
SwI3sConfig configFromCommands(const std::vector<CommandReplay>& cmds, int forceColumns = 0,
                               const std::vector<std::tuple<int, std::uint32_t, int>>& overrides = {});

// As-of-cursor register state from the SAME command replay + override fold as
// gridFromCommands, PLUS bus reads overlaid (READ-tagged) — the register map's value
// + provenance source, so register map / grid / audio share one decode authority.
// Each tuple is (device, addr, cur, has_cur, cur_src, next, has_next, next_src) where
// *_src is CRegisterModel::Source (1 written, 2 read). Reads are folded in HERE only
// (never in gridFromCommands), so they colour the register map without perturbing the
// grid/audio config. `overrides` are (device, address, value), pre-filtered to the
// cursor's config section.
std::vector<std::tuple<int, std::uint32_t, std::uint8_t, bool, std::uint8_t,
                       std::uint8_t, bool, std::uint8_t>>
registersFromCommands(const std::vector<CommandReplay>& cmds,
                      const std::vector<std::tuple<int, std::uint32_t, int>>& overrides = {});

// Encode a config as the per-device register writes that would produce it (the
// inverse of CRegisterModel's decode). Shared by registersFromCsv and
// Decoder::configRegisters.
std::vector<std::tuple<int, std::uint32_t, int>> registersFromConfig(const SwI3sConfig& cfg);

// Encode a visualizer CSV config as the per-device register writes that would
// produce it (the inverse of CRegisterModel's decode). Returns (device, within-
// device address, value) triples — for showing the EXPECTED config in the
// register view (CSV provenance) and for round-trip validation against
// gridFromCsv via gridFromRegisters.
std::vector<std::tuple<int, std::uint32_t, int>> registersFromCsv(const std::string& csvPath);

} // namespace swi3score

#endif // SWI3SCORE_DECODER_H
