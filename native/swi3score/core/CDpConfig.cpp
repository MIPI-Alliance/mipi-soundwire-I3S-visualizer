// Visualizer-format CSV loader for audio configuration. See CDpConfig.h.
//
// CSV layout (one field per line): interface fields are "Name,value"; per-
// dataport fields are "Name,v0,v1,...,v11" (one column per dataport). Field
// names match the visualizer's CSVFields. Unknown fields are ignored;
// recognised-but-missing fields keep their defaults.

#include "CDpConfig.h"

#include <cstdlib>
#include <fstream>
#include <map>
#include <sstream>
#include <algorithm>

namespace {

// Cells LoadCsv could not read as written, by field name: "<where> '<cell>' read as <v>".
// parseInt/parseBool record into it while LoadCsv runs on this thread; `tlField` and
// `tlWhere` name the cell being read.
thread_local std::map<std::string, std::vector<std::string>>* tlBadCells = nullptr;
thread_local std::string tlField;
thread_local std::string tlWhere;

void noteBadCell(const std::string& raw, const std::string& readAs)
{
    if (tlBadCells == nullptr || tlField.empty()) return;
    std::string where = tlWhere.empty() ? "" : tlWhere + " ";
    (*tlBadCells)[tlField].push_back(where + "'" + raw + "' read as " + readAs);
}

std::string trim(const std::string& s)
{
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

// Decimal, 0x hex or 0b binary. A cell that is not a number reads as 0, and one with
// trailing characters as its leading digits; both are recorded (noteBadCell). A blank
// cell reads as 0 unrecorded.
int parseInt(const std::string& raw)
{
    std::string s = trim(raw);
    if (s.empty()) return 0;
    int value = 0;
    size_t used = 0;
    size_t start = 0;
    try {
        if (s.size() > 2 && s[0] == '0' && (s[1] == 'b' || s[1] == 'B')) {
            start = 2;
            value = static_cast<int>(std::stoul(s.substr(2), &used, 2));
        } else if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
            start = 2;
            value = static_cast<int>(std::stoul(s.substr(2), &used, 16));
        } else {
            value = std::stoi(s, &used);
        }
    } catch (...) {
        noteBadCell(s, "0");
        return 0;
    }
    if (start + used != s.size()) noteBadCell(s, std::to_string(value));
    return value;
}

// true/1/yes and false/0/no, any case. Anything else reads as false and is recorded; a
// blank cell reads as false unrecorded.
bool parseBool(const std::string& raw)
{
    std::string s = trim(raw);
    std::transform(s.begin(), s.end(), s.begin(), ::tolower);
    if (s == "true" || s == "1" || s == "yes") return true;
    if (!(s.empty() || s == "false" || s == "0" || s == "no")) noteBadCell(trim(raw), "false");
    return false;
}

std::string joinList(const std::vector<std::string>& items)
{
    std::string out;
    for (size_t i = 0; i < items.size(); ++i) {
        if (i) out += ", ";
        out += items[i];
    }
    return out;
}

std::string dpLabel(size_t column) { return "DP" + std::to_string(column); }

std::string cdsSourceLabel(int index)
{
    return index == 0 ? std::string("Manager") : "Device " + std::to_string(index - 1);
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
    warnings.clear();
    std::ifstream in(path);
    if (!in) {
        error = "Cannot open config CSV: " + path;
        return false;
    }

    // Unreadable cells by field (parseInt/parseBool), fields with values beyond their last
    // column, and the raw values that are narrowed on the way in.
    std::map<std::string, std::vector<std::string>> badCells;
    std::vector<std::string> fieldOrder;
    std::map<std::string, std::vector<std::string>> extraCells;
    struct CellScope {
        explicit CellScope(std::map<std::string, std::vector<std::string>>* m) { tlBadCells = m; }
        ~CellScope() { tlBadCells = nullptr; tlField.clear(); tlWhere.clear(); }
    } cellScope(&badCells);
    std::vector<int> rawSampleSize(swi3s::kMaxPeripherals, 0);
    std::vector<int> rawEnableCh(swi3s::kMaxPeripherals, 0);
    std::vector<int> rawDpNumber(swi3s::kMaxPeripherals, 0);
    std::vector<bool> gotDpNumber(swi3s::kMaxPeripherals, false);
    bool gotDeviceRow = false;

    dps.assign(swi3s::kMaxPeripherals, SwI3sDpConfig());
    for (size_t i = 0; i < dps.size(); ++i) {
        dps[i].dpNumber = static_cast<int>(i);   // CSV column index = DP number
    }

    // Records the non-empty cells of row `v` past column `last` (1-based value columns).
    auto noteExtra = [&](const std::vector<std::string>& v, size_t last) {
        for (size_t i = last + 1; i < v.size(); ++i) {
            if (!v[i].empty()) extraCells[v[0]].push_back("'" + v[i] + "'");
        }
    };

    // Per-dataport setter applied across the 12 value columns.
    auto setDp = [&](const std::vector<std::string>& v, auto setter) {
        for (size_t i = 1; i < v.size() && (i - 1) < dps.size(); ++i) {
            tlWhere = dpLabel(i - 1);
            setter(dps[i - 1], v[i]);
        }
        tlWhere.clear();
        noteExtra(v, dps.size());
    };
    auto dpIndex = [&](const SwI3sDpConfig& d) { return static_cast<size_t>(&d - dps.data()); };

    // Per-SOURCE setter for the 13-wide CDS rows (index 0 = Manager, i = Device i-1).
    // A SHORT OR BLANK CELL KEEPS THE FIELD'S DEFAULT rather than reading as 0: on
    // CDS_DriveType 0 means Special, so a row truncated by a hand edit would otherwise
    // relabel the whole bus as passively driven. Same rule as both Python engines.
    auto setCds = [&](const std::vector<std::string>& v, std::vector<int>& dst) {
        for (int i = 0; i < SwI3sConfig::kCdsSources; ++i) {
            size_t col = static_cast<size_t>(i) + 1;
            tlWhere = cdsSourceLabel(i);
            if (col < v.size() && !v[col].empty()) dst[i] = parseInt(v[col]);
        }
        tlWhere.clear();
        noteExtra(v, SwI3sConfig::kCdsSources);
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
        tlField = key;
        tlWhere.clear();
        if (std::find(fieldOrder.begin(), fieldOrder.end(), key) == fieldOrder.end())
            fieldOrder.push_back(key);

        // Interface-level fields.
        if (key == "NumColumns_REG")            NumColumns = parseInt(val);
        else if (key == "SkippingDenominator_REG") SkippingDenominator = parseInt(val);
        else if (key == "PHY3Enabled")          PHY3Enabled = parseBool(val);
        else if (key == "RowRate") {
            char* end = nullptr;
            RowRateKHz = std::strtod(val.c_str(), &end);
            if (!val.empty() && end != nullptr && *end != '\0')
                noteBadCell(val, std::to_string(RowRateKHz));
        }
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
        else if (key == "EnableCh_REG")         setDp(v, [&](SwI3sDpConfig& d, const std::string& s){ const int n = parseInt(s); rawEnableCh[dpIndex(d)] = n; d.EnableCh = static_cast<U16>(n); });
        else if (key == "ChannelGrouping_REG")  setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.ChannelGrouping = parseInt(s); });
        else if (key == "Spacing_REG")          setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.Spacing = parseInt(s); });
        // SampleSize is a 5-bit register field: masked as the register path (decodeDp) and
        // registersFromConfig mask it, so the audio decode, the grid and the register map
        // read one width, and the sample assembly's shift stays within 32 bits.
        else if (key == "SampleSize_REG")       setDp(v, [&](SwI3sDpConfig& d, const std::string& s){ const int n = parseInt(s); rawSampleSize[dpIndex(d)] = n; d.SampleSize = n & 0x1F; });
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
        else if (key == "DeviceNumber_REG")     { gotDeviceRow = true; setDp(v, [](SwI3sDpConfig& d, const std::string& s){ d.deviceNum = parseInt(s); }); }
        // v3.0.0: explicit logical DP number 0-31. Overrides the column-index
        // default (set above) only when the cell carries a valid non-negative value.
        else if (key == "DataPortNumber")       setDp(v, [&](SwI3sDpConfig& d, const std::string& s){ if (!s.empty()) { int n = parseInt(s); rawDpNumber[dpIndex(d)] = n; gotDpNumber[dpIndex(d)] = true; if (n >= 0) d.dpNumber = n; } });
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
    // so a config CSV writes the manager as device 0 plus ManagerDataport=True; recombine
    // them here, since 0 is a REAL peripheral address. registersFromConfig skips
    // deviceNum < 0: a manager data port is not addressable as a peripheral register write.
    // A peripheral port whose DeviceNumber_REG is outside 0..kMaxPeripherals-1 names no
    // device on the bus, so it is disabled: the audio decode, the grid and the register
    // map all leave it out.
    std::vector<std::string> noDevice;
    for (SwI3sDpConfig& d : dps) {
        if (d.managerDp) {
            d.deviceNum = -1;
        } else if (d.deviceNum < 0 || d.deviceNum >= swi3s::kMaxPeripherals) {
            if (d.Enabled && d.numChannels() > 0) {
                noDevice.push_back(dpLabel(dpIndex(d)) + " (" +
                    (gotDeviceRow ? std::to_string(d.deviceNum) : std::string("none given")) + ")");
            }
            d.Enabled = false;
        }
    }
    tlBadCells = nullptr;
    collectWarnings(badCells, fieldOrder, extraCells, noDevice, rawSampleSize, rawEnableCh,
                    rawDpNumber, gotDpNumber);
    return true;
}

void SwI3sConfig::collectWarnings(
    const std::map<std::string, std::vector<std::string>>& badCells,
    const std::vector<std::string>& fieldOrder,
    const std::map<std::string, std::vector<std::string>>& extraCells,
    const std::vector<std::string>& noDevice,
    const std::vector<int>& rawSampleSize, const std::vector<int>& rawEnableCh,
    const std::vector<int>& rawDpNumber, const std::vector<bool>& gotDpNumber)
{
    // Unreadable cells, in the file's field order.
    for (const std::string& f : fieldOrder) {
        auto it = badCells.find(f);
        if (it == badCells.end()) continue;
        const size_t n = it->second.size();
        warnings.push_back(f + ": " + std::to_string(n) + (n == 1 ? " cell is" : " cells are") +
                           " not a valid value: " + joinList(it->second) + ".");
    }
    for (const std::string& f : fieldOrder) {
        auto it = extraCells.find(f);
        if (it == extraCells.end()) continue;
        const bool cds = f.find("PerSource") != std::string::npos;
        warnings.push_back(f + ": values past the last " +
                           (cds ? std::string("CDS source (13 columns)")
                                : "data port column (" + std::to_string(dps.size()) + ")") +
                           " are ignored: " + joinList(it->second) + ".");
    }
    if (!noDevice.empty()) {
        warnings.push_back("DeviceNumber_REG names no device on the bus (0.." +
                           std::to_string(swi3s::kMaxPeripherals - 1) + ") for " +
                           joinList(noDevice) + "; " +
                           (noDevice.size() == 1 ? "that port is" : "those ports are") +
                           " disabled and left out of the audio, the grid and the registers.");
    }

    // Interface fields.
    const int cols = NumColumns + 1;
    if (cols < swi3s::kMinColumnCount || cols > swi3s::kMaxColumnCount) {
        warnings.push_back("NumColumns_REG " + std::to_string(NumColumns) + " is outside " +
                           std::to_string(swi3s::kMinColumnCount - 1) + ".." +
                           std::to_string(swi3s::kMaxColumnCount - 1) + " (" +
                           std::to_string(swi3s::kMinColumnCount) + " to " +
                           std::to_string(swi3s::kMaxColumnCount) +
                           " columns); the decode uses " + std::to_string(columnCount()) +
                           " columns and the expected registers carry its low 5 bits (" +
                           std::to_string(NumColumns & 0x1F) + ").");
    }
    auto fit = [&](const std::string& name, long value, int bits, const std::string& where) {
        const long max = (1L << bits) - 1;
        if (value >= 0 && value <= max) return;
        warnings.push_back(name + " " + std::to_string(value) + (where.empty() ? "" : " for " + where) +
                           " does not fit its " + std::to_string(bits) + "-bit register field (0.." +
                           std::to_string(max) + "); the expected registers carry its low " +
                           std::to_string(bits) + " bits (" + std::to_string(value & max) +
                           ") and the decode uses the value as written.");
    };
    fit("SkippingDenominator_REG", SkippingDenominator, 12, "");
    fit("CDS_BitWidth_REG", CdsBitWidth, 3, "");
    struct CdsField { const char* name; const std::vector<int>* values; int bits; };
    const CdsField cdsFields[] = {
        {"CDS_TailWidthPerSource", &CdsTailWidth, 2},
        {"CDS_DriveTypePerSource", &CdsDriveType, 1},
        {"CDS_EndDriveEarlyPerSource", &CdsEndDriveEarly, 1},
    };
    for (const CdsField& f : cdsFields) {
        const long max = (1L << f.bits) - 1;
        std::vector<std::string> over;
        for (int i = 0; i < kCdsSources && i < static_cast<int>(f.values->size()); ++i) {
            const int v = (*f.values)[i];
            if (v < 0 || v > max) over.push_back(cdsSourceLabel(i) + " = " + std::to_string(v));
        }
        if (!over.empty()) {
            warnings.push_back(std::string(f.name) + " does not fit its " + std::to_string(f.bits) +
                               "-bit register field (0.." + std::to_string(max) + ") for " +
                               joinList(over) + "; the expected registers carry its low " +
                               std::to_string(f.bits) + " bits.");
        }
    }

    // Per-port fields, for the ports that carry audio: one warning per field, listing ports.
    std::vector<std::string> sizeMasked, enableWide, dpNumBad;
    struct DpField { const char* name; int (*get)(const SwI3sDpConfig&); int bits; };
    const DpField dpFields[] = {
        {"SampleGrouping_REG",      [](const SwI3sDpConfig& d) { return d.SampleGrouping; }, 3},
        {"ChannelGrouping_REG",     [](const SwI3sDpConfig& d) { return d.ChannelGrouping; }, 4},
        {"Spacing_REG",             [](const SwI3sDpConfig& d) { return d.Spacing; }, 4},
        {"Interval_REG",            [](const SwI3sDpConfig& d) { return d.Interval; }, 12},
        {"SkippingNumerator_REG",   [](const SwI3sDpConfig& d) { return d.SkippingNumerator; }, 12},
        {"Offset_REG",              [](const SwI3sDpConfig& d) { return d.Offset; }, 12},
        {"HorizontalStart_REG",     [](const SwI3sDpConfig& d) { return d.HorizontalStart; }, 5},
        {"HorizontalCount_REG",     [](const SwI3sDpConfig& d) { return d.HorizontalCount; }, 5},
        {"TailWidth_REG",           [](const SwI3sDpConfig& d) { return d.TailWidth; }, 2},
        {"BitWidth_REG",            [](const SwI3sDpConfig& d) { return d.BitWidth; }, 2},
        {"FlowMode_REG",            [](const SwI3sDpConfig& d) { return d.FlowMode; }, 2},
        {"PortMode_REG",            [](const SwI3sDpConfig& d) { return d.PortMode; }, 2},
        {"FCP_HorizontalStart_REG", [](const SwI3sDpConfig& d) { return d.FCP_HorizontalStart; }, 5},
        {"FCP_BitWidth_REG",        [](const SwI3sDpConfig& d) { return d.FCP_BitWidth; }, 2},
        {"FCP_TailWidth_REG",       [](const SwI3sDpConfig& d) { return d.FCP_TailWidth; }, 2},
        {"FCP_Offset_REG",          [](const SwI3sDpConfig& d) { return d.FCP_Offset; }, 12},
    };
    std::map<std::string, std::vector<std::string>> overByField;
    for (size_t k = 0; k < dps.size(); ++k) {
        const SwI3sDpConfig& d = dps[k];
        if (!(d.Enabled && d.numChannels() > 0)) continue;
        const std::string at = dpLabel(k);
        if (rawSampleSize[k] != d.SampleSize)
            sizeMasked.push_back(at + " = " + std::to_string(rawSampleSize[k]) + " (read as " +
                                 std::to_string(d.SampleSize) + ")");
        if (rawEnableCh[k] < 0 || rawEnableCh[k] > 0xFFFF)
            enableWide.push_back(at + " = " + std::to_string(rawEnableCh[k]));
        if (gotDpNumber[k] && (rawDpNumber[k] < 0 || rawDpNumber[k] > 31))
            dpNumBad.push_back(at + " = " + std::to_string(rawDpNumber[k]));
        for (const DpField& f : dpFields) {
            const int v = f.get(d);
            if (v < 0 || v > (1 << f.bits) - 1)
                overByField[f.name].push_back(at + " = " + std::to_string(v));
        }
    }
    if (!sizeMasked.empty()) {
        warnings.push_back("SampleSize_REG does not fit its 5-bit register field (0..31) for " +
                           joinList(sizeMasked) + "; the audio decode, the grid and the registers read its low 5 bits.");
    }
    if (!enableWide.empty()) {
        warnings.push_back("EnableCh_REG does not fit its 16 channel bits for " +
                           joinList(enableWide) + "; only channels 0..15 are read.");
    }
    if (!dpNumBad.empty()) {
        warnings.push_back("DataPortNumber is outside 0..31 for " + joinList(dpNumBad) +
                           "; a negative number is ignored (the port keeps its column's number) "
                           "and a larger one addresses no data port register block.");
    }
    for (const DpField& f : dpFields) {
        auto it = overByField.find(f.name);
        if (it == overByField.end()) continue;
        const int max = (1 << f.bits) - 1;
        warnings.push_back(std::string(f.name) + " does not fit its " + std::to_string(f.bits) +
                           "-bit register field (0.." + std::to_string(max) + ") for " +
                           joinList(it->second) + "; the expected registers carry its low " +
                           std::to_string(f.bits) + " bits and the decode uses the value as written.");
    }
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
