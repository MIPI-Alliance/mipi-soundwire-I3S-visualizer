// Visualizer-format CSV loader for audio configuration. See CDpConfig.h.
//
// CSV layout (one field per line): interface fields are "Name,value"; per-
// dataport fields are "Name,v0,v1,...,v11" (one column per dataport). Field
// names match the visualizer's CSVFields. Unknown fields are ignored;
// recognised-but-missing fields keep their defaults.

#include "CDpConfig.h"

#include <cstdlib>
#include <fstream>
#include <sstream>
#include <algorithm>

namespace {

std::string trim(const std::string& s)
{
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

int parseInt(const std::string& raw)
{
    std::string s = trim(raw);
    if (s.empty()) return 0;
    try {
        if (s.size() > 2 && s[0] == '0' && (s[1] == 'b' || s[1] == 'B')) {
            return static_cast<int>(std::stoul(s.substr(2), nullptr, 2));
        }
        if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
            return static_cast<int>(std::stoul(s.substr(2), nullptr, 16));
        }
        return std::stoi(s);
    } catch (...) {
        return 0;
    }
}

bool parseBool(const std::string& raw)
{
    std::string s = trim(raw);
    std::transform(s.begin(), s.end(), s.begin(), ::tolower);
    return (s == "true" || s == "1" || s == "yes");
}

std::vector<std::string> splitCsv(const std::string& line)
{
    std::vector<std::string> out;
    std::stringstream ss(line);
    std::string item;
    while (std::getline(ss, item, ',')) {
        out.push_back(trim(item));
    }
    return out;
}

} // namespace

bool SwI3sConfig::LoadCsv(const std::string& path, std::string& error)
{
    std::ifstream in(path);
    if (!in) {
        error = "Cannot open config CSV: " + path;
        return false;
    }

    dps.assign(swi3s::kMaxPeripherals, SwI3sDpConfig());
    for (size_t i = 0; i < dps.size(); ++i) {
        dps[i].dpNumber = static_cast<int>(i);   // CSV column index = DP number
    }

    // Per-dataport setter applied across the 12 value columns.
    auto setDp = [&](const std::vector<std::string>& v, auto setter) {
        for (size_t i = 1; i < v.size() && (i - 1) < dps.size(); ++i) {
            setter(dps[i - 1], v[i]);
        }
    };

    // Per-SOURCE setter for the 13-wide CDS rows (index 0 = Manager, i = Device i-1).
    // A SHORT OR BLANK CELL KEEPS THE FIELD'S DEFAULT rather than reading as 0: on
    // CDS_DriveType 0 means Special, so a row truncated by a hand edit would otherwise
    // relabel the whole bus as passively driven. Same rule as both Python engines.
    auto setCds = [](const std::vector<std::string>& v, std::vector<int>& dst) {
        for (int i = 0; i < SwI3sConfig::kCdsSources; ++i) {
            size_t col = static_cast<size_t>(i) + 1;
            if (col < v.size() && !v[col].empty()) dst[i] = parseInt(v[col]);
        }
    };
    // The guard arrives as two rows mirroring its two registers; recombined after the loop
    // into CdsGuard (0 off, 1 G0, 2 G1).
    std::vector<int> guardEn, guardPol;
    // Legacy pre-per-source scalars, broadcast to every source only if no per-source row
    // appeared. Every device may use the CDS regardless of its data ports, so a global
    // value is SHARED by all sources — matching the Python loaders.
    int legacyGuardEn = -1, legacyGuardPol = -1, legacyTail = -1;
    bool gotTail = false, gotGuard = false;

    std::string line;
    while (std::getline(in, line)) {
        line = trim(line);
        if (line.empty()) continue;

        std::vector<std::string> v = splitCsv(line);
        if (v.empty()) continue;
        const std::string& key = v[0];
        const std::string val = (v.size() > 1) ? v[1] : "";

        // Interface-level fields.
        if (key == "NumColumns_REG")            NumColumns = parseInt(val);
        else if (key == "SkippingDenominator_REG") SkippingDenominator = parseInt(val);
        else if (key == "PHY3Enabled")          PHY3Enabled = parseBool(val);
        else if (key == "RowRate")              RowRateKHz = atof(val.c_str());
        else if (key == "Description")          description = val;

        // Control Data Stream. CDS_BitWidth is bus-wide; the rest are per source because
        // each register lives in that source's own CDS block (0x1100).
        else if (key == "CDS_BitWidth_REG")     CdsBitWidth = parseInt(val);
        else if (key == "CDS_GuardEnabledPerSource")  { guardEn.assign(SwI3sConfig::kCdsSources, 0);  setCds(v, guardEn);  gotGuard = true; }
        else if (key == "CDS_GuardPolarityPerSource") { guardPol.assign(SwI3sConfig::kCdsSources, 0); setCds(v, guardPol); }
        else if (key == "CDS_TailWidthPerSource")     { setCds(v, CdsTailWidth); gotTail = true; }
        else if (key == "CDS_DriveTypePerSource")     setCds(v, CdsDriveType);
        else if (key == "CDS_EndDriveEarlyPerSource") setCds(v, CdsEndDriveEarly);
        // Legacy scalars from a pre-per-source file.
        else if (key == "CDS_GuardEnabled_REG")  legacyGuardEn  = parseBool(val) ? 1 : 0;
        else if (key == "CDS_GuardPolarity_REG") legacyGuardPol = parseBool(val) ? 1 : 0;
        else if (key == "CDS_TailWidth_REG")     legacyTail     = parseInt(val);

        // Per-dataport fields.
        else if (key == "EnableCh_REG")         setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.EnableCh = static_cast<U16>(parseInt(s)); });
        else if (key == "ChannelGrouping_REG")  setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.ChannelGrouping = parseInt(s); });
        else if (key == "Spacing_REG")          setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.Spacing = parseInt(s); });
        else if (key == "SampleSize_REG")       setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.SampleSize = parseInt(s); });
        else if (key == "SampleGrouping_REG")   setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.SampleGrouping = parseInt(s); });
        else if (key == "Interval_REG")         setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.Interval = parseInt(s); });
        else if (key == "SkippingNumerator_REG")setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.SkippingNumerator = parseInt(s); });
        else if (key == "Offset_REG")           setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.Offset = parseInt(s); });
        else if (key == "HorizontalStart_REG")  setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.HorizontalStart = parseInt(s); });
        else if (key == "HorizontalCount_REG")  setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.HorizontalCount = parseInt(s); });
        else if (key == "TailWidth_REG")        setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.TailWidth = parseInt(s); });
        else if (key == "BitWidth_REG")         setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.BitWidth = parseInt(s); });
        else if (key == "PortDirection_REG")    setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.PortDirection = parseBool(s); });
        else if (key == "GuardEnable_REG")      setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.GuardEnable = parseBool(s); });
        else if (key == "GuardPolarity_REG")    setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.GuardPolarity = parseBool(s); });
        else if (key == "SubRowInterval_REG")   setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.SubRowInterval = parseBool(s); });
        else if (key == "FlowMode_REG")         setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.FlowMode = parseInt(s); });
        else if (key == "PortMode_REG")         setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.PortMode = parseInt(s); });
        else if (key == "ScramblerEn_REG")      setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.ScramblerEn = parseBool(s); });
        else if (key == "Enabled")              setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.Enabled = parseBool(s); });
        else if (key == "DeviceNumber_REG")     setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.deviceNum = parseInt(s); });
        // v3.0.0: explicit logical DP number 0-31. Overrides the column-index
        // default (set above) only when the cell carries a valid non-negative value.
        else if (key == "DataPortNumber")       setDp(v, [](SwI3sDpConfig& d, const std::string& s){ if (!s.empty()) { int n = parseInt(s); if (n >= 0) d.dpNumber = n; } });
        else if (key == "FCP_HorizontalStart_REG") setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.FCP_HorizontalStart = parseInt(s); });
        else if (key == "FCP_BitWidth_REG")     setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.FCP_BitWidth = parseInt(s); });
        else if (key == "FCP_TailWidth_REG")    setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.FCP_TailWidth = parseInt(s); });
        else if (key == "FCP_Offset_REG")       setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.FCP_Offset = parseInt(s); });
        else if (key == "FCP_GuardEnable_REG")  setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.FCP_GuardEnable = parseBool(s); });
        else if (key == "FCP_GuardPolarity_REG")setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.FCP_GuardPolarity = parseBool(s); });
        // A MANAGER data port. Read into its own field and decoded after the loop, because
        // DeviceNumber_REG and ManagerDataport are two rows of one encoding and applying the
        // flag inline would depend on which row the writer happened to emit first.
        else if (key == "ManagerDataport")      setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.managerDp = parseBool(s); });
        // Other visualizer-only fields (Name, RowRate, RowsToDraw, S0Width,
        // DisplayFields, ...) are ignored.
    }

    // Recombine the split guard rows (0 off, 1 G0, 2 G1); a missing polarity row leaves the
    // enabled entries at G0.
    if (gotGuard) {
        for (int i = 0; i < SwI3sConfig::kCdsSources; ++i) {
            const bool en = guardEn[i] != 0;
            const bool pol = (i < static_cast<int>(guardPol.size())) && guardPol[i] != 0;
            CdsGuard[i] = en ? (pol ? 2 : 1) : 0;
        }
    } else if (legacyGuardEn > 0) {
        CdsGuard.assign(SwI3sConfig::kCdsSources, legacyGuardPol > 0 ? 2 : 1);
    }
    if (!gotTail && legacyTail > 0) {
        CdsTailWidth.assign(SwI3sConfig::kCdsSources, legacyTail);
    }

    // Decode the manager sentinel. DeviceNumber_REG is a 0..11 register and cannot hold -1,
    // so a config CSV writes the manager as device 0 plus ManagerDataport=True and expects a
    // reader to recombine them. This half was missing, and 0 is a REAL peripheral address, so
    // every manager data port impersonated device 0:
    //   * grid cells attributed the manager's ports to device 0, indistinguishable from a
    //     genuine device-0 port with the same number, and disagreeing with the reference
    //     model, which decodes the pair correctly;
    //   * registersFromConfig emitted the manager's port configuration as register writes
    //     ADDRESSED TO device 0 — 103 writes to device 0 against 23 for its peers on a config
    //     with four manager ports, including two conflicting values for DP0's FlowMode.
    // Both loops there already skip deviceNum < 0, so restoring the sentinel is all that is
    // needed: a manager data port is not addressable as a peripheral register write.
    for (SwI3sDpConfig& d : dps) {
        if (d.managerDp) {
            d.deviceNum = -1;
        }
    }

    return true;
}

double SwI3sConfig::SampleRateHz(const SwI3sDpConfig& d) const
{
    // RowRate is in kHz (e.g. 3072 -> 3.072 MHz rows/s).
    double rowRate = RowRateKHz * 1000.0;
    if (rowRate <= 0.0) {
        return 0.0;
    }

    if (!d.SubRowInterval) {
        // One sample group per Interval. Each group carries SampleGrouping+1
        // samples. Skipping drops a fraction of intervals.
        double base = (d.SampleGrouping + 1) * rowRate / (d.Interval + 1);
        if (d.SkippingNumerator == 0 || SkippingDenominator == 0) {
            return base;
        }
        // A numerator >= denominator would skip every (or more than every) interval,
        // yielding a zero/negative rate; clamp to 0 rather than report a bogus value.
        if (d.SkippingNumerator >= SkippingDenominator) {
            return 0.0;
        }
        return base * (SkippingDenominator - d.SkippingNumerator) / SkippingDenominator;
    }

    // SRI: one or more transports repeat within the horizontal window each row.
    int numChannels = d.numChannels();
    int txpSlot = d.txpEnabled() ? 1 : 0;
    int transportWidth = numChannels * (d.SampleSize + 1 + txpSlot)
                       * (d.SampleGrouping + 1) * (d.BitWidth + 1);
    int availableCols = d.HorizontalCount + 1;

    int groupsPerRow;
    if (transportWidth == 0 || availableCols < transportWidth) {
        groupsPerRow = 0;
    } else if (d.Spacing == 0) {
        groupsPerRow = 1;   // Spacing 0 pauses until next row
    } else {
        int gap = d.Spacing - 1;
        int cadence = transportWidth + gap;
        groupsPerRow = (availableCols + gap) / cadence;
    }
    if (groupsPerRow <= 0) {
        return 0.0;
    }
    int samplesPerRow = groupsPerRow * (d.SampleGrouping + 1);
    return samplesPerRow * rowRate;
}
