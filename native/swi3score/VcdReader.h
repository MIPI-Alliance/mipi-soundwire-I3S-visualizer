// The value-change reader of an IEEE-1364 VCD (swi3s_studio/ingest/vcd.py routes through it).
//
// The caller hands the file's bytes in chunks of any size (`feed`), then calls `finish` at
// the end of the file. The bytes are read as the text Python's UTF-8 decoder gives with
// errors="replace" (each maximal ill-formed subpart is one U+FFFD), split on the characters
// str.isspace() accepts, ASCII and non-ASCII. A token is held as the UTF-8 of its characters,
// so an ident compares as the bytes of the same str. A token longer than kMaxTokenChars
// characters stops the scan with kTooLong when it is reached.
//
// The header is read a token at a time up to `$enddefinitions`: `$var` declares an ident
// (the first declaration of an ident decides; width "1" makes it one bit wide; a
// declaration of more than kMaxDeclWords words is skipped to its `$end`), and every other
// `$` block is skipped to its `$end` (`$enddefinitions` included). The value changes, in
// stream order:
//  - `#t` sets the time (a whole number below 2**63); a bare `#` is counted, a malformed one
//    stops the scan with kBadStamp; a time below the one before is counted;
//  - a token starting b/B/r/R is a value whose ident is the NEXT token, whatever it is: a
//    `b0`/`b1` (any case) of a one-bit ident is a 0/1 change, a `bx`/`bz` of one an x/z
//    value, and any other value of a watched ident outside $dumpoff that is not all x/z
//    is counted as unread;
//  - `0x`/`1x` is a 0/1 change of ident `x`;
//  - `$comment` skips to the next `$end`, `$dumpoff` opens a $dumpoff block and `$end`
//    closes it, and any other `$` token is a marker;
//  - any other token is an x/z value of the ident after its first character, counted for a
//    watched ident that has had a 0/1 value, outside $dumpoff.
// A block still open at the end of the file is reported by its keyword.
//
// Two modes:
//  - capture: the 0/1 changes of two idents become edges (several changes of one signal at
//    one time collapse to the level the step ends on), with the notes the import warnings
//    are made from, and the edge budget checked every `checkEvery` edges;
//  - counts: transitions per ident, stopping at the change after `maxChanges` changes.
//
// The scanner returns facts; the Python side words every warning and error.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace swi3score {

// A growable uint64 array in a caller-supplied allocator, grown by an eighth at a time.
class EdgeArray {
public:
    using Realloc = void* (*)(void*, std::size_t);
    using Free = void (*)(void*);

    EdgeArray(Realloc r, Free f) : realloc_(r), free_(f) {}
    ~EdgeArray();
    EdgeArray(const EdgeArray&) = delete;
    EdgeArray& operator=(const EdgeArray&) = delete;

    bool push(std::uint64_t v)
    {
        if (n_ == cap_ && !grow()) return false;
        p_[n_++] = v;
        return true;
    }
    void pop() { --n_; }
    std::size_t size() const { return n_; }
    std::uint64_t back() const { return p_[n_ - 1]; }
    // The buffer, shrunk to its size, handed to the caller (free it with the allocator's
    // Free); nullptr when empty. The array is empty afterwards.
    std::uint64_t* release();

private:
    bool grow();
    Realloc realloc_;
    Free free_;
    std::uint64_t* p_ = nullptr;
    std::size_t n_ = 0;
    std::size_t cap_ = 0;
};

class VcdScanner {
public:
    static constexpr std::size_t kMaxTokenChars = 1 << 16;
    static constexpr int kMaxDeclWords = 16;

    enum Status { kOk = 0, kStopped = 1, kError = 2 };
    enum Error { kNoError, kBadStamp, kTooLong, kTooLarge, kNoMemory };

    // One note on a watched line: a count and the time of the first one, and the order of
    // its first record among the notes of its kind.
    struct Note {
        std::uint64_t count = 0;
        std::uint64_t first = 0;
        std::uint64_t order = 0;
    };

    struct Line {
        explicit Line(EdgeArray::Realloc r, EdgeArray::Free f) : edges(r, f) {}
        std::string ident;
        bool oneBit = false;
        bool seen = false;          // has had a 0/1 value
        int last = -1;              // level of the latest 0/1 value; -1 before the first
        bool init = false;
        std::int64_t lastT = -1;    // time of the latest transition
        bool ascending = true;      // every edge is at or after the one before
        EdgeArray edges;
        Note xz, unread, same;
    };

    // Capture mode over `clock` and `data` (distinct). `budget` 0 checks nothing.
    VcdScanner(std::string clock, std::string data, std::int64_t budget,
               std::int64_t checkEvery, std::int64_t bytesPerEdge,
               EdgeArray::Realloc r, EdgeArray::Free f);
    // Counts mode; `maxChanges` < 0 reads every change.
    explicit VcdScanner(std::int64_t maxChanges);

    int feed(const std::uint8_t* p, std::size_t n);
    int finish();

    int status() const { return status_; }
    Error error() const { return error_; }
    const std::string& errorToken() const { return errTok_; }
    std::uint64_t errorEdges() const { return errEdges_; }
    std::uint64_t errorTime() const { return errTime_; }

    // Capture-mode results.
    Line& line(int k) { return *lines_[k]; }
    std::uint64_t backCount() const { return backCount_; }
    std::uint64_t backFirst() const { return backFirst_; }
    std::uint64_t bare() const { return bare_; }
    bool hasOpenBlock() const { return hasOpenBlock_; }
    const std::string& openBlock() const { return openBlock_; }
    bool headerOpen() const { return headerOpen_; }

    // Counts-mode results: (ident, transitions) in the order of each ident's first
    // transition, whether the scan stopped at the limit, and the time of the last change read.
    std::vector<std::pair<std::string, std::uint64_t>> counts() const;
    bool cut() const { return cut_; }
    std::uint64_t lastTimeRead() const { return tSeen_; }

private:
    enum Phase {
        kHTop, kHSkip, kHDecl, kHEndDefSkip,   // header
        kVTop, kVIdent, kVComment,             // value changes
    };
    enum ValueKind { kZero, kOne, kXz1, kAllXz, kOther };

    struct Entry {
        std::uint64_t key;
        int last;
        std::uint64_t count;
        std::uint64_t order;
    };

    // Tokenizer.
    bool token(const char* p, std::size_t n);         // false once the scan has stopped
    bool tokenChar(const char* bytes, std::size_t n);
    bool space();
    void toBuffer(std::size_t upto);
    bool fail(Error e);

    // Grammar.
    bool header(const char* p, std::size_t n);
    bool value(const char* p, std::size_t n);
    bool valueIdent(const char* p, std::size_t n);
    bool change(const char* ident, std::size_t n, int level);
    void unknown(const char* ident, std::size_t n);
    int watched(const char* ident, std::size_t n) const;
    void declare();
    void note(Note& rec, std::uint64_t& order);

    // Counts-mode ident table.
    std::uint64_t keyOf(const char* p, std::size_t n);
    Entry& entry(std::uint64_t key);

    bool capture_;
    int status_ = kOk;
    Error error_ = kNoError;
    std::string errTok_;
    std::uint64_t errEdges_ = 0, errTime_ = 0;

    // Tokenizer state.
    const std::uint8_t* chunk_ = nullptr;
    bool inTok_ = false;
    bool bufMode_ = false;
    std::size_t viewStart_ = 0;
    std::size_t chars_ = 0;
    std::string buf_;
    int u8need_ = 0;
    std::uint8_t u8lo_ = 0, u8hi_ = 0;
    std::uint32_t u8cp_ = 0;
    char u8raw_[4] = {0, 0, 0, 0};
    int u8len_ = 0;

    // Grammar state.
    Phase phase_ = kHTop;
    std::string skipKeyword_;
    int declWords_ = 0;
    bool declWidthOne_ = false;
    std::string declIdent_;
    std::unordered_set<std::string> declared_;
    std::unordered_set<std::string> oneBitNames_;
    std::unordered_set<std::uint64_t> oneBitKeys_;
    bool pendB_ = false;
    ValueKind pendKind_ = kOther;
    bool inDumpoff_ = false;
    std::uint64_t t_ = 0;
    std::uint64_t backCount_ = 0, backFirst_ = 0, bare_ = 0;
    bool hasOpenBlock_ = false;
    std::string openBlock_;
    bool headerOpen_ = false;

    // Capture mode.
    std::vector<std::unique_ptr<Line>> lines_;
    std::int64_t budget_ = 0, checkEvery_ = 0, bytesPerEdge_ = 0, nextCheck_ = 0;
    std::uint64_t xzOrder_ = 0, unreadOrder_ = 0, sameOrder_ = 0;

    // Counts mode.
    std::int64_t maxChanges_ = -1;
    std::uint64_t seen_ = 0, tSeen_ = 0;
    bool cut_ = false;
    std::uint64_t firstOrder_ = 0;
    std::vector<Entry> entries_;
    std::vector<std::int32_t> slots_;
    std::uint64_t mask_ = 0;
    std::uint64_t cacheKey_ = ~0ull;
    std::int32_t cacheIdx_ = -1;
    std::unordered_map<std::string, std::uint64_t> longIds_;
    std::vector<std::string> longNames_;
};

}  // namespace swi3score
