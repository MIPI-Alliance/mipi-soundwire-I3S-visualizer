// Synthetic SWI3S PHY2 demo stream. See Demo.h. The plan-building logic mirrors
// SwI3sSimulationDataGenerator (and test/test_simulation_roundtrip.cpp), reusing
// the core's 8b/10b encoder, CRC, register model, placement cascade and
// scrambler so the produced stream decodes back to the generated audio.
//
// The demo starts in safe-lock-2 (2 columns, control only), commits to 8 columns
// (audio starts), then commits to 16 columns (audio continues, uninterrupted). It
// carries two 1-channel 16-bit PCM ports (48 kHz) and two 1-channel PDM ports
// (3.072 MHz). Audio rate is held constant across the 8->16 commit by rewriting the
// rate-determining registers alongside NumColumns: PCM Interval 63->31; and the PDM
// ports switch to SampleGrouping=2 (two adjacent samples/row) so 3.072 MHz survives the
// halved row rate. DP0 (PCM) and DP2 (PDM) run unscrambled, DP1/DP3 scrambled, to show
// both transports. See the maintainers' demo-rework notes.

#include "Demo.h"

#include <array>
#include <cmath>
#include <deque>
#include <map>
#include <numeric>   // std::lcm (SSPA interval alignment)

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
// The tail is not optional and is easy to leave off. After the data + 2 CRC bytes the parser
// goes eReadPmSpacer -> eReadMgrResp and only emits the command record on the MANAGER's
// response symbol (READ_DATA_OK). A first version of this helper stopped at the CRC, so the
// immediate read decoded into NOTHING AT ALL — no record, no error — because the next
// command's comma arrived mid-spacer and abandoned the phase in progress. The deferred pair
// happened to survive only because idle fill followed it. Silent, and invisible to any
// assertion that counts errors rather than records.
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
// returns -1, and the UI reports NO_RESPONSE (see analysis/responses.py). Driving
// PING_ATTACHED for all 12 (as this used to) claimed twelve attached peripherals on a
// bus that only ever configures one, which the per-peripheral Ping statistics made
// obvious.
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

// LCM of every enabled port's transport pattern period — the row period after which all
// ports are back at the same point in their transport pattern. That is Interval + 1 Rows
// for a plain port, and Interval x SkippingDenominator for a skipping one (see
// SwI3sConfig::patternRows): an SSPA must land on a multiple of THIS, or it restarts a
// skipping port's pattern part-way through and shifts which intervals transport.
long intervalAlignment(const SwI3sConfig& cfg)
{
    long lcm = 1;
    for (const SwI3sDpConfig& d : cfg.dps) {
        if (!d.Enabled || d.numChannels() <= 0 || d.Interval < 0) continue;
        lcm = std::lcm(lcm, cfg.patternRows(d));
    }
    return (lcm > 0) ? lcm : 1;
}

bool anyAudioPort(const SwI3sConfig& cfg)
{
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) return true;
    return false;
}

// Every demo runs the bus at a constant 24.576 MHz UI rate, so a region's Row rate is
// that divided by its column count (8 col -> 3.072 MHz, 16 col -> 1.536 MHz). One SSPA
// per ~100 ms of wall-clock, expressed in that region's Rows.
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
// is ~100 ms, but a demo built with a small audioSamplesPerChannel has regions far
// shorter than that, and a region with active data ports carrying NO SSPA would leave the
// whole feature unexercised in most tests. So when a full period doesn't fit, fall back to
// roughly one SSPA at the region's midpoint (still interval-aligned). Long/realistic
// captures get the true 100 ms cadence.
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
// On FBCSE (PHY1/PHY2) the CDS is additionally NRZS-encoded (Table 133: a CDS 0 inverts
// the bus value, a 1 repeats it), so those 0101 idle bits render on the wire as the
// ..1100.. "zebra" — the same rendered form as the Cold-Start idle preamble. On DLV (PHY3)
// the CDS is not NRZS-encoded, so the identical 0101 idle bits render as 0101 directly.
// Emitting all-ones instead would NRZS-hold the FBCSE level flat (no zebra), which a real
// bus never does when idle.
const long kPingRowPeriod = 2048;   // half the {ASW2601} 4096-Row limit (error margin)

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

// PCM: a distinct sine per stream, MSb..LSb over (sampleSize+1) bits.
U64 sineSample(int channel, U64 index, int sampleSize)
{
    double amp = (double)(1 << sampleSize) * 0.45;
    double w = 2.0 * kPi * (double)(channel + 1) * (double)index / 64.0;
    int v = (int)std::lround(amp * std::sin(w));
    U64 mask = (1ull << (sampleSize + 1)) - 1;
    return (U64)v & mask;
}

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
    double amp = (double)(1 << sampleSize) * 0.45;
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

void emitFcPortWrites(std::vector<bool>& bits, CRegisterModel& regs, U16 dev, const FcGeom& p)
{
    U32 base = swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)p.dp;
    // 0x09: [7:5]=SampleGrouping(=0), [4:0]=SampleSize.
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpSampleSizeGrouping,
              {(U8)(p.sampleSize & 0x1F)});
    // 0x0B: bit3=ScramblerEn(0 -> unscrambled, so TX_PRESENT gating is decoupled from
    // the descrambler LFSR), bit2=PortDirection (1=SINK), [1:0]=PortMode(0).
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpScramDirMode,
              {(U8)((p.sink ? 1 : 0) << 2)});
    // 0x0E: FlowMode [1:0].
    emitWrite(bits, regs, dev, base + swi3s::reg::kDpFlowMode, {(U8)(p.flowMode & 0x3)});
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
    // Show both transports side by side: DP0 (PCM) and DP2 (PDM) run UNSCRAMBLED
    // (PortControl.ScramblerEn=0); DP1/DP3 keep the reset default (scrambling ON). 0x0B is
    // single-rank, so this holds for the whole capture (both geometries).
    for (int dp : {0, 2})
        emitWrite(bits, regs, dev,
                  swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)dp
                      + swi3s::reg::kDpScramDirMode, {0x00});
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group);
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
    regs.Commit(group);
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
    // A DEFERRED Read, emitted here because no demo carried one and nothing tested the
    // path: ReadSetup answered REMOTE_READ_DEFERRED (no data), then the ReadData delivery
    // inheriting its byte count + address. An IMMEDIATE read goes first for contrast — the
    // two shapes produce a DIFFERENT NUMBER OF COMMAND RECORDS from the same logical read,
    // which is what per-command statistics group by.
    //
    // Appended BEFORE the idle fill on purpose: fillIdleWithPings pads to `totalRows`, so
    // whatever these consume it emits less idle to reach the same total. The demo's row
    // count, audio timing and SSP rows are therefore unchanged — only the command list
    // grows. Adding them after the fill would have shifted every downstream row.
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

    auto reconfigure = [&](const std::vector<const SwI3sDpConfig*>& dd, int cc) {
        columnCount = cc;
        N = (int)dd.size();
        ports.assign(N, CDataPort());
        scram.clear();
        for (int p = 0; p < N; ++p) {
            ports[p].Configure(*dd[p], cc, cfg8.SkippingDenominator);
            ports[p].Initialize();
            ports[p].SyncToSSP();
            for (int ch = 0; ch < 16; ++ch)
                if (dd[p]->EnableCh & (1u << ch)) scram[key(p, ch)] = CDescrambler();
        }
        portReady = true;
    };

    std::vector<bool> levels;
    levels.reserve((size_t)(ssp2 * 8 + rows16 * 16));
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (r == ssp1) { dps = dps8.data(); reconfigure(dps8, 8); }
        else if (r == ssp2) { dps = dps16.data(); reconfigure(dps16, 16); }  // audio index kept
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
// test_flow_control_demo.py::_flow_demo_freq) — keep them in sync.
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
// jittered-DRQ + 0/1-interval TX response delay) are added together with their decode
// so the drive and the decode stay in lockstep — see the FCP re-drive work.
std::vector<bool> MakeDemoLevelsFlowControl(int audioSamplesPerChannel)
{
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
    for (const FcPort& fp : fps) emitFcPortWrites(bits, regs, fp.mask, fp.geom);
    appendPing(bits, allDev, alertDev);
    long sspBit = (long)appendSscr(bits, allDev, group, rowDelay);
    regs.Commit(group);
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
    //                  (Table 156). The manager drives the DRQ bit on the bus at the
    //                  FCP cell; the demo drives a deterministic DRQ coin and the
    //                  transport MUST mirror it d intervals later.
    //   ASYNC          TxPresent[n] = Earlier_FCP_DRQ[n] AND SourceReady[n] (Table 155):
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
    std::vector<char> curDrq(P, 0);                    // this interval's DRQ level to drive
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
                                    // Earlier_FCP_DRQ (Table 155). Model a source whose
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
                        }
                        level = curDrq[p] != 0;
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

std::vector<bool> MakeDemoLevelsPhy1(int audioSamplesPerChannel)
{
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kPcmSS = 15;   // 16-bit PCM
    const int kPdmSS = 0;    // 1-bit PDM
    // Constant 4-column geometry at 1.536 MRows/s (RowRate 1536 in the reference CSVs):
    //   PCM Interval 31 -> 32 rows/sample -> 48 kHz; PDM SampleGrouping 2 + Interval 0 ->
    //   2 samples/row -> 3.072 MHz. The row rate never changes (same column count), so these
    //   registers are identical in both configs — only the placement (HorizontalStart) moves.
    const int kPcmInt = 31;
    const int kPdmSG = 1, kPdmInt = 0;

    // Two placements over the same 4 columns (CDS at Column 0), matching the reference
    // phy1_bus_config_1 / _2. Fields: {dp, sampleSize, hstart, hcount, interval, sampleGroup,
    // spacing, offset}. config_1: PCM pair share col 1 (DP0 Offset 0, DP1 Offset 16), PDM in
    // cols 2-3. config_2: the PCM pair moves to col 3, the PDM to cols 1-2.
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
    // DP0 (PCM) and DP2 (PDM) run UNSCRAMBLED; DP1 keeps the reset default (scrambling ON).
    for (int dp : {0, 2})
        emitWrite(bits, regs, dev,
                  swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)dp
                      + swi3s::reg::kDpScramDirMode, {0x00});
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group);
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
    regs.Commit(group);
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

    auto reconfigure = [&](const std::vector<const SwI3sDpConfig*>& dd, int cc) {
        columnCount = cc;
        N = (int)dd.size();
        ports.assign(N, CDataPort());
        scram.clear();
        for (int p = 0; p < N; ++p) {
            ports[p].Configure(*dd[p], cc, cfg1.SkippingDenominator);
            ports[p].Initialize();
            ports[p].SyncToSSP();
            for (int ch = 0; ch < 16; ++ch)
                if (dd[p]->EnableCh & (1u << ch)) scram[key(p, ch)] = CDescrambler();
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
        if (r == ssp1) { dps = dps1.data(); reconfigure(dps1, 4); }
        else if (r == ssp2) { dps = dps2.data(); reconfigure(dps2, 4); }
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
// KEEPS transmitting its sine on the wire afterwards, so a decoder that fails to drop the
// port on the DSCR would keep emitting dp1 audio — the regression this exercises. A correct
// decode stops dp1 at the DSCR and continues dp0 uninterrupted (a DSCR must not re-anchor
// the SSP, so dp0's phase is preserved). Returns bus levels for MemorySampleSource.
std::vector<bool> MakeDscrDisableLevels(int samplesPerChannel, bool disableAll)
{
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 63, cols = 8;      // 16-bit PCM; 8-col 48 kHz (64 rows/sample)
    if (samplesPerChannel < 8) samplesPerChannel = 8;
    const PortGeom g[2] = { {0, kSS, 1, 0, kInt, 0, 0}, {1, kSS, 2, 0, kInt, 0, 0} };

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    for (const PortGeom& p : g) emitPortWrites(bits, regs, dev, p);
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);        // SSCR: enable both
    regs.Commit(group);
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
    (void)cfg1;

    long rows1 = (long)samplesPerChannel * (kInt + 1);
    long ssp2 = ssp1 + rows1;                                  // DSCR takes effect here
    long blkStart = ssp2 - (int)rowDelay - (long)blk.size();
    for (long s = (long)bits.size(); (long)bits.size() < blkStart; )
        bits.push_back((((long)bits.size() - s) & 1) != 0);   // D10.2 0101 idle (§8.1.2.9), 0 first
    bits.insert(bits.end(), blk.begin(), blk.end());
    regs.Commit(group);                                        // disable dp1 in the model
    long totalRows = ssp2 + (long)samplesPerChannel * (kInt + 1);

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

// Test fixture for the IMMEDIATE (single-ranked) config-write decode path: one 16-bit PCM
// port (dp0, col1) that is SCRAMBLED on the wire the whole time. Halfway through, a single
// WriteA32 clears its PortControl.ScramblerEn (0x0B bit3) — a single-ranked register that
// takes committed effect immediately (no commit). A correct decode reconfigures on that
// write and STOPS descrambling, so the second half no longer matches the generated sine;
// a decode that only reconfigures at commits keeps descrambling and the whole stream
// matches. Returns bus levels for MemorySampleSource.
std::vector<bool> MakeImmediateScramblerLevels(int samplesPerChannel, bool midInterval)
{
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 63, cols = 8;      // 16-bit PCM; 8-col 48 kHz (64 rows/sample)
    if (samplesPerChannel < 8) samplesPerChannel = 8;
    const PortGeom g = {0, kSS, 1, 0, kInt, 0, 0};

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    emitPortWrites(bits, regs, dev, g);           // no PortControl write -> ScramblerEn=1 (reset)
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group);
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
    for (long s = (long)bits.size(); (long)bits.size() < writeRow; )
        bits.push_back((((long)bits.size() - s) & 1) != 0);   // D10.2 0101 idle (§8.1.2.9), 0 first
    bits.insert(bits.end(), blk.begin(), blk.end());
    long totalRows = writeRow + (long)blk.size() + (long)samplesPerChannel * (kInt + 1);

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
    emitWrite(bits, regs, dev, swi3s::reg::kNumColumns_Next, {0x07});
    emitPortWrites(bits, regs, dev, g);
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group);
    SwI3sConfig cfg; regs.BuildConfig(cfg); cfg.NumColumns = cols - 1;
    if (cfg.SkippingDenominator < 1) cfg.SkippingDenominator = 1;
    long ssp1 = ssp1Bit + 1 + (int)rowDelay;
    long rowsPerSample = (long)interval + 1;

    // After phase-1 audio, a single WriteA32 to EnableCh0/HCount's _CURR alias (0xC1)
    // re-asserts EnableCh0 (bit7) with the same HorizontalCount — content unchanged, the
    // write's ADDRESS is what the rule keys on.
    long rows1 = (long)samplesPerChannel * rowsPerSample;
    long writeRow = ssp1 + rows1;
    U32 currAddr = swi3s::reg::kDpBase + swi3s::reg::kDpEnableCh0HCount_Curr;
    std::vector<bool> blk;
    emitWrite(blk, regs, dev, currAddr, {(U8)(0x80 | (g.hcount & 0x1F))});
    for (long s = (long)bits.size(); (long)bits.size() < writeRow; )
        bits.push_back((((long)bits.size() - s) & 1) != 0);   // D10.2 0101 idle (§8.1.2.9), 0 first
    bits.insert(bits.end(), blk.begin(), blk.end());
    long totalRows = writeRow + (long)blk.size() + (long)samplesPerChannel * rowsPerSample;

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

// Test fixture for Payload Interval Skipping (see Demo.h). One unscrambled 16-bit PCM
// source port on an 8-column bus, Interval = 32 Rows, carrying a RAMP (sample n has value
// n) so a dropped or duplicated sample shows up as a wrong value rather than as plausible
// audio. `numerator`/`denominator` are the skipping ratio; the port transports
// (D - N) of every D intervals and leaves the rest idle.
//
// The generator's ports are Initialize()d and deliberately NOT SyncToSSP()d: Initialize is
// the path the placement cross-check pins against the normative Python model, so the
// decode's SSP re-anchor has to AGREE with it rather than share a mistake with it. Periodic
// SSPAs land only on rows Interval x SkippingDenominator apart (the pattern period, where
// the accumulated skipping is back at 0), so re-anchoring must be a no-op. With
// `misalignedSspa` they land one Interval off that — still on a row where
// row_in_interval == 0, so the ONLY thing wrong is the skipping phase, which is exactly
// the unexpected SSP of Section 9.1.6.2.1.
std::vector<bool> MakeSkippingLevels(int samplesPerChannel, int numerator, int denominator,
                                     bool misalignedSspa)
{
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kSS = 15, kInt = 31, cols = 8;   // 16-bit PCM; 32 Rows/Interval on an 8-col bus
    if (samplesPerChannel < 8) samplesPerChannel = 8;
    if (denominator < 1) denominator = 1;
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
    regs.Commit(group);
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
// PHY3 (DLV) demo. Mirrors MakeDemoLevels' audio content (2x 16-bit PCM @48 kHz,

// 2x 1-bit PDM @3.072 MHz; DP0/DP2 unscrambled, DP1/DP3 scrambled) but over DLV
// framing instead of FBCSE. Returned as per-UI LOGICAL differential levels (1 =
// DP high/DN low, 0 = DP low/DN high); the Python side turns these into the
// complementary DP/DN edge arrays. See docs / §12.1.10, §12.2.5.
//
// DLV row framing (unlike FBCSE where Column 0 is the NRZS CDS and cols 1.. are
// payload): Column 0 = Sync1 (logical 1; the 0->1 into it is the Row Sync Point the
// receiver PLL locks to), CDS is a PLAIN-NRZ bit at CDS_HorizontalStart (=2, the
// Safe-Lock-4 default; no NRZS in DLV), the last column = Sync0 (logical 0, so the
// next row's Sync1 is a clean rising edge), and data ports occupy the columns in
// between (register HorizontalStart, cols 4..13 here). Non-driven columns hold the
// previous level (bus keeper) so edges appear only on value changes.
//
// Geometry path is Safe-Lock-4 (4 col, command-only) -> a single commit to 16 col
// carrying all four ports (4 payload columns at 8-col DLV can't fit 2 PCM + 2 PDM
// once Sync1/CDS/Sync0 are reserved, so PHY3 uses one reconfig, not PHY2's two).
DemoLevels MakeDemoLevelsPhy3(int audioSamplesPerChannel)
{
    const U16 dev = 0x001;
    const U8  group = 0x01, rowDelay = 14;
    const int kPcmSS = 15;         // 16-bit PCM
    const int kPdmSS = 0;          // 1-bit PDM
    const int kCdsH  = 2;          // CDS at Column 2 (Safe-Lock-4 default; §12.1.10.1)
    const int kSlCols = 4;         // Safe-Lock-4
    const int kOpCols = 16;        // operational geometry
    // 16-col operational geometry runs at row rate 3.072 MRows/s (the PLL reference held
    // constant; UI rate 49.152 MHz). To keep 48 kHz PCM / 3.072 MHz PDM at that row rate:
    // PCM Interval 63 (3072/64 = 48 kHz); PDM SampleGrouping=2 + Interval 1 (2 samples /
    // 2 rows -> 3.072 MHz) — the same audio-rate registers as PHY2's 8-col phase. Payload
    // columns 4..13 (Sync1=0, handover=1, CDS=2, handover=3, ..., handover=14, Sync0=15).
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
    for (const PortGeom& p : geom) emitPortWrites(bits, regs, dev, p);
    for (int dp : {0, 2})                                   // DP0/DP2 run UNSCRAMBLED
        emitWrite(bits, regs, dev,
                  swi3s::reg::kDpBase + (U32)swi3s::reg::kDpStride * (U32)dp
                      + swi3s::reg::kDpScramDirMode, {0x00});
    appendPing(bits);
    long ssp1Bit = (long)appendSscr(bits, dev, group, rowDelay);
    regs.Commit(group);
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
            } else if (c == kCdsH) {
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