// Standard 8b/10b decoder + Robust Token matcher. See C8b10bDecoder.h.

#include "C8b10bDecoder.h"
#include "SwI3sProtocolDefs.h"

namespace {

// Standard 5b/6b code (RD-), abcdei with a@bit5. Index = 5-bit value (D.x).
const U8 kCode5b6bMinus[32] = {
    0x27, 0x1D, 0x2D, 0x31, 0x35, 0x29, 0x19, 0x38,
    0x39, 0x25, 0x15, 0x34, 0x0D, 0x2C, 0x1C, 0x17,
    0x1B, 0x23, 0x13, 0x32, 0x0B, 0x2A, 0x1A, 0x3A,
    0x33, 0x26, 0x16, 0x36, 0x0E, 0x2E, 0x1E, 0x2B
};

// Standard 3b/4b code (RD-), fghj with f@bit3. Index = 3-bit value (D.x.y).
const U8 kCode3b4bMinus[8] = {
    0xB, 0x9, 0x5, 0xC, 0xD, 0xA, 0x6, 0xE  // y=7 is primary (P7); A7 added below
};

// Robust Token raw 10-bit symbols (Table 38), index = token number.
const U16 kRobustToken[16] = {
    0x299, 0x196, 0x345, 0x32A, 0x2A5, 0x159, 0x2C6, 0x135,
    0x25A, 0x1A9, 0x236, 0x1CA, 0x0D5, 0x0BA, 0x269, 0x166
};

inline int popcount(unsigned v) {
    int c = 0;
    while (v) { c += (v & 1); v >>= 1; }
    return c;
}

struct DecodeTables {
    U8 dec6[64];  // abcdei -> 5-bit value, 0xFF = invalid
    U8 dec4[16];  // fghj   -> 3-bit value, 0xFF = invalid

    DecodeTables() {
        for (int i = 0; i < 64; ++i) dec6[i] = 0xFF;
        for (int i = 0; i < 16; ++i) dec4[i] = 0xFF;

        for (int x = 0; x < 32; ++x) {
            U8 c = kCode5b6bMinus[x];
            dec6[c] = static_cast<U8>(x);
            if (popcount(c) != 3) {  // non-neutral: complement is the RD+ form
                dec6[(~c) & 0x3F] = static_cast<U8>(x);
            }
        }
        // D.07 is the one *balanced* 5b/6b sub-block, so its complement is not
        // added by the loop above (popcount == 3). Both 111000 (RD-) and 000111
        // (RD+) are legal and decode to 7 -- mirror the 3b/4b D.x.7 handling
        // below. Without this, any byte with low-5-bits == 7 (0x07, 0x27, ...,
        // 0xE7) fails to decode whenever the transmitter emits the RD+ form,
        // which (being disparity-dependent) shows up as intermittent CRC errors
        // on Write packets / register data. See SwI3sProtocolDefs comma table.
        dec6[0x07] = 7;   // D.07 RD+ (000111)
        for (int y = 0; y < 8; ++y) {
            U8 c = kCode3b4bMinus[y];
            dec4[c] = static_cast<U8>(y);
            if (popcount(c) != 2) {
                dec4[(~c) & 0xF] = static_cast<U8>(y);
            }
        }
        // D.x.3 is the balanced 3b/4b sub-block (the 5b/6b D.07 analog): 1100
        // (RD-) and 0011 (RD+) are both legal and decode to 3, but the loop
        // skips the complement for balanced codes (popcount == 2), so 0011 was
        // unmapped -- the same intermittent-CRC failure mode as D.07 above.
        dec4[0x3] = 3;   // D.x.3 RD+ (0011)
        // D.x.A7 alternate encodings of y=7 (0111 / 1000).
        dec4[0x7] = 7;
        dec4[0x8] = 7;
    }
};

const DecodeTables gTables;

} // namespace

void C8b10bDecoder::Reset()
{
    mWindow = 0;
    mAligned = false;
    mBitsSinceSymbol = 0;
}

void C8b10bDecoder::ResyncSymbolPhase()
{
    // Keep alignment, but make the next 10 pushed bits a fresh symbol. Clearing
    // the window prevents stale gap bits from forming a spurious symbol/comma.
    mWindow = 0;
    mAligned = true;
    mBitsSinceSymbol = 0;
}

bool C8b10bDecoder::PushBit(bool bit, Symbol& out)
{
    mWindow = static_cast<U16>(((mWindow << 1) | (bit ? 1 : 0)) & 0x3FF);

    // A K.28.7 comma (re)establishes symbol alignment at any bit boundary.
    bool isComma = (mWindow == swi3s::kCommaSymbolRDm1) ||
                   (mWindow == swi3s::kCommaSymbolRDp1);
    if (isComma) {
        mAligned = true;
        mBitsSinceSymbol = 0;
        out.raw = mWindow;
        out.aligned = true;
        out.isComma = true;
        out.isControl = true;
        out.valid = true;
        out.byte = swi3s::kCommaByte;
        return true;
    }

    if (!mAligned) {
        return false;
    }

    if (++mBitsSinceSymbol < swi3s::kSymbolBits) {
        return false;
    }
    mBitsSinceSymbol = 0;

    out.raw = mWindow;
    out.aligned = true;
    out.isComma = false;
    bool ctrl = false, comma = false;
    U8 value = 0;
    out.valid = DecodeSymbol(mWindow, value, ctrl, comma);
    out.isControl = ctrl;
    out.byte = value;
    return true;
}

bool C8b10bDecoder::DecodeSymbol(U16 raw, U8& value, bool& isControl, bool& isComma)
{
    if (raw == swi3s::kCommaSymbolRDm1 || raw == swi3s::kCommaSymbolRDp1) {
        value = swi3s::kCommaByte;
        isControl = true;
        isComma = true;
        return true;
    }
    isComma = false;
    isControl = false;

    U8 x = gTables.dec6[(raw >> 4) & 0x3F];
    U8 y = gTables.dec4[raw & 0xF];
    if (x == 0xFF || y == 0xFF) {
        return false;
    }
    value = static_cast<U8>((y << 5) | x);
    return true;
}

int C8b10bDecoder::RobustToken(U16 rawSymbol)
{
    // Exact match first.
    for (int t = 0; t < 16; ++t) {
        if (kRobustToken[t] == rawSymbol) {
            return t;
        }
    }
    // Tokens are min Hamming distance 4 apart, so a single-bit error still
    // resolves to a unique nearest token.
    int best = -1, bestDist = 99;
    for (int t = 0; t < 16; ++t) {
        int d = popcount(static_cast<unsigned>(kRobustToken[t] ^ rawSymbol));
        if (d < bestDist) { bestDist = d; best = t; }
    }
    return (bestDist <= 1) ? best : -1;
}

U16 C8b10bDecoder::EncodeByte(U8 value)
{
    U8 x = value & 0x1F;
    U8 y = (value >> 5) & 0x7;
    return static_cast<U16>((kCode5b6bMinus[x] << 4) | kCode3b4bMinus[y]);
}

U16 C8b10bDecoder::EncodeToken(int token)
{
    return (token >= 0 && token < 16) ? kRobustToken[token] : 0;
}

U16 C8b10bDecoder::CommaSymbol()
{
    return swi3s::kCommaSymbolRDm1;
}
