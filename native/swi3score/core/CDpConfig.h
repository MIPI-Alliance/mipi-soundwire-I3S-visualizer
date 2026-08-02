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
    int  deviceNum = -1;          // owning peripheral (Device Mask bit / DeviceNumber_REG)
    int  dpNumber = -1;           // data-port index within the device (0..31)

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

    std::vector<SwI3sDpConfig> dps;   // index = dataport number

    int columnCount() const { return NumColumns + 1; }

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
