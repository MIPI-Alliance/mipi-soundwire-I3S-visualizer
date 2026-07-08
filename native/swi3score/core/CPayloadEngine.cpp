// Audio payload reconstruction engine. See CPayloadEngine.h.

#include "CPayloadEngine.h"

#include <cstdlib>

void CPayloadEngine::Configure(const SwI3sConfig& cfg)
{
    mCfg = cfg;
    mDps.clear();
    // Peripheral-SOURCED data arrives ~1 UI late on the manager's column grid:
    // the peripheral receives the forwarded clock, then drives its data back with a
    // clock-to-data + round-trip propagation delay. As a sniffer we therefore sample
    // a source port's data ONE COLUMN LATER than its register HorizontalStart.
    // Measured = 1 UI (recovers clean tones on a verified IV/PDM capture; 0 leaves
    // sources as noise, 2 over-reads). Sinks (manager-driven) are aligned -> shift 0.
    // SWI3S_SRC_SHIFT overrides the measured default for re-tuning on a very
    // different bus rate / trace length.
    const char* sh = std::getenv("SWI3S_SRC_SHIFT");
    mSourceShift = sh ? std::atoi(sh) : kDefaultSourceShift;

    // Debug dump of assembled data bits for one DP (see header).
    mDumpCount = 0;
    if (!mDumpFile) {
        const char* dpath = std::getenv("SWI3S_DUMP_PATH");
        if (dpath) {
            mDumpFile = std::fopen(dpath, "w");
            const char* dd = std::getenv("SWI3S_DUMP_DP");  mDumpDp = dd ? std::atoi(dd) : -1;
            const char* dm = std::getenv("SWI3S_DUMP_MAX"); mDumpMax = dm ? std::atol(dm) : 200000;
        }
    }

    for (size_t i = 0; i < cfg.dps.size(); ++i) {
        const SwI3sDpConfig& dc = cfg.dps[i];
        if (!dc.Enabled || dc.numChannels() == 0) {
            continue;
        }
        DpRuntime rt;
        rt.dpIndex = (dc.dpNumber >= 0) ? dc.dpNumber : static_cast<int>(i);
        rt.deviceNum = dc.deviceNum;
        rt.scramblerEn = dc.ScramblerEn;
        rt.sampleSize = dc.SampleSize;
        rt.isSource = dc.isSource();
        rt.port.Configure(dc, cfg.columnCount(), cfg.SkippingDenominator);
        rt.port.Initialize(mStartColumn);
        rt.hasFcp = dc.drqEnabled();
        if (rt.hasFcp) {
            rt.fcp.Configure(dc, cfg.columnCount());
            rt.fcp.Initialize();
        }
        // ChannelRuntime[16] is default-constructed (each descrambler seeded); only
        // enabled channels are ever ticked, so disabled entries stay unused.
        mDps.push_back(std::move(rt));
    }
}

void CPayloadEngine::Reset()
{
    Configure(mCfg);
}

void CPayloadEngine::SyncToSSP()
{
    for (DpRuntime& rt : mDps) {
        rt.port.SyncToSSP();
        rt.pending.clear();        // re-anchoring drops in-flight (shifted) bits
        if (rt.hasFcp) {
            rt.fcp.Initialize();   // re-anchor the FCP row/interval to the SSP
        }
    }
}

void CPayloadEngine::Tick(bool level, U64 sampleNumber, std::vector<AudioSample>& out)
{
    // Assemble one decoded data/tx-present bit into its channel's running sample.
    // `lvl` is the physical level sampled for this bit; `sn` the capture sample.
    auto applyBit = [&](DpRuntime& rt, int channel, int bitInChannel, bool isData,
                        bool lvl, U64 sn) {
        if (channel < 0 || channel >= 16) {
            return;
        }
        ChannelRuntime& cs = rt.channels[channel];
        // Both DATA and TX_PRESENT bits pass through the channel descrambler (when
        // enabled) to keep the LFSR in step.
        bool recovered = rt.scramblerEn ? cs.descram.Descramble(lvl) : lvl;
        if (!isData) {
            return;  // TX_PRESENT advances the LFSR but is not audio
        }
        if (mCollectBits && mBitSamples.size() < kMaxBitSamples) {
            // Record where this data bit was sampled; the MSB (bitInChannel ==
            // sampleSize) begins a new sample interval.
            mBitSamples.push_back({sn, rt.deviceNum, rt.dpIndex, channel,
                                   bitInChannel == rt.sampleSize});
        }
        if (mDumpFile && (mDumpDp < 0 || rt.dpIndex == mDumpDp) && mDumpCount < mDumpMax) {
            std::fprintf(mDumpFile, "%llu %d %d %d %d\n",
                         (unsigned long long)sn, rt.dpIndex, channel, bitInChannel, recovered ? 1 : 0);
            ++mDumpCount;
        }
        if (bitInChannel == rt.sampleSize) {  // MSB: start a new sample
            cs.acc = 0;
            cs.startSample = sn;
        }
        cs.acc |= (static_cast<U64>(recovered ? 1 : 0) << bitInChannel);
        if (bitInChannel == 0) {  // LSB: sample complete
            AudioSample s;
            s.dp = rt.dpIndex;
            s.deviceNum = rt.deviceNum;
            s.channel = channel;
            s.value = cs.acc;
            s.sampleSize = rt.sampleSize + 1;
            s.index = cs.index++;
            s.startSample = cs.startSample;
            s.endSample = sn;
            out.push_back(s);
        }
    };

    for (DpRuntime& rt : mDps) {
        // NOTE: the Flow Control Port's DRQ output was annotation-only and never
        // consumed by any caller, so it is no longer ticked here — dropping a whole
        // per-DP FCP clock_tick cascade per UI on RxControlled/Async captures.
        DpEmit e = rt.port.clock_tick();

        bool isBit = e.sampleHere &&
                     (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent);
        bool isData = (e.slot == SwI3sSlot::Data);

        if (mSourceShift <= 0 || !rt.isSource) {
            // Fast path (the default: no diagnostic source shift): sample this bit
            // with this UI's level.
            if (isBit) {
                applyBit(rt, e.channel, e.bitInChannel, isData, level, sampleNumber);
            }
            continue;
        }
        // DIAGNOSTIC source shift (SWI3S_SRC_SHIFT env, off by default): defer the
        // level<->bit association by N UIs, so a bit located at column C is sampled
        // with the level N UIs later (probing the peripheral's clock-to-data
        // round-trip skew). Push every UI so the delay is measured in UIs, not in
        // sample events.
        rt.pending.push_back({isBit, e.channel, e.bitInChannel, isData});
        if (static_cast<int>(rt.pending.size()) > mSourceShift) {
            DpRuntime::PendingBit p = rt.pending.front();
            rt.pending.pop_front();
            if (p.has) {
                applyBit(rt, p.channel, p.bitInChannel, p.isData, level, sampleNumber);
            }
        }
    }
}
