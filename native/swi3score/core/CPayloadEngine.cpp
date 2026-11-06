// Audio payload reconstruction engine. See CPayloadEngine.h.

#include "CPayloadEngine.h"

#include <algorithm>
#include <cstdint>
#include <cstdlib>

void CPayloadEngine::Configure(const SwI3sConfig& cfg, bool keepDrqHistory)
{
    mCfg = cfg;
    // The ports in force before this call: a (device,dp) port that stays enabled keeps its
    // running sample indices and surviving scramblers (carryChannels), and at a commit its
    // DRQ pipeline (carryDrq); its transport phase is rebuilt and re-anchored.
    std::vector<DpRuntime> previous = std::move(mDps);
    // The running sample index belongs to the (device, dp, channel) and continues across
    // a re-anchor. A channel's LFSR is seeded when the channel is enabled or ScramblerEn
    // turns on, so a channel enabled before and after, with ScramblerEn set both times,
    // keeps its descrambler; the FCP's DRQ descrambler continues the same way under
    // FCP_ScramblerEn.
    // The sample being assembled is rebuilt with its port.
    auto carryChannels = [](const DpRuntime& from, DpRuntime& to) {
        const bool scrambled = from.scramblerEn && to.scramblerEn;
        for (int ch = 0; ch < 16; ++ch) {
            to.channels[ch].index = from.channels[ch].index;
            const bool survives = (from.cfg.EnableCh & to.cfg.EnableCh & (1u << ch)) != 0;
            if (scrambled && survives) to.channels[ch].descram = from.channels[ch].descram;
        }
        if (from.hasFcp && to.hasFcp && from.cfg.FCP_ScramblerEn && to.cfg.FCP_ScramblerEn)
            to.fcpDescram = from.fcpDescram;
    };
    // The DRQ pipeline of a port that has an FCP before and after the commit (see the
    // header): the history and the TxPresent interval count carry when the old port is at
    // the start of an interval, the rule SyncToSSP applies to an SSP.
    auto carryDrq = [](const DpRuntime& from, DpRuntime& to) {
        if (!from.hasFcp || !to.hasFcp) return;
        to.lastDrqSample = from.lastDrqSample;
        if (from.port.SaveState().rowInInterval != 0) return;
        to.drqHistory = from.drqHistory;
        to.drqBase = from.drqBase;
        to.drqCount = from.drqCount;
        to.txpInterval = from.txpInterval;
    };
    mDps.clear();
    mFcStats = FlowControlStats{};   // fresh handshake accounting per (re)configure
    // Source-port sample shift (UIs): a peripheral-SOURCED bit is read with the level
    // this many UIs later, for the peripheral's clock-to-data + round-trip propagation
    // delay on the manager's column grid. Sinks (manager-driven) are never shifted.
    // Default kDefaultSourceShift; SWI3S_SRC_SHIFT overrides it.
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
        DpRuntime rt = buildRuntime(dc, static_cast<int>(i));
        auto it = std::find_if(previous.begin(), previous.end(), [&](const DpRuntime& old) {
            return old.deviceNum == rt.deviceNum && old.dpIndex == rt.dpIndex;
        });
        if (it != previous.end()) {
            carryChannels(*it, rt);
            if (keepDrqHistory) carryDrq(*it, rt);
        }
        mDps.push_back(std::move(rt));
    }
}

CPayloadEngine::DpRuntime CPayloadEngine::buildRuntime(const SwI3sDpConfig& dc, int idx) const
{
    DpRuntime rt;
    rt.dpIndex = (dc.dpNumber >= 0) ? dc.dpNumber : idx;
    rt.deviceNum = dc.deviceNum;
    rt.scramblerEn = dc.ScramblerEn;
    rt.cfg = dc;
    rt.isSource = dc.isSource();
    rt.port.Configure(dc, mCfg.columnCount(), mCfg.SkippingDenominator);
    rt.port.Initialize(mStartColumn);
    rt.hasFcp = dc.drqEnabled();
    if (rt.hasFcp) {
        // The FCP shares the data port's bus column and interval phase.
        rt.fcp.Configure(dc, mCfg.columnCount());
        rt.fcp.Initialize(rt.port.Column(), rt.port.RowInInterval());
    }
    // ChannelRuntime[16] is default-constructed (each descrambler seeded); only
    // enabled channels are ever ticked, so disabled entries stay unused.
    return rt;
}

void CPayloadEngine::Reconfigure(const SwI3sConfig& cfg)
{
    mCfg = cfg;
    // Carry surviving (device,dp) ports over UNTOUCHED (interval phase + descrambler
    // state preserved — a DSCR commits config but must not re-anchor the SSP); drop ports
    // no longer enabled; build newly enabled ports fresh. Order follows cfg.dps (the
    // BuildConfig / payload order), the same order Configure would produce.
    std::vector<DpRuntime> next;
    next.reserve(cfg.dps.size());
    for (size_t i = 0; i < cfg.dps.size(); ++i) {
        const SwI3sDpConfig& dc = cfg.dps[i];
        if (!dc.Enabled || dc.numChannels() == 0) continue;
        int dpIdx = (dc.dpNumber >= 0) ? dc.dpNumber : static_cast<int>(i);
        auto it = std::find_if(mDps.begin(), mDps.end(), [&](const DpRuntime& rt) {
            return rt.deviceNum == dc.deviceNum && rt.dpIndex == dpIdx;
        });
        if (it == mDps.end()) {
            next.push_back(buildRuntime(dc, static_cast<int>(i)));   // newly enabled
        } else if (it->cfg.sameTransport(dc)) {
            next.push_back(std::move(*it));      // unchanged survivor: keep runtime + phase
        } else {
            // Survivor whose config changed, by a DSCR commit (dual-ranked transport
            // fields) or by an immediate single-ranked write (SampleSize/SampleGrouping,
            // ScramblerEn/PortMode/PortDirection, FlowControl, Skipping registers).
            // The new config applies from this row without re-anchoring the port
            // (CDataPort::ApplyConfigKeepingPhase): only an SSP restarts the interval
            // ({ASW5103}), so the interval phase and skipping accumulator carry. SSCR and
            // DSCR generate the same Commit operation (§4.9.4), effective at the commit
            // point (§9.1.13). If this interval's transport has not begun, the new config
            // governs it, channel set included; otherwise the interval finishes on the
            // channel set it began with and the new one governs from the next interval.
            // Within a sample the working counters (current_bit_in_sample,
            // samples_remaining_in_sample_group) reload from the registers only at
            // sample/group/interval boundaries (§14.2.5.3/.4), so a sample in flight
            // finishes under the old SampleSize/SampleGrouping; the data port reports the
            // SampleSize each sample was loaded with (DpEmit::sampleSize).
            //
            // The runtime is updated in place, so the per-channel assembly (descrambler
            // LFSR, part-assembled value, start sample, running index, TX_PRESENT latch),
            // the source-shift queue and the DRQ history carry across the write. A channel
            // that the new config enables and the old one did not gets a freshly seeded
            // descrambler and an empty accumulator; its index continues. ScramblerEn
            // turning on seeds every channel's descrambler, and FCP_ScramblerEn turning on
            // seeds the FCP's; the rest of each channel's state carries.
            DpRuntime& rt = *it;
            const std::uint32_t newlyEnabled = dc.EnableCh & ~rt.cfg.EnableCh;
            const bool scramblerOn = dc.ScramblerEn && !rt.scramblerEn;
            for (int ch = 0; ch < 16; ++ch) {
                if (newlyEnabled & (1u << ch)) {
                    const U64 index = rt.channels[ch].index;
                    rt.channels[ch] = ChannelRuntime{};
                    rt.channels[ch].index = index;
                } else if (scramblerOn) {
                    rt.channels[ch].descram = CDescrambler{};
                }
            }
            const bool hadFcp = rt.hasFcp;
            const bool fcpScramblerOn = dc.FCP_ScramblerEn && !rt.cfg.FCP_ScramblerEn;
            rt.scramblerEn = dc.ScramblerEn;
            rt.cfg = dc;
            rt.isSource = dc.isSource();
            rt.port.ApplyConfigKeepingPhase(dc, mCfg.columnCount(), mCfg.SkippingDenominator);
            rt.hasFcp = dc.drqEnabled();
            if (rt.hasFcp) {
                // CFlowControlPort::Configure binds the config without touching the row /
                // column phase, so a port that already had an FCP keeps its DRQ placement.
                // A new FCP starts at the restored data port's column and interval row.
                rt.fcp.Configure(dc, mCfg.columnCount());
                if (!hadFcp) rt.fcp.Initialize(rt.port.Column(), rt.port.RowInInterval());
            }
            if (rt.hasFcp && (!hadFcp || fcpScramblerOn)) rt.fcpDescram = CDescrambler{};
            if (!rt.hasFcp || !hadFcp) {
                rt.drqHistory.clear();
                rt.drqBase = 0;
                rt.drqCount = 0;
                rt.txpInterval = 0;
            }
            if (!rt.hasFcp) rt.lastDrqSample = 0;
            next.push_back(std::move(rt));
        }
    }
    mDps = std::move(next);
}

void CPayloadEngine::Reset()
{
    mDps.clear();
    Configure(mCfg);
}

bool CPayloadEngine::SyncToSSP()
{
    bool midSkipCycle = false;
    for (DpRuntime& rt : mDps) {
        // Is this port ALREADY at its sync point? An SSPA re-asserts the phase the ports
        // are running at (it must land on a row where row_in_interval == 0, or it would
        // shift the transport and garble the audio), so for a correctly-placed one the
        // interval numbering does NOT change and the in-flight DRQ pipeline stays: a DRQ
        // sent d intervals ago still pairs with the TxPresent that transports after the
        // SSP. A re-anchor that DOES move the phase (SSCR to a new phase, a manual SSP
        // row, a misplaced SSPA) invalidates the pipeline, so clear it in that case.
        const bool alreadyAtSsp = (rt.port.SaveState().rowInInterval == 0);
        // A skipping port has a SECOND alignment to satisfy: its pattern repeats every
        // Interval x SkippingDenominator Rows, so a row that is interval-aligned can still
        // be mid-skip-cycle. Reported by the caller, and asked BEFORE the re-anchor clears
        // the accumulator (Section 9.1.6.2.1).
        if (!rt.port.SkipPatternAtCycleStart()) midSkipCycle = true;
        rt.port.SyncToSSP();
        rt.pending.clear();        // re-anchoring drops in-flight (shifted) bits
        if (rt.hasFcp) {
            // Re-anchor the FCP row/interval to the SSP, at the data port's column.
            rt.fcp.Initialize(rt.port.Column(), rt.port.RowInInterval());
            if (!alreadyAtSsp) {
                rt.drqHistory.clear(); // phase moved; startup DRQ=1 again
                rt.drqBase = 0;
                rt.drqCount = 0;
                rt.txpInterval = 0;
            }
        }
    }
    return midSkipCycle;
}

std::vector<CDataPort::State> CPayloadEngine::SaveState() const
{
    std::vector<CDataPort::State> out;
    out.reserve(mDps.size());
    for (const DpRuntime& rt : mDps) out.push_back(rt.port.SaveState());
    return out;
}

void CPayloadEngine::RestoreState(const SwI3sConfig& cfg, int startColumn,
                                  const std::vector<CDataPort::State>& ports)
{
    mStartColumn = startColumn;
    mDps.clear();
    Configure(cfg);   // rebuilds mDps for the enabled ports in the saved order
    for (std::size_t i = 0; i < mDps.size() && i < ports.size(); ++i)
        mDps[i].port.RestoreState(ports[i]);
}

void CPayloadEngine::TickScheduleOnly()
{
    for (DpRuntime& rt : mDps) {
        DpEmit e = rt.port.clock_tick();
        // Keep the diagnostic source-shift queue at the same depth Tick would leave it
        // (so a windowed decode with SWI3S_SRC_SHIFT set anchors identically). Values
        // are irrelevant here — nothing is collected during the fast-forward.
        if (mSourceShift > 0 && rt.isSource) {
            bool isBit = e.sampleHere &&
                         (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent);
            rt.pending.push_back({isBit, e.channel, e.bitInChannel, e.sampleSize,
                                  e.slot == SwI3sSlot::Data});
            if (static_cast<int>(rt.pending.size()) > mSourceShift) rt.pending.pop_front();
        }
    }
}

void CPayloadEngine::Tick(bool level, U64 sampleNumber, std::vector<AudioSample>& out)
{
    // Assemble one decoded data/tx-present bit into its channel's running sample.
    // `lvl` is the physical level sampled for this bit; `sn` the capture sample.
    // `sampleSize` is the SampleSize field (bits - 1) the bit's sample was loaded with.
    auto applyBit = [&](DpRuntime& rt, int channel, int bitInChannel, int sampleSize,
                        bool isData, bool lvl, U64 sn) {
        if (channel < 0 || channel >= 16) {
            return;
        }
        ChannelRuntime& cs = rt.channels[channel];
        // Both DATA and TX_PRESENT bits pass through the channel descrambler (when
        // enabled) to keep the LFSR in step.
        bool recovered = rt.scramblerEn ? cs.descram.Descramble(lvl) : lvl;
        if (!isData) {
            // TX_PRESENT: not audio, but in the flow-controlled modes its value
            // gates the sample that immediately follows on this channel. recovered
            // == 1 means the transmitter used this transport opportunity (data
            // valid); == 0 means the opportunity was skipped (no sample this
            // interval — the jitter we must remove at the receiver). Latch it for
            // the LSB below. NORMAL ports emit no TX_PRESENT bit, so txpValid stays
            // true and every sample is emitted. The LFSR was already advanced above.
            if (rt.cfg.txpEnabled()) {
                cs.txpValid = recovered;
            }
            return;  // TX_PRESENT advances the LFSR but is not audio
        }
        if (mCollectBits && sn >= mBitSampleStart && mBitSamples.size() < kMaxBitSamples) {
            // Record where this data bit was sampled; the MSB (bitInChannel ==
            // sampleSize) begins a new sample interval.
            mBitSamples.push_back({sn, rt.deviceNum, rt.dpIndex, channel,
                                   bitInChannel == sampleSize});
        }
        if (mDumpFile && (mDumpDp < 0 || rt.dpIndex == mDumpDp) && mDumpCount < mDumpMax) {
            std::fprintf(mDumpFile, "%llu %d %d %d %d\n",
                         (unsigned long long)sn, rt.dpIndex, channel, bitInChannel, recovered ? 1 : 0);
            ++mDumpCount;
        }
        if (bitInChannel == sampleSize) {  // MSB: start a new sample
            cs.acc = 0;
            cs.startSample = sn;
        }
        if (bitInChannel >= 0 && bitInChannel < 64)   // a shift past the accumulator is UB
            cs.acc |= (static_cast<U64>(recovered ? 1 : 0) << bitInChannel);
        if (bitInChannel == 0) {  // LSB: sample complete
            // Flow control: only EMIT the sample if its TX_PRESENT gated it valid.
            // An unused transport opportunity (TX_PRESENT == 0) is fully assembled
            // and descrambled (keeping the LFSR in step) but produces no audio, so
            // audio() is the de-jittered stream and `index` counts only real
            // samples. Re-arm txpValid for the next sample on this channel (the
            // next TX_PRESENT will re-latch it; NORMAL ports have none, so it must
            // stay true).
            bool emit = cs.txpValid;
            cs.txpValid = true;
            if (emit) {
                AudioSample s;
                s.dp = rt.dpIndex;
                s.deviceNum = rt.deviceNum;
                s.channel = channel;
                s.value = cs.acc;
                s.sampleSize = sampleSize + 1;
                s.index = cs.index++;
                s.startSample = cs.startSample;
                s.endSample = sn;
                s.flowMode = rt.cfg.FlowMode;
                // Annotate the sample with the capture position of the most recent DRQ
                // observed on this port (0 for non-DRQ modes) — ties a transported
                // sample to the handshake that permitted it, for the UI/inspection.
                s.drqSample = rt.lastDrqSample;
                out.push_back(s);
            }
        }
    };

    for (DpRuntime& rt : mDps) {
        DpEmit e = rt.port.clock_tick();

        // Flow control (RxControlled / Async): re-drive the FCP in lockstep so we can
        // read the DRQ bit off the bus and validate the DRQ<->TxPresent handshake
        // (SWI3S §14.2.2 {ASW5205}). The FCP for a SOURCE data port is a SINK DRQ
        // (the manager drives it; we sample it). Each DRQ is read at its bit's last UI
        // (FcpEmit::drqObservePoint), either direction, and recorded by its interval
        // NUMBER (drqCount), so the handshake lookback is correct regardless of whether
        // the DRQ column falls before or after the data window within an interval.
        if (rt.hasFcp) {
            FcpEmit fe = rt.fcp.clock_tick(rt.port.IntervalSkipped());
            if (fe.slot == SwI3sSlot::Drq && fe.drqObservePoint) {
                // The DRQ bit is recovered by the FCP's own descrambler when
                // FCP_ScramblerEn is set (one LFSR step per DRQ bit), else read raw.
                const bool drq = rt.cfg.FCP_ScramblerEn ? rt.fcpDescram.Descramble(level)
                                                        : level;
                rt.lastDrqSample = sampleNumber;
                ++mFcStats.drqBits;
                rt.drqHistory.push_back(drq ? 1 : 0);
                ++rt.drqCount;
                // Bound the window: only the last d+2 intervals are ever read back.
                int keep = rt.cfg.FlowControlDelay + 1 + 2;
                while (static_cast<int>(rt.drqHistory.size()) > keep) {
                    rt.drqHistory.pop_front();
                    ++rt.drqBase;
                }
            }
        }

        // Validate the handshake at the first TxPresent of a fresh transport (once per
        // interval; all channels carry the same TxPresent per {ASW5205}). Earlier_FCP_DRQ
        // is the DRQ from interval (T - d), looked up by absolute interval index; startup
        // assumes 1 for the first d intervals.
        if (rt.hasFcp && e.slot == SwI3sSlot::TxPresent && e.freshTransport) {
            int d = rt.cfg.FlowControlDelay + 1;
            U64 T = rt.txpInterval;
            char earlier = 1;                                   // startup DRQ=1
            if (T >= static_cast<U64>(d)) {
                U64 want = T - static_cast<U64>(d);             // absolute interval index
                if (want >= rt.drqBase && (want - rt.drqBase) < rt.drqHistory.size())
                    earlier = rt.drqHistory[static_cast<size_t>(want - rt.drqBase)];
            }
            // TxPresent is scrambled like the channel's data: the check reads the
            // recovered bit, from a copy of the channel descrambler so the state the
            // sample assembly steps below is untouched.
            bool recoveredTxp = level;
            if (rt.scramblerEn && e.channel >= 0 && e.channel < 16) {
                CDescrambler peek = rt.channels[e.channel].descram;
                recoveredTxp = peek.Descramble(level);
            }
            char txp = recoveredTxp ? 1 : 0;
            ++mFcStats.checked;
            bool fail = (rt.cfg.FlowMode == kFlowRxControlled)
                            ? (txp != earlier)          // RX: bijective
                            : (txp && !earlier);        // ASYNC: data only if requested
            if (fail) ++mFcStats.fails;
            ++rt.txpInterval;
        }

        bool isBit = e.sampleHere &&
                     (e.slot == SwI3sSlot::Data || e.slot == SwI3sSlot::TxPresent);
        bool isData = (e.slot == SwI3sSlot::Data);

        if (mSourceShift <= 0 || !rt.isSource) {
            // Fast path (the default: no diagnostic source shift): sample this bit
            // with this UI's level.
            if (isBit) {
                applyBit(rt, e.channel, e.bitInChannel, e.sampleSize, isData, level,
                         sampleNumber);
            }
            continue;
        }
        // DIAGNOSTIC source shift (SWI3S_SRC_SHIFT env, off by default): defer the
        // level<->bit association by N UIs, so a bit located at column C is sampled
        // with the level N UIs later (probing the peripheral's clock-to-data
        // round-trip skew). Push every UI so the delay is measured in UIs, not in
        // sample events.
        rt.pending.push_back({isBit, e.channel, e.bitInChannel, e.sampleSize, isData});
        if (static_cast<int>(rt.pending.size()) > mSourceShift) {
            DpRuntime::PendingBit p = rt.pending.front();
            rt.pending.pop_front();
            if (p.has) {
                applyBit(rt, p.channel, p.bitInChannel, p.sampleSize, p.isData, level,
                         sampleNumber);
            }
        }
    }
}
