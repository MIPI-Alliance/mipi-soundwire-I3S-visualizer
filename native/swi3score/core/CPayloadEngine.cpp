// Audio payload reconstruction engine. See CPayloadEngine.h.

#include "CPayloadEngine.h"

#include <algorithm>
#include <cstdlib>

void CPayloadEngine::Configure(const SwI3sConfig& cfg)
{
    mCfg = cfg;
    mDps.clear();
    mFcStats = FlowControlStats{};   // fresh handshake accounting per (re)configure
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
        mDps.push_back(buildRuntime(dc, static_cast<int>(i)));
    }
}

CPayloadEngine::DpRuntime CPayloadEngine::buildRuntime(const SwI3sDpConfig& dc, int idx) const
{
    DpRuntime rt;
    rt.dpIndex = (dc.dpNumber >= 0) ? dc.dpNumber : idx;
    rt.deviceNum = dc.deviceNum;
    rt.scramblerEn = dc.ScramblerEn;
    rt.sampleSize = dc.SampleSize;
    rt.cfg = dc;
    rt.isSource = dc.isSource();
    rt.port.Configure(dc, mCfg.columnCount(), mCfg.SkippingDenominator);
    rt.port.Initialize(mStartColumn);
    rt.hasFcp = dc.drqEnabled();
    if (rt.hasFcp) {
        rt.fcp.Configure(dc, mCfg.columnCount());
        rt.fcp.Initialize();
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
            // Survivor whose config changed via an IMMEDIATE (single-ranked) write — the
            // only fields that can change without a commit/SSP are SampleSize/SampleGrouping
            // (0x09), ScramblerEn/PortMode (0x0B), FlowMode (0x0E), SkippingNumerator (0x0C).
            // Re-apply the new config but RESTORE the transport phase (do NOT re-anchor):
            // this is spec-correct, NOT a bug — per SWI3S v1.1 §14.2.5.3/.4 the working
            // counters (current_bit_in_sample, samples_remaining_in_sample_group) are reloaded
            // from the registers only at sample/group/interval boundaries, and §9.1.6.2.1 says
            // only an SSP (SSPA/SSCR) or channel enable/prepare re-anchors the interval. So the
            // preserved counters correctly FINISH the in-flight sample under the OLD geometry,
            // and the NEW SampleSize/SampleGrouping (now in dc/mCfg) is picked up at the next
            // boundary reload. Resetting the counters here would WRONGLY truncate that sample.
            CDataPort::State phase = it->port.SaveState();
            DpRuntime rebuilt = buildRuntime(dc, static_cast<int>(i));
            rebuilt.port.RestoreState(phase);
            next.push_back(std::move(rebuilt));
        }
    }
    mDps = std::move(next);
}

void CPayloadEngine::Reset()
{
    Configure(mCfg);
}

bool CPayloadEngine::SyncToSSP()
{
    bool midSkipCycle = false;
    for (DpRuntime& rt : mDps) {
        // Is this port ALREADY at its sync point? An SSPA re-asserts the phase the ports
        // are running at (it must land on a row where row_in_interval == 0, or it would
        // shift the transport and garble the audio), so for a correctly-placed one the
        // interval numbering does NOT change — and the in-flight DRQ pipeline is still
        // valid. Clearing it there invents a handshake violation: the DRQ was genuinely
        // sent d intervals ago and its sample legitimately transports after the SSP, but
        // a cleared history has nothing to pair the TxPresent with. A re-anchor that DOES
        // move the phase (SSCR to a new phase, a manual SSP row, a misplaced SSPA) really
        // does invalidate the pipeline, so keep clearing in that case.
        const bool alreadyAtSsp = (rt.port.SaveState().rowInInterval == 0);
        // A skipping port has a SECOND alignment to satisfy: its pattern repeats every
        // Interval x SkippingDenominator Rows, so a row that is interval-aligned can still
        // be mid-skip-cycle. Reported by the caller, and asked BEFORE the re-anchor clears
        // the accumulator (Section 9.1.6.2.1).
        if (!rt.port.SkipPatternAtCycleStart()) midSkipCycle = true;
        rt.port.SyncToSSP();
        rt.pending.clear();        // re-anchoring drops in-flight (shifted) bits
        if (rt.hasFcp) {
            rt.fcp.Initialize();   // re-anchor the FCP row/interval to the SSP
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
    Configure(cfg);   // rebuilds mDps for the enabled ports in the saved order
    for (std::size_t i = 0; i < mDps.size() && i < ports.size(); ++i)
        mDps[i].port.RestoreState(ports[i]);
    // Descrambler LFSRs are intentionally left reset: a windowed re-decode using this
    // path reads only bit SAMPLE POINTS (schedule), never assembled values.
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
            rt.pending.push_back({isBit, e.channel, e.bitInChannel, e.slot == SwI3sSlot::Data});
            if (static_cast<int>(rt.pending.size()) > mSourceShift) rt.pending.pop_front();
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
            // TX_PRESENT: not audio, but in the flow-controlled modes its value
            // gates the sample that immediately follows on this channel. recovered
            // == 1 means the transmitter used this transport opportunity (data
            // valid); == 0 means the opportunity was skipped (no sample this
            // interval — the jitter we must remove at the receiver). Latch it for
            // the LSB below. NORMAL ports emit no TX_PRESENT bit, so txpValid stays
            // true and every sample is emitted (unchanged behaviour). The LFSR was
            // already advanced above.
            if (rt.cfg.txpEnabled()) {
                cs.txpValid = recovered;
            }
            return;  // TX_PRESENT advances the LFSR but is not audio
        }
        if (mCollectBits && sn >= mBitSampleStart && mBitSamples.size() < kMaxBitSamples) {
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
                s.sampleSize = rt.sampleSize + 1;
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
        // (the manager drives it; we sample it). Each DRQ is recorded by its interval
        // NUMBER (drqCount), so the handshake lookback is correct regardless of whether
        // the DRQ column falls before or after the data window within an interval.
        if (rt.hasFcp) {
            FcpEmit fe = rt.fcp.clock_tick(rt.port.IntervalSkipped());
            if (fe.slot == SwI3sSlot::Drq && fe.drqSamplePoint) {
                // DRQ is not scrambled by the audio LFSR (separate FCP scrambler,
                // off in the flow-control demo); read the raw level.
                rt.lastDrqSample = sampleNumber;
                ++mFcStats.drqBits;
                rt.drqHistory.push_back(level ? 1 : 0);
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
            char txp = level ? 1 : 0;                            // unscrambled TxPresent
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
