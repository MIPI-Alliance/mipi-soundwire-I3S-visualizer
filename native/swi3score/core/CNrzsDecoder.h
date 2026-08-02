// NRZS line decoder for the PHY2 Control Data Stream.
//
// PHY2 line-codes the CDS as NRZS relative to the immediately preceding UI
// (Section 11.1.1.7, Table 134): a physical TOGGLE between consecutive UIs
// decodes to 0, no toggle decodes to 1. An undriven CDS bit is held at its
// previous level by the bus keeper and therefore decodes to 1.
//
// The reference UI is the one immediately before the CDS bit, which is the
// last column of the previous row -- not the previous CDS bit. So the caller
// must feed this decoder *every* UI level in order: call Observe() for non-CDS
// UIs to keep the reference current, and Decode() for the column-0 CDS UI.

#ifndef SWI3S_CNRZSDECODER_H
#define SWI3S_CNRZSDECODER_H

#include <LogicPublicTypes.h>

class CNrzsDecoder
{
public:
    CNrzsDecoder() : mPrev(BIT_LOW) {}

    // Seed the reference level (e.g. before the first UI). At cold/warm start
    // the Manager encodes Column 0 as if the previous UI was 0 ({ASW3708}).
    void Reset(BitState seed = BIT_LOW) { mPrev = seed; }

    // Update the reference with a UI that is not being decoded.
    void Observe(BitState level) { mPrev = level; }

    // Decode this UI's CDS bit against the reference, then advance the
    // reference. Returns the decoded bit (true = 1).
    bool Decode(BitState level)
    {
        bool bit = (level == mPrev);  // same -> 1, toggle -> 0
        mPrev = level;
        return bit;
    }

private:
    BitState mPrev;
};

#endif // SWI3S_CNRZSDECODER_H
