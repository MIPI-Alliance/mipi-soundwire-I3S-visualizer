// Column-count auto-detection for mid-stream / unconfigured captures.
//
// SWI3S does not signal the column count on the bus ({ASW3713} advises a
// receiver to "try all possible Column Counts until it reattaches"). Given a
// buffer of raw data-line levels (one per UI, starting on a rising clock edge),
// this hypothesizes each legal PHY2 column count (even, 2..32) AND each Column-0
// phase offset, reconstructs the Column-0 CDS bit stream by NRZS decoding, runs
// it through the command parser, and scores how many CRC-valid command phases
// result. The (count, offset) with the most valid phases wins -- a wrong guess
// almost never yields valid CRCs.
//
// The phase offset matters because, with >2 columns, several columns of a row
// fall on a rising clock edge, so "first rising edge" does not by itself locate
// Column 0; the offset disambiguates which rising-edge UI begins the row.

#ifndef SWI3S_CCOLUMNDETECTOR_H
#define SWI3S_CCOLUMNDETECTOR_H

#include <vector>
#include <LogicPublicTypes.h>

class CColumnDetector
{
public:
    // 'levels' holds the raw data-line level per UI (true = high), beginning on
    // a rising clock edge. Returns the detected column count, or 0 if none
    // validated. If 'offset' is non-null it receives the Column-0 phase offset
    // (0..count-1): Column 0 sits at indices where (i - offset) % count == 0.
    static int Detect(const std::vector<bool>& levels, int* offset = nullptr,
                      int minValidPhases = 2);

    // Score a single candidate: number of CRC-valid command phases found with
    // Column 0 at indices where (i - offset) % columnCount == 0.
    static int Score(const std::vector<bool>& levels, int columnCount,
                     int offset = 0);

    // Best Column-0 phase offset for a KNOWN column count (e.g. supplied by a
    // config CSV or forced), scored like Detect over even offsets. The count
    // alone doesn't locate Column 0 when >2 columns fall on rising edges, so the
    // phase must still be searched. Returns 0 if none validate.
    static int BestOffset(const std::vector<bool>& levels, int columnCount,
                          int minValidPhases = 1);
};

#endif // SWI3S_CCOLUMNDETECTOR_H
