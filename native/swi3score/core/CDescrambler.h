// Payload descrambler for one SWI3S channel (Section 14.1.3 / 14.2.3).
//
// Multiplicative (self-synchronizing) 9-bit LFSR, polynomial 1 + Y^-5 + Y^-9.
// Register Q8..Q0; Q8 is the most-recent bit. Taps Q4 (Y^-5) and Q0 (Y^-9).
// The RECEIVED bit is shifted into Q8 (this keeps source/sink LFSRs in step).
// Seed 0b101010100 (Q8=1..Q0=0), loaded when the channel is enabled and when its
// scrambler enable (ScramblerEn, or FCP_ScramblerEn for an FCP) turns on; otherwise the
// LFSR runs on across every sample of the channel, commits included.
//
// A No-Toggle detector (FSM NT_0..NT_16) forces a bit inversion after 16
// consecutive unchanged received bits to bound DC content; the descrambler
// inverts its 17th recovered bit to stay in lockstep. Matches the spec's
// Test Cases 1-3.

#ifndef SWI3S_CDESCRAMBLER_H
#define SWI3S_CDESCRAMBLER_H

#include <LogicPublicTypes.h>
#include "SwI3sProtocolDefs.h"

class CDescrambler
{
public:
    CDescrambler() { Reset(); }

    void Reset()
    {
        mQ = static_cast<U16>(swi3s::kScramblerResetState);   // Q8..Q0
        mNtState = 0;
    }

    // Recover one data bit from a received (scrambled) bit.
    bool Descramble(bool received)
    {
        bool tap = (((mQ >> kTapBitY5) ^ (mQ >> kTapBitY9)) & 1) != 0;   // Q4 ^ Q0
        bool ntInvert = (mNtState == kNtLimit);
        bool recovered = received ^ tap ^ ntInvert;

        // Toggle = received ^ previous-received (= current Q8 before shift).
        bool prev = ((mQ >> 8) & 1) != 0;
        bool toggle = received ^ prev;

        // Shift the received bit into Q8 (Q8<-received, others shift to Q0).
        mQ = static_cast<U16>(((mQ >> 1) | (received ? 0x100 : 0)) & 0x1FF);

        // Advance the No-Toggle FSM.
        if (mNtState == kNtLimit) {
            mNtState = toggle ? 0 : 1;   // forced bit starts a fresh run
        } else {
            mNtState = toggle ? 0 : (mNtState + 1);
        }

        return recovered;
    }

    // Inverse of Descramble: given a clear (recovered) bit, produce the bit that
    // must be sent on the wire so that a peer descrambler recovers it. Runs the
    // identical LFSR/No-Toggle state machine, driven by the SENT bit, so a
    // scrambler and descrambler started from the same seed stay in lockstep:
    // Descramble(Scramble(clear)) == clear. Used by the simulator to synthesise
    // scrambled audio payload.
    bool Scramble(bool clear)
    {
        bool tap = (((mQ >> kTapBitY5) ^ (mQ >> kTapBitY9)) & 1) != 0;   // Q4 ^ Q0
        bool ntInvert = (mNtState == kNtLimit);
        bool sent = clear ^ tap ^ ntInvert;

        bool prev = ((mQ >> 8) & 1) != 0;
        bool toggle = sent ^ prev;

        mQ = static_cast<U16>(((mQ >> 1) | (sent ? 0x100 : 0)) & 0x1FF);

        if (mNtState == kNtLimit) {
            mNtState = toggle ? 0 : 1;
        } else {
            mNtState = toggle ? 0 : (mNtState + 1);
        }

        return sent;
    }

private:
    // Y^-k is register bit Q(9-k): Q8 holds the most recent bit. kTapBitY5 is the bit
    // index of the Y^-5 tap (Q4), kTapBitY9 that of the Y^-9 tap (Q0).
    static const int kTapBitY5 = static_cast<int>(swi3s::kScramblerStateBits - swi3s::kScramblerTap0);
    static const int kTapBitY9 = static_cast<int>(swi3s::kScramblerStateBits - swi3s::kScramblerTap1);
    static const int kNtLimit = static_cast<int>(swi3s::kNoToggleLimit);

    U16 mQ;
    int mNtState;
};

#endif // SWI3S_CDESCRAMBLER_H
