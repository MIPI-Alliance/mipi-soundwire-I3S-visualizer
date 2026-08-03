// SWI3S PHY2 protocol constants.
//
// Values verified against the MIPI SoundWire I3S (SWI3S) Specification
// v1.1r06 via the project spec service. Items still to be transcribed from
// the spec during later build phases are marked "TODO(spec)".

#ifndef SWI3S_PROTOCOL_DEFS_H
#define SWI3S_PROTOCOL_DEFS_H

#include <vector>
#include <LogicPublicTypes.h>

namespace swi3s {

// ---------------------------------------------------------------------------
// Frame shape (Section 4.2.2, {ASW3710})
// ---------------------------------------------------------------------------
// "Column Count" is the number of UIs per row. The NumColumns register field
// holds Column Count - 1 (excess-1). Generic range is 2..32 columns; PHY2
// (FBCSE) additionally requires an EVEN column count.
static const int kMinColumnCount   = 2;
static const int kMaxColumnCount   = 32;

// All legal PHY2 column counts (even, 2..32), in ascending order. Used by the
// hypothesize-and-validate column detector and by the settings UI.
extern const std::vector<int> kPhy2ColumnCounts;

inline bool IsLegalPhy2ColumnCount(int columnCount)
{
    return (columnCount >= kMinColumnCount) && (columnCount <= kMaxColumnCount) &&
           ((columnCount & 1) == 0);
}

// Cold/warm-start fixed CDS placement for PHY1 & PHY2 ({ASW3707}).
static const int kColdStartColumnCount = 2;
static const int kCdsColumn            = 0;  // CDS_HorizontalStart is RO 0 on PHY1/PHY2
static const int kDlvCdsColumn         = 2;  // PHY3 (DLV) frames S1 at col 0, CDS at col 2

// ---------------------------------------------------------------------------
// CDS line coding (Section 11.1.1.7, Table 134, {ASW4108})
// ---------------------------------------------------------------------------
// PHY1 & PHY2 line-code the CDS as NRZS relative to the immediately preceding
// UI: a physical TOGGLE decodes to 0, no toggle decodes to 1. (This is the
// inverse sense of classic SoundWire NRZI, where a change means 1.)
inline bool NrzsDecode(BitState previousUi, BitState thisUi)
{
    return previousUi == thisUi;  // same level -> 1, toggled -> 0
}

// ---------------------------------------------------------------------------
// 8b/10b comma (Section 7.1.1.2, Table 37)
// ---------------------------------------------------------------------------
// K.28.7 is the Start-of-Phase Marker, the only symbol with two like bits
// followed by five opposite bits, so it is findable at any bit alignment.
static const U8  kCommaByte       = 0xFC;   // K.28.7
static const U16 kCommaSymbolRDm1 = 0x0F8;  // 0011111000, sets running disparity -1
static const U16 kCommaSymbolRDp1 = 0x307;  // 1100000111, sets running disparity +1
static const int kSymbolBits      = 10;

// ---------------------------------------------------------------------------
// Payload scrambler (Section 14.2.3, {ASW5308}/{ASW5309})
// ---------------------------------------------------------------------------
// Self-synchronizing LFSR, polynomial 1 + Y^-5 + Y^-9 (9-bit state),
// reset value 0b101010100. A No-Toggle Detector inverts the 17th input bit
// after 16 consecutive unchanged output bits. Applied per channel when
// DPn_ScramblerEn = 1.
static const unsigned kScramblerStateBits = 9;
static const unsigned kScramblerResetState = 0b101010100;
static const unsigned kScramblerTap0 = 5;   // Y^-5
static const unsigned kScramblerTap1 = 9;   // Y^-9
static const unsigned kNoToggleLimit = 16;

// ---------------------------------------------------------------------------
// System & Link Control register offsets (Section 15.2 / Table 170-171)
// ---------------------------------------------------------------------------
// Dual-ranked: _CURR = _NEXT + 0x40. A WriteA32 to a _NEXT register followed
// by a Commit (SSCR/DSCR) at an SSP applies the new value.
static const U32 kRegUiRateRange_Next   = 0x80;
static const U32 kRegNumColumns_Next    = 0x81;  // Column Count = field + 1
static const U32 kRegRowRateRange_Next  = 0x82;
static const U32 kRegCurrRankOffset     = 0x40;

// ---------------------------------------------------------------------------
// Command Transport Protocol (Section 8.1, Tables 66/67/97)
// ---------------------------------------------------------------------------

// A phase is: K.28.7 SPM, then a 6-token Phase Header, then (if Packet_Length
// > 0) the Manager Packet + CRC16, then the per-phase response slots, then a
// Protocol Spacer.
static const int kPhaseHeaderTokens = 6;  // PhaseID, DevMask H/M/L, PktLen H/L

// PhaseID token values (Table 66).
enum PhaseId {
    kPhaseGetStatus    = 0,
    kPhaseWrite        = 1,
    kPhaseReadSetup    = 2,
    kPhaseReadData     = 3,
    kPhaseCommit       = 4,
    kPhaseAnnounce     = 5,
    kPhaseCalibratePhy = 6,
};

// Command opcodes, interpreted per PhaseID (Table 97). The opcode is the first
// Manager Packet byte.
enum Opcode {
    kOpPing        = 0x00,  // GetStatus
    kOpReadA32     = 0x00,  // ReadSetup
    kOpWriteA32    = 0x00,  // Write
    kOpUnblock     = 0x01,  // Write
    kOpSspa        = 0x00,  // Announce
    kOpExitDormant = 0x01,  // Announce
    kOpSscr        = 0x00,  // Commit
    kOpDscr        = 0x01,  // Commit
    kOpInitCal     = 0x00,  // CalibratePhy
    kOpTrimCal     = 0x01,  // CalibratePhy
};

// Protocol Spacer sizes in bits (Table 68). PM default assumes
// ShortProtocolSpacer = 0 (reset default).
static const int kSpacerBitsMP = 10;  // Manager -> Peripheral
static const int kSpacerBitsPP = 10;  // Peripheral -> Peripheral
static const int kSpacerBitsPM = 20;  // Peripheral -> Manager (default)

static const int kMaxPeripherals = 12;  // 12-bit Device Mask

// CRC-16 over the Manager Packet (see CCrc16.h): poly 0xA2EB, init 0.

// ---------------------------------------------------------------------------
// Register address map (Section 15.2, Tables 163/164/166/167/169/170)
// The target device is selected by the Phase Header Device Mask; the 32-bit
// WriteA32/ReadA32 address is an offset WITHIN that peripheral's own space.
// Every peripheral has the identical layout. Dual-ranked registers: _CURR =
// _NEXT + 0x40; a Commit (SSCR/DSCR) promotes _NEXT -> _CURR.
namespace reg {

static const U32 kCurrRankOffset = 0x40;

// System & Link Control block (base 0x1000), one per device.
static const U32 kSlcBase            = 0x1000;
static const U32 kDeviceNumber       = 0x1000;  // [3:0]
static const U32 kSkippingDenomHi    = 0x1012;  // [11:8]  (MSB-first)
static const U32 kSkippingDenomLo    = 0x1013;  // [7:0]
static const U32 kSyncPointOffset    = 0x1014;  // [2:0], valid 0-5
static const U32 kUiRateRange_Next   = 0x1080;  // [4:0]
static const U32 kNumColumns_Next    = 0x1081;  // [4:0], Column Count = +1
static const U32 kRowRateRange_Next  = 0x1082;  // [7:0]
static const U32 kShortSpacer_Next   = 0x1083;  // bit0

// Data Port blocks: DPn at 0x2000 + 256*n, n = 0..31.
static const U32 kDpBase   = 0x2000;
static const U32 kDpStride = 0x100;
static const int kMaxDataPorts = 32;

inline U32 DpAddr(int n, U32 offset) { return kDpBase + kDpStride * n + offset; }
inline bool InDpBlock(U32 addr) { return addr >= kDpBase && addr < kDpBase + kDpStride * kMaxDataPorts; }
inline int  DpIndexOf(U32 addr) { return static_cast<int>((addr - kDpBase) / kDpStride); }
inline U32  DpOffsetOf(U32 addr) { return (addr - kDpBase) % kDpStride; }

// DP register offsets within a block. Single-ranked control (0x00-0x3F) apply
// immediately; transport (_NEXT 0x80-0xBF) need a Commit to take effect.
static const U32 kDpSampleSizeGrouping = 0x09;  // [7:5]=SampleGrouping(ex-1), [4:0]=SampleSize(ex-1)
static const U32 kDpCommitGroupMemb    = 0x0A;  // [3:0] = membership of CG0..CG3 (reset 0b0001)
static const U32 kDpScramDirMode       = 0x0B;  // bit3=ScramblerEn, bit2=PortDirection, [1:0]=PortMode
static const U32 kDpSkipNumLo          = 0x0C;  // [7:0]   (LSB-first)
static const U32 kDpSkipNumHi          = 0x0D;  // [3:0] -> [11:8]
static const U32 kDpFlowMode           = 0x0E;  // [1:0]
static const U32 kDpBitWidthHStart_N   = 0x80;  // [7:6]=BitWidth(ex-1), [4:0]=HorizontalStart
static const U32 kDpEnableCh0HCount_N  = 0x81;  // bit7=EnableCh0, [4:0]=HorizontalCount(ex-1)
static const U32 kDpTailSubSpacing_N   = 0x82;  // [7:6]=TailWidth, bit5=SubRowInterval, [3:0]=Spacing
static const U32 kDpOffIntLo_N         = 0x83;  // [7:4]=Offset[3:0], [3:0]=Interval[3:0]
static const U32 kDpIntHi_N            = 0x84;  // Interval[11:4]   (Interval ex-1)
static const U32 kDpOffHi_N            = 0x85;  // Offset[11:4]
static const U32 kDpChannelGrouping_N  = 0x87;  // [3:0]
static const U32 kDpGuard_N            = 0x8A;  // bit1=GuardEnable, bit0=GuardPolarity
static const U32 kDpEnableCh1_7_N      = 0x90;  // bits[7:1] = ch1..7 (bit0 reserved)
static const U32 kDpEnableCh8_15_N     = 0x91;  // bits[7:0] = ch8..15
static const U32 kDpFcpBwHStart_N      = 0xA0;  // [7:6]=FCP_BitWidth, [4:0]=FCP_HorizontalStart
static const U32 kDpFcpTail_N          = 0xA2;  // [7:6]=FCP_TailWidth
static const U32 kDpFcpOffLo_N         = 0xA3;  // [7:4]=FCP_Offset[3:0]
static const U32 kDpFcpOffHi_N         = 0xA5;  // FCP_Offset[11:4]
static const U32 kDpFcpGuard_N         = 0xAA;  // bit1=FCP_GuardEnable, bit0=FCP_GuardPolarity

// EnableCh is dual-ranked (it lives in the _NEXT 0x80-0xBF transport block above), so
// each of its three registers has a _CURR alias at offset + kCurrRankOffset (Section
// 15.2): the byte a manager writes directly to _CURR takes effect on the COMMITTED
// state with no Commit (bypassing the normal _NEXT -> _CURR promotion at an SSP).
static const U32 kDpEnableCh0HCount_Curr = kDpEnableCh0HCount_N + kCurrRankOffset;  // 0xC1
static const U32 kDpEnableCh1_7_Curr     = kDpEnableCh1_7_N     + kCurrRankOffset;  // 0xD0
static const U32 kDpEnableCh8_15_Curr    = kDpEnableCh8_15_N    + kCurrRankOffset;  // 0xD1

// True if `off` (a DP-block byte offset, e.g. from DpOffsetOf) is one of the three
// EnableCh _CURR register bytes (ch0, ch1-7, ch8-15).
inline bool IsEnableChCurrOffset(U32 off)
{
    return off == kDpEnableCh0HCount_Curr || off == kDpEnableCh1_7_Curr ||
           off == kDpEnableCh8_15_Curr;
}

} // namespace reg

// ---------------------------------------------------------------------------
// SSP anchoring (Section 9.1.13)
// ---------------------------------------------------------------------------
// Sync_Point_Delay = Row_Delay - SyncPointOffset (rows, 9..15). The SSP occurs
// that many Row Sync Points after the Phase's Command Reference Point. Row_Delay
// is a Manager-Packet byte in SSPA/SSCR/DSCR (byte after opcode + group mask);
// valid values 14 or 15. SyncPointOffset is SLC 0x1014 (0..5, default 0).
static const U8  kSspRowDelayMin = 14;
static const U8  kSspRowDelayMax = 15;
static const int kSspDefaultSyncPointOffset = 0;

// TODO(spec): transcribe the §14.2.5 within-sample bit / channel ordering notes
// already captured in CDataPort; nothing further outstanding here.

} // namespace swi3s

#endif // SWI3S_PROTOCOL_DEFS_H
