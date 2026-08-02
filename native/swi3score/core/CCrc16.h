// CRC-16 used by the SWI3S Command Transport Protocol Manager Packet.
//
// Section 8.1.13: polynomial x16+x15+x13+x9+x7+x6+x5+x3+x+1 = 0xA2EB (normal,
// non-reflected), init 0x0000, data fed MSB-first, no output reflection, no
// final XOR. Covers the packet bytes (opcode + info + data), not the CRC bytes.
// Worked vector: bytes {0x80,0x35} -> 0xCC89.

#ifndef SWI3S_CCRC16_H
#define SWI3S_CCRC16_H

#include <LogicPublicTypes.h>

class CCrc16
{
public:
    static const U16 kPoly = 0xA2EB;

    CCrc16() : mCrc(0) {}

    void Reset() { mCrc = 0; }

    void PushByte(U8 b)
    {
        for (int i = 7; i >= 0; --i) {
            bool inbit = (b >> i) & 1;
            bool top = ((mCrc >> 15) & 1) ^ inbit;
            mCrc = static_cast<U16>(mCrc << 1);
            if (top) {
                mCrc ^= kPoly;
            }
        }
    }

    U16 Value() const { return mCrc; }

    // Convenience: CRC over a byte span.
    static U16 Compute(const U8* data, size_t len)
    {
        CCrc16 c;
        for (size_t i = 0; i < len; ++i) {
            c.PushByte(data[i]);
        }
        return c.Value();
    }

private:
    U16 mCrc;
};

#endif // SWI3S_CCRC16_H
