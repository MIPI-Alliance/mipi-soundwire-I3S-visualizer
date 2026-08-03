// SWI3S Command Transport Protocol parser (Section 8.1).
//
// Consumes the NRZS-decoded Column-0 CDS bit stream and reconstructs command
// phases: K.28.7 SPM -> 6-token Phase Header (PhaseID, 12-bit Device Mask,
// 8-bit Packet Length) -> Manager Packet (opcode + info/data + CRC16) ->
// per-phase response. The decoder always re-hunts the K.28.7 comma to frame
// the next phase, so it tolerates arbitrary idle gaps between phases.
//
// Scope note: WriteA32/ReadA32/Ping command fields and Ping peripheral status
// are decoded. Non-Ping peripheral responses and multi-phase ReadData payload
// are emitted at phase granularity for now (see TODOs) -- enough for control
// visibility and for register snooping (which only needs Write/Commit).

#ifndef SWI3S_CCOMMANDTRANSPORTPARSER_H
#define SWI3S_CCOMMANDTRANSPORTPARSER_H

#include <vector>
#include <LogicPublicTypes.h>

#include "C8b10bDecoder.h"
#include "SwI3sProtocolDefs.h"

struct SwI3sCommand
{
    swi3s::PhaseId phase = swi3s::kPhaseGetStatus;
    bool   headerValid = false;
    U16    deviceMask = 0;       // 12-bit, bit i = peripheral i selected
    U8     packetLength = 0;     // Manager Packet bytes (excludes CRC)

    bool   hasManagerPacket = false;
    U8     opcode = 0;
    std::vector<U8> packet;      // opcode + info + data bytes
    bool   crcValid = false;
    U16    crcReceived = 0;
    U16    crcComputed = 0;

    // Decoded command specifics.
    bool   hasAddress = false;
    U32    address = 0;
    std::vector<U8> data;        // write data (Write)
    bool   hasReadByteCount = false;
    U16    readByteCount = 0;   // R+1 bytes; U16 so field 0xFF (256-byte read) doesn't wrap to 0

    // SSPA / SSCR / DSCR (Announce/Commit) carry a group mask + Row_Delay used
    // to compute the Stream Synchronization Point (Section 9.1.13).
    bool   hasSyncPoint = false;     // true for SSPA/SSCR (opcode 0x00), not DSCR
    U8     groupMask = 0;            // Commit Group select (CG0..CG3 = bits 0..3)
    U8     rowDelay = 0;             // valid 14 or 15

    // Commit (SSCR/DSCR) result. The register commit happens only on
    // CONFIRM_COMMIT; CANCEL_COMMIT aborts it (Section 9.1.10).
    bool   isCommit = false;
    bool   commitConfirmed = false;
    bool   commitCancelled = false;

    // Peripheral Response (Write/ReadSetup/ReadData/Commit) and Manager HD10
    // Response (ReadSetup/ReadData/Commit) as Robust Token numbers, or -1.
    int    peripheralResponse = -1;
    int    managerResponse = -1;

    // ReadA32 returned payload (ReadSetup-with-data or ReadData phase). Read
    // data is plain 8b/10b D-codes (NOT descrambled) with its own CRC16.
    bool   hasReadData = false;
    std::vector<U8> readData;
    bool   readDataCrcValid = false;

    // Ping (GetStatus) peripheral status: token number per device, or -1 if
    // the slot was not a recognised token / no response.
    bool   hasPingStatus = false;
    int    pingStatus[swi3s::kMaxPeripherals];

    // Sample range on the bus.
    U64    startSample = 0;
    U64    endSample = 0;

    void clear();
    const char* PhaseName() const;
    const char* CommandName() const;  // resolves opcode within the phase

    // Device Mask cardinality rules (Section 8.1.2.2): GetStatus, Announce and
    // Commit may be multicast; Write / ReadSetup / ReadData / CalibratePhy must
    // select exactly one device.
    int SelectedDeviceCount() const
    {
        int n = 0;
        for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
            if (deviceMask & (1u << i)) ++n;
        }
        return n;
    }
    bool RequiresSingleDevice() const
    {
        return phase == swi3s::kPhaseWrite || phase == swi3s::kPhaseReadSetup ||
               phase == swi3s::kPhaseReadData || phase == swi3s::kPhaseCalibratePhy;
    }
    bool DeviceMaskValid() const
    {
        int n = SelectedDeviceCount();
        return RequiresSingleDevice() ? (n == 1) : (n >= 1);
    }
};

class CCommandTransportParser
{
public:
    CCommandTransportParser() { Reset(); }

    void Reset();

    // ShortProtocolSpacer (SLC 0x1083): selects the Peripheral->Manager spacer
    // size (10 vs 20 bits), which changes the Commit phase length. Defaults to
    // 0 (20-bit PM spacer) per reset.
    void SetShortProtocolSpacer(bool on) { mShortSpacer = on; }

    // Per-device hub depth (0..5). A peripheral behind N hub levels has its
    // response delayed by 2*N frame rows (i.e. 2*N CDS bits), so its response
    // token is read that many bits later. Addresses are assigned so deeper
    // devices respond later, so tokens never overlap. Persists across Reset().
    void SetHubDepths(const int depths[swi3s::kMaxPeripherals]);

    // Feed one NRZS-decoded CDS bit captured at the given sample. Returns true
    // when a command phase has completed; read it via Command().
    bool PushCdsBit(bool bit, U64 sampleNumber);

    const SwI3sCommand& Command() const { return mCmd; }

private:
    enum State {
        eHuntSpm,
        eHeader,
        ePacket,
        ePingSpacer,
        ePingStatus,
        eCommitMpSpacer,    // MP spacer before peripheral responses
        eCommitPeriphResp,  // 12 peripheral response tokens
        eCommitPmSpacer,    // PM spacer before the Manager Response
        eCommitMgrResp,     // CONFIRM_COMMIT / CANCEL_COMMIT (final symbol)
        // Write phase.
        eWriteSpacer,       // MP spacer before peripheral response
        eWriteResp,         // single peripheral response (final symbol)
        // ReadSetup / ReadData shared response + data path.
        eReadMpSpacer,      // MP spacer before peripheral response
        eReadResp,          // peripheral response token
        eReadDataSpacer,    // spacer before peripheral data packet
        eReadData,          // R+1 data bytes + 2 CRC bytes
        eReadPmSpacer,      // spacer(s) before the manager response
        eReadMgrResp,       // READ_DATA_OK / READ_DATA_ERROR (final symbol)
    };

    void onSymbol(const C8b10bDecoder::Symbol& sym, U64 sampleNumber);
    void finishHeader();
    void parseManagerPacket();
    void startReadDataPacket();    // size from pending read byte count
    bool emitAndReset();   // returns true (a command is ready)
    int  singleDeviceIndex() const;   // first set bit of deviceMask, or 0

    C8b10bDecoder mDecoder;
    State         mState;
    SwI3sCommand  mCmd;
    bool          mCommandReady;
    bool          mShortSpacer = false;

    // Header token accumulation.
    int mHeaderTokens[swi3s::kPhaseHeaderTokens];
    int mHeaderCount;

    // Manager Packet accumulation.
    std::vector<U8> mPacketBytes;  // packet + 2 CRC bytes
    int mPacketBytesNeeded;        // packetLength + 2

    // Response / data accumulation.
    int mSpacerSymbolsRemaining;
    int mPingDevice;
    int mCommitRespRemaining;      // peripheral response tokens still expected
    std::vector<U8> mReadBytes;    // peripheral data + 2 CRC bytes
    int mReadBytesNeeded;          // (R+1) data + 2 CRC

    // Per-device pending read: a deferred ReadSetup records its byte count so a
    // later ReadData phase (which carries no length/address) can size + label
    // its returned packet. Indexed by device number.
    int mPendingReadCount[swi3s::kMaxPeripherals];   // R+1 bytes, 0 = none
    U32 mPendingReadAddr[swi3s::kMaxPeripherals];

    U64 mLastSampleNumber;

    // Hub-delay state. mHubDepth survives Reset() (it is config, set once);
    // mSkipBitsRemaining is the sub-symbol gap currently being consumed before a
    // delayed device's response token.
    int mHubDepth[swi3s::kMaxPeripherals] = {0};
    int mSkipBitsRemaining = 0;
};

#endif // SWI3S_CCOMMANDTRANSPORTPARSER_H
