// Synthetic SWI3S PHY2 demo stream. See Demo.h. The plan building reuses the
// core's 8b/10b encoder, CRC, register model, placement cascade and scrambler so
// the produced stream decodes back to the generated audio.
//
// The demo starts in safe-lock-2 (2 columns, control only), commits to 8 columns
// (audio starts), then commits to 16 columns (audio continues, uninterrupted). It
// carries two 1-channel 16-bit PCM ports on 48 kHz transport (DP0 carries 44.1 kHz on
// it by skipping 13 of every 160 intervals) and two 1-channel PDM ports (3.072 MHz).
// Audio rate is held constant across the 8->16 commit by rewriting the dual-ranked
// Intervals alongside NumColumns: PCM Interval 63->31, PDM Interval 1->0, the PDM
// SampleGrouping staying at 2 samples in both geometries. DP0 (PCM) and DP2 (PDM) run
// unscrambled, DP1/DP3 scrambled, to show both transports.

#include "Demo.h"

#include <array>
#include <cmath>
#include <deque>
#include <map>
#include <stdexcept>
#include <string>

#include "C8b10bDecoder.h"
#include "CCrc16.h"
#include "CCommandTransportParser.h"
#include "CRegisterModel.h"
#include "CDataPort.h"
#include "CFlowControlPort.h"
#include "CDescrambler.h"
#include "CDpConfig.h"
#include "SwI3sProtocolDefs.h"

namespace swi3score {
namespace {

// Throws std::invalid_argument when a generator's length `n` (samples, intervals or
// samples per phase) is above kMaxDemoSamplesPerChannel.
void requireDemoLength(int n, const char* generator, const char* argument)
{
    if (n > kMaxDemoSamplesPerChannel)
        throw std::invalid_argument(std::string(generator) + ": " + argument + " " +
                                    std::to_string(n) + " is above " +
                                    std::to_string(kMaxDemoSamplesPerChannel));
}

// The most intervals MakeSkippingLevels builds. A skipping ratio multiplies the intervals a
// sample count needs, so the sample cap alone does not bound the stream.
constexpr long kMaxSkippingIntervals = 4L * kMaxDemoSamplesPerChannel;
// The most Rows a phase of the running-port write fixture spans (samples_per_channel x
// (Interval + 1)): kMaxDemoSamplesPerChannel samples at a 64-row Interval.
constexpr long long kMaxRunningPortRows = 64LL * kMaxDemoSamplesPerChannel;

// The largest value of a 12-bit register field: Interval, SkippingNumerator,
// SkippingDenominator.
constexpr int kMaxRegisterField12 = 0xFFF;

const double kPi = 3.14159265358979323846;

void appendSymbol(std::vector<bool>& bits, U16 sym)
{
    for (int i = 9; i >= 0; --i) bits.push_back((sym >> i) & 1);
}

void appendWriteA32(std::vector<bool>& bits, U16 devMask, U32 addr,
                    const U8* data, int n)
{
    appendSymbol(bits, C8b10bDecoder::CommaSymbol());
    int len = 5 + n;
    int hdr[6] = { swi3s::kPhaseWrite, (devMask>>8)&0xF, (devMask>>4)&0xF, devMask&0xF,
                   (len>>4)&0xF, len&0xF };
    for (int t : hdr) appendSymbol(bits, C8b10bDecoder::EncodeToken(t));
    std::vector<U8> pkt = { 0x00, (U8)(addr>>24),(U8)(addr>>16),(U8)(addr>>8),(U8)addr };
    for (int i = 0; i < n; ++i) pkt.push_back(data[i]);
    for (U8 b : pkt) appendSymbol(bits, C8b10bDecoder::EncodeByte(b));
    U16 crc = CCrc16::Compute(pkt.data(), pkt.size());
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc >> 8));
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc & 0xFF));
    appendSymbol(bits, 0x155);                          // MP spacer
    appendSymbol(bits, C8b10bDecoder::EncodeToken(0));  // WRITE_OK
}

// The Mgr-owned spacer + read payload + its own CRC + the tail that CLOSES the phase, shared
// by an immediate ReadSetup and a ReadData delivery.
//
// The tail is not optional. After the data + 2 CRC bytes the parser goes eReadPmSpacer ->
// eReadMgrResp and only emits the command record on the MANAGER's response symbol
// (READ_DATA_OK); without it the next command's comma arrives mid-spacer and abandons the
// phase, so the read produces no record and no error.
void appendReadPayload(std::vector<bool>& bits, const U8* data, int n)
{
    appendSymbol(bits, 0x155);                                    // Mgr-owned data spacer
    for (int i = 0; i < n; ++i) appendSymbol(bits, C8b10bDecoder::EncodeByte(data[i]));
    U16 crc = CCrc16::Compute(data, (size_t)n);
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc >> 8));
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc & 0xFF));
    int pm = swi3s::kSpacerBitsPM / swi3s::kSymbolBits;            // 20/10 = 2 symbols
    if (pm < 1) pm = 1;
    for (int i = 0; i < pm; ++i) appendSymbol(bits, 0x155);        // Peripheral -> Manager
    appendSymbol(bits, C8b10bDecoder::EncodeToken(0));             // READ_DATA_OK -> emits
}

// ReadSetup (ReadA32). `respToken` is what the PERIPHERAL answers:
//   0 = READ_DATA_NOW         -> the data follows in this same phase (IMMEDIATE read)
//   5 = REMOTE_READ_DEFERRED  -> no data now; the parser stashes the byte count + address
//                                (mPendingReadCount/Addr) for a later ReadData phase.
// Packet is opcode, Read Byte Count (excess-1), then Addr[31:24..07:00] big-endian.
void appendReadSetup(std::vector<bool>& bits, U16 devMask, U32 addr, int nbytes,
                     int respToken, const U8* data = nullptr)
{
    appendSymbol(bits, C8b10bDecoder::CommaSymbol());
    const int len = 6;
    int hdr[6] = { swi3s::kPhaseReadSetup, (devMask >> 8) & 0xF, (devMask >> 4) & 0xF,
                   devMask & 0xF, (len >> 4) & 0xF, len & 0xF };
    for (int t : hdr) appendSymbol(bits, C8b10bDecoder::EncodeToken(t));
    std::vector<U8> pkt = { (U8)swi3s::kOpReadA32, (U8)(nbytes - 1),
                            (U8)(addr >> 24), (U8)(addr >> 16), (U8)(addr >> 8), (U8)addr };
    for (U8 b : pkt) appendSymbol(bits, C8b10bDecoder::EncodeByte(b));
    U16 crc = CCrc16::Compute(pkt.data(), pkt.size());
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc >> 8));
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc & 0xFF));
    appendSymbol(bits, 0x155);                                    // MP spacer
    appendSymbol(bits, C8b10bDecoder::EncodeToken(respToken));
    if (respToken == 0 && data) appendReadPayload(bits, data, nbytes);
}

// The deferred delivery. A ReadData phase has NO Manager Packet at all — packetLength 0,
// then the MP spacer, then the peripheral's READ_DATA_NOW, then the payload. It inherits
// the byte count and address from the ReadSetup that deferred.
void appendReadData(std::vector<bool>& bits, U16 devMask, const U8* data, int n)
{
    appendSymbol(bits, C8b10bDecoder::CommaSymbol());
    int hdr[6] = { swi3s::kPhaseReadData, (devMask >> 8) & 0xF, (devMask >> 4) & 0xF,
                   devMask & 0xF, 0, 0 };                         // packetLength == 0
    for (int t : hdr) appendSymbol(bits, C8b10bDecoder::EncodeToken(t));
    appendSymbol(bits, 0x155);                                    // MP spacer
    appendSymbol(bits, C8b10bDecoder::EncodeToken(0));            // READ_DATA_NOW
    appendReadPayload(bits, data, n);
}

// A Ping (GetStatus) whose PingInfo field reports only the peripherals that are
// ACTUALLY on this demo's bus.
//
// `presentMask` is the demo's device mask; `alertMask` picks which of those report
// PING_ALERT instead of PING_ATTACHED. Every ABSENT device leaves the field undriven,
// which on the wire is the all-ones symbol — RobustToken can't resolve it to a token,
// returns -1, and the UI reports NO_RESPONSE (see analysis/responses.py).
const U16 kUndrivenSymbol = 0x3FF;      // undriven bus = all ones; decodes to token -1

void appendPing(std::vector<bool>& bits, U16 presentMask = 0x001, U16 alertMask = 0x000)
{
    appendSymbol(bits, C8b10bDecoder::CommaSymbol());
    int hdr[6] = { swi3s::kPhaseGetStatus, 0xF, 0xF, 0xF, 0, 1 };
    for (int t : hdr) appendSymbol(bits, C8b10bDecoder::EncodeToken(t));
    U8 pkt[1] = { 0x00 };
    appendSymbol(bits, C8b10bDecoder::EncodeByte(pkt[0]));
    U16 crc = CCrc16::Compute(pkt, 1);
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc >> 8));
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc & 0xFF));
    appendSymbol(bits, 0x155);
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
        if (!(presentMask & (1u << i))) {
            appendSymbol(bits, kUndrivenSymbol);          // absent -> NO_RESPONSE
            continue;
        }
        int tok = (alertMask & (1u << i)) ? 8 : 0;        // PING_ALERT : PING_ATTACHED
        appendSymbol(bits, C8b10bDecoder::EncodeToken(tok));
    }
    appendSymbol(bits, 0x155);
}

// A commit request (SSCR = kOpSscr, generates an SSP; DSCR = kOpDscr, commits without
// re-anchoring the SSP). Returns the bit index of the CONFIRM_COMMIT token.
size_t appendSscr(std::vector<bool>& bits, U16 devMask, U8 group, U8 rowDelay,
                  U8 opcode = swi3s::kOpSscr)
{
    appendSymbol(bits, C8b10bDecoder::CommaSymbol());
    int hdr[6] = { swi3s::kPhaseCommit, (devMask>>8)&0xF, (devMask>>4)&0xF, devMask&0xF, 0, 3 };
    for (int t : hdr) appendSymbol(bits, C8b10bDecoder::EncodeToken(t));
    U8 pkt[3] = { opcode, group, rowDelay };
    for (U8 b : pkt) appendSymbol(bits, C8b10bDecoder::EncodeByte(b));
    U16 crc = CCrc16::Compute(pkt, 3);
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc >> 8));
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc & 0xFF));
    appendSymbol(bits, 0x155);
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i)
        appendSymbol(bits, C8b10bDecoder::EncodeToken(0));
    appendSymbol(bits, 0x155);
    appendSymbol(bits, 0x155);
    appendSymbol(bits, C8b10bDecoder::EncodeToken(0));   // CONFIRM_COMMIT
    return bits.size() - 1;
}

// A Stream Sync Point Announce (SSPA): Announce phase, opcode 0x00. It generates an SSP
// but commits nothing — its purpose is to re-assert data-port synchronization so a port
// that has drifted is pulled back into phase (and an UNEXPECTED SSPA raises a peripheral
// interrupt). Unlike SSCR there is no CONFIRM_COMMIT and no peripheral-response block:
// kPhaseAnnounce with a non-zero packet length parses the Manager Packet + CRC and then
// emits at phase granularity (CCommandTransportParser::parseManagerPacket's `else`), so
// the phase ENDS on its last CRC symbol. Returns the bit index of that final bit — the
// row the decoder's SSP delay counts from (see Decoder's sspaReanchor path).
size_t appendSspa(std::vector<bool>& bits, U16 devMask, U8 group, U8 rowDelay)
{
    appendSymbol(bits, C8b10bDecoder::CommaSymbol());
    int hdr[6] = { swi3s::kPhaseAnnounce, (devMask>>8)&0xF, (devMask>>4)&0xF, devMask&0xF, 0, 3 };
    for (int t : hdr) appendSymbol(bits, C8b10bDecoder::EncodeToken(t));
    U8 pkt[3] = { swi3s::kOpSspa, group, rowDelay };
    for (U8 b : pkt) appendSymbol(bits, C8b10bDecoder::EncodeByte(b));
    U16 crc = CCrc16::Compute(pkt, 3);
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc >> 8));
    appendSymbol(bits, C8b10bDecoder::EncodeByte(crc & 0xFF));
    return bits.size() - 1;
}

// Length in CDS bits (== Rows) of one SSPA, computed from the emitter so it can never
// drift from it.
long sspaRows()
{
    std::vector<bool> probe;
    appendSspa(probe, 0x001, 0x01, 0);
    return (long)probe.size();
}

// Periodic-SSPA schedule for one audio region (see fillIdle). An SSPA re-anchors
// row_in_interval == 0, so it must land on a row where the ports ALREADY have their
// sync point: any other row would shift the transport phase and garble the audio (the
// same 1/N-phases problem a post-commit capture has). `anchor` is a known SSP row and
// `alignment` the LCM of the enabled ports' interval periods, so the only legal SSP
// rows are anchor + k*alignment.
struct SspaSchedule
{
    bool  on = false;
    long  period = 0;       // target rows between SSPAs (~100 ms of this region's rows)
    long  anchor = 0;       // a row known to be row_in_interval == 0
    long  alignment = 1;    // LCM of the enabled ports' transport pattern periods
    long  lastSsp = 0;      // SSP row of the previous SSPA (or the region's anchor)
    U16   devMask = 0;
    U8    group = 0;
    U8    rowDelay = 0;
};

// The most Rows intervalAlignment returns: more than any demo region holds, so an SSPA
// aligned to it does not fit and none is scheduled, and small enough that the schedule's
// row arithmetic stays within a 32-bit long.
constexpr long kMaxAlignmentRows = 1L << 29;

// LCM of every enabled port's transport pattern period — the row period after which all
// ports are back at the same point in their transport pattern. A port's period is its
// interval length in Rows, (Interval + 1), times SkippingDenominator for a skipping port
// (SwI3sConfig::patternRows): an SSPA must land on a multiple of THIS, or it restarts a
// skipping port's pattern part-way through and shifts which intervals transport. Capped
// at kMaxAlignmentRows (SwI3sConfig::patternPeriod).
long intervalAlignment(const SwI3sConfig& cfg)
{
    return static_cast<long>(cfg.patternPeriod(kMaxAlignmentRows));
}

bool anyAudioPort(const SwI3sConfig& cfg)
{
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) return true;
    return false;
}

// The SSPA period in Rows is 100 ms at a 24.576 MHz UI rate divided by the region's
// column count (2,457,600 / columns). That is 100 ms of wall-clock on the PHY2 and
// flow-control demos, which run at 24.576 MHz (8 col -> 3.072 MHz Rows, 16 col ->
// 1.536 MHz); the PHY1 demo (6.144 MHz UI) gets 400 ms and the PHY3 demo (49.152 MHz
// UI) 50 ms.
const double kDemoUiRateHz  = 24576000.0;
const double kSspaPeriodSec = 0.100;

long sspaPeriodRows(int numColumns)
{
    if (numColumns < 1) numColumns = 1;
    return (long)(kDemoUiRateHz * kSspaPeriodSec / (double)numColumns);
}

// Build the periodic-SSPA schedule for one audio region.
//
// `anchorSsp` is the region's own SSP (from the commit that opened it) — a row we know is
// row_in_interval == 0 — and `regionEnd` the row the region runs to. The nominal cadence
// is sspaPeriodRows(numColumns), but a demo built with a small audioSamplesPerChannel has
// regions far shorter than that, and a region with active data ports carrying NO SSPA
// would leave the whole feature unexercised in most tests. So when a full period doesn't
// fit, fall back to roughly one SSPA at the region's midpoint (still interval-aligned).
// Long captures get the full period.
SspaSchedule makeSspaSchedule(const SwI3sConfig& cfg, long anchorSsp, long regionEnd,
                              int numColumns, U16 devMask, U8 group, U8 rowDelay)
{
    SspaSchedule s;
    s.on = anyAudioPort(cfg);
    if (!s.on) return s;                      // no active data ports -> nothing to re-sync
    s.alignment = intervalAlignment(cfg);
    s.anchor = anchorSsp;
    s.lastSsp = anchorSsp;
    s.devMask = devMask;
    s.group = group;
    s.rowDelay = rowDelay;
    s.period = sspaPeriodRows(numColumns);
    const long need = (long)rowDelay + sspaRows();
    if (anchorSsp + s.period + need >= regionEnd) {
        // Shorter than one period: aim for the midpoint, rounded down to the alignment
        // (at least one full alignment step so it still lands on a real SSP row).
        long mid = (regionEnd - anchorSsp) / 2;
        long k = mid / s.alignment;
        s.period = (k > 0 ? k : 1) * s.alignment;
    }
    return s;
}

// The Manager must Ping at least every 4096 Rows or a peripheral loses command
// confidence and falls off the bus ({ASW2601}); real Managers Ping more often so an
// occasional errored Ping still leaves one good one in the window. This mirrors that:
// fill the CDS with the idle pattern up to `targetLen` Rows, dropping in a Ping every
// kPingRowPeriod Rows so the decode never loses confidence during the long audio phases.
// A Ping that wouldn't finish before targetLen is skipped — the control block that begins
// at targetLen is itself a confidence-refreshing command.
//
// The idle pattern is the alternating D10.2 sequence 0101010101 ({ASW2201}: when the
// Manager has no Command to send it drives a continuous stream of idle 0-1 bit pairs, one
// CDS bit per Row) — this is the idle pattern at the CDS-bit layer, independent of PHY.
// On FBCSE (PHY1/PHY2) the CDS is additionally NRZS-encoded (the "PHY1 & PHY2: NRZS
// Encoding for CDS" table: a CDS 0 inverts the bus value, a 1 repeats it), so those 0101
// idle bits render on the wire as the ..1100.. "zebra" — the same rendered form as the
// Cold-Start idle preamble. On DLV (PHY3) the CDS is not NRZS-encoded, so the identical
// 0101 idle bits render as 0101 directly.
// Emitting all-ones instead would NRZS-hold the FBCSE level flat (no zebra), which a real
// bus never does when idle.
const long kPingRowPeriod = 2048;   // half the {ASW2601} 4096-Row limit (error margin)

// The single-event test fixtures open with Safe-Lock-2 enumeration: the Manager Pings
// while peripherals attach. A blind decode picks the column count with the most CRC-valid
// phases in its leading window (262,144 UIs, at most 16 keep-alive Pings at 8 columns),
// so these Pings keep the Safe-Lock-2 start ahead at any samplesPerChannel.
const int kFixtureEnumerationPings = 24;

void appendEnumerationPings(std::vector<bool>& bits)
{
    for (int i = 0; i < kFixtureEnumerationPings; ++i) appendPing(bits);
}

// Fill the CDS with idle + keep-alive Pings up to `targetLen` Rows, and (when `sspa` is
// on) drop in periodic SSPAs.
//
// SSPA placement is the delicate part. The decoder takes an SSPA's SSP as
// (row of the command's LAST CDS bit) + 1 + Row_Delay - SyncPointOffset, and re-anchors
// every port's row_in_interval to 0 there. So to re-assert the phase the ports are
// ALREADY running at — rather than shift it — the SSP row must be congruent to a known
// SSP row modulo the ports' interval alignment. We therefore choose the target SSP row
// first (the next aligned row at least `period` past the previous one) and back-compute
// where the command has to START: startRow = sspRow - Row_Delay - sspaRows().
void fillIdleWithPings(std::vector<bool>& bits, long targetLen,
                       SspaSchedule* sspa = nullptr,
                       U16 pingPresentMask = 0x001, U16 pingAlertMask = 0x000)
{
    std::vector<bool> ping;
    appendPing(ping, pingPresentMask, pingAlertMask);    // one Ping's CDS bits (== Rows)
    const long sspaLen = (sspa && sspa->on) ? sspaRows() : 0;
    long sinceCmd = 0;                                   // Rows since the last command
    long idleBit = 0;                                    // index within the current idle run

    // Next SSPA: the first aligned SSP row >= lastSsp + period, and the row its command
    // must start on to land there. -1 = none scheduled / no longer fits.
    long nextStart = -1, nextSsp = -1;
    auto scheduleNext = [&]() {
        nextStart = nextSsp = -1;
        if (!sspa || !sspa->on || sspa->period <= 0) return;
        long want = sspa->lastSsp + sspa->period;
        long k = (want - sspa->anchor + sspa->alignment - 1) / sspa->alignment;   // ceil
        if (k < 0) k = 0;
        long ssp = sspa->anchor + k * sspa->alignment;
        long start = ssp - (long)sspa->rowDelay - sspaLen;
        // Must fit entirely before targetLen, and not start before where we already are.
        if (start < (long)bits.size() || ssp >= targetLen) return;
        nextStart = start;
        nextSsp = ssp;
    };
    scheduleNext();

    while ((long)bits.size() < targetLen) {
        if (nextStart >= 0 && (long)bits.size() == nextStart) {
            appendSspa(bits, sspa->devMask, sspa->group, sspa->rowDelay);
            sspa->lastSsp = nextSsp;
            sinceCmd = 0;                                // an SSPA refreshes command confidence
            idleBit = 0;
            scheduleNext();
            continue;
        }
        // Don't let a Ping straddle the next SSPA's start row — that would push the SSPA
        // late and its SSP off the aligned row.
        bool pingFits = (long)(bits.size() + (long)ping.size()) <= targetLen &&
                        (nextStart < 0 || (long)(bits.size() + (long)ping.size()) <= nextStart);
        if (sinceCmd >= kPingRowPeriod && pingFits) {
            bits.insert(bits.end(), ping.begin(), ping.end());
            sinceCmd = 0;
            idleBit = 0;                                 // idle resumes on the canonical 0 after a Ping
        } else {
            bits.push_back((idleBit++ & 1) != 0);        // idle CDS Row: D10.2 0101010101 (§8.1.2.9), 0 first
            ++sinceCmd;
        }
    }
}

// Pad `bits` with idle and keep-alive Pings to `startRow`, then append the event block
// `blk`, which must start there for its event to land on the row the fixture documents.
// Throws std::invalid_argument when the CDS already runs past `startRow`: the fixture's
// count argument (named `countName`, as Python passes it) is too small to hold the block.
void placeEventBlock(std::vector<bool>& bits, long startRow, const std::vector<bool>& blk,
                     const char* fixture, const char* countName, int count)
{
    if (startRow < static_cast<long>(bits.size()))
        throw std::invalid_argument(
            std::string(fixture) + ": " + countName + " " + std::to_string(count) +
            " leaves no room for the event block: it must start at CDS row " +
            std::to_string(startRow) + ", and the setup runs to row " +
            std::to_string(bits.size()) + ".");
    fillIdleWithPings(bits, startRow);
    bits.insert(bits.end(), blk.begin(), blk.end());
}

// PCM: a distinct sine per stream, MSb..LSb over (sampleSize+1) bits.
//
// Indexed in SAMPLES, not seconds — one cycle per 64 transported samples — so a port running a
// lower effective rate produces the same sample SEQUENCE at a lower pitch. That is what makes a
// skipping port need no special handling here: a skipped interval transports nothing, `index`
// does not advance, and the stream continues where it left off. Which is precisely what payload
// skipping is: a 44.1 kHz source dropping 48 kHz transport opportunities.
U64 sineSample(int channel, U64 index, int sampleSize)
{
    double amp = std::ldexp(1.0, sampleSize) * 0.45;
    double w = 2.0 * kPi * (double)(channel + 1) * (double)index / 64.0;
    int v = (int)std::lround(amp * std::sin(w));
    U64 mask = (1ull << (sampleSize + 1)) - 1;
    return (U64)v & mask;
}

// ---------------------------------------------------------------- payload interval skipping
//
// 44.1 kHz carried on 48 kHz transport opportunities (Section 14.1.10). 44100/48000 = 147/160
// exactly, so skipping 13 of every 160 intervals gives 44.1 kHz with no rounding at all — and
// 160 is the SMALLEST denominator that can, since 147/160 is already in lowest terms.
//
// The demo's PCM ports keep their 48 kHz Interval; only DP0 gains a numerator (two register
// writes). The rest follows from the existing machinery:
//
//   * the payload loop drives real CDataPort instances, so `startInterval` runs the accumulator
//     and a skipped interval simply emits nothing;
//   * `sineSample` is indexed in samples, so DP0's stream continues across a skipped interval at
//     a correspondingly lower pitch (see sineSample);
//   * `intervalAlignment` already multiplies a skipping port's period by the denominator, so the
//     periodic SSPAs land only where the accumulator is back at 0 — an SSPA anywhere else
//     restarts the pattern part-way through, which is the unexpected SSP of Section 9.1.6.2.1.
//
// That alignment is 64 x 160 = 10240 rows, so a region shorter than ~160 samples/channel cannot
// hold an interval-aligned SSPA and gets none. tests/test_sspa_demo.py uses 1500
// samples/channel so every region holds one.
const int kSkipNumerator   = 13;
const int kSkipDenominator = 160;      // 48000 * (160-13)/160 == 44100 exactly

// A single register write both emitted onto the CDS bit stream AND applied to the
// register model (so BuildConfig reflects it after a Commit).
void emitWrite(std::vector<bool>& bits, CRegisterModel& regs, U16 dev, U32 addr,
               std::initializer_list<U8> bytes)
{
    std::vector<U8> data(bytes);
    appendWriteA32(bits, dev, addr, data.data(), (int)data.size());
    SwI3sCommand c; c.clear();
    c.phase = swi3s::kPhaseWrite; c.opcode = swi3s::kOpWriteA32;
    c.deviceMask = dev; c.hasAddress = true; c.address = addr; c.data = data;
    regs.OnCommand(c);
}

// Emit the bus-level denominator plus one port's numerator. Byte orders are OPPOSITE and both
// are called out as exceptions in the register tables: SLC_SkippingDenominator is MSB-first
// ([11:8] at the LOWER address), DPn_SkippingNumerator is LSB-first. Written as explicit bytes
// so neither can be "tidied" into the other's order.
void emitSkipping(std::vector<bool>& bits, CRegisterModel& regs, U16 dev, int dp,
                  int numerator = kSkipNumerator, int denominator = kSkipDenominator)
{
    emitWrite(bits, regs, dev, swi3s::reg::kSkippingDenomHi,
              {(U8)((denominator >> 8) & 0x0F)});
    emitWrite(bits, regs, dev, swi3s::reg::kSkippingDenomLo, {(U8)(denominator & 0xFF)});
    emitWrite(bits, regs, dev,
              swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)dp
                  + swi3s::reg::kDpSkipNumLo,
              {(U8)(numerator & 0xFF), (U8)((numerator >> 8) & 0x0F)});
}

// One port's placement/rate for a geometry. dp = data-port index within the device.
// sampleGroup/spacing are register fields (excess-1 count / column stride): sampleGroup=1
// transports 2 samples per group, spacing=1 packs them in adjacent columns (0 gap).
struct PortGeom { int dp; int sampleSize; int hstart; int hcount; int interval;
                  int sampleGroup; int spacing; int offset = 0; };

// Emit the _NEXT config writes for a port. SampleSizeGrouping (0x09) is single-rank; the
// rest are _NEXT (promoted at the next commit). Single channel: ch0 only (bit7 of 0x81).
void emitPortWrites(std::vector<bool>& bits, CRegisterModel& regs, U16 dev, const PortGeom& p)
{
    U32 base = swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)p.dp;
    // 0x09: [7:5]=SampleGrouping(ex-1), [4:0]=SampleSize(ex-1).
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpSampleSizeGrouping,
              {(U8)(((p.sampleGroup & 0x7) << 5) | (p.sampleSize & 0x1F))});
    // 0x80..0x85: HStart, EnableCh0|HCount, {[7:6]TailWidth,bit5 SubRowInterval,[3:0]Spacing},
    // {[7:4]Offset[3:0],[3:0]Interval[3:0]}, Interval[11:4], Offset[11:4].
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpBitWidthHStart_N,
              {(U8)(p.hstart & 0x1F), (U8)(0x80 | (p.hcount & 0x1F)), (U8)(p.spacing & 0xF),
               (U8)(((p.offset & 0xF) << 4) | (p.interval & 0xF)), (U8)((p.interval >> 4) & 0xFF),
               (U8)((p.offset >> 4) & 0xFF)});
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpChannelGrouping_N, {0x00});  // group = numChannels
}

// --- Flow-control demo helpers ----------------------------------------------

// A distinct audio tone at an EXPLICIT frequency (Hz), unlike sineSample() whose
// frequency is hard-wired to (channel+1)*sampleRate/64. MSb..LSb over (sampleSize+1)
// bits, two's-complement masked, so the bit-exact round-trip test regenerates the
// identical value with the same formula.
U64 sineAt(double freqHz, double sampleRateHz, U64 index, int sampleSize)
{
    double amp = std::ldexp(1.0, sampleSize) * 0.45;
    double w = 2.0 * kPi * freqHz * (double)index / sampleRateHz;
    int v = (int)std::lround(amp * std::sin(w));
    U64 mask = (1ull << (sampleSize + 1)) - 1;
    return (U64)v & mask;
}

// A small deterministic PRNG (SplitMix64-style) so the transport jitter is identical
// on every synthesis run — a fixed seed is what makes the jittered capture bit-exact
// reproducible (and scripts/tests can regenerate the exact same pattern). NOT for
// cryptographic use; just a reproducible coin flip per data stream.
struct Lcg
{
    U64 s;
    explicit Lcg(U64 seed) : s(seed) {}
    U64 next()
    {
        s += 0x9E3779B97F4A7C15ull;
        U64 z = s;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
        return z ^ (z >> 31);
    }
    int coin() { return (int)(next() & 1); }   // 0 or 1 interval of jitter
};

// The scramblers a generator's ports start with after a reconfigure that re-anchors them
// (an SSCR). `scram` is keyed by p * 32 + ch, p indexing `prev`; the result is keyed the
// same way over `next`. A channel enabled on the same (device, data port) in both, with
// ScramblerEn set both times, keeps its LFSR; every other enabled channel of `next` is
// seeded. CPayloadEngine::Configure applies the same rule.
std::map<int, CDescrambler> carryScramblers(const std::map<int, CDescrambler>& scram,
                                            const std::vector<const SwI3sDpConfig*>& prev,
                                            const SwI3sDpConfig* const* next, int nNext)
{
    std::map<int, CDescrambler> out;
    for (int p = 0; p < nNext; ++p) {
        const SwI3sDpConfig& d = *next[p];
        int q = -1;
        for (int i = 0; i < (int)prev.size(); ++i)
            if (prev[i]->deviceNum == d.deviceNum && prev[i]->dpNumber == d.dpNumber) q = i;
        for (int ch = 0; ch < 16; ++ch) {
            if (!(d.EnableCh & (1u << ch))) continue;
            CDescrambler lfsr;
            if (q >= 0 && d.ScramblerEn && prev[q]->ScramblerEn &&
                (prev[q]->EnableCh & (1u << ch))) {
                auto it = scram.find(q * 32 + ch);
                if (it != scram.end()) lfsr = it->second;
            }
            out[p * 32 + ch] = lfsr;
        }
    }
    return out;
}

// Emit one peripheral flow-control data-port's config writes for THIS demo: unlike
// emitPortWrites (single channel, no direction/flow/FCP), this writes a full multi-
// channel port with its PortDirection, FlowMode and (for DRQ modes) FCP placement, so
// the sniffed capture reconstructs the four flow modes. Single geometry (no _NEXT/commit
// dance beyond the one commit the caller issues). 'enableMask' is the 16-bit EnableCh.
struct FcGeom {
    int dp;             // data-port index within the device (0)
    int sampleSize;     // excess-1
    int interval;       // rows/interval - 1
    int offset;         // rows from SSP before the window opens
    U16 enableMask;     // enabled channels
    int flowMode;       // kFlowNormal / TxControlled / RxControlled / Async
    bool sink;          // true = SINK (manager drives), false = SOURCE (peripheral drives)
    int fcpHStart;      // FCP DRQ column (DRQ modes only; ignored otherwise)
    int fcpOffset;      // FCP DRQ row-in-interval
};

void emitFcPortWrites(std::vector<bool>& bits, CRegisterModel& regs, U16 dev, const FcGeom& p,
                      bool fcpScrambled)
{
    U32 base = swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)p.dp;
    // 0x09: [7:5]=SampleGrouping(=0), [4:0]=SampleSize.
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpSampleSizeGrouping,
              {(U8)(p.sampleSize & 0x1F)});
    // 0x0B: bit3=ScramblerEn(0 -> unscrambled, so TX_PRESENT gating is decoupled from
    // the descrambler LFSR), bit2=PortDirection (1=SINK), [1:0]=PortMode(0).
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpScramDirMode,
              {(U8)((p.sink ? 1 : 0) << 2)});
    // 0x0E: FlowMode [1:0], FCP_ScramblerEn [3], FlowControlDelay [7] = 0.
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpFlowMode,
              {(U8)((p.flowMode & 0x3) | (fcpScrambled ? 0x08 : 0x00))});
    // 0x80..0x85: BitWidth(0)/HStart=2, EnableCh0|HCount=12, Tail/Sub/Spacing=0,
    // Offset[3:0]|Interval[3:0], Interval[11:4], Offset[11:4].
    const int hstart = 2, hcount = 12;
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpBitWidthHStart_N,
              {(U8)(hstart & 0x1F),
               (U8)(((p.enableMask & 1) ? 0x80 : 0x00) | (hcount & 0x1F)),
               (U8)0x00,
               (U8)(((p.offset & 0xF) << 4) | (p.interval & 0xF)),
               (U8)((p.interval >> 4) & 0xFF),
               (U8)((p.offset >> 4) & 0xFF)});
    // 0x90/0x91: EnableCh ch1..7 (register bits[7:1] map 1:1 to channels 1..7, bit0
    // reserved) and ch8..15 (register bits[7:0] -> channels 8..15). Mirror decodeDp.
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpEnableCh1_7_N,
              {(U8)(p.enableMask & 0xFE)});
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpEnableCh8_15_N,
              {(U8)((p.enableMask >> 8) & 0xFF)});
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpChannelGrouping_N, {0x00});
    // FCP placement for the DRQ modes so the decoder can locate the DRQ column.
    if (p.flowMode == kFlowRxControlled || p.flowMode == kFlowAsync) {
        emitWrite(bits, regs, dev, base + swi3s::reg::kDpFcpBwHStart_N,
                  {(U8)(p.fcpHStart & 0x1F)});
        emitWrite(bits, regs, dev, base + swi3s::reg::kDpFcpOffLo_N,
                  {(U8)((p.fcpOffset & 0xF) << 4)});
        emitWrite(bits, regs, dev, base + swi3s::reg::kDpFcpOffHi_N,
                  {(U8)((p.fcpOffset >> 4) & 0xFF)});
    }
}

} // namespace

std::vector<bool> MakeDemoLevels(int audioSamplesPerChannel)
{
    requireDemoLength(audioSamplesPerChannel, "make_demo_levels", "audio_samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kPcmSS = 15;   // 16-bit PCM
    const int kPdmSS = 0;    // 1-bit PDM
    // PCM Interval per geometry (rows/interval - 1) for a constant 48 kHz:
    //   8-col row rate 3.072 MHz -> 64 rows -> 63;  16-col 1.536 MHz -> 32 rows -> 31.
    const int kPcmInt8 = 63, kPcmInt16 = 31;
    // PDM holds 3.072 MHz across the commit WITHOUT changing SampleGrouping (0x09 is
    // single-ranked — it would take effect immediately, not at the commit). Instead keep
    // SampleGrouping = 2 samples (register 1) in BOTH geometries and vary the DUAL-RANKED
    // Interval so the change applies cleanly at the commit: 2 rows/interval @ 8-col (row
    // 3.072 MHz -> 2 samples / 2 rows = 3.072 MHz), 1 row/interval @ 16-col (row 1.536 MHz
    // -> 2 samples / row = 3.072 MHz). Two columns each (Spacing=1, adjacent).
    const int kPdmSG = 1, kPdmInt8 = 1, kPdmInt16 = 0;

    // Port layout. Column 0 is the CDS; column 1 (right after it) and the last column are
    // kept FREE for the CDS / S1 handovers (EnforceCDSHandover / EnforceS1Handover), so
    // data lives in cols 2..(N-2). Fields: {dp, sampleSize, hstart, hcount, interval,
    // sampleGroup, spacing, offset}.
    //
    // 8-col: only cols 2..6 (five) are free for four ports, so the two PCM ports SHARE
    // column 2, time-interleaved — DP0 at Offset 0, DP1 at Offset 16 (= one 16-bit
    // sample's 16 rows), both Interval 63; the two PDM ports take cols 3-4 and 5-6.
    // 16-col: room for one column each — DP0 col 2, DP1 col 3, PDM cols 4-5 and 6-7.
    const PortGeom geom8[4] = {
        {0, kPcmSS, 2, 0, kPcmInt8, 0, 0, 0},       {1, kPcmSS, 2, 0, kPcmInt8, 0, 0, 16},
        {2, kPdmSS, 3, 1, kPdmInt8, kPdmSG, 1, 0},  {3, kPdmSS, 5, 1, kPdmInt8, kPdmSG, 1, 0},
    };
    const PortGeom geom16[4] = {
        {0, kPcmSS, 2, 0, kPcmInt16, 0, 0, 0},        {1, kPcmSS, 3, 0, kPcmInt16, 0, 0, 0},
        {2, kPdmSS, 4, 1, kPdmInt16, kPdmSG, 1, 0},   {3, kPdmSS, 6, 1, kPdmInt16, kPdmSG, 1, 0},
    };

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);   // idle preamble
    appendPing(bits);

    // ---- commit #1: safe-lock-2 -> 8 columns (audio starts) ----
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});   // 8 columns
    for (const PortGeom& p : geom8) emitPortWrites(bits, regs, dev, p);
    // DP0 carries 44.1 kHz on its 48 kHz transport opportunities — 13 of every 160 intervals
    // are skipped (Section 14.1.10; see emitSkipping). Written ONCE, before the first commit:
    // the register model keeps the value, so it also governs the 16-column geometry, where the
    // Interval halves to 31 and the skipping pattern's period halves with it.
    emitSkipping(bits, regs, dev, /*dp=*/0);
    // Show both transports side by side: DP0 (PCM) and DP2 (PDM) run UNSCRAMBLED
    // (PortControl.ScramblerEn=0); DP1/DP3 keep the reset default (scrambling ON). 0x0B is
    // single-rank, so this holds for the whole capture (both geometries).
    for (int dp : {0, 2})
        emitWrite(bits, regs, dev,
                  swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)dp
                      + swi3s::reg::kDpScramDirMode, {0x00});
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group, dev);
    SwI3sConfig cfg8; regs.BuildConfig(cfg8);
    cfg8.NumColumns = 7;
    if (cfg8.SkippingDenominator < 1) cfg8.SkippingDenominator = 1;

    long ssp1 = ssp1Bit + 1 + (int)rowDelay;

    // ---- commit #2: 8 -> 16 columns (audio continues) ----
    // Build the 16-col control block FIRST so its length is known: the 8-col audio phase
    // must be long enough to carry it, or SSCR#2 would overrun its intended SSP row (only
    // bites tiny-N demos; the real 1 s demo has millions of 8-col rows).
    std::vector<bool> blk;
    emitWrite(blk, regs, dev, swi3s::reg::kNumColumns_Next, {0x0F});    // 16 columns
    for (const PortGeom& p : geom16) emitPortWrites(blk, regs, dev, p);
    appendPing(blk);
    appendSscr(blk, dev, group, rowDelay);                        // confirm bit = blk.back()

    // 8-col audio phase: ~1/3 of the audio, but at least enough interval-aligned rows to
    // hold the 16-col control block plus a spare interval.
    long minRows8 = (long)blk.size() + (int)rowDelay + (kPcmInt8 + 1);
    long minSamples8 = (minRows8 + kPcmInt8) / (kPcmInt8 + 1);     // ceil to whole intervals
    long samples8 = audioSamplesPerChannel / 3;
    if (samples8 < minSamples8) samples8 = minSamples8;
    long rows8 = samples8 * (kPcmInt8 + 1);
    long ssp2 = ssp1 + rows8;

    long blkStart = ssp2 - (int)rowDelay - (long)blk.size();      // so confirm row = ssp2-rowDelay-1
    // 8-col audio: idle CDS + keep-alive Pings + periodic SSPAs re-asserting the port phase.
    SspaSchedule sspa8 = makeSspaSchedule(cfg8, ssp1, blkStart, 8, dev, group, rowDelay);
    fillIdleWithPings(bits, blkStart, &sspa8);
    bits.insert(bits.end(), blk.begin(), blk.end());
    regs.Commit(group, dev);
    SwI3sConfig cfg16; regs.BuildConfig(cfg16);
    cfg16.NumColumns = 15;
    if (cfg16.SkippingDenominator < 1) cfg16.SkippingDenominator = 1;

    long samples16 = (long)audioSamplesPerChannel - samples8;
    if (samples16 < 1) samples16 = 1;
    long rows16 = samples16 * (kPcmInt16 + 1);
    long totalRows = ssp2 + rows16;

    // 16-col audio runs to the end with no further reconfiguration, so keep the CDS
    // Pinging here too (else confidence lapses after 8192 Rows -> a spurious re-hunt),
    // plus this region's own periodic SSPAs (its geometry -> its own alignment/cadence).
    SspaSchedule sspa16 = makeSspaSchedule(cfg16, ssp2, totalRows, 16, dev, group, rowDelay);
    // A DEFERRED Read: ReadSetup answered REMOTE_READ_DEFERRED (no data), then the ReadData
    // delivery inheriting its byte count + address. An IMMEDIATE read goes first for
    // contrast — the two shapes produce a DIFFERENT NUMBER OF COMMAND RECORDS from the same
    // logical read, which is what per-command statistics group by.
    //
    // Appended BEFORE the idle fill: fillIdleWithPings pads to `totalRows`, so whatever
    // these consume it emits less idle to reach the same total. The demo's row count, audio
    // timing and SSP rows are therefore unchanged — only the command list grows.
    const U8 immData[2] = { 0x5A, 0xA5 };
    const U8 defData[2] = { 0xC3, 0x3C };
    appendReadSetup(bits, dev, 0x0000'2040u, 2, 0, immData);      // immediate: one record
    appendReadSetup(bits, dev, 0x0000'2044u, 2, 5);               // deferred: no data yet
    appendReadData(bits, dev, defData, 2);                        // ... delivered here
    fillIdleWithPings(bits, totalRows, &sspa16);

    // Enabled ports (BuildConfig order = decoder payload order) for each geometry.
    auto enabledDps = [](const SwI3sConfig& c) {
        std::vector<const SwI3sDpConfig*> v;
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0) v.push_back(&d);
        return v;
    };
    std::vector<const SwI3sDpConfig*> dps8 = enabledDps(cfg8), dps16 = enabledDps(cfg16);

    // A distinct tone per (port, channel); key persists across the geometry change so
    // the sine phase / PDM modulator continue uninterrupted at the 8->16 commit.
    auto tone = [](int p, int ch) { return p * 2 + ch; };
    auto key = [](int p, int ch) { return p * 32 + ch; };

    std::map<int, CDescrambler> scram;
    std::map<int, U64> curSample, sampleIndex;
    std::map<int, double> sdAcc;                        // PDM sigma-delta integrator

    int columnCount = 2;                                // safe-lock-2 to start
    const SwI3sDpConfig* const* dps = nullptr;
    int N = 0;
    std::vector<CDataPort> ports;
    bool portReady = false;

    // Each region's ports take that region's own config (its SkippingDenominator). A
    // surviving scrambled channel keeps its LFSR across the SSCR (carryScramblers).
    std::vector<const SwI3sDpConfig*> live;            // the ports before the reconfigure
    auto reconfigure = [&](const SwI3sConfig& cfg, const std::vector<const SwI3sDpConfig*>& dd,
                           int cc) {
        columnCount = cc;
        N = (int)dd.size();
        ports.assign(N, CDataPort());
        scram = carryScramblers(scram, live, dd.data(), N);
        live = dd;
        for (int p = 0; p < N; ++p) {
            ports[p].Configure(*dd[p], cc, cfg.SkippingDenominator);
            ports[p].Initialize();
            ports[p].SyncToSSP();
        }
        portReady = true;
    };

    std::vector<bool> levels;
    levels.reserve((size_t)(ssp2 * 8 + rows16 * 16));
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) { dps = dps8.data(); reconfigure(cfg8, dps8, 8); }
        else if (r == ssp2) { dps = dps16.data(); reconfigure(cfg16, dps16, 16); }  // index kept
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[64];
            if (portReady)
                for (int p = 0; p < N; ++p) es[p] = ports[p].clock_tick();

            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                level = cur;
                if (portReady) {
                    for (int p = 0; p < N; ++p) {
                        const DpEmit& e = es[p];
                        if (e.sampleHere &&
                            (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                            int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                            bool clear;
                            if (e.slot == SwI3sSlot::Data) {
                                int ss = dps[p]->SampleSize;
                                if (b == ss) {
                                    if (ss == 0) {          // PDM: 1st-order sigma-delta of a
                                        // low audio tone so playback is clearly audible:
                                        // dp2 = 250 Hz, dp3 = 750 Hz (freq / 3.072 MHz per bit).
                                        double freqHz = 250.0 + 500.0 *
                                            (double)(dps[p]->dpNumber - 2);
                                        double t = 2.0 * kPi * freqHz
                                                   * (double)sampleIndex[k]++ / 3072000.0;
                                        sdAcc[k] += 0.45 * std::sin(t);
                                        U64 bit = (sdAcc[k] >= 0.0) ? 1 : 0;
                                        sdAcc[k] -= bit ? 1.0 : -1.0;
                                        curSample[k] = bit;
                                    } else {
                                        curSample[k] = sineSample(tone(p, ch),
                                                                  sampleIndex[k]++, ss);
                                    }
                                }
                                clear = ((curSample[k] >> b) & 1) != 0;
                            } else {
                                clear = false;
                            }
                            // Scramble only if the port's ScramblerEn is set; DP0/DP2 run
                            // unscrambled (raw clear bit), matching the decode which reads
                            // ScramblerEn and skips descrambling for them.
                            level = dps[p]->ScramblerEn ? scram[k].Scramble(clear) : clear;
                            break;                          // disjoint windows: one owner
                        }
                    }
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// A distinct tone per (device, channel) for the flow-control demo. Lowest = 20 Hz
// (dev0 NORMAL), spread by octaves so the bit-exact round-trip test can tell every
// stream apart. The test MUST replicate this table exactly (see
// tests/test_flow_control_demo.py::_FLOW_FREQ) — keep them in sync.
double flowDemoFreq(int deviceNum, int channel)
{
    struct Tone { int dev; int ch; double hz; };
    static const Tone kTones[] = {
        {0, 4,   20.0},                                   // NORMAL       (the 48 kHz reference)
        {1, 5,   55.0}, {1, 6,  110.0},                   // TX_CONTROLLED
        {2, 7,  220.0}, {2, 8,  440.0},                   // RX_CONTROLLED
        {3, 7,  880.0}, {3, 8, 1760.0},                   // ASYNC
    };
    for (const Tone& t : kTones)
        if (t.dev == deviceNum && t.ch == channel) return t.hz;
    return 20.0;
}

// Flow-control demo: four PERIPHERAL data ports, one per flow mode, on four devices —
// the four ports a bus sniffer actually sees (the manager's own DPs are configured
// off-bus and never appear, so the capture collapses to these four). Mirrors
// tests/fixtures/flow_control_demo.csv's visible DP4-7:
//
//   dev0  DP  NORMAL         1ch (ch4)     Interval 31 -> 48 ksps, every interval
//         transports (no TX_PRESENT, no gating) — the reference stream.
//   dev1  DP  TX_CONTROLLED  2ch (ch5,6)   Interval 15 -> 96 k transport opportunities/s
//         carrying 48 ksps audio; each sample sent at opportunity 2i + jitter(0/1).
//   dev2  DP  RX_CONTROLLED  2ch (ch7,8)   as TX; peripheral SOURCEs the data.
//   dev3  DP  ASYNC          2ch (ch7,8)   as RX.
//
// The audio is sampled UNIFORMLY at 48 kHz; only the TRANSPORT is jittered (0 or 1
// interval late, a deterministic coin per sample), exercising the TX_PRESENT
// hand-shaking. The receiver de-jitters for free: the decoder emits a sample only
// when its TX_PRESENT read 1, in order, so audio() == the uniform 48 kHz stream and
// the round-trip is bit-exact. The DRQ bus bits (RX/ASYNC handshake, and ASYNC's
// jittered-DRQ + 0/1-interval TX response delay) are driven at the FCP cells, mirroring
// the decode's FCP re-drive.
std::vector<bool> MakeDemoLevelsFlowControl(int audioSamplesPerChannel, bool fcpScrambled)
{
    requireDemoLength(audioSamplesPerChannel, "make_demo_levels_flow_control", "audio_samples_per_channel");
    const U8 group = 0x01, rowDelay = 14;
    const int kSS = 15;                                   // 16-bit samples throughout

    // The four visible peripheral ports (device mask, device number, geometry).
    struct FcPort { U16 mask; int deviceNum; FcGeom geom; };
    const FcPort fps[4] = {
        {0x001, 0, {0, kSS, 31, 0, 0x0010, kFlowNormal,       true,  0,  0}},
        {0x002, 1, {0, kSS, 15, 2, 0x0060, kFlowTxControlled, true,  0,  0}},
        {0x004, 2, {0, kSS, 15, 5, 0x0180, kFlowRxControlled, false, 11, 7}},
        {0x008, 3, {0, kSS, 15, 8, 0x0180, kFlowAsync,        false, 11, 10}},
    };
    const U16 allDev = 0x00F;
    // Device 3 raises PING_ALERT; devices 0-2 report PING_ATTACHED. Keeps the ALERT
    // decode path exercised on a peripheral that is genuinely on this bus (the
    // single-device demos can only show ATTACHED).
    const U16 alertDev = 0x008;

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);   // idle preamble
    appendPing(bits, allDev, alertDev);

    // ---- single commit: 16 columns, all four ports, generates the SSP ----
    // NumColumns is written PER DEVICE: a WriteA32 must select exactly one device
    // (Section 8.1.2.2 — only GetStatus/Announce/Commit may be multicast), so a
    // broadcast write is rejected as malformed and never lands. The Commit/SSCR that
    // follows may be multicast.
    for (const FcPort& fp : fps)
        emitWrite(bits, regs, fp.mask, swi3s::reg::kNumColumns_Next, {0x0F});   // 16 columns
    for (const FcPort& fp : fps) emitFcPortWrites(bits, regs, fp.mask, fp.geom, fcpScrambled);
    appendPing(bits, allDev, alertDev);
    long sspBit = (long)appendSscr(bits, allDev, group, rowDelay);
    regs.Commit(group, allDev);
    SwI3sConfig cfg; regs.BuildConfig(cfg);
    cfg.NumColumns = 15;
    if (cfg.SkippingDenominator < 1) cfg.SkippingDenominator = 1;

    long ssp = sspBit + 1 + (int)rowDelay;

    // Rows to carry N samples/channel on the SLOWEST-throughput port. NORMAL: 32
    // rows/sample. The flow ports have a 16-row interval, but ASYNC gates transport
    // on TWO independent coins (DRQ ~1/2 AND SourceReady ~1/2 => ~1/4 of opportunities
    // carry data), so its worst-case rate is ~4 intervals/sample = 64 rows/sample.
    // Size for that so every channel reaches N samples; faster ports simply emit more
    // (harmless — the round-trip regenerates the expected sine for whatever length).
    long N = audioSamplesPerChannel;
    long postRows = 64 * N + 128;
    long totalRows = ssp + postRows;
    // idle CDS + keep-alive Pings + periodic SSPAs re-asserting the port phase. The
    // flow-control ports gate transport on DRQ/SourceReady, so a port that has skipped
    // opportunities is exactly the case an SSPA exists to re-synchronize.
    SspaSchedule sspa = makeSspaSchedule(cfg, ssp, totalRows, 16, allDev, group, rowDelay);
    fillIdleWithPings(bits, totalRows, &sspa, allDev, alertDev);

    // The bus runs Safe-Lock-2 (2 columns, CDS only) through the config phase, then the
    // SSCR commits to 16 columns where the audio begins — same ladder the standard demo
    // uses. A cold-started decoder comes out of PHY-select seeded at Safe-Lock-2 and
    // GROWS the column count by snooping the commit (it does not blind-detect), so the
    // capture MUST present the 2->16 transition or the decode never leaves 2 columns.
    const int kAudioColumns = 16;

    // Enabled ports in BuildConfig (= decoder payload) order: dev0,dev1,dev2,dev3.
    std::vector<const SwI3sDpConfig*> dps;
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) dps.push_back(&d);
    const int P = (int)dps.size();

    std::vector<CDataPort> ports(P);
    std::vector<CFlowControlPort> fcps(P);
    for (int p = 0; p < P; ++p) {
        ports[p].Configure(*dps[p], kAudioColumns, cfg.SkippingDenominator);
        ports[p].Initialize();
        ports[p].SyncToSSP();
        if (dps[p]->drqEnabled()) {
            fcps[p].Configure(*dps[p], kAudioColumns);
            fcps[p].Initialize();
        }
    }

    // Flow-control transport model (SWI3S §14.2.2). Per interval a port has a
    // transport opportunity; whether it carries a sample depends on the mode:
    //   NORMAL         always carries (no TX_PRESENT, no DRQ).
    //   TX_CONTROLLED  source decides via SourceReady (a deterministic coin) — a
    //                  "0/1 interval of transport jitter" with no DRQ.
    //   RX_CONTROLLED  bijective: TxPresent[n] == Earlier_FCP_DRQ[n], where the DRQ
    //                  is the sink's request d = FlowControlDelay+1 intervals earlier
    //                  (the "Selection of FCP_DRQ Bit in Source DP" table). The manager
    //                  drives the DRQ bit on the bus at the FCP cell; the demo drives a
    //                  deterministic DRQ coin and the transport MUST mirror it d intervals
    //                  later.
    //   ASYNC          TxPresent[n] = Earlier_FCP_DRQ[n] AND SourceReady[n] (the "Effect
    //                  of DPn_FlowMode when DPn_PortMode is Normal" table):
    //                  the sink holds DRQ HIGH (standing permission), and the source's
    //                  own SourceReady gate (a producer + small FIFO) decides when to
    //                  emit, with a random 0/1-interval delay. Data appears only in a
    //                  requested slot, so {ASW5205} holds; delivery = production rate, so
    //                  the audio is fully carried. (Spec §14.1.12 leaves SourceReady /
    //                  SinkReady / FIFO / latency ImpDef; this is one faithful choice.)
    // Uniform sampling at the source: only the transport is jittered, so the decode's
    // TX_PRESENT gating + the display's de-jitter recover the source stream. A fixed
    // per-device seed keeps it bit-exact reproducible.
    std::vector<U64> audioIndex(P, 0);
    std::vector<std::array<U64, 16>> sampleVal(P);
    std::vector<std::deque<char>> drqHist(P);          // sink DRQ requests, oldest..newest
    std::vector<char> curTxp(P, 0);                    // this interval's TxPresent decision
    std::vector<char> curDrq(P, 0);                    // this interval's DRQ request
    std::vector<char> drqWire(P, 0);                   // the DRQ level on the bus
    // One LFSR per FCP, seeded when the port is enabled and stepped once per DRQ bit
    // (cfg FCP_ScramblerEn, as BuildConfig read it back from 0x0E bit 3).
    std::vector<CDescrambler> fcpScram(P);
    std::vector<int>  srcAccum(P, 0), srcPending(P, 0); // ASYNC source producer + FIFO occupancy
    std::vector<Lcg> lcgDrq, lcgSrc;
    for (int p = 0; p < P; ++p) {
        lcgDrq.emplace_back(0xD00D0000ull + (U64)dps[p]->deviceNum);
        lcgSrc.emplace_back(0x5EED0000ull + (U64)dps[p]->deviceNum);
        sampleVal[p].fill(0);
    }

    std::vector<bool> levels;
    levels.reserve((size_t)totalRows * kAudioColumns);
    bool cur = false;
    bool started = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp) started = true;
        // Safe-Lock-2 (CDS only) until the SSP row; the audio geometry (16 columns)
        // begins there, matching the committed NumColumns the decoder snoops.
        int columnCount = started ? kAudioColumns : swi3s::kColdStartColumnCount;
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[4];
            FcpEmit fes[4];
            if (started) {
                for (int p = 0; p < P; ++p) {
                    es[p] = ports[p].clock_tick();
                    if (dps[p]->drqEnabled())
                        fes[p] = fcps[p].clock_tick(ports[p].IntervalSkipped());
                }
            }

            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;                 // NRZS CDS
            } else {
                level = cur;                              // hold unless a port drives
                if (started) {
                    // Data / TX_PRESENT emission (whichever port owns this cell).
                    for (int p = 0; p < P; ++p) {
                        const DpEmit& e = es[p];
                        if (!(e.sampleHere &&
                              (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)))
                            continue;

                        // A fresh transport opportunity: decide (once per interval,
                        // shared by all channels) whether it carries a sample, per the
                        // port's flow mode. curDrq[p] for this interval was latched at
                        // the DRQ cell earlier in the interval (FCP_Offset < data Offset).
                        if (e.freshTransport) {
                            const int fm = dps[p]->FlowMode;
                            if (fm == kFlowNormal) {
                                curTxp[p] = 1;
                            } else if (fm == kFlowTxControlled) {
                                curTxp[p] = (char)lcgSrc[p].coin();   // SourceReady only
                            } else {
                                // RX / ASYNC: Earlier_FCP_DRQ = the DRQ from d intervals
                                // ago (startup assumes 1). d = FlowControlDelay + 1.
                                int d = dps[p]->FlowControlDelay + 1;
                                char earlier = ((int)drqHist[p].size() >= d)
                                    ? drqHist[p][drqHist[p].size() - d] : 1;
                                if (fm == kFlowRxControlled) {
                                    curTxp[p] = earlier;               // bijective (mode 2)
                                } else {
                                    // ASYNC (mode 3): TxPresent = SourceReady AND
                                    // Earlier_FCP_DRQ (the "Effect of DPn_FlowMode when
                                    // DPn_PortMode is Normal" table). Model a source whose
                                    // audio arrives at ~half the opportunity rate (a
                                    // producer feeding a small FIFO) and which may DELAY
                                    // its response by 0/1 interval. Emit only in a DRQ-
                                    // requested slot (gated by `earlier`), so {ASW5205} is
                                    // never violated; the sink holds DRQ high (standing
                                    // permission, below) so a deferred sample still lands
                                    // in a requested slot. Delivered rate = production rate
                                    // (bounded FIFO), so audio is fully delivered.
                                    srcAccum[p] += 1;
                                    if (srcAccum[p] >= 2) { srcAccum[p] -= 2; ++srcPending[p]; }
                                    bool send = srcPending[p] > 0 && earlier &&
                                                (srcPending[p] >= 2 || lcgSrc[p].coin());
                                    curTxp[p] = send ? 1 : 0;
                                    if (send) --srcPending[p];
                                }
                            }
                            if (curTxp[p]) ++audioIndex[p];
                        }

                        if (e.slot == SwI3sSlot::TxPresent) {
                            level = curTxp[p] != 0;       // data-valid flag on the bus
                        } else {                          // Data
                            int ch = e.channel, b = e.bitInChannel;
                            if (curTxp[p]) {
                                U64 sIdx = audioIndex[p] - 1;   // this interval's sample ordinal
                                if (b == kSS)             // MSB: generate the held sample
                                    sampleVal[p][ch] = sineAt(
                                        flowDemoFreq(dps[p]->deviceNum, ch),
                                        48000.0, sIdx, kSS);
                                level = ((sampleVal[p][ch] >> b) & 1) != 0;
                            } else {
                                level = false;            // unused opportunity: don't-care
                            }
                        }
                        break;                            // one owner per cell (no clash)
                    }

                    // DRQ emission (RX / ASYNC): the manager drives the DRQ bit onto the
                    // bus at the FCP cell, pushed to history so the transport decision can
                    // look back d intervals. FCP_HorizontalStart (col 11) is disjoint from
                    // the data window (cols 2..9 at the DRQ row), so no cell is double-driven.
                    // RX (mode 2): the sink requests at ~half the opportunity rate (a 48 k
                    // request cadence the source mirrors bijectively). ASYNC (mode 3): DRQ =
                    // SinkReady is held HIGH as standing permission (lightly jittered to
                    // exercise the sink-not-ready path); the source's own SourceReady gate
                    // shapes delivery, so a held request lets a delayed response still land
                    // in a requested slot.
                    for (int p = 0; p < P; ++p) {
                        if (!dps[p]->drqEnabled() || fes[p].slot != SwI3sSlot::Drq)
                            continue;
                        if (fes[p].drqSamplePoint) {
                            if (dps[p]->FlowMode == kFlowAsync)
                                curDrq[p] = ((lcgDrq[p].next() & 0x7) != 0) ? 1 : 0;  // ~7/8 high
                            else
                                curDrq[p] = (char)lcgDrq[p].coin();                   // RX: ~48 k requests
                            drqHist[p].push_back(curDrq[p]);
                            const bool req = curDrq[p] != 0;
                            drqWire[p] = (dps[p]->FCP_ScramblerEn ? fcpScram[p].Scramble(req)
                                                                  : req) ? 1 : 0;
                        }
                        level = drqWire[p] != 0;
                        break;
                    }
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// See Demo.h. Phase k starts at eventRow[k]: an SSCR's SSP (the port re-anchored there), a
// DSCR's commit row, or the row after a single-ranked write's last CDS bit (both keep the
// port's interval phase). A channel's scrambler is seeded at the phase that enables the
// channel and at the phase that turns ScramblerEn on.
std::vector<bool> MakeChannelReseedLevels(int samplesPerPhase)
{
    requireDemoLength(samplesPerPhase, "make_channel_reseed_levels", "samples_per_phase");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 63, cols = 8;
    if (samplesPerPhase < 16) samplesPerPhase = 16;
    enum Event { kSscr, kDscr, kWrite };
    const int kPhases = 7;
    const Event event[kPhases] = {kSscr, kSscr, kSscr, kDscr, kDscr, kWrite, kWrite};
    const std::uint32_t enable[kPhases] = {0x3, 0x1, 0x3, 0x1, 0x3, 0x3, 0x3};
    const PortGeom g = {0, kSS, 1, 1, kInt, 0, 0};          // Columns 1 and 2
    const U32 enCh1 = swi3s::reg::kDpBase + swi3s::reg::kDpEnableCh1_7_N;
    const U32 portControl = swi3s::reg::kDpBase + swi3s::reg::kDpScramDirMode;
    const long phaseRows = (long)samplesPerPhase * (kInt + 1);

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    emitPortWrites(bits, regs, dev, g);
    emitWrite(bits, regs, dev, enCh1, {0x02});
    appendPing(bits);
    long eventRow[kPhases];
    eventRow[0] = (long)appendSscr(bits, dev, group, rowDelay) + 1 + rowDelay;

    SwI3sDpConfig dp[kPhases];
    int skipDen = 1;
    auto settle = [&](int k) {
        if (event[k] != kWrite) regs.Commit(group, dev);
        SwI3sConfig c; regs.BuildConfig(c);
        if (c.SkippingDenominator > 1) skipDen = c.SkippingDenominator;
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0) { dp[k] = d; return true; }
        return false;
    };
    if (!settle(0)) return {};
    for (int k = 1; k < kPhases; ++k) {
        std::vector<bool> blk;
        if (event[k] == kWrite) {
            // 0x0B: ScramblerEn 0, then 1 (PortDirection and PortMode stay 0).
            emitWrite(blk, regs, dev, portControl, {(U8)(k == 5 ? 0x00 : 0x08)});
        } else {
            emitWrite(blk, regs, dev, enCh1, {(U8)((enable[k] & 0x2) ? 0x02 : 0x00)});
            appendPing(blk);
            appendSscr(blk, dev, group, rowDelay,
                       event[k] == kDscr ? swi3s::kOpDscr : swi3s::kOpSscr);
        }
        eventRow[k] = eventRow[0] + k * phaseRows;
        const long lead = (event[k] == kWrite) ? 0 : (long)rowDelay;
        placeEventBlock(bits, eventRow[k] - lead - (long)blk.size(), blk,
                        "make_channel_reseed_levels", "samples_per_phase", samplesPerPhase);
        if (!settle(k)) return {};
    }
    const long totalRows = eventRow[kPhases - 1] + phaseRows;
    fillIdleWithPings(bits, totalRows);

    CDataPort port;
    CDescrambler scram[2];
    U64 curSample[2] = {0, 0}, sampleIndex[2] = {0, 0};
    int columnCount = swi3s::kColdStartColumnCount, phase = -1;
    std::vector<bool> levels;
    bool cur = false;
    for (long r = 0; r < totalRows; ++r) {
        if (phase + 1 < kPhases && r == eventRow[phase + 1]) {
            ++phase;
            if (event[phase] == kSscr) {
                columnCount = cols;
                port.Configure(dp[phase], cols, skipDen);
                port.Initialize();
                port.SyncToSSP();
            } else {
                port.ApplyConfigKeepingPhase(dp[phase], cols, skipDen);
            }
            const std::uint32_t before = (phase > 0) ? enable[phase - 1] : 0;
            const bool scramblerOn = dp[phase].ScramblerEn &&
                                     !(phase > 0 && dp[phase - 1].ScramblerEn);
            for (int ch = 0; ch < 2; ++ch)
                if ((enable[phase] & ~before & (1u << ch)) || scramblerOn)
                    scram[ch] = CDescrambler();
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit e;
            if (phase >= 0) e = port.clock_tick();
            bool level = cur;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else if (phase >= 0 && e.sampleHere && e.slot == SwI3sSlot::Data &&
                       e.channel >= 0 && e.channel < 2) {
                const int ch = e.channel, b = e.bitInChannel;
                if (b == kSS) curSample[ch] = sineSample(ch, sampleIndex[ch]++, kSS);
                const bool clear = ((curSample[ch] >> b) & 1) != 0;
                level = dp[phase].ScramblerEn ? scram[ch].Scramble(clear) : clear;
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// See Demo.h. Phase k starts at eventRow[k]: an SSCR's SSP, a DSCR's commit row, or the row
// after a single-ranked write's last CDS bit. The generator applies each event to its data
// port and FCP at that row as the decode does, and sends each DRQ raw while
// FCP_ScramblerEn is 0.
std::vector<bool> MakeFcpReseedLevels(int intervalsPerPhase, const std::string& generatorSeed)
{
    requireDemoLength(intervalsPerPhase, "make_fcp_reseed_levels", "intervals_per_phase");
    enum Seed { kAtEnable, kAlsoSscr, kAlsoDscr, kEveryDrq, kFcpEnableOnly, kFirstOnly };
    Seed rule;
    if (generatorSeed == "enable")        rule = kAtEnable;
    else if (generatorSeed == "sscr")     rule = kAlsoSscr;
    else if (generatorSeed == "dscr")     rule = kAlsoDscr;
    else if (generatorSeed == "interval") rule = kEveryDrq;
    else if (generatorSeed == "resume")   rule = kFcpEnableOnly;
    else if (generatorSeed == "continue") rule = kFirstOnly;
    else throw std::invalid_argument("generator_seed must be enable, sscr, dscr, interval, "
                                     "resume or continue, not '" + generatorSeed + "'");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 15, cols = 16;
    const int requestedIntervals = intervalsPerPhase;
    if (intervalsPerPhase < 64) intervalsPerPhase = 64;
    const long phaseRows = (long)intervalsPerPhase * (kInt + 1);
    // Data in Rows 2-4 of each interval from Column 2, the DRQ at Column 11, Row 12.
    const FcGeom g = {0, kSS, kInt, 2, 0x0001, kFlowRxControlled, false, 11, 12};
    const U32 base = swi3s::reg::kDpBase;

    enum Event { kSscr, kDscr, kWrite };
    const int kPhases = 7;
    const Event event[kPhases] = {kSscr, kSscr, kDscr, kWrite, kWrite, kWrite, kWrite};
    // 0x0E for phases 3-6: TX_CONTROLLED and RX_CONTROLLED with FCP_ScramblerEn 1, then
    // RX_CONTROLLED with it 0 and with it 1.
    const U8 flowControl[kPhases] = {0, 0, 0, (U8)(kFlowTxControlled | 0x08),
                                     (U8)(kFlowRxControlled | 0x08), (U8)kFlowRxControlled,
                                     (U8)(kFlowRxControlled | 0x08)};

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x0F});
    emitFcPortWrites(bits, regs, dev, g, /*fcpScrambled=*/true);
    appendPing(bits);
    long eventRow[kPhases];
    eventRow[0] = (long)appendSscr(bits, dev, group, rowDelay) + 1 + rowDelay;

    SwI3sDpConfig dp[kPhases];
    auto settle = [&](int k) {
        if (event[k] != kWrite) regs.Commit(group, dev);
        SwI3sConfig c; regs.BuildConfig(c);
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0) { dp[k] = d; return true; }
        return false;
    };
    if (!settle(0)) return {};
    for (int k = 1; k < kPhases; ++k) {
        std::vector<bool> blk;
        if (event[k] == kWrite) {
            emitWrite(blk, regs, dev, base + swi3s::reg::kDpFlowMode, {flowControl[k]});
        } else {
            emitWrite(blk, regs, dev, base + swi3s::reg::kDpEnableCh1_7_N,
                      {(U8)(k == 1 ? 0x02 : 0x00)});
            appendPing(blk);
            appendSscr(blk, dev, group, rowDelay,
                       event[k] == kDscr ? swi3s::kOpDscr : swi3s::kOpSscr);
        }
        eventRow[k] = eventRow[0] + k * phaseRows;
        const long lead = (event[k] == kWrite) ? 0 : (long)rowDelay;
        placeEventBlock(bits, eventRow[k] - lead - (long)blk.size(), blk,
                        "make_fcp_reseed_levels", "intervals_per_phase", requestedIntervals);
        if (!settle(k)) return {};
    }
    const long totalRows = eventRow[kPhases - 1] + phaseRows;
    fillIdleWithPings(bits, totalRows);

    CDataPort port;
    CFlowControlPort fcp;
    CDescrambler drqScram;
    bool fcpOn = false;
    std::deque<char> drqHist;            // this FCP's DRQ requests, oldest..newest
    Lcg lcg(0xD00D0000ull);
    char txp = 0, drqWire = 0;
    U64 audioIndex = 0;
    U64 sampleVal[16] = {};
    int columnCount = swi3s::kColdStartColumnCount, phase = -1;
    std::vector<bool> levels;
    bool cur = false;
    for (long r = 0; r < totalRows; ++r) {
        if (phase + 1 < kPhases && r == eventRow[phase + 1]) {
            ++phase;
            const SwI3sDpConfig& d = dp[phase];
            const bool hasFcp = d.drqEnabled();
            if (event[phase] == kSscr) {
                columnCount = cols;
                port.Configure(d, cols, 1);
                port.Initialize();
                port.SyncToSSP();
            } else {
                port.ApplyConfigKeepingPhase(d, cols, 1);
            }
            if (hasFcp) {
                fcp.Configure(d, cols);
                if (event[phase] == kSscr || !fcpOn)
                    fcp.Initialize(port.Column(), port.RowInInterval());
            }
            if (hasFcp != fcpOn) drqHist.clear();
            const bool scramblerOn = d.FCP_ScramblerEn &&
                                     !(phase > 0 && dp[phase - 1].FCP_ScramblerEn);
            bool seed = hasFcp && (!fcpOn || (scramblerOn && rule != kFcpEnableOnly)) &&
                        !(rule == kFirstOnly && phase > 0);
            if ((rule == kAlsoSscr && phase == 1) || (rule == kAlsoDscr && phase == 2))
                seed = true;
            if (seed) drqScram = CDescrambler();
            fcpOn = hasFcp;
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit e;
            FcpEmit fe;
            if (phase >= 0) {
                e = port.clock_tick();
                if (fcpOn) fe = fcp.clock_tick(port.IntervalSkipped());
            }
            bool level = cur;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else if (phase >= 0 && e.sampleHere &&
                       (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                if (e.freshTransport) {
                    // RX_CONTROLLED: TX_PRESENT is the DRQ d intervals earlier (1 before
                    // the FCP has sent d since its enable; an SSCR or DSCR carries the
                    // history). TX_CONTROLLED: every opportunity carries.
                    const int dly = dp[phase].FlowControlDelay + 1;
                    if (dp[phase].FlowMode == kFlowRxControlled)
                        txp = ((int)drqHist.size() >= dly) ? drqHist[drqHist.size() - dly] : 1;
                    else
                        txp = 1;
                    if (txp) ++audioIndex;
                }
                if (e.slot == SwI3sSlot::TxPresent) {
                    level = txp != 0;
                } else if (txp && e.channel >= 0 && e.channel < 16) {
                    const int ch = e.channel, b = e.bitInChannel;
                    if (b == kSS) sampleVal[ch] = sineSample(ch, audioIndex - 1, kSS);
                    level = ((sampleVal[ch] >> b) & 1) != 0;
                } else {
                    level = false;
                }
            } else if (fcpOn && fe.slot == SwI3sSlot::Drq) {
                if (fe.drqSamplePoint) {
                    const char req = (char)lcg.coin();
                    drqHist.push_back(req);
                    while (drqHist.size() > 8) drqHist.pop_front();
                    if (rule == kEveryDrq) drqScram = CDescrambler();
                    const bool wire = dp[phase].FCP_ScramblerEn ? drqScram.Scramble(req != 0)
                                                                : (req != 0);
                    drqWire = wire ? 1 : 0;
                }
                level = drqWire != 0;
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// See Demo.h. Phase k starts at commit k's row (an SSCR's SSP or a DSCR's commit row, an
// interval boundary unless commitRowOffset moves the second); the generator applies the new
// FCP placement and column count to its port and FCP there as the decode does. With
// `reenable` the port is off through phase 1 and enabled again at phase 2.
std::vector<bool> MakeFcpMoveLevels(int intervalsPerPhase, bool dscr, int fcpHStart,
                                    int fcpOffset, int flowControlDelay, bool scrambled,
                                    int fcpBitWidth, bool reenable, int commitRowOffset,
                                    int columnsAfter)
{
    requireDemoLength(intervalsPerPhase, "make_fcp_move_levels", "intervals_per_phase");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 15, cols = 16;
    if (fcpHStart < 1 || fcpHStart > cols - 1 || fcpOffset < 0 || fcpOffset > kInt ||
        (fcpOffset >= 2 && fcpOffset <= 4 && fcpHStart <= 14 &&
         (long long)fcpHStart + fcpBitWidth >= 2))
        throw std::invalid_argument("make_fcp_move_levels: the DRQ must lie in Columns "
                                    "1..15 and Rows 0..15 of the interval, off Columns "
                                    "2..14 of Rows 2..4");
    if (flowControlDelay != 0 && flowControlDelay != 1)
        throw std::invalid_argument("make_fcp_move_levels: flow_control_delay must be 0 or 1");
    if (fcpBitWidth < 0 || fcpBitWidth > 3 || fcpHStart + fcpBitWidth > cols - 1)
        throw std::invalid_argument("make_fcp_move_levels: fcp_bit_width must be 0..3 and "
                                    "keep the DRQ in Columns 1..15");
    if (commitRowOffset < 0 || commitRowOffset > kInt ||
        (commitRowOffset != 0 && (dscr || reenable)))
        throw std::invalid_argument("make_fcp_move_levels: commit_row_offset must be 0..15, "
                                    "and 0 with dscr or reenable");
    if ((columnsAfter != cols && columnsAfter != 2 * cols) ||
        (columnsAfter != cols && reenable))
        throw std::invalid_argument("make_fcp_move_levels: columns_after must be 16 or 32, "
                                    "and 16 with reenable");
    const long phaseRows = (long)intervalsPerPhase * (kInt + 1);
    // Data in Rows 2-4 of each interval from Column 2, the DRQ at Column 11, Row 12.
    const FcGeom g = {0, kSS, kInt, 2, 0x0001, kFlowRxControlled, false, 11, 12};
    const U32 base = swi3s::reg::kDpBase;

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x0F});
    emitFcPortWrites(bits, regs, dev, g, /*fcpScrambled=*/false);
    if (flowControlDelay)
        emitWrite(bits, regs, dev, base + swi3s::reg::kDpFlowMode,
                  {(U8)(kFlowRxControlled | 0x80)});
    if (scrambled) emitWrite(bits, regs, dev, base + swi3s::reg::kDpScramDirMode, {0x08});
    appendPing(bits);
    const int kCommits = reenable ? 3 : 2;
    long eventRow[3];
    eventRow[0] = (long)appendSscr(bits, dev, group, rowDelay) + 1 + rowDelay;

    SwI3sDpConfig dp[3];
    bool enabled[3] = {true, !reenable, true};
    auto settle = [&](int k) {
        regs.Commit(group, dev);
        SwI3sConfig c; regs.BuildConfig(c);
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0) { dp[k] = d; return true; }
        return false;
    };
    if (!settle(0)) return {};
    // 0x81: EnableCh0 [7] | HorizontalCount 12 (Columns 2..14), as emitFcPortWrites sets it.
    const U32 enableCh0 = base + swi3s::reg::kDpEnableCh0HCount_N;
    for (int k = 1; k < kCommits; ++k) {
        std::vector<bool> blk;
        if (reenable && k == 1) {
            emitWrite(blk, regs, dev, enableCh0, {(U8)0x0C});
        } else {
            emitWrite(blk, regs, dev, base + swi3s::reg::kDpFcpBwHStart_N,
                      {(U8)((fcpBitWidth << 6) | (fcpHStart & 0x1F))});
            emitWrite(blk, regs, dev, base + swi3s::reg::kDpFcpOffLo_N,
                      {(U8)((fcpOffset & 0xF) << 4)});
            emitWrite(blk, regs, dev, base + swi3s::reg::kDpFcpOffHi_N,
                      {(U8)((fcpOffset >> 4) & 0xFF)});
            if (reenable) emitWrite(blk, regs, dev, enableCh0, {(U8)0x8C});
            if (columnsAfter != cols)
                emitWrite(blk, regs, dev, swi3s::reg::kNumColumns_Next,
                          {(U8)(columnsAfter - 1)});
        }
        appendPing(blk);
        appendSscr(blk, dev, group, rowDelay, dscr ? swi3s::kOpDscr : swi3s::kOpSscr);
        eventRow[k] = eventRow[k - 1] + phaseRows + (k == 1 ? commitRowOffset : 0);
        placeEventBlock(bits, eventRow[k] - (long)rowDelay - (long)blk.size(), blk,
                        "make_fcp_move_levels", "intervals_per_phase", intervalsPerPhase);
        if (settle(k) != enabled[k]) return {};
    }
    const long totalRows = eventRow[kCommits - 1] + phaseRows;
    fillIdleWithPings(bits, totalRows);

    CDataPort port;
    CFlowControlPort fcp;
    CDescrambler scram;                  // channel 0's, seeded at its enable, continues after
    std::vector<char> drqs;              // the FCP's DRQ requests since the port's enable
    size_t txps = 0;                     // fresh transports since the port's enable
    Lcg lcg(0xD00D0000ull);
    char txp = 0, drqWire = 0;
    U64 audioIndex = 0;
    U64 sampleVal = 0;
    int columnCount = swi3s::kColdStartColumnCount, phase = -1;
    std::vector<bool> levels;
    bool cur = false;
    bool on = false;                     // the port is enabled
    for (long r = 0; r < totalRows; ++r) {
        if (phase + 1 < kCommits && r == eventRow[phase + 1]) {
            ++phase;
            const SwI3sDpConfig& d = dp[phase];
            const bool colsChanged = phase > 0 && columnsAfter != columnCount;
            columnCount = (phase == 0) ? cols : columnsAfter;
            if (!enabled[phase]) {
                on = false;
            } else if (!on) {
                // The port's enable: a new interval phase, sample index, scrambler and
                // DRQ history. A DSCR enables it on Column 0 of its commit row, as an SSCR's
                // SSP does.
                port.Configure(d, columnCount, 1);
                port.Initialize();
                if (!dscr || phase == 0) port.SyncToSSP();
                fcp.Configure(d, columnCount);
                fcp.Initialize(port.Column(), port.RowInInterval());
                drqs.clear();
                txps = 0;
                audioIndex = 0;
                scram = CDescrambler();
                on = true;
            } else if (!dscr || colsChanged) {
                // A rebuild: an SSCR re-anchors the port at its SSP, a DSCR that changes
                // the column count starts its interval on Column 0 of its commit row. The
                // DRQ history carries only from an interval boundary.
                port.Configure(d, columnCount, 1);
                port.Initialize();
                if (!dscr) port.SyncToSSP();
                fcp.Configure(d, columnCount);
                fcp.Initialize(port.Column(), port.RowInInterval());
                if (commitRowOffset != 0) {
                    drqs.clear();
                    txps = 0;
                }
            } else {
                port.ApplyConfigKeepingPhase(d, cols, 1);
                fcp.Configure(d, cols);
            }
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit e;
            FcpEmit fe;
            if (on) {
                e = port.clock_tick();
                fe = fcp.clock_tick(port.IntervalSkipped());
            }
            bool level = cur;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else if (on && e.sampleHere &&
                       (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                if (e.freshTransport) {
                    // Interval T since the enable carries DRQ number T - d, 1 for T < d.
                    const size_t dly = (size_t)dp[phase].FlowControlDelay + 1;
                    txp = (txps >= dly) ? drqs[txps - dly] : 1;
                    ++txps;
                    if (txp) ++audioIndex;
                }
                if (e.slot == SwI3sSlot::TxPresent) {
                    level = txp != 0;
                } else if (txp && e.channel == 0) {
                    const int b = e.bitInChannel;
                    if (b == kSS) sampleVal = sineSample(0, audioIndex - 1, kSS);
                    level = ((sampleVal >> b) & 1) != 0;
                } else {
                    level = false;
                }
                // TX_PRESENT and every data bit, used or not, step the channel's LFSR.
                if (scrambled) level = scram.Scramble(level);
            } else if (on && fe.slot == SwI3sSlot::Drq) {
                if (fe.drqSamplePoint) {
                    const char req = (char)lcg.coin();
                    drqs.push_back(req);
                    drqWire = req;
                }
                level = drqWire != 0;
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// See Demo.h. The FCP of a SINK data port drives a SOURCE DRQ, which CFlowControlPort follows
// with its guard and tail; phase 1 starts at the second commit's row (an interval boundary).
std::vector<bool> MakeFcpGuardTailLevels(int intervalsPerPhase, bool dscr, int fcpHStart,
                                         int fcpOffset, int fcpBitWidth, int fcpTailWidth,
                                         bool fcpGuardEnable, bool fcpGuardPolarity, bool async,
                                         bool firstGuardEnable, bool firstGuardPolarity)
{
    requireDemoLength(intervalsPerPhase, "make_fcp_guard_tail_levels", "intervals_per_phase");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 15, cols = 16;
    if (fcpBitWidth < 0 || fcpBitWidth > 3 || fcpTailWidth < 0 || fcpTailWidth > 3)
        throw std::invalid_argument("make_fcp_guard_tail_levels: fcp_bit_width and "
                                    "fcp_tail_width must be 0..3");
    // The FCP's cells, DRQ to last tail UI, in one row, summed in long long so that no
    // fcpHStart wraps the sum.
    const long long last = (long long)fcpHStart + fcpBitWidth + (fcpGuardEnable ? 1 : 0) +
                           fcpTailWidth;
    if (fcpHStart < 1 || last > cols - 1 || fcpOffset < 0 || fcpOffset > kInt ||
        (fcpOffset >= 2 && fcpOffset <= 4 && fcpHStart <= 14 && last >= 2))
        throw std::invalid_argument("make_fcp_guard_tail_levels: the DRQ, guard and tail "
                                    "must lie in Columns 1..15 and Rows 0..15 of the "
                                    "interval, off Columns 2..14 of Rows 2..4");
    const long phaseRows = (long)intervalsPerPhase * (kInt + 1);
    const int flowMode = async ? kFlowAsync : kFlowRxControlled;
    // A sink port: data in Rows 2-4 of each interval from Column 2, the DRQ at Column 11,
    // Row 12, no tail, its guard (Column 12) as firstGuardEnable / firstGuardPolarity say.
    const FcGeom g = {0, kSS, kInt, 2, 0x0001, flowMode, true, 11, 12};
    const U32 base = swi3s::reg::kDpBase;

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x0F});
    emitFcPortWrites(bits, regs, dev, g, /*fcpScrambled=*/false);
    // FCP_GuardEnable [1] | FCP_GuardPolarity [0]; the reset value 0 is not written.
    if (firstGuardEnable || firstGuardPolarity)
        emitWrite(bits, regs, dev, base + swi3s::reg::kDpFcpGuard_N,
                  {(U8)(((firstGuardEnable ? 1 : 0) << 1) | (firstGuardPolarity ? 1 : 0))});
    appendPing(bits);
    long eventRow[2];
    eventRow[0] = (long)appendSscr(bits, dev, group, rowDelay) + 1 + rowDelay;

    SwI3sDpConfig dp[2];
    auto settle = [&](int k) {
        regs.Commit(group, dev);
        SwI3sConfig c; regs.BuildConfig(c);
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0) { dp[k] = d; return true; }
        return false;
    };
    if (!settle(0)) return {};
    // The second commit: FCP placement, FCP_TailWidth and the FCP guard.
    std::vector<bool> blk;
    emitWrite(blk, regs, dev, base + swi3s::reg::kDpFcpBwHStart_N,
              {(U8)((fcpBitWidth << 6) | (fcpHStart & 0x1F))});
    emitWrite(blk, regs, dev, base + swi3s::reg::kDpFcpTail_N, {(U8)(fcpTailWidth << 6)});
    emitWrite(blk, regs, dev, base + swi3s::reg::kDpFcpOffLo_N, {(U8)((fcpOffset & 0xF) << 4)});
    emitWrite(blk, regs, dev, base + swi3s::reg::kDpFcpOffHi_N, {(U8)((fcpOffset >> 4) & 0xFF)});
    emitWrite(blk, regs, dev, base + swi3s::reg::kDpFcpGuard_N,
              {(U8)(((fcpGuardEnable ? 1 : 0) << 1) | (fcpGuardPolarity ? 1 : 0))});
    appendPing(blk);
    appendSscr(blk, dev, group, rowDelay, dscr ? swi3s::kOpDscr : swi3s::kOpSscr);
    eventRow[1] = eventRow[0] + phaseRows;
    placeEventBlock(bits, eventRow[1] - (long)rowDelay - (long)blk.size(), blk,
                    "make_fcp_guard_tail_levels", "intervals_per_phase", intervalsPerPhase);
    if (!settle(1)) return {};
    const long totalRows = eventRow[1] + phaseRows;
    fillIdleWithPings(bits, totalRows);

    CDataPort port;
    CFlowControlPort fcp;
    std::vector<char> drqs;              // the FCP's DRQ requests since the port's enable
    size_t txps = 0;                     // fresh transports since the port's enable
    Lcg lcg(0xD00D0000ull);
    char txp = 0, drqWire = 0;
    U64 audioIndex = 0;
    U64 sampleVal = 0;
    int columnCount = swi3s::kColdStartColumnCount, phase = -1;
    std::vector<bool> levels;
    bool cur = false;
    for (long r = 0; r < totalRows; ++r) {
        if (phase + 1 < 2 && r == eventRow[phase + 1]) {
            ++phase;
            const SwI3sDpConfig& d = dp[phase];
            if (phase == 0 || !dscr) {
                columnCount = cols;
                port.Configure(d, cols, 1);
                port.Initialize();
                port.SyncToSSP();
                fcp.Configure(d, cols);
                fcp.Initialize(port.Column(), port.RowInInterval());
            } else {
                port.ApplyConfigKeepingPhase(d, cols, 1);
                fcp.Configure(d, cols);
            }
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit e;
            FcpEmit fe;
            if (phase >= 0) {
                e = port.clock_tick();
                fe = fcp.clock_tick(port.IntervalSkipped());
            }
            bool level = cur;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else if (phase >= 0 && e.sampleHere &&
                       (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                if (e.freshTransport) {
                    // Interval T since the port's enable carries DRQ number T - d as its
                    // TX_PRESENT, 1 for T < d: the RX_CONTROLLED rule, which an ASYNC port
                    // also passes.
                    const size_t dly = (size_t)dp[phase].FlowControlDelay + 1;
                    txp = (txps >= dly) ? drqs[txps - dly] : 1;
                    ++txps;
                    if (txp) ++audioIndex;
                }
                if (e.slot == SwI3sSlot::TxPresent) {
                    level = txp != 0;
                } else if (txp && e.channel == 0) {
                    const int b = e.bitInChannel;
                    if (b == kSS) sampleVal = sineSample(0, audioIndex - 1, kSS);
                    level = ((sampleVal >> b) & 1) != 0;
                } else {
                    level = false;
                }
            } else if (phase >= 0 && fe.slot == SwI3sSlot::Drq) {
                // A source DRQ is driven on its first UI and held for the rest of its bit.
                if (fe.drqSamplePoint) {
                    const char req = (char)lcg.coin();
                    drqs.push_back(req);
                    drqWire = req;
                }
                level = drqWire != 0;
            } else if (phase >= 0 && (fe.slot == SwI3sSlot::Guard0 ||
                                      fe.slot == SwI3sSlot::Guard1)) {
                level = fe.slot == SwI3sSlot::Guard1;   // the guard drives its polarity
            }                                           // a Tail UI holds the bus level
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

std::vector<bool> MakeDemoLevelsPhy1(int audioSamplesPerChannel)
{
    requireDemoLength(audioSamplesPerChannel, "make_demo_levels_phy1", "audio_samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kPcmSS = 15;   // 16-bit PCM
    const int kPdmSS = 0;    // 1-bit PDM
    // Constant 4-column geometry at 1.536 MRows/s:
    //   PCM Interval 31 -> 32 rows/sample -> 48 kHz; PDM SampleGrouping 2 + Interval 0 ->
    //   2 samples/row -> 3.072 MHz. The row rate never changes (same column count), so these
    //   registers are identical in both configs — only the placement (HorizontalStart) moves.
    const int kPcmInt = 31;
    const int kPdmSG = 1, kPdmInt = 0;

    // Two placements over the same 4 columns (CDS at Column 0). Fields: {dp, sampleSize,
    // hstart, hcount, interval, sampleGroup, spacing, offset}. config_1: PCM pair share
    // col 1 (DP0 Offset 0, DP1 Offset 16), PDM in cols 2-3. config_2: the PCM pair moves to
    // col 3, the PDM to cols 1-2.
    const PortGeom cfgA[3] = {
        {0, kPcmSS, 1, 0, kPcmInt, 0, 0, 0},   {1, kPcmSS, 1, 0, kPcmInt, 0, 0, 16},
        {2, kPdmSS, 2, 1, kPdmInt, kPdmSG, 1, 0},
    };
    const PortGeom cfgB[3] = {
        {0, kPcmSS, 3, 0, kPcmInt, 0, 0, 0},   {1, kPcmSS, 3, 0, kPcmInt, 0, 0, 16},
        {2, kPdmSS, 1, 1, kPdmInt, kPdmSG, 1, 0},
    };

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);   // idle preamble
    appendPing(bits);

    // ---- commit #1: safe-lock-2 -> 4 columns, config_1 placement (audio starts) ----
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x03});   // 4 columns
    for (const PortGeom& p : cfgA) emitPortWrites(bits, regs, dev, p);
    // DP0 carries 44.1 kHz on 48 kHz transport opportunities (see emitSkipping). Here DP0 and
    // DP1 SHARE a column, time-interleaved at Offset 0 and Offset 16, so a skipped DP0 interval
    // leaves the first 16 rows of that column idle while DP1 keeps transporting in the second
    // half — the two ports' patterns are independent even though their slots are adjacent.
    emitSkipping(bits, regs, dev, /*dp=*/0);
    // DP0 (PCM) and DP2 (PDM) run UNSCRAMBLED; DP1 keeps the reset default (scrambling ON).
    for (int dp : {0, 2})
        emitWrite(bits, regs, dev,
                  swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)dp
                      + swi3s::reg::kDpScramDirMode, {0x00});
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group, dev);
    SwI3sConfig cfg1; regs.BuildConfig(cfg1);
    cfg1.NumColumns = 3;
    if (cfg1.SkippingDenominator < 1) cfg1.SkippingDenominator = 1;
    long ssp1 = ssp1Bit + 1 + (int)rowDelay;

    // ---- commit #2: reposition the ports (config_2), SAME 4 columns ----
    std::vector<bool> blk;
    for (const PortGeom& p : cfgB) emitPortWrites(blk, regs, dev, p);
    appendPing(blk);
    appendSscr(blk, dev, group, rowDelay);                        // confirm bit = blk.back()

    // config_1 audio phase: half the audio, but at least enough interval-aligned rows to
    // hold the config_2 control block plus a spare interval.
    long minRows1 = (long)blk.size() + (int)rowDelay + (kPcmInt + 1);
    long minSamples1 = (minRows1 + kPcmInt) / (kPcmInt + 1);      // ceil to whole intervals
    long samples1 = audioSamplesPerChannel / 2;
    if (samples1 < minSamples1) samples1 = minSamples1;
    long rows1 = samples1 * (kPcmInt + 1);
    long ssp2 = ssp1 + rows1;

    long blkStart = ssp2 - (int)rowDelay - (long)blk.size();      // so confirm row = ssp2-rowDelay-1
    // config_1 audio: idle CDS + keep-alive Pings + periodic SSPAs (4-column geometry).
    SspaSchedule sspaA = makeSspaSchedule(cfg1, ssp1, blkStart, 4, dev, group, rowDelay);
    fillIdleWithPings(bits, blkStart, &sspaA);
    bits.insert(bits.end(), blk.begin(), blk.end());
    regs.Commit(group, dev);
    SwI3sConfig cfg2; regs.BuildConfig(cfg2);
    cfg2.NumColumns = 3;
    if (cfg2.SkippingDenominator < 1) cfg2.SkippingDenominator = 1;

    long samples2 = (long)audioSamplesPerChannel - samples1;
    if (samples2 < 1) samples2 = 1;
    long rows2 = samples2 * (kPcmInt + 1);
    long totalRows = ssp2 + rows2;
    // config_2 audio: the ports were REPOSITIONED (placement-only), so this region has
    // its own SSP anchor even though the column count is unchanged.
    SspaSchedule sspaB = makeSspaSchedule(cfg2, ssp2, totalRows, 4, dev, group, rowDelay);
    fillIdleWithPings(bits, totalRows, &sspaB);

    auto enabledDps = [](const SwI3sConfig& c) {
        std::vector<const SwI3sDpConfig*> v;
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0) v.push_back(&d);
        return v;
    };
    std::vector<const SwI3sDpConfig*> dps1 = enabledDps(cfg1), dps2 = enabledDps(cfg2);

    auto tone = [](int p, int ch) { return p * 2 + ch; };
    auto key = [](int p, int ch) { return p * 32 + ch; };

    std::map<int, CDescrambler> scram;
    std::map<int, U64> curSample, sampleIndex;
    std::map<int, double> sdAcc;                        // PDM sigma-delta integrator

    int columnCount = 2;                                // safe-lock-2 to start
    const SwI3sDpConfig* const* dps = nullptr;
    int N = 0;
    std::vector<CDataPort> ports;
    bool portReady = false;

    // Each region's ports take that region's own config (its SkippingDenominator). A
    // surviving scrambled channel keeps its LFSR across the SSCR (carryScramblers).
    std::vector<const SwI3sDpConfig*> live;            // the ports before the reconfigure
    auto reconfigure = [&](const SwI3sConfig& cfg, const std::vector<const SwI3sDpConfig*>& dd,
                           int cc) {
        columnCount = cc;
        N = (int)dd.size();
        ports.assign(N, CDataPort());
        scram = carryScramblers(scram, live, dd.data(), N);
        live = dd;
        for (int p = 0; p < N; ++p) {
            ports[p].Configure(*dd[p], cc, cfg.SkippingDenominator);
            ports[p].Initialize();
            ports[p].SyncToSSP();
        }
        portReady = true;
    };

    std::vector<bool> levels;
    levels.reserve((size_t)(totalRows * 4));
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        // config_1 placement comes up at ssp1; config_2 repositions the ports at ssp2 (SAME 4
        // columns — only their columns move). The audio index is kept across the reconfigure
        // so the tones continue uninterrupted.
        if (r == ssp1) { dps = dps1.data(); reconfigure(cfg1, dps1, 4); }
        else if (r == ssp2) { dps = dps2.data(); reconfigure(cfg2, dps2, 4); }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[64];
            if (portReady)
                for (int p = 0; p < N; ++p) es[p] = ports[p].clock_tick();

            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                level = cur;
                if (portReady) {
                    for (int p = 0; p < N; ++p) {
                        const DpEmit& e = es[p];
                        if (e.sampleHere &&
                            (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                            int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                            bool clear;
                            if (e.slot == SwI3sSlot::Data) {
                                int ss = dps[p]->SampleSize;
                                if (b == ss) {
                                    if (ss == 0) {          // PDM: 1st-order sigma-delta, dp2 = 250 Hz
                                        double freqHz = 250.0 + 500.0 *
                                            (double)(dps[p]->dpNumber - 2);
                                        double t = 2.0 * kPi * freqHz
                                                   * (double)sampleIndex[k]++ / 3072000.0;
                                        sdAcc[k] += 0.45 * std::sin(t);
                                        U64 bit = (sdAcc[k] >= 0.0) ? 1 : 0;
                                        sdAcc[k] -= bit ? 1.0 : -1.0;
                                        curSample[k] = bit;
                                    } else {
                                        curSample[k] = sineSample(tone(p, ch),
                                                                  sampleIndex[k]++, ss);
                                    }
                                }
                                clear = ((curSample[k] >> b) & 1) != 0;
                            } else {
                                clear = false;
                            }
                            level = dps[p]->ScramblerEn ? scram[k].Scramble(clear) : clear;
                            break;                          // disjoint windows: one owner
                        }
                    }
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// Test fixture for the DSCR-disable decode path (see Decoder mPendingSspSyncPoint). An
// 8-column bus with two 1-channel 16-bit PCM ports (dp0 col1, dp1 col2) enabled by an
// SSCR; after `samplesPerChannel` samples a DSCR disables dp1 (clears its EnableCh0). dp1
// KEEPS transmitting its sine on the wire afterwards, so a decoder that does not drop the
// port on the DSCR keeps emitting dp1 audio. A correct decode stops dp1 at the DSCR and
// continues dp0 uninterrupted (a DSCR must not re-anchor the SSP, so dp0's phase is
// preserved). Returns bus levels for MemorySampleSource.
std::vector<bool> MakeDscrDisableLevels(int samplesPerChannel, bool disableAll)
{
    requireDemoLength(samplesPerChannel, "make_dscr_disable_levels", "samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 63, cols = 8;      // 16-bit PCM; 8-col 48 kHz (64 rows/sample)
    const PortGeom g[2] = { {0, kSS, 1, 0, kInt, 0, 0}, {1, kSS, 2, 0, kInt, 0, 0} };

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    for (const PortGeom& p : g) emitPortWrites(bits, regs, dev, p);
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);        // SSCR: enable both
    regs.Commit(group, dev);
    SwI3sConfig cfg1; regs.BuildConfig(cfg1); cfg1.NumColumns = cols - 1;
    if (cfg1.SkippingDenominator < 1) cfg1.SkippingDenominator = 1;
    long ssp1 = ssp1Bit + 1 + (int)rowDelay;

    // DSCR-disable block: clear dp1's EnableCh0/HCount (0x81 -> 0x00), then a DSCR commit
    // (kOpDscr: commits without generating an SSP).
    std::vector<bool> blk;
    U32 dp1 = swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * 1u;
    emitWrite(blk, regs, dev, dp1 + swi3s::reg::kDpBitWidthHStart_N,
              {0x02, 0x00, 0x00, (U8)(kInt & 0xF), (U8)((kInt >> 4) & 0xFF), 0x00});
    if (disableAll) {   // also disable dp0 -> the DSCR disables the LAST enabled port too
        U32 dp0 = swi3s::reg::kDpBase;
        emitWrite(blk, regs, dev, dp0 + swi3s::reg::kDpBitWidthHStart_N,
                  {0x01, 0x00, 0x00, (U8)(kInt & 0xF), (U8)((kInt >> 4) & 0xFF), 0x00});
    }
    appendPing(blk);
    appendSscr(blk, dev, group, rowDelay, swi3s::kOpDscr);

    long rows1 = (long)samplesPerChannel * (kInt + 1);
    long ssp2 = ssp1 + rows1;                                  // DSCR takes effect here
    long blkStart = ssp2 - (int)rowDelay - (long)blk.size();
    placeEventBlock(bits, blkStart, blk, "make_dscr_disable_levels", "samples_per_channel",
                    samplesPerChannel);
    regs.Commit(group, dev);                                        // disable dp1 in the model
    long totalRows = ssp2 + (long)samplesPerChannel * (kInt + 1);
    fillIdleWithPings(bits, totalRows);   // idle + keep-alive Pings to the end

    std::vector<const SwI3sDpConfig*> dps;                     // BOTH ports drive the wire
    for (const SwI3sDpConfig& d : cfg1.dps)
        if (d.Enabled && d.numChannels() > 0) dps.push_back(&d);
    int N = (int)dps.size();
    auto tone = [](int p, int ch) { return p * 2 + ch; };
    auto key  = [](int p, int ch) { return p * 32 + ch; };

    std::map<int, CDescrambler> scram;
    std::map<int, U64> curSample, sampleIndex;
    std::vector<CDataPort> ports;
    int columnCount = 2;
    bool portReady = false;
    std::vector<bool> levels;
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) {                                       // ports come up at the SSP
            columnCount = cols;
            ports.assign(N, CDataPort());
            scram.clear();
            for (int p = 0; p < N; ++p) {
                ports[p].Configure(*dps[p], cols, cfg1.SkippingDenominator);
                ports[p].Initialize();
                ports[p].SyncToSSP();
                for (int ch = 0; ch < 16; ++ch)
                    if (dps[p]->EnableCh & (1u << ch)) scram[key(p, ch)] = CDescrambler();
            }
            portReady = true;
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[8];
            if (portReady) for (int p = 0; p < N; ++p) es[p] = ports[p].clock_tick();
            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                level = cur;
                if (portReady) {
                    for (int p = 0; p < N; ++p) {
                        const DpEmit& e = es[p];
                        if (e.sampleHere &&
                            (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                            int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                            bool clear;
                            if (e.slot == SwI3sSlot::Data) {
                                int ss = dps[p]->SampleSize;
                                if (b == ss)
                                    curSample[k] = sineSample(tone(p, ch), sampleIndex[k]++, ss);
                                clear = ((curSample[k] >> b) & 1) != 0;
                            } else {
                                clear = false;
                            }
                            level = dps[p]->ScramblerEn ? scram[k].Scramble(clear) : clear;
                            break;
                        }
                    }
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// Test fixture for a DSCR that ENABLES: dp0 (col1, ch0) comes up at the SSCR; halfway, a DSCR
// commits either a second port, dp1 (col2), or (`enableChannel`) channel 1 on dp0, whose
// window widens to columns 1-2. The DSCR does not re-anchor the SSP: dp0 keeps its interval
// phase, a new port starts on Column 0 of the row the commit takes effect with its interval
// anchored there, and every newly enabled channel's descrambler is freshly seeded. Each
// channel carries sineSample(p * 2 + ch, index, 15), scrambled. Returns bus levels for
// MemorySampleSource.
std::vector<bool> MakeDscrEnableLevels(int samplesPerChannel, bool enableChannel)
{
    requireDemoLength(samplesPerChannel, "make_dscr_enable_levels", "samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 63, cols = 8;      // 16-bit PCM; 8-col 48 kHz (64 rows/sample)
    const PortGeom g[2] = { {0, kSS, 1, 0, kInt, 0, 0}, {1, kSS, 2, 0, kInt, 0, 0} };

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    emitPortWrites(bits, regs, dev, g[0]);
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);        // SSCR: dp0 ch0 only
    regs.Commit(group, dev);
    SwI3sConfig cfg1; regs.BuildConfig(cfg1); cfg1.NumColumns = cols - 1;
    if (cfg1.SkippingDenominator < 1) cfg1.SkippingDenominator = 1;
    long ssp1 = ssp1Bit + 1 + (int)rowDelay;

    std::vector<bool> blk;                                     // the new config, then a DSCR
    if (enableChannel) {
        const U32 dp0 = swi3s::reg::kDpBase;
        emitWrite(blk, regs, dev, dp0 + swi3s::reg::kDpEnableCh0HCount_N, {0x81});  // ch0, 2 cols
        emitWrite(blk, regs, dev, dp0 + swi3s::reg::kDpEnableCh1_7_N, {0x02});      // ch1
    } else {
        emitPortWrites(blk, regs, dev, g[1]);
    }
    appendPing(blk);
    appendSscr(blk, dev, group, rowDelay, swi3s::kOpDscr);
    long rows1 = (long)samplesPerChannel * (kInt + 1);
    long ssp2 = ssp1 + rows1;                                  // the DSCR takes effect here
    long blkStart = ssp2 - (int)rowDelay - (long)blk.size();
    placeEventBlock(bits, blkStart, blk, "make_dscr_enable_levels", "samples_per_channel",
                    samplesPerChannel);
    regs.Commit(group, dev);
    SwI3sConfig cfg2; regs.BuildConfig(cfg2); cfg2.NumColumns = cols - 1;
    if (cfg2.SkippingDenominator < 1) cfg2.SkippingDenominator = 1;
    long totalRows = ssp2 + (long)samplesPerChannel * (kInt + 1);
    fillIdleWithPings(bits, totalRows);   // idle + keep-alive Pings to the end

    auto portOf = [](const SwI3sConfig& c, int dp, SwI3sDpConfig& out) {
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0 && d.dpNumber == dp) { out = d; return true; }
        return false;
    };
    SwI3sDpConfig before[2], after[2];
    if (!portOf(cfg1, 0, before[0]) || !portOf(cfg2, 0, after[0])) return {};
    if (!enableChannel && !portOf(cfg2, 1, after[1])) return {};
    const int nPorts = enableChannel ? 1 : 2;
    auto key = [](int p, int ch) { return p * 32 + ch; };

    std::map<int, CDescrambler> scram;
    std::map<int, U64> curSample, sampleIndex;
    CDataPort ports[2];
    bool live[2] = {false, false};
    int columnCount = 2;
    std::vector<bool> levels;
    bool cur = false;
    auto seed = [&](int p, std::uint32_t channels) {
        for (int ch = 0; ch < 16; ++ch)
            if (channels & (1u << ch)) scram[key(p, ch)] = CDescrambler();
    };

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) {
            columnCount = cols;
            ports[0].Configure(before[0], cols, cfg1.SkippingDenominator);
            ports[0].Initialize();
            ports[0].SyncToSSP();
            seed(0, before[0].EnableCh);
            live[0] = true;
        }
        if (r == ssp2) {
            if (enableChannel) {                               // same port, phase kept
                ports[0].ApplyConfigKeepingPhase(after[0], cols, cfg2.SkippingDenominator);
                seed(0, after[0].EnableCh & ~before[0].EnableCh);
            } else {                                           // a new port, anchored here
                ports[1].Configure(after[1], cols, cfg2.SkippingDenominator);
                ports[1].Initialize();
                seed(1, after[1].EnableCh);
                live[1] = true;
            }
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[2];
            for (int p = 0; p < nPorts; ++p) if (live[p]) es[p] = ports[p].clock_tick();
            bool level = cur;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                for (int p = 0; p < nPorts; ++p) {
                    const DpEmit& e = es[p];
                    if (!live[p] || !e.sampleHere || e.slot != SwI3sSlot::Data) continue;
                    int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                    if (b == kSS) curSample[k] = sineSample(p * 2 + ch, sampleIndex[k]++, kSS);
                    level = scram[k].Scramble(((curSample[k] >> b) & 1) != 0);
                    break;
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// See Demo.h. The DSCR's commit point is commitRow; the generator applies the new channel
// set to the running port at that row as the decode does, keeping its interval phase.
std::vector<bool> MakeDscrChannelSetLevels(int samplesPerChannel, unsigned fromMask,
                                           unsigned toMask, int rowInInterval)
{
    requireDemoLength(samplesPerChannel, "make_dscr_channel_set_levels", "samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 63, cols = 8;      // 16-bit PCM; 8-col 48 kHz (64 rows/sample)
    fromMask &= 0x7;
    toMask &= 0x7;
    if (fromMask == 0) fromMask = 0x1;
    if (rowInInterval < 0) rowInInterval = 0;
    if (rowInInterval > kInt) rowInInterval = kInt;
    const PortGeom g = {0, kSS, 1, 6, kInt, 0, 0};        // Columns 1-7
    const U32 dp0 = swi3s::reg::kDpBase;

    CRegisterModel regs;
    auto writeChannels = [&](std::vector<bool>& out, unsigned mask) {
        emitWrite(out, regs, dev, dp0 + swi3s::reg::kDpEnableCh0HCount_N,
                  {(U8)(((mask & 0x1) ? 0x80 : 0x00) | (g.hcount & 0x1F))});
        emitWrite(out, regs, dev, dp0 + swi3s::reg::kDpEnableCh1_7_N, {(U8)(mask & 0xFE)});
    };
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    emitPortWrites(bits, regs, dev, g);
    writeChannels(bits, fromMask);
    appendPing(bits);
    const long ssp1 = (long)appendSscr(bits, dev, group, rowDelay) + 1 + rowDelay;
    regs.Commit(group, dev);
    SwI3sConfig cfg1; regs.BuildConfig(cfg1);
    if (cfg1.SkippingDenominator < 1) cfg1.SkippingDenominator = 1;

    std::vector<bool> blk;
    writeChannels(blk, toMask);
    appendPing(blk);
    appendSscr(blk, dev, group, rowDelay, swi3s::kOpDscr);
    const long commitRow = ssp1 + (long)samplesPerChannel * (kInt + 1) + rowInInterval;
    placeEventBlock(bits, commitRow - (long)rowDelay - (long)blk.size(), blk,
                    "make_dscr_channel_set_levels", "samples_per_channel", samplesPerChannel);
    regs.Commit(group, dev);
    SwI3sConfig cfg2; regs.BuildConfig(cfg2);
    if (cfg2.SkippingDenominator < 1) cfg2.SkippingDenominator = 1;
    const long totalRows = commitRow + (long)samplesPerChannel * (kInt + 1);
    fillIdleWithPings(bits, totalRows);

    auto portOf = [](const SwI3sConfig& c, SwI3sDpConfig& out) {
        for (const SwI3sDpConfig& d : c.dps)
            if (d.Enabled && d.numChannels() > 0 && d.dpNumber == 0) { out = d; return true; }
        return false;
    };
    SwI3sDpConfig before, after;
    if (!portOf(cfg1, before)) return {};
    const bool afterLive = portOf(cfg2, after);

    CDescrambler scram[3];
    U64 curSample[3] = {0, 0, 0}, sampleIndex[3] = {0, 0, 0};
    CDataPort port;
    bool live = false;
    int columnCount = 2;
    std::vector<bool> levels;
    bool cur = false;
    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) {
            columnCount = cols;
            port.Configure(before, cols, cfg1.SkippingDenominator);
            port.Initialize();
            port.SyncToSSP();
            live = true;
        }
        if (r == commitRow) {
            live = afterLive;
            if (live) {
                port.ApplyConfigKeepingPhase(after, cols, cfg2.SkippingDenominator);
                for (int ch = 0; ch < 3; ++ch)
                    if (toMask & ~fromMask & (1u << ch)) scram[ch] = CDescrambler();
            }
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit e;
            if (live) e = port.clock_tick();
            bool level = cur;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else if (live && e.sampleHere && e.slot == SwI3sSlot::Data &&
                       e.channel >= 0 && e.channel < 3) {
                const int ch = e.channel, b = e.bitInChannel;
                if (b == kSS) curSample[ch] = sineSample(ch, sampleIndex[ch]++, kSS);
                level = scram[ch].Scramble(((curSample[ch] >> b) & 1) != 0);
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// Test fixture for the IMMEDIATE (single-ranked) config-write decode path: one 16-bit PCM
// port (dp0, col1) that is SCRAMBLED on the wire the whole time. Halfway through, a single
// WriteA32 clears its PortControl.ScramblerEn (0x0B bit3) — a single-ranked register that
// takes committed effect immediately (no commit). A correct decode reconfigures on that
// write and STOPS descrambling, so the second half no longer matches the generated sine;
// a decode that only reconfigures at commits keeps descrambling and the whole stream
// matches. Returns bus levels for MemorySampleSource.
std::vector<bool> MakeImmediateScramblerLevels(int samplesPerChannel, bool midInterval)
{
    requireDemoLength(samplesPerChannel, "make_immediate_scrambler_levels", "samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 63, cols = 8;      // 16-bit PCM; 8-col 48 kHz (64 rows/sample)
    if (samplesPerChannel < 8) samplesPerChannel = 8;
    const PortGeom g = {0, kSS, 1, 0, kInt, 0, 0};

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    emitPortWrites(bits, regs, dev, g);           // no PortControl write -> ScramblerEn=1 (reset)
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group, dev);
    SwI3sConfig cfg; regs.BuildConfig(cfg); cfg.NumColumns = cols - 1;
    if (cfg.SkippingDenominator < 1) cfg.SkippingDenominator = 1;
    long ssp1 = ssp1Bit + 1 + (int)rowDelay;

    // After phase-1 audio, a single-ranked write clears dp0's ScramblerEn (0x0B -> 0x00).
    // `midInterval` shifts the write half an interval off the sample boundary so the decode's
    // phase-preserving reconfigure lands MID-interval (row_in_interval != 0) — the case where
    // a windowed replay that re-Configures (row_in_interval -> 0) drifts vs. the live Reconfigure.
    long rows1 = (long)samplesPerChannel * (kInt + 1) + (midInterval ? (kInt + 1) / 2 : 0);
    long writeRow = ssp1 + rows1;
    std::vector<bool> blk;
    emitWrite(blk, regs, dev, swi3s::reg::kDpBase + swi3s::reg::kDpScramDirMode, {0x00});
    fillIdleWithPings(bits, writeRow);   // idle + keep-alive Pings up to the event block
    bits.insert(bits.end(), blk.begin(), blk.end());
    long totalRows = writeRow + (long)blk.size() + (long)samplesPerChannel * (kInt + 1);
    fillIdleWithPings(bits, totalRows);   // idle + keep-alive Pings to the end

    // The generator SCRAMBLES throughout (its port config is the SSCR snapshot, ScramblerEn=1);
    // only the decode is told to stop (the 0x0B write on the CDS).
    std::vector<const SwI3sDpConfig*> dps;
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) dps.push_back(&d);
    int N = (int)dps.size();
    auto tone = [](int p, int ch) { return p * 2 + ch; };
    auto key  = [](int p, int ch) { return p * 32 + ch; };

    std::map<int, CDescrambler> scram;
    std::map<int, U64> curSample, sampleIndex;
    std::vector<CDataPort> ports;
    int columnCount = 2;
    bool portReady = false;
    std::vector<bool> levels;
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) {
            columnCount = cols;
            ports.assign(N, CDataPort());
            scram.clear();
            for (int p = 0; p < N; ++p) {
                ports[p].Configure(*dps[p], cols, cfg.SkippingDenominator);
                ports[p].Initialize();
                ports[p].SyncToSSP();
                for (int ch = 0; ch < 16; ++ch)
                    if (dps[p]->EnableCh & (1u << ch)) scram[key(p, ch)] = CDescrambler();
            }
            portReady = true;
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[8];
            if (portReady) for (int p = 0; p < N; ++p) es[p] = ports[p].clock_tick();
            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                level = cur;
                if (portReady) {
                    for (int p = 0; p < N; ++p) {
                        const DpEmit& e = es[p];
                        if (e.sampleHere &&
                            (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                            int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                            bool clear;
                            if (e.slot == SwI3sSlot::Data) {
                                int ss = dps[p]->SampleSize;
                                if (b == ss)
                                    curSample[k] = sineSample(tone(p, ch), sampleIndex[k]++, ss);
                                clear = ((curSample[k] >> b) & 1) != 0;
                            } else {
                                clear = false;
                            }
                            level = scram[k].Scramble(clear);   // always scrambled on the wire
                            break;
                        }
                    }
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// Test fixture for a SampleSize change through the IMMEDIATE (single-ranked) write path:
// one PCM port (dp0, col1, one bit per row, 64-row Interval) carries `fromSize`+1-bit
// samples; a WriteA32 to SampleSizeGrouping (0x09) sets SampleSize to `toSize`. The write's
// last CDS bit lands `rowsIntoSample` rows into a sample, and the decoder reconfigures at
// the next row boundary, so that sample is in flight across the reconfigure. The wire
// follows the same rule as the decode: the in-flight sample finishes at its old width and
// the next sample starts at the new one. Each sample is sineSample(0, index, width - 1),
// scrambled. Returns bus levels for MemorySampleSource.
std::vector<bool> MakeImmediateSampleSizeLevels(int samplesPerChannel, int fromSize, int toSize,
                                                int rowsIntoSample)
{
    requireDemoLength(samplesPerChannel, "make_immediate_sample_size_levels", "samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kInt = 63, cols = 8;
    if (samplesPerChannel < 8) samplesPerChannel = 8;
    fromSize &= 0x1F;
    toSize &= 0x1F;
    if (rowsIntoSample < 0) rowsIntoSample = 0;
    if (rowsIntoSample > kInt) rowsIntoSample = kInt;
    const PortGeom g = {0, fromSize, 1, 0, kInt, 0, 0};

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    emitPortWrites(bits, regs, dev, g);
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group, dev);
    SwI3sConfig cfg; regs.BuildConfig(cfg); cfg.NumColumns = cols - 1;
    if (cfg.SkippingDenominator < 1) cfg.SkippingDenominator = 1;
    long ssp1 = ssp1Bit + 1 + (int)rowDelay;

    std::vector<bool> blk;
    emitWrite(blk, regs, dev, swi3s::reg::kDpBase + swi3s::reg::kDpSampleSizeGrouping,
              {(U8)(toSize & 0x1F)});
    // The row the decode reconfigures at: the first row after the write's last CDS bit.
    const long applyRow = ssp1 + (long)samplesPerChannel * (kInt + 1) + rowsIntoSample;
    const long writeRow = applyRow - (long)blk.size();
    fillIdleWithPings(bits, writeRow);   // idle + keep-alive Pings up to the event block
    bits.insert(bits.end(), blk.begin(), blk.end());
    const long totalRows = applyRow + (long)samplesPerChannel * (kInt + 1);
    fillIdleWithPings(bits, totalRows);   // idle + keep-alive Pings to the end

    SwI3sDpConfig dp;
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) { dp = d; break; }
    SwI3sDpConfig dpNew = dp;
    dpNew.SampleSize = toSize;

    CDescrambler scram;
    U64 curSample = 0, sampleIndex = 0;
    CDataPort port;
    int columnCount = 2;
    bool portReady = false;
    std::vector<bool> levels;
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) {
            columnCount = cols;
            port.Configure(dp, cols, cfg.SkippingDenominator);
            port.Initialize();
            port.SyncToSSP();
            portReady = true;
        }
        if (r == applyRow && portReady) {
            // The decode's in-place reconfigure: new config, transport phase restored.
            port.ApplyConfigKeepingPhase(dpNew, cols, cfg.SkippingDenominator);
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit e;
            if (portReady) e = port.clock_tick();
            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                level = cur;
                if (portReady && e.sampleHere &&
                    (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                    bool clear = false;
                    if (e.slot == SwI3sSlot::Data) {
                        if (e.bitInChannel == e.sampleSize)
                            curSample = sineSample(0, sampleIndex++, e.sampleSize);
                        clear = ((curSample >> e.bitInChannel) & 1) != 0;
                    }
                    level = scram.Scramble(clear);
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// One scrambled 16-bit PCM port (dp0, col1) committed with Interval `interval`; after
// `samplesPerChannel` samples a single WriteA32 writes `value` to the register at `address`.
// With `beforeRun` that write is the last setup write before the SSCR instead. The wire
// carries the committed config throughout.
static std::vector<bool> runningPortWriteLevels(int samplesPerChannel, int interval, U32 address,
                                                U8 value, bool beforeRun = false)
{
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, cols = 8;      // 16-bit PCM, 8-col bus
    if (samplesPerChannel < 8) samplesPerChannel = 8;
    if (interval < 0) interval = 0;
    const PortGeom g = {0, kSS, 1, 0, interval, 0, 0};

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    appendEnumerationPings(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    emitPortWrites(bits, regs, dev, g);
    if (beforeRun) emitWrite(bits, regs, dev, address, {value});
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group, dev);
    SwI3sConfig cfg; regs.BuildConfig(cfg); cfg.NumColumns = cols - 1;
    if (cfg.SkippingDenominator < 1) cfg.SkippingDenominator = 1;
    long ssp1 = ssp1Bit + 1 + (int)rowDelay;
    long rowsPerSample = (long)interval + 1;

    long rows1 = (long)samplesPerChannel * rowsPerSample;
    long writeRow = ssp1 + rows1;
    std::vector<bool> blk;
    if (!beforeRun) emitWrite(blk, regs, dev, address, {value});
    fillIdleWithPings(bits, writeRow);   // idle + keep-alive Pings up to the event block
    bits.insert(bits.end(), blk.begin(), blk.end());
    long totalRows = writeRow + (long)blk.size() + (long)samplesPerChannel * rowsPerSample;
    fillIdleWithPings(bits, totalRows);   // idle + keep-alive Pings to the end

    std::vector<const SwI3sDpConfig*> dps;
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) dps.push_back(&d);
    int N = (int)dps.size();
    auto tone = [](int p, int ch) { return p * 2 + ch; };
    auto key  = [](int p, int ch) { return p * 32 + ch; };

    std::map<int, CDescrambler> scram;
    std::map<int, U64> curSample, sampleIndex;
    std::vector<CDataPort> ports;
    int columnCount = 2;
    bool portReady = false;
    std::vector<bool> levels;
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) {
            columnCount = cols;
            ports.assign(N, CDataPort());
            scram.clear();
            for (int p = 0; p < N; ++p) {
                ports[p].Configure(*dps[p], cols, cfg.SkippingDenominator);
                ports[p].Initialize();
                ports[p].SyncToSSP();
                for (int ch = 0; ch < 16; ++ch)
                    if (dps[p]->EnableCh & (1u << ch)) scram[key(p, ch)] = CDescrambler();
            }
            portReady = true;
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[8];
            if (portReady) for (int p = 0; p < N; ++p) es[p] = ports[p].clock_tick();
            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                level = cur;
                if (portReady) {
                    for (int p = 0; p < N; ++p) {
                        const DpEmit& e = es[p];
                        if (e.sampleHere &&
                            (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                            int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                            bool clear;
                            if (e.slot == SwI3sSlot::Data) {
                                int ss = dps[p]->SampleSize;
                                if (b == ss)
                                    curSample[k] = sineSample(tone(p, ch), sampleIndex[k]++, ss);
                                clear = ((curSample[k] >> b) & 1) != 0;
                            } else {
                                clear = false;
                            }
                            level = scram[k].Scramble(clear);   // always scrambled on the wire
                            break;
                        }
                    }
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// Test fixture for the EnableCh_CURR protocol-error rule (see Demo.h / Decoder.h
// CommandRec::enablechCurrError). One PCM port (dp0) is configured with Interval =
// `interval` and committed via SSCR; after `samplesPerChannel` rows a single WriteA32
// re-writes dp0's EnableCh0/HCount register (0x81) DIRECTLY AT ITS _CURR ALIAS (0x81 +
// 0x40 = 0xC1) — re-asserting EnableCh0 (bit7) and the same HorizontalCount, so the
// write is a content no-op but still exercises the address-based rule. The port keeps
// transmitting its sine on the wire throughout (the write doesn't change the wire, only
// whether the decode flags it), so this fixture isolates the rule check from any audio
// side effect. Returns bus levels for MemorySampleSource.
std::vector<bool> MakeEnableChCurrWriteLevels(int samplesPerChannel, int interval)
{
    requireDemoLength(samplesPerChannel, "make_enablech_curr_write_levels", "samples_per_channel");
    if (interval > kMaxRegisterField12)
        throw std::invalid_argument("make_enablech_curr_write_levels: interval " +
                                    std::to_string(interval) + " does not fit the 12-bit "
                                    "Interval register (0.." +
                                    std::to_string(kMaxRegisterField12) + ")");
    const long long rows = static_cast<long long>(std::max(samplesPerChannel, 8)) *
                           (static_cast<long long>(std::max(interval, 0)) + 1);
    if (rows > kMaxRunningPortRows)
        throw std::invalid_argument("make_enablech_curr_write_levels: samples_per_channel " +
                                    std::to_string(samplesPerChannel) + " at interval " +
                                    std::to_string(interval) + " needs " +
                                    std::to_string(rows) + " Rows per phase, above " +
                                    std::to_string(kMaxRunningPortRows));
    return runningPortWriteLevels(samplesPerChannel, interval,
                                  swi3s::reg::kDpBase + swi3s::reg::kDpEnableCh0HCount_Curr, 0x80);
}

std::vector<bool> MakeRunningPortWriteLevels(int samplesPerChannel, int offset, int value,
                                             std::uint32_t base, bool beforeRun)
{
    requireDemoLength(samplesPerChannel, "make_running_port_write_levels", "samples_per_channel");
    return runningPortWriteLevels(samplesPerChannel, 63, base + (static_cast<U32>(offset) & 0xFF),
                                  static_cast<U8>(value & 0xFF), beforeRun);
}

// Test fixture for Payload Interval Skipping (see Demo.h). One unscrambled 16-bit PCM
// source port on an 8-column bus, Interval = 32 Rows, carrying a RAMP (sample n has value
// n) so a dropped or duplicated sample shows up as a wrong value rather than as plausible
// audio. `numerator`/`denominator` are the skipping ratio; the port transports
// (D - N) of every D intervals and leaves the rest idle.
//
// The generator's ports are Initialize()d and NOT SyncToSSP()d: Initialize is the path the
// placement cross-check pins against the normative Python model, independent of the
// decode's SSP re-anchor. Periodic
// SSPAs land only on rows Interval x SkippingDenominator apart (the pattern period, where
// the accumulated skipping is back at 0), so re-anchoring must be a no-op. With
// `misalignedSspa` they land one Interval off that — still on a row where
// row_in_interval == 0, so the ONLY thing wrong is the skipping phase, which is exactly
// the unexpected SSP of Section 9.1.6.2.1.
std::vector<bool> MakeSkippingLevels(int samplesPerChannel, int numerator, int denominator,
                                     bool misalignedSspa)
{
    requireDemoLength(samplesPerChannel, "make_skipping_levels", "samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 31, cols = 8;   // 16-bit PCM; 32 Rows/Interval on an 8-col bus
    if (samplesPerChannel < 8) samplesPerChannel = 8;
    if (numerator > kMaxRegisterField12 || denominator > kMaxRegisterField12)
        throw std::invalid_argument("make_skipping_levels: numerator " +
                                    std::to_string(numerator) + " and denominator " +
                                    std::to_string(denominator) + " must fit their 12-bit "
                                    "registers (0.." + std::to_string(kMaxRegisterField12) + ")");
    if (denominator < 1) denominator = 0;      // written as 0; the registers read it as 1
    if (numerator < 0) numerator = 0;
    const PortGeom g = {0, kSS, 1, 0, kInt, 0, 0};

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {(U8)(cols - 1)});
    // SLC_SkippingDenominator is MSB-first: [11:8] at the LOWER address (0x1012), [7:0] at
    // 0x1013 — the opposite order to DPn_SkippingNumerator below. Both orderings are called
    // out as exceptions in the register tables, so write them as two explicit bytes.
    emitWrite(bits, regs, dev, swi3s::reg::kSkippingDenomHi, {(U8)((denominator >> 8) & 0x0F)});
    emitWrite(bits, regs, dev, swi3s::reg::kSkippingDenomLo, {(U8)(denominator & 0xFF)});
    emitPortWrites(bits, regs, dev, g);
    // 0x0B = 0x00: ScramblerEn off (its reset is 1), SOURCE, PortMode 0. Unscrambled so a
    // mis-phased skip surfaces as the wrong sample VALUE, not as descrambler noise.
    emitWrite(bits, regs, dev, swi3s::reg::kDpBase + swi3s::reg::kDpScramDirMode, {0x00});
    // DPn_SkippingNumerator is LSB-first: [7:0] at 0x0C, [11:8] in 0x0D[3:0].
    emitWrite(bits, regs, dev, swi3s::reg::kDpBase + swi3s::reg::kDpSkipNumLo,
              {(U8)(numerator & 0xFF), (U8)((numerator >> 8) & 0x0F)});
    appendPing(bits);
    long sspBit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group, dev);
    SwI3sConfig cfg; regs.BuildConfig(cfg); cfg.NumColumns = cols - 1;
    if (cfg.SkippingDenominator < 1) cfg.SkippingDenominator = 1;
    const long ssp = sspBit + 1 + (long)rowDelay;

    // Rows enough for `samplesPerChannel` TRANSPORTED samples: only (D - N) of every D
    // intervals carry one.
    const long denom = cfg.SkippingDenominator;
    const long num = (numerator < denom) ? numerator : 0;
    const long usable = denom - num;
    const long intervals = (usable > 0)
        ? (samplesPerChannel * denom + usable - 1) / usable + 2
        : samplesPerChannel + 2;
    if (intervals > kMaxSkippingIntervals)
        throw std::invalid_argument(
            "make_skipping_levels: " + std::to_string(samplesPerChannel) +
            " samples at skipping ratio " + std::to_string(numerator) + "/" +
            std::to_string(denominator) + " need " + std::to_string(intervals) +
            " intervals, above " + std::to_string(kMaxSkippingIntervals));
    const long totalRows = ssp + intervals * (kInt + 1);

    SspaSchedule sspa = makeSspaSchedule(cfg, ssp, totalRows, cols, dev, group, rowDelay);
    // Shift the SSPA cadence one Interval off the pattern boundary. The generator's own
    // ports are unaffected (they never re-anchor), so the wire keeps the true pattern and
    // the decode meets an SSP where its accumulated skipping is NOT 0.
    if (misalignedSspa) sspa.anchor += (kInt + 1);
    fillIdleWithPings(bits, totalRows, &sspa);

    std::vector<const SwI3sDpConfig*> dps;
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) dps.push_back(&d);
    const int N = (int)dps.size();
    auto key = [](int p, int ch) { return p * 32 + ch; };

    std::map<int, U64> curSample, sampleIndex;
    std::vector<CDataPort> ports;
    int columnCount = 2;
    bool portReady = false;
    std::vector<bool> levels;
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp) {
            columnCount = cols;
            ports.assign(N, CDataPort());
            for (int p = 0; p < N; ++p) {
                ports[p].Configure(*dps[p], cols, cfg.SkippingDenominator);
                ports[p].Initialize();          // NOT SyncToSSP -- see the comment above
            }
            portReady = true;
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[8];
            if (portReady) for (int p = 0; p < N; ++p) es[p] = ports[p].clock_tick();
            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                level = cur;                    // a skipped interval drives nothing: line holds
                if (portReady) {
                    for (int p = 0; p < N; ++p) {
                        const DpEmit& e = es[p];
                        if (e.sampleHere && e.slot == SwI3sSlot::Data) {
                            int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                            int ss = dps[p]->SampleSize;
                            if (b == ss)
                                curSample[k] = sampleIndex[k]++ & ((1ull << (ss + 1)) - 1);
                            level = ((curSample[k] >> b) & 1) != 0;
                            break;
                        }
                    }
                }
            }
            levels.push_back(level);
            cur = level;
        }
    }
    return levels;
}

// ===========================================================================
// PHY3 (DLV) demo. Mirrors MakeDemoLevels' audio content (2x 16-bit PCM on 48 kHz
// transport, DP0 carrying 44.1 kHz by skipping; 2x 1-bit PDM @3.072 MHz; DP0/DP2
// unscrambled, DP1/DP3 scrambled) but over DLV
// framing instead of FBCSE. Returned as per-UI LOGICAL differential levels (1 =
// DP high/DN low, 0 = DP low/DN high); the Python side turns these into the
// complementary DP/DN edge arrays. See §12.1.10, §12.2.5.
//
// DLV row framing (unlike FBCSE where Column 0 is the NRZS CDS and cols 1.. are
// payload): Column 0 = Sync1 (logical 1; the 0->1 into it is the Row Sync Point the
// receiver PLL locks to), CDS is a PLAIN-NRZ bit at CDS_HorizontalStart (=2, the
// Safe-Lock-4 default; no NRZS in DLV), the last column = Sync0 (logical 0, so the
// next row's Sync1 is a clean rising edge), and data ports occupy the columns in
// between (register HorizontalStart, cols 4..9 here). Non-driven columns hold the
// previous level (bus keeper) so edges appear only on value changes.
//
// Geometry path is Safe-Lock-4 (4 col, command-only) -> a single commit to 16 col
// carrying all four ports (4 payload columns at 8-col DLV can't fit 2 PCM + 2 PDM
// once Sync1/CDS/Sync0 are reserved, so PHY3 uses one reconfig, not PHY2's two).
DemoLevels MakeDemoLevelsPhy3(int audioSamplesPerChannel, int cdsColumn)
{
    requireDemoLength(audioSamplesPerChannel, "make_demo_levels_phy3", "audio_samples_per_channel");
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kPcmSS = 15;         // 16-bit PCM
    const int kPdmSS = 0;          // 1-bit PDM
    const int kCdsH  = 2;          // CDS at Column 2 (CDS_HorizontalStart's PHY3 reset value)
    const int kSlCols = 4;         // Safe-Lock-4
    const int kOpCols = 16;        // operational geometry
    // 16-col operational geometry runs at row rate 3.072 MRows/s (the PLL reference held
    // constant; UI rate 49.152 MHz). To keep 48 kHz PCM / 3.072 MHz PDM at that row rate:
    // PCM Interval 63 (3072/64 = 48 kHz); PDM SampleGrouping=2 + Interval 1 (2 samples /
    // 2 rows -> 3.072 MHz) — the same audio-rate registers as PHY2's 8-col phase. Payload
    // columns 4..9 (Sync1=0, handover=1, CDS=2, handover=3, ports 4..9, columns 10..13
    // free, handover=14, Sync0=15).
    const int kPcmInt = 63, kPdmSG = 1, kPdmInt = 1;
    const PortGeom geom[4] = {
        {0, kPcmSS, 4, 0, kPcmInt, 0, 0},          // dp0 PCM  col 4
        {1, kPcmSS, 5, 0, kPcmInt, 0, 0},          // dp1 PCM  col 5
        {2, kPdmSS, 6, 1, kPdmInt, kPdmSG, 1},     // dp2 PDM  cols 6-7
        {3, kPdmSS, 8, 1, kPdmInt, kPdmSG, 1},     // dp3 PDM  cols 8-9
    };

    CRegisterModel regs;
    std::vector<bool> bits;                                 // one CDS bit per Row
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);  // idle preamble
    appendPing(bits);

    // ---- single commit: Safe-Lock-4 -> 16 columns (audio starts) ----
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x0F});   // 16 columns
    // A CDS column other than the reset value is programmed with the geometry and moves
    // the CDS at the same commit. It lies between Sync1 and Sync0 (1..14) and off the
    // data ports' columns; any other value raises.
    bool cdsOnPort = false;
    for (const PortGeom& p : geom)
        if (cdsColumn >= p.hstart && cdsColumn <= p.hstart + p.hcount) cdsOnPort = true;
    if (cdsColumn < 1 || cdsColumn > kOpCols - 2 || cdsOnPort)
        throw std::invalid_argument(
            "cds_column " + std::to_string(cdsColumn) +
            " must lie in 1..14 and off the data-port columns 4..9");
    const int opCds = cdsColumn;
    if (opCds != kCdsH)
        emitWrite(bits, regs, dev, swi3s::reg::kCdsHorizontalStart_N, {(U8)opCds});
    for (const PortGeom& p : geom) emitPortWrites(bits, regs, dev, p);
    emitSkipping(bits, regs, dev, /*dp=*/0);                // DP0: 44.1 kHz, see emitSkipping
    for (int dp : {0, 2})                                   // DP0/DP2 run UNSCRAMBLED
        emitWrite(bits, regs, dev,
                  swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)dp
                      + swi3s::reg::kDpScramDirMode, {0x00});
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group, dev);
    SwI3sConfig cfg; regs.BuildConfig(cfg);
    cfg.NumColumns = kOpCols - 1;
    if (cfg.SkippingDenominator < 1) cfg.SkippingDenominator = 1;
    long ssp1 = ssp1Bit + 1 + (int)rowDelay;

    long samples = audioSamplesPerChannel;
    if (samples < 1) samples = 1;
    long rows = samples * (kPcmInt + 1);
    long totalRows = ssp1 + rows;
    // keep-alive Pings + periodic SSPAs through audio. On DLV the CDS is plain NRZ, so
    // the SSPA's bits render directly — but the SSP arithmetic is identical (CDS bit ==
    // Row either way), so the same aligned placement applies.
    SspaSchedule sspa = makeSspaSchedule(cfg, ssp1, totalRows, kOpCols, dev, group, rowDelay);
    fillIdleWithPings(bits, totalRows, &sspa);

    std::vector<const SwI3sDpConfig*> dps;
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) dps.push_back(&d);
    int N = (int)dps.size();
    auto tone = [](int p, int ch) { return p * 2 + ch; };
    auto key  = [](int p, int ch) { return p * 32 + ch; };

    std::map<int, CDescrambler> scram;
    std::map<int, U64> curSample, sampleIndex;
    std::map<int, double> sdAcc;                            // PDM sigma-delta integrator
    std::vector<CDataPort> ports;
    int columnCount = kSlCols;                              // Safe-Lock-4 to start
    bool portReady = false;

    std::vector<bool> levels;
    levels.reserve((size_t)(totalRows * kOpCols));
    std::vector<int> rowCols;                       // per-row column count (row-major levels)
    rowCols.reserve((size_t)totalRows);
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) {                                   // ports come up at the SSP
            columnCount = kOpCols;
            ports.assign(N, CDataPort());
            scram.clear();
            for (int p = 0; p < N; ++p) {
                ports[p].Configure(*dps[p], kOpCols, cfg.SkippingDenominator);
                ports[p].Initialize();
                ports[p].SyncToSSP();
                for (int ch = 0; ch < 16; ++ch)
                    if (dps[p]->EnableCh & (1u << ch)) scram[key(p, ch)] = CDescrambler();
            }
            portReady = true;
        }
        for (int c = 0; c < columnCount; ++c) {
            DpEmit es[64];
            if (portReady) for (int p = 0; p < N; ++p) es[p] = ports[p].clock_tick();

            bool level;
            if (c == 0) {
                level = true;                              // Sync1 (Row Sync rising edge)
            } else if (c == columnCount - 1) {
                level = false;                             // Sync0 (clean edge into next row)
            } else if (c == (r >= ssp1 ? opCds : kCdsH)) {
                level = (r < (long)bits.size()) ? (bool)bits[r] : true;   // CDS: plain NRZ
            } else {
                level = cur;                               // keeper holds unless a port drives
                if (portReady) {
                    for (int p = 0; p < N; ++p) {
                        const DpEmit& e = es[p];
                        if (e.sampleHere &&
                            (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                            int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                            bool clear;
                            if (e.slot == SwI3sSlot::Data) {
                                int ss = dps[p]->SampleSize;
                                if (b == ss) {
                                    if (ss == 0) {          // PDM sigma-delta: dp2=250Hz, dp3=750Hz
                                        double freqHz = 250.0 + 500.0 *
                                            (double)(dps[p]->dpNumber - 2);
                                        double t = 2.0 * kPi * freqHz
                                                   * (double)sampleIndex[k]++ / 3072000.0;
                                        sdAcc[k] += 0.45 * std::sin(t);
                                        U64 bit = (sdAcc[k] >= 0.0) ? 1 : 0;
                                        sdAcc[k] -= bit ? 1.0 : -1.0;
                                        curSample[k] = bit;
                                    } else {
                                        curSample[k] = sineSample(tone(p, ch),
                                                                  sampleIndex[k]++, ss);
                                    }
                                }
                                clear = ((curSample[k] >> b) & 1) != 0;
                            } else {
                                clear = false;
                            }
                            level = dps[p]->ScramblerEn ? scram[k].Scramble(clear) : clear;
                            break;                          // disjoint windows: one owner
                        }
                    }
                }
            }
            levels.push_back(level);
            cur = level;
        }
        rowCols.push_back(columnCount);             // this row's width (4 safe-lock, 16 audio)
    }
    return DemoLevels{std::move(levels), std::move(rowCols)};
}

} // namespace swi3score