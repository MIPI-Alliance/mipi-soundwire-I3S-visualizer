// Per-dataport and interface configuration for SWI3S audio decoding.
//
// Field names and semantics mirror the companion visualizer's register model
// (and its CSV schema), so a visualizer CSV loads directly and the snoop path
// can populate the same structs. Register values are stored RAW (excess-1
// where the hardware uses it, e.g. SampleSize, Interval, NumColumns).

#ifndef SWI3S_CDPCONFIG_H
#define SWI3S_CDPCONFIG_H

#include <string>
#include <vector>
#include <LogicPublicTypes.h>

#include "SwI3sProtocolDefs.h"

// Flow modes (matches visualizer FlowMode).
enum SwI3sFlowMode {
    kFlowNormal       = 0,
    kFlowTxControlled = 1,
    kFlowRxControlled = 2,
    kFlowAsync        = 3,
};

struct SwI3sDpConfig
{
    U16  EnableCh = 0;            // 16-bit channel-enable bitmask
    int  ChannelGrouping = 0;
    int  Spacing = 0;
    int  SampleSize = 0;          // excess-1: sample bits = SampleSize + 1
    int  SampleGrouping = 0;
    int  Interval = 0;            // rows per interval - 1
    int  SkippingNumerator = 0;
    int  Offset = 0;              // rows from SSP before the window opens
    int  HorizontalStart = 0;
    int  HorizontalCount = 0;     // last column = HorizontalStart + HorizontalCount
    int  TailWidth = 0;
    int  BitWidth = 0;            // UIs per data bit - 1 (wide bits)
    bool PortDirection = false;   // true = SINK, false = SOURCE
    bool GuardEnable = false;
    bool GuardPolarity = false;
    bool SubRowInterval = false;
    int  FlowMode = kFlowNormal;
    int  FlowControlDelay = 1;    // DRQ->TxPresent pipeline: d = FlowControlDelay + 1
                                  // non-skipped intervals (0->1, 1->2). Reset 1 (0x0E bit7).
    int  PortMode = 0;
    bool ScramblerEn = false;
    bool Enabled = false;
    int  deviceNum = -1;          // owning peripheral (Device Mask bit / DeviceNumber_REG),
                                  // or -1 for a MANAGER data port. DeviceNumber_REG holds
                                  // 0..11 and cannot carry that sentinel, so a config CSV
                                  // encodes the manager as device 0 + ManagerDataport=True;
                                  // LoadCsv decodes the pair back to -1 (see managerDp).
    int  dpNumber = -1;           // data-port index within the device (0..31)
    bool managerDp = false;       // the CSV's ManagerDataport flag, as read. Only the decoded
                                  // deviceNum == -1 should be tested; this is kept so the
                                  // decode is order-independent of the two CSV rows.

    // Flow Control Port registers.
    int  FCP_HorizontalStart = 0;
    int  FCP_BitWidth = 0;
    int  FCP_TailWidth = 0;
    int  FCP_Offset = 0;
    bool FCP_GuardEnable = false;
    bool FCP_GuardPolarity = false;

    // --- Derived (mirror DataPortConfig properties) ---
    int numChannels() const
    {
        int c = 0;
        for (int i = 0; i < 16; ++i) if (EnableCh & (1u << i)) ++c;
        return c;
    }
    int horizontalEnd() const { return HorizontalStart + HorizontalCount; }
    bool isSource() const { return !PortDirection; }
    // TX_PRESENT is present in ALL three flow-controlled modes ({ASW5203}):
    // TX_CONTROLLED/ASYNC carry data-validity, RX_CONTROLLED carries a copy of the
    // FCP_DRQ received 1-2 Intervals earlier. Only NORMAL omits it. Mirrors the
    // visualizer DataPortConfig._txp_enabled.
    bool txpEnabled() const { return FlowMode == kFlowTxControlled
                                  || FlowMode == kFlowRxControlled
                                  || FlowMode == kFlowAsync; }
    bool drqEnabled() const { return FlowMode == kFlowRxControlled || FlowMode == kFlowAsync; }
    int effectiveChannelGrouping() const
    {
        return (ChannelGrouping == 0) ? numChannels() : ChannelGrouping;
    }
    // Channel NUMBER of the index-th enabled channel (-1 if out of range).
    int channelFromIndex(int index) const
    {
        int count = -1;
        for (int i = 0; i < 16; ++i) {
            if (EnableCh & (1u << i)) {
                if (++count == index) return i;
            }
        }
        return -1;
    }

    // Same transport/decode-relevant config? (Ignores derived helpers and FCP electricals
    // not used by the payload engine's sample assembly.) Used to decide whether a mid-
    // stream reconfigure must re-apply a surviving port or can carry it over untouched.
    bool sameTransport(const SwI3sDpConfig& o) const
    {
        return deviceNum == o.deviceNum && dpNumber == o.dpNumber &&
               EnableCh == o.EnableCh && ChannelGrouping == o.ChannelGrouping &&
               Spacing == o.Spacing && SampleSize == o.SampleSize &&
               SampleGrouping == o.SampleGrouping && Interval == o.Interval &&
               SkippingNumerator == o.SkippingNumerator && Offset == o.Offset &&
               HorizontalStart == o.HorizontalStart && HorizontalCount == o.HorizontalCount &&
               TailWidth == o.TailWidth && BitWidth == o.BitWidth &&
               PortDirection == o.PortDirection && GuardEnable == o.GuardEnable &&
               GuardPolarity == o.GuardPolarity && SubRowInterval == o.SubRowInterval &&
               FlowMode == o.FlowMode && PortMode == o.PortMode &&
               FlowControlDelay == o.FlowControlDelay &&
               ScramblerEn == o.ScramblerEn;
    }
};

struct SwI3sConfig
{
    int  NumColumns = swi3s::kColdStartColumnCount - 1;  // column count - 1
    int  SkippingDenominator = 1;
    bool PHY3Enabled = false;
    double RowRateKHz = 0.0;          // rows per second, in kHz (CSV "RowRate")
    std::string description;

    // --- Control Data Stream, PER SOURCE ---
    //
    // The CDS is time-multiplexed: each source drives it in its own turn under its own
    // copy of the CDS registers (block 0x1100), so these are lists and not scalars.
    // INDEX 0 IS THE MANAGER, index d+1 is Device d (0..11) — the same convention the
    // authoring model uses (bus_config.cds_src_index).
    //
    // THE MANAGER SLOT HAS NO REGISTER. Only the twelve peripherals are addressable, so
    // registersFromConfig emits entries for indices 1..12 and BuildConfig can only ever
    // fill those; index 0 survives a CSV round trip but is invisible on the wire. A
    // decoded capture therefore leaves it at its default, which is honest rather than
    // guessed — see the note in registersFromConfig.
    //
    // Defaults match the authoring model, INCLUDING the deliberate asymmetry:
    // CdsDriveType defaults to 1 (Normal) against its register reset of 0, because a
    // config that never mentions it must keep reading as it always did; CdsEndDriveEarly
    // defaults to 0 (drive to the end of the UI) WITH its reset, that being the ordinary
    // behaviour. See the CDS_* constants in bus_config.py for the full reasoning.
    static const int kCdsSources = 13;               // Manager + Device 0..11
    int  CdsBitWidth = 0;                            // excess-1: UIs per CDS bit - 1
    std::vector<int>  CdsGuard;                      // 0 off, 1 G0, 2 G1
    std::vector<int>  CdsTailWidth;                  // 0..3 UIs
    std::vector<int>  CdsDriveType;                  // 0 Special, 1 Normal
    std::vector<int>  CdsEndDriveEarly;              // 0 full UI, 1 stop early

    SwI3sConfig()
        : CdsGuard(kCdsSources, 0), CdsTailWidth(kCdsSources, 0),
          CdsDriveType(kCdsSources, 1), CdsEndDriveEarly(kCdsSources, 0) {}

    std::vector<SwI3sDpConfig> dps;   // index = dataport number

    int columnCount() const { return NumColumns + 1; }

    // Rows after which a port's transport pattern repeats. Without skipping that is one
    // Interval. With skipping the accumulator has to cycle through SkippingDenominator
    // steps before the SAME intervals transport again, so the pattern is Interval x
    // SkippingDenominator Rows long — which is also the interval the SSP calculation uses
    // (Section 4.2.3.1), and therefore the spacing of the only rows an SSP may legally
    // land on. Mirrors the visualizer's Interface.interval_lcm; a Numerator at or above
    // the Denominator skips everything and has no pattern, so it falls back to Interval.
    long patternRows(const SwI3sDpConfig& d) const
    {
        long rows = static_cast<long>(d.Interval) + 1;
        if (d.SkippingNumerator > 0 && d.SkippingNumerator < SkippingDenominator)
            rows *= SkippingDenominator;
        return rows;
    }

    // Audio sample rate (Hz) for a dataport, derived from RowRate + the port's
    // Interval / SampleGrouping / Skipping / SubRowInterval. Mirrors the
    // visualizer's _calculate_sample_rate. Returns 0 if it cannot be computed
    // (no RowRate, or an SRI transport that does not fit).
    double SampleRateHz(const SwI3sDpConfig& d) const;
    bool anyAudioEnabled() const
    {
        for (const auto& dp : dps) {
            if (dp.Enabled && dp.numChannels() > 0) return true;
        }
        return false;
    }

    // Load a visualizer-format CSV. Returns false (and sets 'error') on a
    // malformed file. Missing optional fields fall back to defaults.
    bool LoadCsv(const std::string& path, std::string& error);
};

#endif // SWI3S_CDPCONFIG_H
