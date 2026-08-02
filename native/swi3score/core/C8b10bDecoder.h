// Standard IBM/ANSI 8b/10b symbol decoder for the SWI3S Control Data Stream.
//
// SWI3S uses canonical 8b/10b (Section 7.1.3): 5b/6b + 3b/4b sub-blocks. The
// only K-code the protocol uses is K.28.7 (the Start-of-Phase Marker). Wire
// order is a b c d e i f g h j with 'a' first, so as we shift bits in MSB-first
// the assembled 10-bit value has a@bit9 .. j@bit0; abcdei = sym>>4, fghj = sym&0xF.
//
// Robust Tokens (Section 7.1.4, Table 38) are 16 fixed, zero-disparity D-code
// symbols used for Phase Header / response fields; they are matched directly
// against the raw 10-bit symbol (RobustToken()).

#ifndef SWI3S_C8B10BDECODER_H
#define SWI3S_C8B10BDECODER_H

#include <LogicPublicTypes.h>

class C8b10bDecoder
{
public:
    struct Symbol {
        U16  raw = 0;          // 10-bit codeword, a@bit9 .. j@bit0
        bool aligned = false;  // symbol boundary is locked
        bool isComma = false;  // K.28.7 SPM
        bool isControl = false;// any K-code
        bool valid = false;    // decoded to a legal D/K code
        U8   byte = 0;         // decoded 8-bit value (D-codes)
    };

    C8b10bDecoder() { Reset(); }

    // Drop symbol alignment (e.g. after sync loss).
    void Reset();

    // Realign the symbol boundary to start fresh at the NEXT pushed bit, keeping
    // alignment locked. Used after a hub-delay gap (a sub-symbol run of CDS bits
    // that belongs to no token was consumed outside the decoder): the following
    // 10 bits then frame the delayed response symbol.
    void ResyncSymbolPhase();

    // Feed one CDS wire bit. Returns true and fills 'out' whenever a complete
    // symbol is available: on every K.28.7 comma (which (re)establishes
    // alignment) and, once aligned, every 10 bits.
    bool PushBit(bool bit, Symbol& out);

    // Framing lock. While set, PushBit does NOT treat a K.28.7 bit-pattern as a
    // comma/realignment event — it holds the existing symbol boundary by counting
    // bits mod 10 (SWI3S §7.2.2 {ASW1907}). The Command Transport parser engages
    // this while it is consuming a phase's known-length DATA region (Manager Packet
    // payload+CRC, or ReadData payload+CRC), where uncontrolled byte values — the
    // CRC especially — can accidentally form the 10-bit SPM pattern at a sub-symbol
    // offset. Per Figures 88-90 / {ASW2206} an SPM mid-phase is only the ARMING
    // event for header detection and must not, by itself, re-frame or abandon the
    // in-progress phase. Cleared at each phase boundary so a genuine next-phase SPM
    // still (re)aligns normally.
    void SetFramingLocked(bool locked) { mFramingLocked = locked; }
    bool FramingLocked() const { return mFramingLocked; }

    // Match a raw 10-bit symbol against the 16 Robust Tokens (Table 38).
    // Returns the token number 0..15, or -1 if it is not a (near) token.
    static int RobustToken(U16 rawSymbol);

    // Decode a raw symbol as a standard 8b/10b code. Returns false if the
    // codeword is not a legal D-code or the K.28.7 comma.
    static bool DecodeSymbol(U16 rawSymbol, U8& value, bool& isControl, bool& isComma);

    // --- Encoding (for the simulator and round-trip tests) ---
    // Produces valid codewords the decoder above accepts. Running disparity is
    // not tracked: the RD- D-code variant is emitted (both variants decode to
    // the same value), which is sufficient for stimulus generation.
    static U16 EncodeByte(U8 value);     // D-code 10-bit symbol (a@bit9..j@bit0)
    static U16 EncodeToken(int token);   // Robust Token symbol, token 0..15
    static U16 CommaSymbol();            // K.28.7 SPM

private:
    U16  mWindow;      // rolling last-10-bits
    bool mAligned;
    int  mBitsSinceSymbol;
    bool mFramingLocked = false;  // hold symbol boundary through a data region (see SetFramingLocked)
};

#endif // SWI3S_C8B10BDECODER_H
