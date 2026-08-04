// Register snooping + config reconstruction. See CRegisterModel.h.

#include "CRegisterModel.h"
#include "CCommandTransportParser.h"
#include "SwI3sProtocolDefs.h"

using namespace swi3s;

void CRegisterModel::Reset()
{
    mDev.clear();
}

// Normalise a written address to its _NEXT-base form: a write to a _CURR
// register (offset >= 0x40 within the dual-ranked window) maps onto the same
// base so reads are uniform. We treat 0x40-sized dual-rank windows at SLC
// 0x1080+ and DP _NEXT 0x80+.
static U32 toBase(U32 addr)
{
    // SLC dual-ranked: 0x1080.._0x10BF (_NEXT), 0x10C0..0x10FF (_CURR).
    if (addr >= reg::kSlcBase + 0xC0 && addr < reg::kSlcBase + 0x100) {
        return addr - reg::kCurrRankOffset;
    }
    // DP dual-ranked: within each 256-byte block, _NEXT 0x80..0xBF, _CURR 0xC0..0xFF.
    if (reg::InDpBlock(addr)) {
        U32 off = reg::DpOffsetOf(addr);
        if (off >= 0xC0) return addr - reg::kCurrRankOffset;
    }
    return addr;
}

static bool isDualRanked(U32 baseAddr)
{
    if (baseAddr >= reg::kSlcBase + 0x80 && baseAddr < reg::kSlcBase + 0xC0) return true;
    if (reg::InDpBlock(baseAddr)) {
        U32 off = reg::DpOffsetOf(baseAddr);
        return off >= 0x80 && off < 0xC0;
    }
    return false;
}

void CRegisterModel::applyWrite(int dev, U32 addr, U8 value)
{
    DeviceRegs& d = mDev[dev];
    U32 base = toBase(addr);
    bool currAlias = (base != addr);        // toBase mapped a _CURR alias down to its base
    if (isDualRanked(base) && !currAlias) {
        d.next[base] = value;           // _NEXT write: stage; needs Commit
        d.nextSrc[base] = SrcWritten;
    } else if (isDualRanked(base)) {
        // Direct write to a dual-ranked _CURR alias (the Current Value Register):
        // takes effect IMMEDIATELY with no Commit, and per spec (SWI3S v1.1 Table
        // 165 / {ASW5612}, Dual-Curr-is-RW) the same value is mirrored into the
        // _NEXT register (NVR == CVR). BuildConfig reads _CURR, so this changes the
        // decoded config right away — matching WriteIsImmediate()/ForceOverride(),
        // which already treat a _CURR alias as immediate. (EnableCh_CURR is the
        // canonical example; a bare WriteA32 only actually starts a stream when the
        // port's Interval == 1 Row — see the EnableCh_CURR error rule in Decoder.)
        d.cur[base] = value;
        d.curSrc[base] = SrcWritten;
        d.next[base] = value;
        d.nextSrc[base] = SrcWritten;
    } else {
        d.cur[base] = value;            // single-ranked / direct: immediate
        d.curSrc[base] = SrcWritten;
    }
}

bool CRegisterModel::WriteIsImmediate(U32 addr) const
{
    // Mirrors applyWrite: a write to a dual-ranked base stages in _NEXT (needs Commit);
    // anything else lands in _CURR the moment it's written (immediate committed effect).
    return !isDualRanked(toBase(addr));
}

void CRegisterModel::ForceOverride(int dev, U32 addr, U8 value)
{
    // Force ONLY the rank the caller addressed: a _NEXT-base address stages _NEXT, a
    // _CURR alias (or a single-ranked register) sets _CURR. It does NOT touch the other
    // rank and does NOT commit — so editing _NEXT never copies to _CURR (edit _CURR
    // directly for that), and no other register is promoted. BuildConfig reads _CURR, so
    // only a _CURR override changes the decoded config/audio/grid.
    DeviceRegs& d = mDev[dev];
    U32 base = toBase(addr);
    bool currAlias = (base != addr);        // toBase mapped a _CURR alias down to its base
    if (isDualRanked(base) && !currAlias) {
        d.next[base] = value;
        d.nextSrc[base] = SrcWritten;
    } else {
        d.cur[base] = value;
        d.curSrc[base] = SrcWritten;
    }
}

void CRegisterModel::ApplyRead(int dev, U32 addr, U8 value)
{
    // A bus read reveals the peripheral's live value; record it tagged READ, in the
    // bank the register-map display reads from. Rank-aware and mirroring the Python
    // display overlay: a _CURR-alias read updates the committed bank; a _NEXT-base
    // (dual) or single-ranked read updates the live display bank. Reads apply now —
    // they don't wait for a commit — and never feed BuildConfig/get (config = writes).
    DeviceRegs& d = mDev[dev];
    U32 base = toBase(addr);
    bool currAlias = (base != addr);    // toBase mapped a _CURR alias down to its base
    if (currAlias) {
        d.cur[base] = value;
        d.curSrc[base] = SrcRead;
    } else if (isDualRanked(base)) {
        d.next[base] = value;           // dual _NEXT read -> live display bank
        d.nextSrc[base] = SrcRead;
    } else {
        d.cur[base] = value;            // single-ranked read -> its one value
        d.curSrc[base] = SrcRead;
    }
}

void CRegisterModel::commitDevice(int dev, U8 groupMask)
{
    auto it = mDev.find(dev);
    if (it == mDev.end()) return;
    DeviceRegs& d = it->second;

    // A data port commits only if its CommitGroupMemb intersects the mask;
    // SLC/geometry registers are Commit Group 0. (Section 9.1.10.4)
    // CommitGroupMemb is single-ranked (offset 0x0A), reset 0b0001 = CG0.
    auto inGroup = [&](U32 baseAddr) -> bool {
        if (reg::InDpBlock(baseAddr)) {
            int n = reg::DpIndexOf(baseAddr);
            auto m = d.cur.find(reg::DpAddr(n, reg::kDpCommitGroupMemb));
            U8 memb = (m != d.cur.end()) ? m->second
                                         : resetOf(reg::DpAddr(n, reg::kDpCommitGroupMemb));
            return (memb & groupMask) != 0;
        }
        // SLC dual-ranked (geometry, row rate, spacer) = CG0.
        return (groupMask & 0x1) != 0;
    };

    for (auto kv = d.next.begin(); kv != d.next.end(); ) {
        if (inGroup(kv->first)) {
            d.cur[kv->first] = kv->second;
            auto s = d.nextSrc.find(kv->first);           // carry provenance across commit
            d.curSrc[kv->first] = (s != d.nextSrc.end()) ? s->second : U8(SrcWritten);
            if (s != d.nextSrc.end()) d.nextSrc.erase(s);
            kv = d.next.erase(kv);
        } else {
            ++kv;
        }
    }
}

void CRegisterModel::OnCommand(const SwI3sCommand& cmd)
{
    // WriteA32 only STAGES values (single-ranked apply immediately inside
    // applyWrite; dual-ranked wait for Commit() at the SSP). A Write must
    // target exactly one device; ignore a malformed multicast write.
    if (cmd.phase == kPhaseWrite && cmd.opcode == kOpWriteA32 && cmd.hasAddress) {
        if (!cmd.DeviceMaskValid()) {
            return;
        }
        U32 addr = cmd.address;
        for (U8 value : cmd.data) {
            for (int dev = 0; dev < kMaxPeripherals; ++dev) {
                if (cmd.deviceMask & (1u << dev)) {
                    applyWrite(dev, addr, value);
                }
            }
            ++addr;
        }
    }
    // NOTE: Commit promotion is deferred to Commit() at the SSP row, called by
    // the WorkerThread only for a CONFIRM_COMMIT'd SSCR.
}

void CRegisterModel::Commit(U8 groupMask)
{
    for (auto& kv : mDev) {
        commitDevice(kv.first, groupMask);
    }
}

U8 CRegisterModel::resetOf(U32 baseAddr)
{
    // Per-register spec reset for the decode path. The DP-block registers with a
    // non-zero reset (mirrors data/registers.json; verified by test_register_resets):
    if (reg::InDpBlock(baseAddr)) {
        switch (reg::DpOffsetOf(baseAddr)) {
            case reg::kDpCommitGroupMemb: return 0x01;  // CommitGroupMemb -> CG0
            case reg::kDpScramDirMode:    return 0x08;  // PortControl.ScramblerEn = 1
            case reg::kDpFlowMode:        return 0x88;  // FlowControl: FlowControlDelay=1, FCP_ScramblerEn=1
            default:                      return 0x00;
        }
    }
    // SLC/CDS/PHY registers the decode reads all reset to 0; the PHY electrical
    // resets are display-only (served from data/registers.json), not read here.
    return 0x00;
}

U8 CRegisterModel::get(int dev, U32 baseAddr) const
{
    auto it = mDev.find(dev);
    if (it == mDev.end()) return resetOf(baseAddr);
    auto r = it->second.cur.find(baseAddr);
    return (r != it->second.cur.end()) ? r->second : resetOf(baseAddr);
}

std::vector<std::tuple<int, U32, U8>> CRegisterModel::CommittedSnapshot() const
{
    std::vector<std::tuple<int, U32, U8>> out;
    for (const auto& dk : mDev)
        for (const auto& rk : dk.second.cur)
            out.emplace_back(dk.first, rk.first, rk.second);
    return out;
}

std::vector<std::tuple<int, U32, U8, bool, U8, U8, bool, U8>> CRegisterModel::Snapshot() const
{
    std::vector<std::tuple<int, U32, U8, bool, U8, U8, bool, U8>> out;
    for (const auto& dk : mDev) {
        const DeviceRegs& d = dk.second;
        // Union of committed + staged addresses (cur/next are sorted maps).
        std::map<U32, char> addrs;   // value unused; a set keyed by address
        for (const auto& rk : d.cur)  addrs[rk.first] = 1;
        for (const auto& rk : d.next) addrs[rk.first] = 1;
        auto srcOf = [](const std::map<U32, U8>& m, U32 a) -> U8 {
            auto it = m.find(a);
            return (it != m.end()) ? it->second : U8(SrcWritten);   // present w/o tag => written
        };
        for (const auto& ak : addrs) {
            U32 a = ak.first;
            auto ci = d.cur.find(a);
            auto ni = d.next.find(a);
            bool hasCur = ci != d.cur.end();
            bool hasNext = ni != d.next.end();
            out.emplace_back(dk.first, a,
                             hasCur ? ci->second : U8(0), hasCur, hasCur ? srcOf(d.curSrc, a) : U8(SrcDefault),
                             hasNext ? ni->second : U8(0), hasNext, hasNext ? srcOf(d.nextSrc, a) : U8(SrcDefault));
        }
    }
    return out;
}

bool CRegisterModel::has(int dev, U32 baseAddr) const
{
    auto it = mDev.find(dev);
    if (it == mDev.end()) return false;
    return it->second.cur.find(baseAddr) != it->second.cur.end();
}

int CRegisterModel::SyncPointOffset(int deviceNum) const
{
    if (has(deviceNum, reg::kSyncPointOffset)) {
        return get(deviceNum, reg::kSyncPointOffset) & 0x7;
    }
    return kSspDefaultSyncPointOffset;
}

int CRegisterModel::ColumnCount() const
{
    int rep = representativeDevice();
    if (rep < 0 || !has(rep, reg::kNumColumns_Next)) return 0;
    return (get(rep, reg::kNumColumns_Next) & 0x1F) + 1;
}

int CRegisterModel::CurrentInterval(int dev, int dp) const
{
    U8 oilo = get(dev, reg::DpAddr(dp, reg::kDpOffIntLo_N));
    return (oilo & 0xF) | (get(dev, reg::DpAddr(dp, reg::kDpIntHi_N)) << 4);
}

bool CRegisterModel::ShortProtocolSpacer() const
{
    int rep = representativeDevice();
    if (rep < 0) return false;
    return (get(rep, reg::kShortSpacer_Next) & 1) != 0;
}

SwI3sDpConfig CRegisterModel::decodeDp(int dev, int n) const
{
    SwI3sDpConfig c;
    c.deviceNum = dev;
    c.dpNumber = n;

    U8 sg_ss = get(dev, reg::DpAddr(n, reg::kDpSampleSizeGrouping));
    c.SampleSize = sg_ss & 0x1F;
    c.SampleGrouping = (sg_ss >> 5) & 0x7;

    // PortControl (ScramDirMode): un-written, ScramblerEn reads its reset (1, see
    // resetOf) so the descrambler runs by default — matching the spec reset.
    U8 sdm = get(dev, reg::DpAddr(n, reg::kDpScramDirMode));
    c.ScramblerEn = (sdm >> 3) & 1;
    c.PortDirection = (sdm >> 2) & 1;
    c.PortMode = sdm & 0x3;

    c.SkippingNumerator = get(dev, reg::DpAddr(n, reg::kDpSkipNumLo)) |
                          ((get(dev, reg::DpAddr(n, reg::kDpSkipNumHi)) & 0xF) << 8);
    c.FlowMode = get(dev, reg::DpAddr(n, reg::kDpFlowMode)) & 0x3;
    c.FlowControlDelay = (get(dev, reg::DpAddr(n, reg::kDpFlowMode)) >> 7) & 0x1;  // 0x0E bit7

    U8 bw_hs = get(dev, reg::DpAddr(n, reg::kDpBitWidthHStart_N));
    c.BitWidth = (bw_hs >> 6) & 0x3;
    c.HorizontalStart = bw_hs & 0x1F;

    U8 en_hc = get(dev, reg::DpAddr(n, reg::kDpEnableCh0HCount_N));
    c.HorizontalCount = en_hc & 0x1F;
    U16 enable = 0;
    if (en_hc & 0x80) enable |= (1u << 0);                       // EnableCh0
    enable |= (U16)((get(dev, reg::DpAddr(n, reg::kDpEnableCh1_7_N)) >> 1) & 0x7F) << 1;  // ch1..7
    enable |= (U16)get(dev, reg::DpAddr(n, reg::kDpEnableCh8_15_N)) << 8;                  // ch8..15
    c.EnableCh = enable;

    U8 tsi = get(dev, reg::DpAddr(n, reg::kDpTailSubSpacing_N));
    c.TailWidth = (tsi >> 6) & 0x3;
    c.SubRowInterval = (tsi >> 5) & 1;
    c.Spacing = tsi & 0xF;

    U8 oilo = get(dev, reg::DpAddr(n, reg::kDpOffIntLo_N));
    c.Interval = (oilo & 0xF) | (get(dev, reg::DpAddr(n, reg::kDpIntHi_N)) << 4);
    c.Offset = ((oilo >> 4) & 0xF) | (get(dev, reg::DpAddr(n, reg::kDpOffHi_N)) << 4);

    c.ChannelGrouping = get(dev, reg::DpAddr(n, reg::kDpChannelGrouping_N)) & 0xF;

    U8 guard = get(dev, reg::DpAddr(n, reg::kDpGuard_N));
    c.GuardEnable = (guard >> 1) & 1;
    c.GuardPolarity = guard & 1;

    U8 fbw = get(dev, reg::DpAddr(n, reg::kDpFcpBwHStart_N));
    c.FCP_BitWidth = (fbw >> 6) & 0x3;
    c.FCP_HorizontalStart = fbw & 0x1F;
    c.FCP_TailWidth = (get(dev, reg::DpAddr(n, reg::kDpFcpTail_N)) >> 6) & 0x3;
    c.FCP_Offset = ((get(dev, reg::DpAddr(n, reg::kDpFcpOffLo_N)) >> 4) & 0xF) |
                   (get(dev, reg::DpAddr(n, reg::kDpFcpOffHi_N)) << 4);
    U8 fg = get(dev, reg::DpAddr(n, reg::kDpFcpGuard_N));
    c.FCP_GuardEnable = (fg >> 1) & 1;
    c.FCP_GuardPolarity = fg & 1;

    // A dataport is considered enabled once any channel is enabled.
    c.Enabled = (c.EnableCh != 0);
    return c;
}

int CRegisterModel::representativeDevice() const
{
    // Pick the device that has programmed bus geometry (NumColumns), else the
    // first device we have any state for.
    for (const auto& kv : mDev) {
        if (kv.second.cur.count(reg::kNumColumns_Next)) return kv.first;
    }
    return mDev.empty() ? -1 : mDev.begin()->first;
}

bool CRegisterModel::BuildConfig(SwI3sConfig& out) const
{
    int rep = representativeDevice();
    if (rep < 0) return false;

    // Bus geometry (per-device but identical across the link; read from rep).
    if (has(rep, reg::kNumColumns_Next)) {
        out.NumColumns = get(rep, reg::kNumColumns_Next) & 0x1F;
    }
    if (has(rep, reg::kSkippingDenomLo) || has(rep, reg::kSkippingDenomHi)) {
        out.SkippingDenominator = get(rep, reg::kSkippingDenomLo) |
                                  ((get(rep, reg::kSkippingDenomHi) & 0xF) << 8);
        if (out.SkippingDenominator == 0) out.SkippingDenominator = 1;
    }

    // Every enabled dataport across every device. Start empty and append only the
    // enabled ports — do NOT pre-fill kMaxPeripherals disabled placeholders, which
    // padded config_dataports() with phantom entries and skewed the positional
    // fallback index by kMaxPeripherals.
    out.dps.clear();
    bool any = false;
    for (const auto& kv : mDev) {
        int dev = kv.first;
        for (int n = 0; n < reg::kMaxDataPorts; ++n) {
            SwI3sDpConfig c = decodeDp(dev, n);
            if (c.Enabled && c.numChannels() > 0) {
                out.dps.push_back(c);   // appended; index is positional only
                any = true;
            }
        }
    }
    return any;
}
