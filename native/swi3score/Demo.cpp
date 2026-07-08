// Synthetic SWI3S PHY2 demo stream. See Demo.h. The plan-building logic mirrors
// SwI3sSimulationDataGenerator (and test/test_simulation_roundtrip.cpp), reusing
// the core's 8b/10b encoder, CRC, register model, placement cascade and
// scrambler so the produced stream decodes back to the generated sine.

#include "Demo.h"

#include <cmath>
#include <map>

#include "C8b10bDecoder.h"
#include "CCrc16.h"
#include "CCommandTransportParser.h"
#include "CRegisterModel.h"
#include "CDataPort.h"
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

void appendPing(std::vector<bool>& bits)
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
        int tok = (i == 0) ? 0 : (i == 1) ? 8 : 0;
        appendSymbol(bits, C8b10bDecoder::EncodeToken(tok));
    }
    appendSymbol(bits, 0x155);
}

size_t appendSscr(std::vector<bool>& bits, U16 devMask, U8 group, U8 rowDelay)
{
    appendSymbol(bits, C8b10bDecoder::CommaSymbol());
    int hdr[6] = { swi3s::kPhaseCommit, (devMask>>8)&0xF, (devMask>>4)&0xF, devMask&0xF, 0, 3 };
    for (int t : hdr) appendSymbol(bits, C8b10bDecoder::EncodeToken(t));
    U8 pkt[3] = { swi3s::kOpSscr, group, rowDelay };
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

U64 sineSample(int channel, U64 index, int sampleSize)
{
    double amp = (double)(1 << sampleSize) * 0.45;
    double w = 2.0 * kPi * (double)(channel + 1) * (double)index / 64.0;
    int v = (int)std::lround(amp * std::sin(w));
    U64 mask = (1ull << (sampleSize + 1)) - 1;
    return (U64)v & mask;
}

} // namespace

std::vector<bool> MakeDemoLevels(int audioSamplesPerChannel)
{
    // Two devices, two stereo data ports each (4 audio streams). A 16-column
    // grid (NumColumns=15): Column 0 = CDS, each DP gets a disjoint 2-column
    // band (1-2, 3-4, 5-6, 7-8). With Interval=31 (32 rows/interval) the audio
    // sample rate is RowRate/32 = (24.576 MHz / 16) / 32 = 48 kHz.
    const int kInterval = 31;
    struct W { U16 dev; U32 addr; std::vector<U8> bytes; };
    std::vector<W> writes;
    auto dpWrites = [&](U16 dev, int n, int hstart, int interval) {
        U32 base = 0x2000u + (U32)n * 0x100u;
        writes.push_back({dev, base + 0x09, {0x07, 0x01, 0x08, 0x00, 0x00, 0x00}});
        writes.push_back({dev, base + 0x80,
                          {(U8)hstart, (U8)(0x80 | 1), 0x00,
                           (U8)(interval & 0xF), (U8)(interval >> 4), 0x00}});
        writes.push_back({dev, base + 0x87, {0x00}});         // ChannelGrouping
        writes.push_back({dev, base + 0x90, {0x02}});         // EnableCh1
    };
    writes.push_back({0x001, 0x1081, {0x0F}});                // dev0 NumColumns=15 -> 16
    dpWrites(0x001, 0, 1, kInterval);                         // dev0 DP0: cols 1-2
    dpWrites(0x001, 1, 3, kInterval);                         // dev0 DP1: cols 3-4
    writes.push_back({0x002, 0x1081, {0x0F}});                // dev1 NumColumns=15 -> 16
    dpWrites(0x002, 0, 5, kInterval);                         // dev1 DP0: cols 5-6
    dpWrites(0x002, 1, 7, kInterval);                         // dev1 DP1: cols 7-8

    const U16 commitMask = 0x003;                             // commit both devices
    const U8 group = 0x01, rowDelay = 14;

    CRegisterModel regs;
    std::vector<bool> bits;
    for (int i = 0; i < 8; ++i) appendSymbol(bits, 0x155);
    appendPing(bits);
    for (const W& w : writes) {
        appendWriteA32(bits, w.dev, w.addr, w.bytes.data(), (int)w.bytes.size());
        SwI3sCommand c; c.clear();
        c.phase = swi3s::kPhaseWrite; c.opcode = swi3s::kOpWriteA32;
        c.deviceMask = w.dev; c.hasAddress = true; c.address = w.addr; c.data = w.bytes;
        regs.OnCommand(c);
    }
    appendPing(bits);
    size_t sscrLastBit = appendSscr(bits, commitMask, group, rowDelay);
    regs.Commit(group);

    long sspRow = (long)sscrLastBit + 1 + (int)rowDelay;

    SwI3sConfig cfg;
    regs.BuildConfig(cfg);
    int columnCount = regs.ColumnCount();
    if (columnCount < swi3s::kMinColumnCount) columnCount = swi3s::kColdStartColumnCount;
    cfg.NumColumns = columnCount - 1;
    if (cfg.SkippingDenominator < 1) cfg.SkippingDenominator = 1;

    // Enabled data ports in BuildConfig order (= the decoder's payload order).
    std::vector<const SwI3sDpConfig*> dps;
    for (const SwI3sDpConfig& d : cfg.dps)
        if (d.Enabled && d.numChannels() > 0) dps.push_back(&d);
    int N = (int)dps.size();

    int sampleSize = N ? dps[0]->SampleSize : 7;
    int rowsPerInterval = N ? (dps[0]->Interval + 1) : 8;
    long audioRows = (long)audioSamplesPerChannel * rowsPerInterval;
    long totalRows = sspRow + audioRows;

    // A distinct sine per (device, dp, channel) so each stream looks different.
    auto tone = [&](int p, int ch) {
        return (dps[p]->deviceNum * 2 + dps[p]->dpNumber) * 2 + ch;
    };
    auto key = [](int p, int ch) { return p * 32 + ch; };

    std::vector<CDataPort> ports(N);
    std::map<int, CDescrambler> scram;
    std::map<int, U64> curSample, sampleIndex;
    bool portReady = false;

    std::vector<bool> levels;
    levels.reserve((size_t)totalRows * columnCount);
    bool cur = false;

    for (long r = 0; r < totalRows; ++r) {
        if (N && r == sspRow) {
            scram.clear();
            for (int p = 0; p < N; ++p) {
                ports[p].Configure(*dps[p], columnCount, cfg.SkippingDenominator);
                ports[p].Initialize();
                ports[p].SyncToSSP();
                for (int ch = 0; ch < 16; ++ch)
                    if (dps[p]->EnableCh & (1u << ch)) scram[key(p, ch)] = CDescrambler();
            }
            portReady = true;
        }
        for (int c = 0; c < columnCount; ++c) {
            // Tick EVERY port each UI (incl. Column 0) to keep them in lockstep.
            DpEmit es[64];
            if (portReady)
                for (int p = 0; p < N; ++p) es[p] = ports[p].clock_tick();

            bool level;
            if (c == swi3s::kCdsColumn) {
                bool bit = (r < (long)bits.size()) ? (bool)bits[r] : true;
                level = bit ? cur : !cur;
            } else {
                level = cur;                            // idle unless a port owns this UI
                if (portReady) {
                    for (int p = 0; p < N; ++p) {
                        const DpEmit& e = es[p];
                        if (e.sampleHere &&
                            (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent)) {
                            int ch = e.channel, b = e.bitInChannel, k = key(p, ch);
                            bool clear;
                            if (e.slot == SwI3sSlot::Data) {
                                if (b == sampleSize)
                                    curSample[k] = sineSample(tone(p, ch),
                                                              sampleIndex[k]++, sampleSize);
                                clear = ((curSample[k] >> b) & 1) != 0;
                            } else {
                                clear = false;
                            }
                            level = scram[k].Scramble(clear);
                            break;                       // disjoint windows: one owner
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

} // namespace swi3score
