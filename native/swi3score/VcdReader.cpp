// The value-change reader of a VCD; see VcdReader.h.
#include "VcdReader.h"

#include <algorithm>
#include <cstring>

namespace swi3score {

namespace {

enum ByteClass : std::uint8_t { kTok = 0, kSpace = 1, kWide = 2 };

// The ASCII characters str.isspace() accepts are kSpace, every other ASCII byte kTok, and
// every byte of 0x80 or above kWide (decoded as UTF-8).
struct ClassTable {
    std::uint8_t c[256];
    ClassTable()
    {
        for (int b = 0; b < 256; ++b) c[b] = b < 0x80 ? kTok : kWide;
        for (int b : {0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x1F, 0x20}) c[b] = kSpace;
    }
};
const ClassTable kClass;

// The non-ASCII characters str.isspace() accepts.
inline bool IsWideSpace(std::uint32_t cp)
{
    return cp == 0x85 || cp == 0xA0 || cp == 0x1680 || (cp >= 0x2000 && cp <= 0x200A) ||
           cp == 0x2028 || cp == 0x2029 || cp == 0x202F || cp == 0x205F || cp == 0x3000;
}

const char kReplacement[3] = {'\xEF', '\xBF', '\xBD'};   // U+FFFD

inline bool Eq(const char* p, std::size_t n, const char* word, std::size_t len)
{
    return n == len && std::memcmp(p, word, len) == 0;
}
#define EQ(p, n, lit) Eq((p), (n), lit, sizeof(lit) - 1)

// Bytes in the UTF-8 character led by `b` (a token holds valid UTF-8).
inline std::size_t CharBytes(std::uint8_t b)
{
    return b < 0x80 ? 1 : b < 0xE0 ? 2 : b < 0xF0 ? 3 : 4;
}

inline bool IsXz(char c) { return c == 'x' || c == 'X' || c == 'z' || c == 'Z'; }

inline std::uint64_t Mix(std::uint64_t x)
{
    x ^= x >> 30; x *= 0xbf58476d1ce4e5b9ull;
    x ^= x >> 27; x *= 0x94d049bb133111ebull;
    return x ^ (x >> 31);
}

constexpr std::uint64_t kLongKeyBase = 1ull << 63;

}  // namespace

// ---- EdgeArray ----------------------------------------------------------------------------

EdgeArray::~EdgeArray()
{
    if (p_) free_(p_);
}

bool EdgeArray::grow()
{
    const std::size_t cap = cap_ + cap_ / 8 + 512;
    void* q = realloc_(p_, cap * sizeof(std::uint64_t));
    if (!q) return false;
    p_ = static_cast<std::uint64_t*>(q);
    cap_ = cap;
    return true;
}

std::uint64_t* EdgeArray::release()
{
    std::uint64_t* out = nullptr;
    if (n_) {
        void* q = realloc_(p_, n_ * sizeof(std::uint64_t));
        out = static_cast<std::uint64_t*>(q ? q : p_);
    } else if (p_) {
        free_(p_);
    }
    p_ = nullptr;
    n_ = cap_ = 0;
    return out;
}

// ---- VcdScanner ---------------------------------------------------------------------------

VcdScanner::VcdScanner(std::string clock, std::string data, std::int64_t budget,
                       std::int64_t checkEvery, std::int64_t bytesPerEdge,
                       EdgeArray::Realloc r, EdgeArray::Free f)
    : capture_(true), budget_(budget), checkEvery_(checkEvery), bytesPerEdge_(bytesPerEdge),
      nextCheck_(checkEvery)
{
    lines_.emplace_back(new Line(r, f));
    lines_.emplace_back(new Line(r, f));
    lines_[0]->ident = std::move(clock);
    lines_[1]->ident = std::move(data);
}

VcdScanner::VcdScanner(std::int64_t maxChanges) : capture_(false), maxChanges_(maxChanges) {}

bool VcdScanner::fail(Error e)
{
    error_ = e;
    status_ = kError;
    return false;
}

void VcdScanner::toBuffer(std::size_t upto)
{
    buf_.assign(reinterpret_cast<const char*>(chunk_) + viewStart_, upto - viewStart_);
    chars_ = upto - viewStart_;
    bufMode_ = true;
}

bool VcdScanner::tokenChar(const char* bytes, std::size_t n)
{
    if (!inTok_) {
        inTok_ = true;
        bufMode_ = true;
        buf_.assign(bytes, n);
        chars_ = 1;
    } else {
        buf_.append(bytes, n);
        ++chars_;
    }
    if (chars_ > kMaxTokenChars) return fail(kTooLong);
    return true;
}

bool VcdScanner::space()
{
    if (!inTok_) return true;
    inTok_ = false;
    return token(buf_.data(), buf_.size());
}

int VcdScanner::feed(const std::uint8_t* p, std::size_t n)
{
    if (status_ != kOk) return status_;
    chunk_ = p;
    std::size_t i = 0;
    while (i < n) {
        const std::uint8_t b = p[i];
        if (u8need_) {                                  // inside a multi-byte character
            if (b >= u8lo_ && b <= u8hi_) {
                u8raw_[u8len_++] = static_cast<char>(b);
                u8cp_ = (u8cp_ << 6) | (b & 0x3Fu);
                u8lo_ = 0x80;
                u8hi_ = 0xBF;
                ++i;
                if (--u8need_ == 0) {
                    const bool ok = IsWideSpace(u8cp_) ? space() : tokenChar(u8raw_, u8len_);
                    if (!ok) return status_;
                }
            } else {                                    // the subpart so far is one U+FFFD,
                u8need_ = 0;                            // and `b` is read again
                if (!tokenChar(kReplacement, 3)) return status_;
            }
            continue;
        }
        const std::uint8_t c = kClass.c[b];
        if (c == kTok) {
            if (!inTok_) {
                inTok_ = true;
                bufMode_ = false;
                viewStart_ = i;
            }
            std::size_t j = i + 1;
            while (j < n && kClass.c[p[j]] == kTok) ++j;
            if (bufMode_) {
                buf_.append(reinterpret_cast<const char*>(p) + i, j - i);
                chars_ += j - i;
                if (chars_ > kMaxTokenChars) {
                    fail(kTooLong);
                    return status_;
                }
            } else if (j - viewStart_ > kMaxTokenChars) {
                fail(kTooLong);
                return status_;
            }
            i = j;
            continue;
        }
        if (c == kSpace) {
            if (inTok_) {
                inTok_ = false;
                const bool ok = bufMode_
                    ? token(buf_.data(), buf_.size())
                    : token(reinterpret_cast<const char*>(p) + viewStart_, i - viewStart_);
                if (!ok) return status_;
            }
            ++i;
            continue;
        }
        // A byte of 0x80 or above starts a character (or is one ill-formed byte).
        if (inTok_ && !bufMode_) toBuffer(i);
        ++i;
        u8raw_[0] = static_cast<char>(b);
        u8len_ = 1;
        u8lo_ = 0x80;
        u8hi_ = 0xBF;
        if (b >= 0xC2 && b <= 0xDF) {
            u8need_ = 1; u8cp_ = b & 0x1Fu;
        } else if (b >= 0xE0 && b <= 0xEF) {
            u8need_ = 2; u8cp_ = b & 0x0Fu;
            if (b == 0xE0) u8lo_ = 0xA0;
            if (b == 0xED) u8hi_ = 0x9F;
        } else if (b >= 0xF0 && b <= 0xF4) {
            u8need_ = 3; u8cp_ = b & 0x07u;
            if (b == 0xF0) u8lo_ = 0x90;
            if (b == 0xF4) u8hi_ = 0x8F;
        } else if (!tokenChar(kReplacement, 3)) {
            return status_;
        }
    }
    if (inTok_ && !bufMode_) toBuffer(n);
    return status_;
}

int VcdScanner::finish()
{
    if (status_ != kOk) return status_;
    if (u8need_) {                                      // the file ends inside a character
        u8need_ = 0;
        if (!tokenChar(kReplacement, 3)) return status_;
    }
    if (inTok_) {
        inTok_ = false;
        if (!token(buf_.data(), buf_.size())) return status_;
    }
    switch (phase_) {
    case kHTop:
    case kHDecl:
        headerOpen_ = true;
        break;
    case kHSkip:
        hasOpenBlock_ = true;
        openBlock_ = skipKeyword_;
        headerOpen_ = true;
        break;
    case kHEndDefSkip:
        hasOpenBlock_ = true;
        openBlock_ = "$enddefinitions";
        break;
    case kVComment:
        hasOpenBlock_ = true;
        openBlock_ = "$comment";
        break;
    default:
        break;
    }
    return status_;
}

bool VcdScanner::token(const char* p, std::size_t n)
{
    switch (phase_) {
    case kVTop:
        return value(p, n);
    case kVIdent:
        return valueIdent(p, n);
    case kVComment:
        if (EQ(p, n, "$end")) phase_ = kVTop;
        return true;
    default:
        return header(p, n);
    }
}

// ---- header -------------------------------------------------------------------------------

bool VcdScanner::header(const char* p, std::size_t n)
{
    switch (phase_) {
    case kHTop:
        if (EQ(p, n, "$enddefinitions")) {
            phase_ = kHEndDefSkip;
        } else if (EQ(p, n, "$var")) {
            phase_ = kHDecl;
            declWords_ = 0;
            declWidthOne_ = false;
            declIdent_.clear();
        } else if (p[0] == '$' && !EQ(p, n, "$end")) {
            phase_ = kHSkip;
            skipKeyword_.assign(p, n);
        }
        break;
    case kHSkip:
        if (EQ(p, n, "$end")) phase_ = kHTop;
        break;
    case kHEndDefSkip:
        if (EQ(p, n, "$end")) {
            phase_ = kVTop;
            for (auto& ln : lines_) ln->oneBit = oneBitNames_.count(ln->ident) != 0;
        }
        break;
    case kHDecl:
        if (EQ(p, n, "$end")) {
            declare();
            phase_ = kHTop;
        } else if (declWords_ >= kMaxDeclWords) {     // not a declaration
            phase_ = kHSkip;
            skipKeyword_ = "$var";
        } else {
            if (declWords_ == 1) declWidthOne_ = n == 1 && p[0] == '1';
            if (declWords_ == 2) declIdent_.assign(p, n);
            ++declWords_;
        }
        break;
    default:
        break;
    }
    return true;
}

void VcdScanner::declare()
{
    if (declWords_ < 4 || !declared_.insert(declIdent_).second || !declWidthOne_) return;
    if (capture_)
        oneBitNames_.insert(declIdent_);
    else
        oneBitKeys_.insert(keyOf(declIdent_.data(), declIdent_.size()));
}

// ---- value changes ------------------------------------------------------------------------

bool VcdScanner::value(const char* p, std::size_t n)
{
    const char c = p[0];
    if (c == '#') {
        if (n == 1) {                                   // a bare "#"
            ++bare_;
            return true;
        }
        // A whole number of ticks below 2**63: ASCII digits, at most 19 after the zeros.
        std::size_t k = 1;
        while (k < n && p[k] == '0') ++k;
        bool ok = n - k <= 19;
        std::uint64_t v = 0;
        for (std::size_t j = 1; ok && j < n; ++j) {
            const unsigned d = static_cast<unsigned char>(p[j]) - '0';
            if (d > 9) ok = false;
            else v = v * 10 + d;
        }
        if (!ok || v >= (1ull << 63)) {
            errTok_.assign(p, n);
            return fail(kBadStamp);
        }
        if (v < t_) {
            if (!backCount_) backFirst_ = v;
            ++backCount_;
        }
        t_ = v;
        return true;
    }
    if (c == 'b' || c == 'B' || c == 'r' || c == 'R') {   // its ident is the next token
        pendB_ = c == 'b' || c == 'B';
        const char* v = p + 1;
        const std::size_t m = n - 1;
        if (m == 1 && v[0] == '0') {
            pendKind_ = kZero;
        } else if (m == 1 && v[0] == '1') {
            pendKind_ = kOne;
        } else if (m == 1 && IsXz(v[0])) {
            pendKind_ = kXz1;
        } else {
            pendKind_ = std::all_of(v, v + m, IsXz) ? kAllXz : kOther;
        }
        phase_ = kVIdent;
        return true;
    }
    if (c == '$') {
        if (EQ(p, n, "$comment")) phase_ = kVComment;
        else if (EQ(p, n, "$dumpoff")) inDumpoff_ = true;
        else if (EQ(p, n, "$end")) inDumpoff_ = false;
        return true;
    }
    if (c == '0' || c == '1') return change(p + 1, n - 1, c - '0');
    const std::size_t k = CharBytes(static_cast<std::uint8_t>(c));   // x/z or other value
    unknown(p + k, n - k);
    return true;
}

bool VcdScanner::valueIdent(const char* p, std::size_t n)
{
    phase_ = kVTop;
    if (pendB_) {
        bool oneBit;
        if (capture_) {
            const int k = watched(p, n);
            oneBit = k >= 0 && lines_[k]->oneBit;
        } else {
            oneBit = oneBitKeys_.count(keyOf(p, n)) != 0;
        }
        if (oneBit) {
            if (pendKind_ == kZero || pendKind_ == kOne) return change(p, n, pendKind_ == kOne);
            if (pendKind_ == kXz1) {
                unknown(p, n);
                return true;
            }
        }
    }
    if (capture_ && !inDumpoff_ && pendKind_ != kXz1 && pendKind_ != kAllXz) {
        const int k = watched(p, n);
        if (k >= 0) note(lines_[k]->unread, unreadOrder_);
    }
    return true;
}

int VcdScanner::watched(const char* p, std::size_t n) const
{
    for (int k = 0; k < static_cast<int>(lines_.size()); ++k) {
        const std::string& id = lines_[k]->ident;
        if (id.size() == n && std::memcmp(id.data(), p, n) == 0) return k;
    }
    return -1;
}

void VcdScanner::note(Note& rec, std::uint64_t& order)
{
    if (rec.count == 0) {
        rec.first = t_;
        rec.order = order++;
    }
    ++rec.count;
}

void VcdScanner::unknown(const char* p, std::size_t n)
{
    if (!capture_ || inDumpoff_) return;
    const int k = watched(p, n);
    if (k >= 0 && lines_[k]->seen) note(lines_[k]->xz, xzOrder_);
}

bool VcdScanner::change(const char* p, std::size_t n, int level)
{
    if (!capture_) {
        if (maxChanges_ >= 0 && seen_ >= static_cast<std::uint64_t>(maxChanges_)) {
            cut_ = true;
            status_ = kStopped;
            return false;
        }
        ++seen_;
        tSeen_ = t_;
        Entry& e = entry(keyOf(p, n));
        if (e.last < 0) {
            e.last = level;                             // the initial level
        } else if (level != e.last) {
            e.last = level;
            if (e.count++ == 0) e.order = firstOrder_++;
        }
        return true;
    }
    const int k = watched(p, n);
    if (k < 0) return true;
    Line& ln = *lines_[k];
    ln.seen = true;
    const auto t = static_cast<std::int64_t>(t_);
    if (ln.last < 0) {
        ln.init = level != 0;
        ln.last = level;
    } else if (level != ln.last) {
        // A further change in one time step: the step keeps the level it ends on.
        if (t == ln.lastT) note(ln.same, sameOrder_);
        if (t == ln.lastT && ln.edges.size() && ln.edges.back() == t_) {
            ln.edges.pop();
        } else {
            if (ln.edges.size() && t_ < ln.edges.back()) ln.ascending = false;
            if (!ln.edges.push(t_)) return fail(kNoMemory);
        }
        ln.last = level;
        ln.lastT = t;
    }
    if (budget_) {
        const auto edges = static_cast<std::int64_t>(lines_[0]->edges.size() +
                                                     lines_[1]->edges.size());
        if (edges >= nextCheck_) {
            nextCheck_ += checkEvery_;
            if (edges * bytesPerEdge_ > budget_) {
                errEdges_ = static_cast<std::uint64_t>(edges);
                errTime_ = t_;
                return fail(kTooLarge);
            }
        }
    }
    return true;
}

// ---- counts-mode ident table --------------------------------------------------------------

std::uint64_t VcdScanner::keyOf(const char* p, std::size_t n)
{
    if (n <= 7) {                                       // the bytes and the length, packed
        std::uint64_t key = static_cast<std::uint64_t>(n) << 56;
        for (std::size_t k = 0; k < n; ++k)
            key |= static_cast<std::uint64_t>(static_cast<unsigned char>(p[k])) << (8 * k);
        return key;
    }
    std::string s(p, n);
    auto it = longIds_.find(s);
    if (it != longIds_.end()) return it->second;
    const std::uint64_t key = kLongKeyBase + longNames_.size();
    longNames_.push_back(s);
    longIds_.emplace(std::move(s), key);
    return key;
}

VcdScanner::Entry& VcdScanner::entry(std::uint64_t key)
{
    if (key == cacheKey_) return entries_[static_cast<std::size_t>(cacheIdx_)];
    if (entries_.size() * 2 >= slots_.size()) {         // grow: keep the table half empty
        const std::size_t cap = std::max<std::size_t>(64, slots_.size() * 2);
        slots_.assign(cap, -1);
        mask_ = cap - 1;
        for (std::size_t j = 0; j < entries_.size(); ++j) {
            std::uint64_t h = Mix(entries_[j].key) & mask_;
            while (slots_[h] >= 0) h = (h + 1) & mask_;
            slots_[h] = static_cast<std::int32_t>(j);
        }
    }
    std::uint64_t h = Mix(key) & mask_;
    while (slots_[h] >= 0) {
        if (entries_[static_cast<std::size_t>(slots_[h])].key == key) break;
        h = (h + 1) & mask_;
    }
    if (slots_[h] < 0) {
        slots_[h] = static_cast<std::int32_t>(entries_.size());
        entries_.push_back(Entry{key, -1, 0, 0});
    }
    cacheKey_ = key;
    cacheIdx_ = slots_[h];
    return entries_[static_cast<std::size_t>(cacheIdx_)];
}

std::vector<std::pair<std::string, std::uint64_t>> VcdScanner::counts() const
{
    std::vector<const Entry*> moved;
    for (const Entry& e : entries_)
        if (e.count) moved.push_back(&e);
    std::sort(moved.begin(), moved.end(),
              [](const Entry* a, const Entry* b) { return a->order < b->order; });
    std::vector<std::pair<std::string, std::uint64_t>> out;
    out.reserve(moved.size());
    for (const Entry* e : moved) {
        std::string ident;
        if (e->key >= kLongKeyBase) {
            ident = longNames_[static_cast<std::size_t>(e->key - kLongKeyBase)];
        } else {
            const std::size_t len = static_cast<std::size_t>(e->key >> 56);
            for (std::size_t k = 0; k < len; ++k)
                ident.push_back(static_cast<char>((e->key >> (8 * k)) & 0xFF));
        }
        out.emplace_back(std::move(ident), e->count);
    }
    return out;
}

}  // namespace swi3score
