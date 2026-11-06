// Visualizer-format CSV loader for audio configuration. See CDpConfig.h.
//
// CSV layout (one field per line): interface fields are "Name,value"; per-
// dataport fields are "Name,v0,v1,...,v11" (one column per dataport). Field
// names match the visualizer's CSVFields. Cells are read as Python's csv reader with
// newline='' reads them: a line ends at LF, CR LF or a lone CR outside a quoted cell, and a
// cell opening with '"' is quoted, may span lines and holds ',' and doubled '""' as
// literals. Unknown fields are ignored and reported, except the visualizer-only ones; a
// row the decode reads that the file does not carry keeps its default and is reported.

#include "CDpConfig.h"

#include <cmath>
#include <cstdlib>
#include <climits>
#include <fstream>
#include <iomanip>
#include <locale>
#include <map>
#include <numeric>
#include <set>
#include <sstream>
#include <unordered_set>
#include <algorithm>
#ifdef _WIN32
#include <filesystem>
#else
#include <sys/stat.h>
#endif

namespace {

// The CSV RowRate field's range, in kHz.
constexpr double kMinRowRateKHz = 1.0;
constexpr double kMaxRowRateKHz = 48000.0;

// Cells LoadCsv could not read as written, by field name: "<where> '<cell>' read as <v>".
// parseInt/parseBool record into it while LoadCsv runs on this thread; `tlField` and
// `tlWhere` name the cell being read.
thread_local std::map<std::string, std::vector<std::string>>* tlBadCells = nullptr;
thread_local std::string tlField;
thread_local std::string tlWhere;

// The most characters (UTF-8 code points) of a cell a warning quotes.
constexpr size_t kQuotedChars = 60;

// `text` with each byte that is not part of a well-formed UTF-8 sequence replaced by
// U+FFFD, so a warning that echoes file text converts to a Python str whatever the file holds.
std::string validUtf8(const std::string& text)
{
    std::string out;
    out.reserve(text.size());
    const size_t n = text.size();
    size_t i = 0;
    while (i < n) {
        const unsigned char b = static_cast<unsigned char>(text[i]);
        size_t len = 0;
        unsigned char lo = 0x80, hi = 0xBF;     // the range of the second byte
        if (b < 0x80) len = 1;
        else if (b >= 0xC2 && b <= 0xDF) len = 2;
        else if (b >= 0xE0 && b <= 0xEF) {
            len = 3;
            if (b == 0xE0) lo = 0xA0;
            if (b == 0xED) hi = 0x9F;
        } else if (b >= 0xF0 && b <= 0xF4) {
            len = 4;
            if (b == 0xF0) lo = 0x90;
            if (b == 0xF4) hi = 0x8F;
        }
        bool ok = len != 0 && i + len <= n;
        for (size_t k = 1; ok && k < len; ++k) {
            const unsigned char c = static_cast<unsigned char>(text[i + k]);
            ok = c >= (k == 1 ? lo : 0x80) && c <= (k == 1 ? hi : 0xBF);
        }
        if (ok) {
            out.append(text, i, len);
            i += len;
        } else {
            out += "\xEF\xBF\xBD";
            ++i;
        }
    }
    return out;
}

// `text` as valid UTF-8, whole when it has at most kQuotedChars code points, else its first
// kQuotedChars - 1 code points and an ellipsis. The cut falls on a code-point boundary.
std::string clipQuoted(const std::string& raw)
{
    const std::string text = validUtf8(raw);
    size_t points = 0;
    size_t cut = text.size();
    for (size_t i = 0; i < text.size(); ++i) {
        if ((static_cast<unsigned char>(text[i]) & 0xC0) == 0x80) continue;
        if (points == kQuotedChars - 1) cut = i;
        if (++points > kQuotedChars) return text.substr(0, cut) + "\xE2\x80\xA6";
    }
    return text;
}

void noteBadCell(const std::string& raw, const std::string& readAs)
{
    if (tlBadCells == nullptr || tlField.empty()) return;
    std::string where = tlWhere.empty() ? "" : tlWhere + " ";
    (*tlBadCells)[tlField].push_back(where + "'" + clipQuoted(raw) + "' read as " + readAs);
}

// The longest prefix of `s` that is a decimal number, [+-] digits [. digits] [e [+-] digits]
// with at least one mantissa digit, read in the classic locale whatever the process
// LC_NUMERIC. Returns the prefix's length (0 when `s` does not open with a number) and sets
// `value`; a prefix too large for a double sets `value` to infinity and one too small to 0,
// as strtod and Python's float() read them.
size_t parseDecimal(const std::string& s, double& value)
{
    auto isDigit = [](char c) { return c >= '0' && c <= '9'; };
    size_t i = 0;
    const bool negative = i < s.size() && s[i] == '-';
    if (i < s.size() && (s[i] == '+' || s[i] == '-')) ++i;
    size_t mantissa = 0;
    long long lead = 0;          // decimal exponent of the first non-zero mantissa digit + 1
    bool nonZero = false;
    while (i < s.size() && isDigit(s[i])) {
        if (nonZero || s[i] != '0') { nonZero = true; ++lead; }
        ++i; ++mantissa;
    }
    if (i < s.size() && s[i] == '.') {
        ++i;
        while (i < s.size() && isDigit(s[i])) {
            if (!nonZero && s[i] != '0') nonZero = true;
            else if (!nonZero) --lead;
            ++i; ++mantissa;
        }
    }
    if (mantissa == 0) { value = 0.0; return 0; }
    long long exponent = 0;
    if (i < s.size() && (s[i] == 'e' || s[i] == 'E')) {
        size_t j = i + 1;
        const bool down = j < s.size() && s[j] == '-';
        if (j < s.size() && (s[j] == '+' || s[j] == '-')) ++j;
        if (j < s.size() && isDigit(s[j])) {
            for (; j < s.size() && isDigit(s[j]); ++j) {
                if (exponent < 100000) exponent = exponent * 10 + (s[j] - '0');
            }
            if (down) exponent = -exponent;
            i = j;
        }
    }
    std::istringstream in(s.substr(0, i));
    in.imbue(std::locale::classic());
    double v = 0.0;
    in >> v;
    if (in.fail()) {
        v = (lead + exponent > 0) ? HUGE_VAL : 0.0;
        if (negative) v = -v;
    }
    value = v;
    return i;
}

// A finite `value` from 0 to below 1e16 as Python's repr writes it: the fewest decimals
// that read back as the same double, and ".0" for a whole number ("1.0", "2822.4"). Written
// in the classic locale.
std::string formatDecimal(double value)
{
    std::string text;
    for (int decimals = 0; decimals <= 17; ++decimals) {
        std::ostringstream out;
        out.imbue(std::locale::classic());
        out << std::fixed << std::setprecision(decimals) << value;
        text = out.str();
        std::istringstream back(text);
        back.imbue(std::locale::classic());
        double read = 0.0;
        back >> read;
        if (read == value) break;
    }
    if (text.find('.') == std::string::npos) text += ".0";
    return text;
}

// `value` as printf's %g writes it in the classic locale (six significant digits), as
// Python's format(value, "g") does: "0.5", "1e+12", "48000.5".
std::string formatGeneral(double value)
{
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out << std::setprecision(6) << value;
    return out.str();
}

std::string trim(const std::string& s)
{
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

// Decimal, 0x hex or 0b binary. A cell that is not a number, or whose value does not fit
// an int, reads as 0, and one with trailing characters as its leading digits; both are
// recorded (noteBadCell). A blank cell reads as 0 unrecorded. A 0x or 0b prefix is followed
// by a digit of its base (a sign or a space there is not a number), as the Python readers'
// 0[xX][0-9a-fA-F]+ and 0[bB][01]+. Hex and binary parse at 64 bits on every platform, so
// the same cell reads the same on all of them.
int parseInt(const std::string& raw)
{
    auto narrow = [](unsigned long long v) {
        if (v > static_cast<unsigned long long>(INT_MAX)) throw std::out_of_range("int");
        return static_cast<int>(v);
    };
    auto isHex = [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    };
    std::string s = trim(raw);
    if (s.empty()) return 0;
    int value = 0;
    size_t used = 0;
    size_t start = 0;
    try {
        if (s.size() > 2 && s[0] == '0' && (s[1] == 'b' || s[1] == 'B')) {
            start = 2;
            if (s[2] != '0' && s[2] != '1') throw std::invalid_argument("binary");
            value = narrow(std::stoull(s.substr(2), &used, 2));
        } else if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
            start = 2;
            if (!isHex(s[2])) throw std::invalid_argument("hex");
            value = narrow(std::stoull(s.substr(2), &used, 16));
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
    // ASCII only: a byte at or above 0x80 is kept as it is.
    std::transform(s.begin(), s.end(), s.begin(), [](char c) {
        return (c >= 'A' && c <= 'Z') ? static_cast<char>(c - 'A' + 'a') : c;
    });
    if (s == "true" || s == "1" || s == "yes") return true;
    if (!(s.empty() || s == "false" || s == "0" || s == "no")) noteBadCell(trim(raw), "false");
    return false;
}

// The most items a warning lists; the rest are counted. BusConfig's import warnings list
// the same number, so the two engines word a file's warnings alike.
constexpr size_t kListedItems = 3;
// The Reserved CDS_TailWidth value (0..2 UI are defined).
constexpr int kCdsTailReserved = 3;

// The first `listed` of `items` comma-joined, then "and N more" for the rest.
std::string joinList(const std::vector<std::string>& items, size_t listed = kListedItems)
{
    std::string out;
    const size_t shown = std::min(items.size(), listed);
    for (size_t i = 0; i < shown; ++i) {
        if (i) out += ", ";
        out += items[i];
    }
    if (items.size() > shown) out += " and " + std::to_string(items.size() - shown) + " more";
    return out;
}

// The rows the decode reads that a config CSV writer always writes, in the order the app
// writes them, each with the legacy scalar row that stands in for it (or nullptr). A file
// that carries neither spelling of a row is reported; Description is optional and the
// visualizer-only rows are not read.
struct ReadRow { const char* name; const char* legacy; };
const ReadRow kReadRows[] = {
    {"NumColumns_REG", nullptr}, {"SkippingDenominator_REG", nullptr},
    {"PHY3Enabled", nullptr}, {"S1TailWidth_REG", nullptr}, {"EnforceS1Handover", nullptr},
    {"CDS_BitWidth_REG", nullptr}, {"RowRate", nullptr},
    {"CDS_GuardEnabledPerSource", "CDS_GuardEnabled_REG"},
    {"CDS_GuardPolarityPerSource", "CDS_GuardPolarity_REG"},
    {"CDS_TailWidthPerSource", "CDS_TailWidth_REG"},
    {"CDS_DriveTypePerSource", "CDS_DriveType"},
    {"CDS_EndDriveEarlyPerSource", nullptr},
    {"Enabled", nullptr}, {"DeviceNumber_REG", nullptr}, {"PortDirection_REG", nullptr},
    {"EnableCh_REG", nullptr}, {"SampleSize_REG", nullptr}, {"SampleGrouping_REG", nullptr},
    {"ChannelGrouping_REG", nullptr}, {"Spacing_REG", nullptr}, {"Interval_REG", nullptr},
    {"Offset_REG", nullptr}, {"HorizontalStart_REG", nullptr}, {"HorizontalCount_REG", nullptr},
    {"BitWidth_REG", nullptr}, {"TailWidth_REG", nullptr}, {"SkippingNumerator_REG", nullptr},
    {"SubRowInterval_REG", nullptr}, {"FlowMode_REG", nullptr}, {"PortMode_REG", nullptr},
    {"ScramblerEn_REG", nullptr}, {"GuardEnable_REG", nullptr}, {"GuardPolarity_REG", nullptr},
    {"FCP_HorizontalStart_REG", nullptr}, {"FCP_BitWidth_REG", nullptr},
    {"FCP_TailWidth_REG", nullptr}, {"FCP_Offset_REG", nullptr},
    {"FCP_GuardEnable_REG", nullptr}, {"FCP_GuardPolarity_REG", nullptr},
    {"DataPortNumber", nullptr}, {"ManagerDataport", nullptr},
};

// "low bit" for a 1-bit field, else "low N bits".
std::string lowBits(int bits)
{
    return bits == 1 ? std::string("low bit") : "low " + std::to_string(bits) + " bits";
}

std::string dpLabel(size_t column) { return "DP" + std::to_string(column); }

std::string cdsSourceLabel(int index)
{
    return index == 0 ? std::string("Manager") : "Device " + std::to_string(index - 1);
}


// Quote state of a CSV row read as Python's csv reader reads it: a cell is quoted only when
// its first character is '"', and a quoted cell ends at a '"' that is not doubled (a doubled
// '"' is a literal quote). A '"' anywhere else is literal. Feed the row's text in pieces, in
// order; each character is scanned once, so a row that spans many lines reads in linear time.
struct CsvQuoteScan
{
    bool quoted = false;
    bool atCellStart = true;
    bool pendingQuote = false;   // a '"' inside a quoted cell, ending the piece before it

    void feed(const std::string& text)
    {
        for (const char c : text) {
            if (pendingQuote) {
                pendingQuote = false;
                if (c == '"') continue;          // doubled: a literal quote, still quoted
                quoted = false;                  // the '"' closed the cell
                atCellStart = false;
            }
            if (quoted) {
                if (c == '"') pendingQuote = true;
            } else if (c == '"' && atCellStart) {
                quoted = true;
            } else {
                atCellStart = (c == ',' || c == '\n');
            }
        }
    }
    // True when the text fed so far ends inside a quoted cell.
    bool open() const { return quoted && !pendingQuote; }
};

// The cells of one CSV row, trimmed, read with CsvQuoteScan's rules: a cell whose first
// character is '"' is quoted, its enclosing quotes are dropped and a doubled '""' inside
// reads as one '"'; characters after its closing quote join the cell. A ',' or line break
// inside a quoted cell is part of it. A row ending in ',' has an empty last cell.
std::vector<std::string> splitCsv(const std::string& line)
{
    std::vector<std::string> out;
    std::string cell;
    bool quoted = false, atCellStart = true;
    for (size_t i = 0; i < line.size(); ++i) {
        const char c = line[i];
        if (quoted) {
            if (c != '"') {
                cell += c;
            } else if (i + 1 < line.size() && line[i + 1] == '"') {
                cell += '"';
                ++i;
            } else {
                quoted = false;
            }
        } else if (c == '"' && atCellStart) {
            quoted = true;
            atCellStart = false;
        } else if (c == ',') {
            out.push_back(trim(cell));
            cell.clear();
            atCellStart = true;
        } else {
            cell += c;
            atCellStart = false;
        }
    }
    if (!line.empty()) out.push_back(trim(cell));
    return out;
}

// Reads one line of `in` into `line` and its ending into `eol`. A line ends at LF, CR LF
// or a lone CR, as Python's csv reader with newline='' splits them; `eol` is empty for a
// last line with no ending. Returns false at the end of the input with nothing read.
bool readCsvLine(std::istream& in, std::string& line, std::string& eol)
{
    using Traits = std::istream::traits_type;
    line.clear();
    eol.clear();
    bool any = false;
    for (Traits::int_type c = in.get(); !Traits::eq_int_type(c, Traits::eof()); c = in.get()) {
        any = true;
        if (c == '\n') { eol = "\n"; return true; }
        if (c == '\r') {
            if (in.peek() == '\n') { in.get(); eol = "\r\n"; }
            else eol = "\r";
            return true;
        }
        line += Traits::to_char_type(c);
    }
    return any;
}

// Opens `path`, which is UTF-8 (as Python passes it). On Windows a narrow std::ifstream path
// is read in the ANSI code page, so the path is converted to a wide path there. A directory
// is not opened: the returned stream is in the failed state.
std::ifstream openUtf8(const std::string& path)
{
#ifdef _WIN32
    return std::ifstream(std::filesystem::u8path(path));
#else
    struct stat st;
    if (::stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode)) {
        std::ifstream none;
        none.setstate(std::ios::failbit);
        return none;
    }
    return std::ifstream(path);
#endif
}

} // namespace

bool SwI3sConfig::LoadCsv(const std::string& path, std::string& error)
{
    warnings.clear();
    std::ifstream in = openUtf8(path);
    if (!in) {
        error = "Cannot open config CSV: " + path;
        warnings.push_back(error);
        return false;
    }

    // Unreadable cells by field (parseInt/parseBool), fields with values beyond their last
    // column, and the raw values that are narrowed on the way in.
    std::map<std::string, std::vector<std::string>> badCells;
    std::vector<std::string> fieldOrder;
    std::unordered_set<std::string> seenFields;      // the keys of fieldOrder
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
            if (!v[i].empty()) extraCells[v[0]].push_back("'" + clipQuoted(v[i]) + "'");
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
    int legacyDrive = -1;
    bool gotDrive = false;
    bool gotLegacyTail = false, gotLegacyDrive = false;
    std::map<std::string, int> fieldCount;
    // Visualizer-only fields the decode does not read; any other unrecognised key is
    // reported.
    static const std::set<std::string> kIgnoredFields = {
        "AppVersion", "Name", "RowsToDraw", "S0Width", "EnforceHandover",
        "EnforceCDSHandover", "DisplayFields", "Save file using excess one"};
    std::vector<std::string> unknownFields;          // in file order, each once
    std::unordered_set<std::string> unknownSeen;
    bool zeroRowRate = false;
    std::string outOfRangeRowRate;   // a RowRate outside the field's range (formatGeneral)
    bool blankDenominator = false;

    std::string line, eol;
    bool firstLine = true;
    size_t lineNo = 0;
    while (readCsvLine(in, line, eol)) {
        ++lineNo;
        // A UTF-8 byte-order mark before the first key is not part of it.
        if (firstLine && line.compare(0, 3, "\xEF\xBB\xBF") == 0) line.erase(0, 3);
        firstLine = false;
        // A quoted cell may span lines (a multi-line Description): the row continues
        // until the cell closes, and the cell keeps each line ending as written. One still
        // open at the end of the file takes the rest of the file, as Python's csv reader
        // reads it, and is reported.
        const size_t rowLine = lineNo;
        CsvQuoteScan quotes;
        quotes.feed(line);
        std::string more, moreEol;
        while (quotes.open() && readCsvLine(in, more, moreEol)) {
            line += eol;
            line += more;
            quotes.feed(eol);
            quotes.feed(more);
            eol = moreEol;
            ++lineNo;
        }
        if (quotes.open()) {
            warnings.push_back("Line " + std::to_string(rowLine) + " opens a quoted cell that "
                               "is not closed; the rest of the file is read as that cell.");
        }
        line = trim(line);
        if (line.empty()) continue;

        std::vector<std::string> v = splitCsv(line);
        if (v.empty()) continue;
        const std::string& key = v[0];
        const std::string val = (v.size() > 1) ? v[1] : "";
        tlField = key;
        tlWhere.clear();
        if (seenFields.insert(key).second) fieldOrder.push_back(key);
        bool recognised = true;

        // Interface-level fields.
        if (key == "NumColumns_REG")            NumColumns = parseInt(val);
        else if (key == "SkippingDenominator_REG") {
            // A blank cell reads as the register's reset value 1, and is reported.
            blankDenominator = val.empty();
            SkippingDenominator = blankDenominator ? 1 : parseInt(val);
        }
        else if (key == "PHY3Enabled")          PHY3Enabled = parseBool(val);
        else if (key == "S1TailWidth_REG")      S1TailWidth = parseInt(val);
        else if (key == "EnforceS1Handover")    S1Handover = parseBool(val);
        else if (key == "RowRate") {
            // Each row sets the rate, so a repeated row's last one is used. A blank cell,
            // one that is not a number, a non-finite or negative rate, or one outside the
            // field's range kMinRowRateKHz..kMaxRowRateKHz sets the default 0 (the decode
            // takes the row rate it measures). Each cell is reported once: a bad cell as
            // such, an out-of-range rate as out of range, a cell reading exactly 0 as 0.
            // The rate is decimal with a '.' point whatever the locale (parseDecimal): a
            // hexadecimal cell (0x...) and inf/nan are not numbers, as for Python's float().
            double rate = 0.0;
            const size_t used = parseDecimal(val, rate);
            const size_t digits = (!val.empty() && (val[0] == '+' || val[0] == '-')) ? 1 : 0;
            const bool hex = val.size() > digits + 1 && val[digits] == '0' &&
                             (val[digits + 1] == 'x' || val[digits + 1] == 'X');
            RowRateKHz = 0.0;
            zeroRowRate = false;
            outOfRangeRowRate.clear();
            if (val.empty()) {
                // unset
            } else if (hex || used == 0 || !std::isfinite(rate) || rate < 0.0) {
                noteBadCell(val, formatDecimal(RowRateKHz));
            } else if (rate != 0.0 && (rate < kMinRowRateKHz || rate > kMaxRowRateKHz)) {
                outOfRangeRowRate = formatGeneral(rate);
            } else if (used != val.size()) {
                RowRateKHz = rate;
                noteBadCell(val, formatDecimal(RowRateKHz));
            } else {
                RowRateKHz = rate;
                zeroRowRate = (rate == 0.0);
            }
        }
        else if (key == "Description")          description = val;

        // Control Data Stream. CDS_BitWidth is bus-wide; the rest are per source because
        // each register lives in that source's own CDS block (0x1100).
        else if (key == "CDS_BitWidth_REG")     CdsBitWidth = parseInt(val);
        else if (key == "CDS_GuardEnabledPerSource")  { guardEn.assign(SwI3sConfig::kCdsSources, 0);  setCds(v, guardEn);  gotGuard = true; }
        else if (key == "CDS_GuardPolarityPerSource") { guardPol.assign(SwI3sConfig::kCdsSources, 0); setCds(v, guardPol); }
        else if (key == "CDS_TailWidthPerSource")     { setCds(v, CdsTailWidth); gotTail = true; }
        else if (key == "CDS_DriveTypePerSource")     { setCds(v, CdsDriveType); gotDrive = true; }
        else if (key == "CDS_EndDriveEarlyPerSource") setCds(v, CdsEndDriveEarly);
        // Legacy scalars from a pre-per-source file.
        else if (key == "CDS_GuardEnabled_REG")  legacyGuardEn  = parseBool(val) ? 1 : 0;
        else if (key == "CDS_GuardPolarity_REG") legacyGuardPol = parseBool(val) ? 1 : 0;
        else if (key == "CDS_TailWidth_REG") {
            legacyTail = parseInt(val);
            gotLegacyTail = true;
        }
        // The scalar drive type is broadcast to every source; a blank cell keeps the default.
        else if (key == "CDS_DriveType") {
            if (!val.empty()) {
                legacyDrive = parseInt(val);
                gotLegacyDrive = true;
            }
        }

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
        // Explicit logical DP number 0-31; overrides the column-index default only when
        // the cell carries a non-negative value.
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
        else recognised = false;
        if (recognised) {
            ++fieldCount[key];
        } else if (!kIgnoredFields.count(key) && unknownSeen.insert(key).second) {
            unknownFields.push_back(clipQuoted(key));
        }
    }
    // A read error ends the loop as the end of the file would; the file is then not read.
    if (in.bad()) {
        error = "Cannot open config CSV: " + path;
        warnings.assign(1, error);
        return false;
    }

    // Recombine the split guard rows (0 off, 1 G0, 2 G1); a missing polarity row leaves the
    // enabled entries at G0. Each row is a 1-bit register field: a value other than 0 or 1
    // reads as 1 and is reported.
    std::vector<std::string> guardWide;
    auto noteGuardWidth = [&](const char* name, const std::vector<int>& values) {
        std::vector<std::string> over;
        for (int i = 0; i < static_cast<int>(values.size()); ++i) {
            if (values[i] != 0 && values[i] != 1)
                over.push_back(cdsSourceLabel(i) + " = " + std::to_string(values[i]));
        }
        if (!over.empty()) {
            guardWide.push_back(std::string(name) + " does not fit its 1-bit register field "
                                "(0..1) for " + joinList(over) + "; a value other than 0 reads as 1.");
        }
    };
    noteGuardWidth("CDS_GuardEnabledPerSource", guardEn);
    noteGuardWidth("CDS_GuardPolarityPerSource", guardPol);
    if (gotGuard) {
        for (int i = 0; i < SwI3sConfig::kCdsSources; ++i) {
            const bool en = guardEn[i] != 0;
            const bool pol = (i < static_cast<int>(guardPol.size())) && guardPol[i] != 0;
            CdsGuard[i] = en ? (pol ? 2 : 1) : 0;
        }
    } else if (legacyGuardEn > 0) {
        CdsGuard.assign(SwI3sConfig::kCdsSources, legacyGuardPol > 0 ? 2 : 1);
    }
    // A negative legacy scalar is ignored; it is reported below, worded as BusConfig words
    // it so one file read by both reports it once.
    if (!gotDrive && legacyDrive >= 0) {
        CdsDriveType.assign(SwI3sConfig::kCdsSources, legacyDrive);
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
    // The PHY3 CDS column, summed in 64 bits. A column past the int range is stored with
    // its low 5 bits (all CDS_HorizontalStart carries) under the largest such int.
    long long cdsStart = -1;
    if (PHY3Enabled) {
        cdsStart = 1LL + std::max(0, S1TailWidth) + (S1Handover ? 1 : 0);
        CdsHorizontalStart = (cdsStart <= INT_MAX)
            ? static_cast<int>(cdsStart)
            : static_cast<int>((INT_MAX & ~0x1F) | (cdsStart & 0x1F));
    }
    tlBadCells = nullptr;

    // SkippingDenominator is at least 1 (the register model's rule): a 0 or negative value
    // would skip every interval while the stream list reports the unskipped rate.
    const int rawDenominator = SkippingDenominator;
    if (SkippingDenominator < 1) SkippingDenominator = 1;

    collectWarnings(badCells, fieldOrder, extraCells, noDevice, rawSampleSize, rawEnableCh,
                    rawDpNumber, gotDpNumber);
    warnings.insert(warnings.end(), guardWide.begin(), guardWide.end());
    if (!gotTail && gotLegacyTail && legacyTail < 0) {
        warnings.push_back("CDS_TailWidth_REG " + std::to_string(legacyTail) +
                           " is negative and is ignored; the CDS sources keep their defaults.");
    }
    if (!gotDrive && gotLegacyDrive && legacyDrive < 0) {
        warnings.push_back("CDS_DriveType " + std::to_string(legacyDrive) +
                           " is negative and is ignored; the CDS sources keep their defaults.");
    }

    if (zeroRowRate) {
        warnings.push_back("RowRate is 0; the decode uses the row rate it measures.");
    }
    if (!outOfRangeRowRate.empty()) {
        warnings.push_back("RowRate " + outOfRangeRowRate + " kHz is outside " +
                           std::to_string(static_cast<int>(kMinRowRateKHz)) + ".." +
                           std::to_string(static_cast<int>(kMaxRowRateKHz)) +
                           " kHz; the decode uses the row rate it measures.");
    }
    if (blankDenominator) {
        warnings.push_back("SkippingDenominator_REG is blank; the decode and the expected "
                           "registers use its reset value 1.");
    } else if (rawDenominator < 1) {
        warnings.push_back("SkippingDenominator_REG " + std::to_string(rawDenominator) +
                           " is below 1; the decode and the expected registers use 1.");
    }
    if (PHY3Enabled) {
        if (S1TailWidth < 0) {
            warnings.push_back("S1TailWidth_REG " + std::to_string(S1TailWidth) +
                               " is negative; the CDS column is placed as for 0.");
        }
        if (cdsStart > 31) {
            warnings.push_back("S1TailWidth_REG " + std::to_string(S1TailWidth) +
                               " places the CDS at column " + std::to_string(cdsStart) +
                               ", beyond its 5-bit CDS_HorizontalStart field (0..31); the expected "
                               "registers carry its low 5 bits (" +
                               std::to_string(cdsStart & 0x1F) + ").");
        }
    }
    std::vector<std::string> missingRows;
    for (const ReadRow& r : kReadRows) {
        if (!seenFields.count(r.name) && !(r.legacy && seenFields.count(r.legacy)))
            missingRows.push_back(r.name);
    }
    if (!missingRows.empty()) {
        const bool one = missingRows.size() == 1;
        warnings.push_back(std::to_string(missingRows.size()) +
                           (one ? " row is missing and takes its default: "
                                : " rows are missing and take their defaults: ") +
                           joinList(missingRows) + ".");
    }
    if (!unknownFields.empty()) {
        warnings.push_back(std::string(unknownFields.size() == 1 ? "Field " : "Fields ") +
                           joinList(unknownFields) + (unknownFields.size() == 1 ? " is" : " are") +
                           " not a config field and " +
                           (unknownFields.size() == 1 ? "is" : "are") + " ignored.");
    }
    // A field given on more than one row takes the last row.
    for (const std::string& f : fieldOrder) {
        auto it = fieldCount.find(f);
        if (it != fieldCount.end() && it->second > 1) {
            warnings.push_back(f + " appears " + std::to_string(it->second) +
                               " times; the last row is used.");
        }
    }
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
                           std::to_string(max) + "); the expected registers carry its " +
                           lowBits(bits) + " (" + std::to_string(value & max) +
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
                               joinList(over) + "; the expected registers carry its " +
                               lowBits(f.bits) + ".");
        }
    }
    // A CDS_TailWidth of 3 fits its field but is Reserved: the value is kept and reported
    // once, worded as BusConfig's reserved_cds_tail_note so one file read by both reports it
    // once.
    std::vector<std::string> reservedTail;
    for (int i = 0; i < kCdsSources && i < static_cast<int>(CdsTailWidth.size()); ++i) {
        if (CdsTailWidth[i] == kCdsTailReserved)
            reservedTail.push_back(cdsSourceLabel(i) + " = " + std::to_string(kCdsTailReserved));
    }
    if (!reservedTail.empty()) {
        const std::string r = std::to_string(kCdsTailReserved);
        warnings.push_back("CDS_TailWidthPerSource: CDS_TailWidth " + r + " is Reserved (0..2 UI "
                           "are defined) for " + joinList(reservedTail) +
                           "; the value is kept as written, the grid draws a " + r +
                           "-UI tail and the expected registers carry " + r + ".");
    }

    // Per-port fields, for the ports that carry audio: one warning per field, listing ports.
    // EnableCh_REG is checked on every enabled port, since a value that does not fit its 16
    // bits can leave the port with no channel.
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
        const std::string at = dpLabel(k);
        if (d.Enabled && (rawEnableCh[k] < 0 || rawEnableCh[k] > 0xFFFF))
            enableWide.push_back(at + " = " + std::to_string(rawEnableCh[k]));
        if (!(d.Enabled && d.numChannels() > 0)) continue;
        if (rawSampleSize[k] != d.SampleSize)
            sizeMasked.push_back(at + " = " + std::to_string(rawSampleSize[k]) + " (read as " +
                                 std::to_string(d.SampleSize) + ")");
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
                           joinList(it->second) + "; the expected registers carry its " +
                           lowBits(f.bits) + " and the decode uses the value as written.");
    }
}

long long SwI3sConfig::patternPeriod(long long cap) const
{
    if (cap < 1) cap = 1;
    long long period = 1;
    for (const SwI3sDpConfig& d : dps) {
        if (!d.Enabled || d.numChannels() <= 0 || d.Interval < 0) continue;
        const long long rows = patternRows(d);
        if (rows < 1) continue;
        const long long step = period / std::gcd(period, rows);
        if (step > cap / rows) return cap;
        period = step * rows;
    }
    return period > cap ? cap : period;
}

double SwI3sConfig::SampleRateHz(const SwI3sDpConfig& d) const
{
    // RowRate is in kHz (e.g. 3072 -> 3.072 MHz rows/s). The fields are as the config
    // wrote them, so all arithmetic is in double: no field value overflows it, and a
    // result that is not a positive finite rate reads as 0.
    double rowRate = RowRateKHz * 1000.0;
    if (!(rowRate > 0.0) || !std::isfinite(rowRate)) {
        return 0.0;
    }
    auto rate = [](double hz) { return (hz > 0.0 && std::isfinite(hz)) ? hz : 0.0; };

    if (!d.SubRowInterval) {
        if (d.Interval < 0) return 0.0;   // no such interval: no rate
        // One sample group per Interval. Each group carries SampleGrouping+1
        // samples. Skipping drops a fraction of intervals.
        double base = (d.SampleGrouping + 1.0) * rowRate / (d.Interval + 1.0);
        if (d.SkippingNumerator == 0 || SkippingDenominator == 0) {
            return rate(base);
        }
        // A numerator >= denominator would skip every (or more than every) interval,
        // yielding a zero/negative rate; clamp to 0 rather than report a bogus value.
        if (d.SkippingNumerator >= SkippingDenominator) {
            return 0.0;
        }
        const double denom = SkippingDenominator;
        return rate(base * (denom - d.SkippingNumerator) / denom);
    }

    // SRI: one or more transports repeat within the horizontal window each row.
    const double txpSlot = d.txpEnabled() ? 1.0 : 0.0;
    const double transportWidth = d.numChannels() * (d.SampleSize + 1.0 + txpSlot)
                                * (d.SampleGrouping + 1.0) * (d.BitWidth + 1.0);
    const double availableCols = d.HorizontalCount + 1.0;

    double groupsPerRow;
    if (!(transportWidth > 0.0) || availableCols < transportWidth) {
        groupsPerRow = 0.0;
    } else if (d.Spacing == 0) {
        groupsPerRow = 1.0;   // Spacing 0 pauses until next row
    } else {
        const double gap = d.Spacing - 1.0;
        const double cadence = transportWidth + gap;
        groupsPerRow = (cadence > 0.0) ? std::floor((availableCols + gap) / cadence) : 0.0;
    }
    if (!(groupsPerRow > 0.0)) {
        return 0.0;
    }
    return rate(groupsPerRow * (d.SampleGrouping + 1.0) * rowRate);
}
