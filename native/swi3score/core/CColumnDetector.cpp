// Column-count auto-detection. See CColumnDetector.h.

#include "CColumnDetector.h"
#include "CCommandTransportParser.h"
#include "SwI3sProtocolDefs.h"

int CColumnDetector::Score(const std::vector<bool>& levels, int columnCount,
                           int offset)
{
    if (columnCount < 2 || offset < 0 || offset >= columnCount ||
        static_cast<int>(levels.size()) < columnCount * 4) {
        return 0;
    }
    CCommandTransportParser parser;
    int valid = 0;

    // Column 0 of each row sits at indices i where (i - offset) % columnCount == 0.
    // The CDS bit there is NRZS-decoded against the immediately-preceding UI, so i must
    // be >= 1. Stride directly across those Column-0 indices instead of testing every
    // index with a modulo + continue (same bit sequence, columnCount× fewer iterations):
    // the first is `offset` itself, except offset 0 (index 0 has no preceding UI) starts
    // at the next row's Column 0, columnCount.
    size_t start = (offset >= 1) ? static_cast<size_t>(offset)
                                 : static_cast<size_t>(columnCount);
    for (size_t i = start; i < levels.size(); i += static_cast<size_t>(columnCount)) {
        bool cdsBit = (levels[i] == levels[i - 1]);  // NRZS: same -> 1, toggle -> 0
        if (parser.PushCdsBit(cdsBit, i)) {
            const SwI3sCommand& c = parser.Command();
            if (c.headerValid && c.hasManagerPacket && c.crcValid) {
                ++valid;
            }
        }
    }
    return valid;
}

int CColumnDetector::Detect(const std::vector<bool>& levels, int* offset,
                            int minValidPhases)
{
    int best = 0, bestScore = 0, bestOffset = 0;
    for (int c = swi3s::kMinColumnCount; c <= swi3s::kMaxColumnCount; c += 2) {
        // Column 0 begins on a rising clock edge. The window starts on a rising
        // edge (index 0), and edges alternate, so Column 0 falls on an even
        // index -> only even offsets are candidates.
        for (int off = 0; off < c; off += 2) {
            int s = Score(levels, c, off);
            if (s > bestScore) {
                bestScore = s;
                best = c;
                bestOffset = off;
            }
        }
    }
    if (bestScore < minValidPhases) {
        if (offset) *offset = 0;
        return 0;
    }
    if (offset) *offset = bestOffset;
    return best;
}

int CColumnDetector::BestOffset(const std::vector<bool>& levels, int columnCount,
                                int minValidPhases)
{
    if (columnCount < swi3s::kMinColumnCount) return 0;
    int bestOff = 0, bestScore = 0;
    // Column 0 begins on a rising clock edge (index 0 is rising, edges alternate),
    // so only even offsets are candidates — same as Detect.
    for (int off = 0; off < columnCount; off += 2) {
        int s = Score(levels, columnCount, off);
        if (s > bestScore) { bestScore = s; bestOff = off; }
    }
    return (bestScore >= minValidPhases) ? bestOff : 0;
}
