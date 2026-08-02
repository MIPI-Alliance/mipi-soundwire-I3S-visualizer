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
        // Other visualizer-only fields (Name, RowRate, RowsToDraw, S0Width,
        // DisplayFields, DeviceNumber_REG, ManagerDataport, ...) are ignored.
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
