// Payload descrambler for one SWI3S channel (Section 14.1.3 / 14.2.3).
//
// Multiplicative (self-synchronizing) 9-bit LFSR, polynomial 1 + Y^-5 + Y^-9.
// Register Q8..Q0; Q8 is the most-recent bit. Taps Q4 (Y^-5) and Q0 (Y^-9).
// The RECEIVED bit is shifted into Q8 (this keeps source/sink LFSRs in step).
// Seed 0b101010100 (Q8=1..Q0=0), reloaded only when the channel is enabled;
// the LFSR runs continuously across all samples of the channel.
//
// A No-Toggle detector (FSM NT_0..NT_16) forces a bit inversion after 16
// consecutive unchanged received bits to bound DC content; the descrambler
// inverts its 17th recovered bit to stay in lockstep. Verified against the
// spec's Test Cases 1-3 (see test/test_descrambler.cpp).

#ifndef SWI3S_CDESCRAMBLER_H
#define SWI3S_CDESCRAMBLER_H

#include <LogicPublicTypes.h>

class CDescrambler
{
public:
    CDescrambler() { Reset(); }

    void Reset()
    {
        mQ = 0x154;   // 0b101010100, Q8..Q0
        mNtState = 0;
    }

    // Recover one data bit from a received (scrambled) bit.
    bool Descramble(bool received)
    {
        bool tap = (((mQ >> 4) ^ (mQ >> 0)) & 1) != 0;   // Q4 ^ Q0
        bool ntInvert = (mNtState == 16);
        bool recovered = received ^ tap ^ ntInvert;

        // Toggle = received ^ previous-received (= current Q8 before shift).
        bool prev = ((mQ >> 8) & 1) != 0;
        bool toggle = received ^ prev;

        // Shift the received bit into Q8 (Q8<-received, others shift to Q0).
        mQ = static_cast<U16>(((mQ >> 1) | (received ? 0x100 : 0)) & 0x1FF);

        // Advance the No-Toggle FSM.
        if (mNtState == 16) {
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
        bool tap = (((mQ >> 4) ^ (mQ >> 0)) & 1) != 0;   // Q4 ^ Q0
        bool ntInvert = (mNtState == 16);
        bool sent = clear ^ tap ^ ntInvert;

        bool prev = ((mQ >> 8) & 1) != 0;
        bool toggle = sent ^ prev;

        mQ = static_cast<U16>(((mQ >> 1) | (sent ? 0x100 : 0)) & 0x1FF);

        if (mNtState == 16) {
            mNtState = toggle ? 0 : 1;
        } else {
            mNtState = toggle ? 0 : (mNtState + 1);
        }

        return sent;
    }

private:
    U16 mQ;
    int mNtState;
};

#endif // SWI3S_CDESCRAMBLER_H
