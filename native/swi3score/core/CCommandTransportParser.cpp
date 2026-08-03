// SWI3S Command Transport Protocol parser. See CCommandTransportParser.h.

#include "CCommandTransportParser.h"
#include "CCrc16.h"
#include "SwI3sResponseNames.h"

void SwI3sCommand::clear()
{
    phase = swi3s::kPhaseGetStatus;
    headerValid = false;
    deviceMask = 0;
    packetLength = 0;
    hasManagerPacket = false;
    opcode = 0;
    packet.clear();
    crcValid = false;
    crcReceived = 0;
    crcComputed = 0;
    hasAddress = false;
    address = 0;
    data.clear();
    hasReadByteCount = false;
    readByteCount = 0;
    hasSyncPoint = false;
    groupMask = 0;
    rowDelay = 0;
    isCommit = false;
    commitConfirmed = false;
    commitCancelled = false;
    peripheralResponse = -1;
    managerResponse = -1;
    hasReadData = false;
    readData.clear();
    readDataCrcValid = false;
    hasPingStatus = false;
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
        pingStatus[i] = -1;
    }
    startSample = 0;
    endSample = 0;
}

const char* SwI3sCommand::PhaseName() const
{
    switch (phase) {
    case swi3s::kPhaseGetStatus:    return "GetStatus";
    case swi3s::kPhaseWrite:        return "Write";
    case swi3s::kPhaseReadSetup:    return "ReadSetup";
    case swi3s::kPhaseReadData:     return "ReadData";
    case swi3s::kPhaseCommit:       return "Commit";
    case swi3s::kPhaseAnnounce:     return "Announce";
    case swi3s::kPhaseCalibratePhy: return "CalibratePhy";
    default:                        return "?";
    }
}

const char* SwI3sCommand::CommandName() const
{
    switch (phase) {
    case swi3s::kPhaseGetStatus: return "Ping";
    case swi3s::kPhaseWrite:     return (opcode == swi3s::kOpUnblock) ? "Unblock" : "WriteA32";
    case swi3s::kPhaseReadSetup: return "ReadA32";
    case swi3s::kPhaseReadData:  return "ReadData";
    case swi3s::kPhaseCommit:    return (opcode == swi3s::kOpDscr) ? "DSCR" : "SSCR";
    case swi3s::kPhaseAnnounce:  return (opcode == swi3s::kOpExitDormant) ? "ExitDormant" : "SSPA";
    case swi3s::kPhaseCalibratePhy: return (opcode == swi3s::kOpTrimCal) ? "TrimCal" : "InitCal";
    default: return "?";
    }
}

void CCommandTransportParser::Reset()
{
    mDecoder.Reset();
    mState = eHuntSpm;
    mCmd.clear();
    mCommandReady = false;
    mHeaderCount = 0;
    mPacketBytes.clear();
    mPacketBytesNeeded = 0;
    mSpacerSymbolsRemaining = 0;
    mPingDevice = 0;
    mCommitRespRemaining = 0;
    mReadBytes.clear();
    mReadBytesNeeded = 0;
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
        mPendingReadCount[i] = 0;
        mPendingReadAddr[i] = 0;
    }
    mLastSampleNumber = 0;
    mSkipBitsRemaining = 0;   // mHubDepth is config: deliberately NOT cleared here
}

void CCommandTransportParser::SetHubDepths(const int depths[swi3s::kMaxPeripherals])
{
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
        int d = depths[i];
        if (d < 0) d = 0;
        if (d > 5) d = 5;
        mHubDepth[i] = d;
    }
}

int CCommandTransportParser::singleDeviceIndex() const
{
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
        if (mCmd.deviceMask & (1u << i)) return i;
    }
    return 0;
}

bool CCommandTransportParser::PushCdsBit(bool bit, U64 sampleNumber)
{
    mLastSampleNumber = sampleNumber;
    mCommandReady = false;

    // Hub-delay gap: a delayed device's response token starts 2*depth CDS bits
    // later. Consume those gap bits WITHOUT feeding the decoder, then realign the
    // symbol boundary so the next 10 bits frame the token. (Only ever > 0 when a
    // device has hub depth set, so the no-hub path is unchanged.)
    if (mSkipBitsRemaining > 0) {
        if (--mSkipBitsRemaining == 0) {
            mDecoder.ResyncSymbolPhase();
        }
        return false;
    }

    C8b10bDecoder::Symbol sym;
    if (mDecoder.PushBit(bit, sym)) {
        onSymbol(sym, sampleNumber);
    }
    return mCommandReady;
}

void CCommandTransportParser::onSymbol(const C8b10bDecoder::Symbol& sym, U64 sampleNumber)
{
    // A comma (K.28.7 SPM) always (re)starts a phase, wherever it appears. Normally it
    // only occurs at a phase boundary (where emitAndReset already cleared everything),
    // but a comma landing MID-PHASE is a resync after a framing glitch — so clear ALL
    // per-phase accumulators here too, or stale counters from the interrupted phase
    // (read bytes, spacer countdown, ping/commit progress) would corrupt the parse
    // until a full clean phase realigned them. mHubDepth is config and is preserved.
    if (sym.isComma) {
        mCmd.clear();
        mCmd.startSample = sampleNumber;
        mState = eHeader;
        mHeaderCount = 0;
        mPacketBytes.clear();
        mPacketBytesNeeded = 0;
        mSpacerSymbolsRemaining = 0;
        mPingDevice = 0;
        mCommitRespRemaining = 0;
        mReadBytes.clear();
        mReadBytesNeeded = 0;
        mSkipBitsRemaining = 0;
        mDecoder.SetFramingLocked(false);   // starting a fresh phase header: not in a data region
        return;
    }

    switch (mState) {
    case eHuntSpm:
        // Waiting for a comma; ignore everything else.
        break;

    case eHeader: {
        int token = C8b10bDecoder::RobustToken(sym.raw);
        if (mHeaderCount < swi3s::kPhaseHeaderTokens) {
            mHeaderTokens[mHeaderCount++] = token;
        }
        if (mHeaderCount == swi3s::kPhaseHeaderTokens) {
            finishHeader();
        }
        break;
    }

    case ePacket:
        // Manager Packet bytes are ordinary D-code bytes.
        mPacketBytes.push_back(sym.byte);
        if (static_cast<int>(mPacketBytes.size()) == mPacketBytesNeeded) {
            parseManagerPacket();
        }
        break;

    case ePingSpacer:
        if (--mSpacerSymbolsRemaining <= 0) {
            mState = ePingStatus;
            mPingDevice = 0;
            mSkipBitsRemaining = 2 * mHubDepth[0];   // device 0's hub delay
        }
        break;

    case ePingStatus:
        if (mPingDevice < swi3s::kMaxPeripherals) {
            mCmd.pingStatus[mPingDevice++] = C8b10bDecoder::RobustToken(sym.raw);
        }
        if (mPingDevice >= swi3s::kMaxPeripherals) {
            emitAndReset();
        } else {
            // Incremental gap before the next device's token: deeper devices
            // respond later, so the extra delay is 2*(depth[next]-depth[prev]).
            int delta = mHubDepth[mPingDevice] - mHubDepth[mPingDevice - 1];
            if (delta > 0) mSkipBitsRemaining = 2 * delta;
        }
        break;

    case eCommitMpSpacer:
        if (--mSpacerSymbolsRemaining <= 0) {
            mState = eCommitPeriphResp;
            mCommitRespRemaining = swi3s::kMaxPeripherals;
        }
        break;

    case eCommitPeriphResp: {
        // 12 peripheral response tokens (one per device). We don't surface them
        // per-device yet, but capture the FIRST real (driven) token as the commit's
        // representative peripheral response (COMMIT_READY = 0 / COMMIT_NOT_READY = 2)
        // so the command table shows a Peripheral Response for an SSCR, not just the
        // Manager Response. Undriven (all-ones) devices decode to -1 and are skipped.
        int tok = C8b10bDecoder::RobustToken(sym.raw);
        if (tok >= 0 && mCmd.peripheralResponse < 0)
            mCmd.peripheralResponse = tok;
        if (--mCommitRespRemaining <= 0) {
            mState = eCommitPmSpacer;
            // PM spacer: 20 bits (ShortProtocolSpacer=0) or 10 bits (=1).
            int pm = mShortSpacer ? swi3s::kSpacerBitsMP : swi3s::kSpacerBitsPM;
            mSpacerSymbolsRemaining = pm / swi3s::kSymbolBits;
            if (mSpacerSymbolsRemaining < 1) mSpacerSymbolsRemaining = 1;
        }
        break;
    }

    case eCommitPmSpacer:
        if (--mSpacerSymbolsRemaining <= 0) {
            mState = eCommitMgrResp;
        }
        break;

    case eCommitMgrResp: {
        // Final symbol of the phase: CONFIRM_COMMIT (RT0) or CANCEL_COMMIT (RT15).
        int tok = C8b10bDecoder::RobustToken(sym.raw);
        mCmd.managerResponse = tok;
        mCmd.commitConfirmed = (tok == 0);
        mCmd.commitCancelled = (tok == 15);
        emitAndReset();
        break;
    }

    // ---- Write phase ----
    case eWriteSpacer:
        if (--mSpacerSymbolsRemaining <= 0) {
            mState = eWriteResp;
            mSkipBitsRemaining = 2 * mHubDepth[singleDeviceIndex()];
        }
        break;

    case eWriteResp:
        // Single peripheral response = final symbol of a Write phase.
        mCmd.peripheralResponse = C8b10bDecoder::RobustToken(sym.raw);
        emitAndReset();
        break;

    // ---- ReadSetup / ReadData shared response + data path ----
    case eReadMpSpacer:
        if (--mSpacerSymbolsRemaining <= 0) {
            mState = eReadResp;
            mSkipBitsRemaining = 2 * mHubDepth[singleDeviceIndex()];
        }
        break;

    case eReadResp: {
        int tok = C8b10bDecoder::RobustToken(sym.raw);
        mCmd.peripheralResponse = tok;
        if (swi3s::IsReadDataNow(mCmd.phase, tok)) {
            startReadDataPacket();   // data follows
        } else {
            // Deferred / failed / error: no data this phase, phase ends here.
            // If a deferred ReadSetup, remember the byte count for later ReadData.
            if (mCmd.phase == swi3s::kPhaseReadSetup && mCmd.hasReadByteCount) {
                for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
                    if (mCmd.deviceMask & (1u << i)) {
                        mPendingReadCount[i] = mCmd.readByteCount;
                        mPendingReadAddr[i] = mCmd.address;
                    }
                }
            }
            emitAndReset();
        }
        break;
    }

    case eReadDataSpacer:
        if (--mSpacerSymbolsRemaining <= 0) {
            mState = eReadData;
            mReadBytes.clear();
            // Hold symbol framing through the ReadData payload+CRC (same rationale as
            // the Manager Packet: uncontrolled D-code bytes/CRC can forge an SPM at a
            // sub-symbol offset). Released when the read data completes below.
            mDecoder.SetFramingLocked(true);
        }
        break;

    case eReadData:
        mReadBytes.push_back(sym.byte);
        if (static_cast<int>(mReadBytes.size()) == mReadBytesNeeded) {
            mDecoder.SetFramingLocked(false);   // ReadData region done: allow re-framing
            int n = mReadBytesNeeded - 2;
            mCmd.readData.assign(mReadBytes.begin(), mReadBytes.begin() + n);
            U16 crcRx = static_cast<U16>((mReadBytes[n] << 8) | mReadBytes[n + 1]);
            U16 crcCalc = CCrc16::Compute(mCmd.readData.data(), n);
            mCmd.readDataCrcValid = (crcRx == crcCalc);
            mCmd.hasReadData = true;
            // A completed delivery clears any pending-read state for the device.
            for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
                if (mCmd.deviceMask & (1u << i)) mPendingReadCount[i] = 0;
            }
            mState = eReadPmSpacer;
            int pm = mShortSpacer ? swi3s::kSpacerBitsMP : swi3s::kSpacerBitsPM;
            mSpacerSymbolsRemaining = pm / swi3s::kSymbolBits;
            if (mSpacerSymbolsRemaining < 1) mSpacerSymbolsRemaining = 1;
        }
        break;

    case eReadPmSpacer:
        if (--mSpacerSymbolsRemaining <= 0) {
            mState = eReadMgrResp;
        }
        break;

    case eReadMgrResp:
        // Manager Response: READ_DATA_OK (RT0) / READ_DATA_ERROR (RT15).
        mCmd.managerResponse = C8b10bDecoder::RobustToken(sym.raw);
        emitAndReset();
        break;
    }
}

void CCommandTransportParser::finishHeader()
{
    bool allValid = true;
    for (int i = 0; i < swi3s::kPhaseHeaderTokens; ++i) {
        if (mHeaderTokens[i] < 0) allValid = false;
    }
    mCmd.headerValid = allValid;

    int t0 = mHeaderTokens[0];
    // PhaseID is only defined for 0..kPhaseCalibratePhy; a token 7..15 (or <0) is a
    // garbled header. Flag it invalid and clamp to a defined enumerator so the cast
    // isn't out-of-range and downstream switches have a well-defined value.
    if (t0 < 0 || t0 > swi3s::kPhaseCalibratePhy) {
        mCmd.headerValid = false;
        t0 = 0;
    }
    mCmd.phase = static_cast<swi3s::PhaseId>(t0);
    mCmd.deviceMask = static_cast<U16>(
        ((mHeaderTokens[1] & 0xF) << 8) |
        ((mHeaderTokens[2] & 0xF) << 4) |
         (mHeaderTokens[3] & 0xF));
    mCmd.packetLength = static_cast<U8>(
        ((mHeaderTokens[4] & 0xF) << 4) | (mHeaderTokens[5] & 0xF));

    if (mCmd.packetLength == 0) {
        // ReadData has no Manager Packet: MP spacer, then peripheral response;
        // data follows on READ_DATA_NOW. Other zero-length phases just emit.
        if (mCmd.phase == swi3s::kPhaseReadData) {
            mState = eReadMpSpacer;
            mSpacerSymbolsRemaining = swi3s::kSpacerBitsMP / swi3s::kSymbolBits;
            if (mSpacerSymbolsRemaining < 1) mSpacerSymbolsRemaining = 1;
        } else {
            emitAndReset();
        }
        return;
    }

    mState = ePacket;
    mPacketBytes.clear();
    mPacketBytesNeeded = mCmd.packetLength + 2;  // + CRC16 high/low
    // Hold symbol framing through the Manager Packet payload+CRC: these are ordinary
    // D-code bytes whose values (the CRC especially) can accidentally spell the K.28.7
    // SPM pattern at a sub-symbol bit offset. Per SWI3S §7.2.2 {ASW1907} the phase
    // decoder maintains alignment by counting bits mod 10 through the data, and a
    // mid-phase SPM is not a phase boundary ({ASW2206}). Released at parseManagerPacket.
    mDecoder.SetFramingLocked(true);
}

void CCommandTransportParser::parseManagerPacket()
{
    // Manager Packet payload+CRC fully consumed: the data region is over, so allow
    // the next genuine SPM (the following phase / the response spacer) to re-frame.
    mDecoder.SetFramingLocked(false);

    const int len = mCmd.packetLength;
    mCmd.packet.assign(mPacketBytes.begin(), mPacketBytes.begin() + len);
    mCmd.hasManagerPacket = true;
    mCmd.opcode = mCmd.packet[0];

    mCmd.crcReceived = static_cast<U16>((mPacketBytes[len] << 8) | mPacketBytes[len + 1]);
    mCmd.crcComputed = CCrc16::Compute(mCmd.packet.data(), len);
    mCmd.crcValid = (mCmd.crcReceived == mCmd.crcComputed);

    // Command-specific field extraction.
    if (mCmd.phase == swi3s::kPhaseWrite && mCmd.opcode == swi3s::kOpWriteA32 && len >= 5) {
        // opcode, Addr[31:24..07:00] big-endian, then write data.
        mCmd.address = (static_cast<U32>(mCmd.packet[1]) << 24) |
                       (static_cast<U32>(mCmd.packet[2]) << 16) |
                       (static_cast<U32>(mCmd.packet[3]) << 8) |
                        static_cast<U32>(mCmd.packet[4]);
        mCmd.hasAddress = true;
        mCmd.data.assign(mCmd.packet.begin() + 5, mCmd.packet.end());
    } else if (mCmd.phase == swi3s::kPhaseReadSetup && mCmd.opcode == swi3s::kOpReadA32 && len >= 6) {
        // opcode, Read Byte Count (excess-1: field R -> R+1 bytes),
        // Addr[31:24..07:00] big-endian.
        mCmd.readByteCount = static_cast<U16>(mCmd.packet[1] + 1);
        mCmd.hasReadByteCount = true;
        mCmd.address = (static_cast<U32>(mCmd.packet[2]) << 24) |
                       (static_cast<U32>(mCmd.packet[3]) << 16) |
                       (static_cast<U32>(mCmd.packet[4]) << 8) |
                        static_cast<U32>(mCmd.packet[5]);
        mCmd.hasAddress = true;
    } else if ((mCmd.phase == swi3s::kPhaseAnnounce || mCmd.phase == swi3s::kPhaseCommit) && len >= 3) {
        // SSPA / SSCR / DSCR: opcode, Group Mask, Row_Delay (Section 9.1.13,
        // Tables 72/74). Group Mask = packet[1], Row_Delay = packet[2].
        mCmd.groupMask = mCmd.packet[1];
        mCmd.rowDelay = mCmd.packet[2];
        // SSP is generated by SSPA (Announce/0x00) and SSCR (Commit/0x00); not
        // by ExitDormant (Announce/0x01) or DSCR (Commit/0x01).
        mCmd.hasSyncPoint = (mCmd.opcode == 0x00);
        mCmd.isCommit = (mCmd.phase == swi3s::kPhaseCommit);
    }

    // Route to the per-phase response stage.
    if (mCmd.phase == swi3s::kPhaseGetStatus) {
        // Ping: MP spacer (10 bits = 1 symbol), then 12 PingInfo tokens.
        mCmd.hasPingStatus = true;
        mState = ePingSpacer;
        mSpacerSymbolsRemaining = swi3s::kSpacerBitsMP / swi3s::kSymbolBits;
        if (mSpacerSymbolsRemaining < 1) mSpacerSymbolsRemaining = 1;
    } else if (mCmd.phase == swi3s::kPhaseCommit) {
        // SSCR/DSCR: MP spacer, 12 peripheral responses, PM spacer, then the
        // Manager Response (CONFIRM/CANCEL) which is the phase's final symbol.
        mState = eCommitMpSpacer;
        mSpacerSymbolsRemaining = swi3s::kSpacerBitsMP / swi3s::kSymbolBits;
        if (mSpacerSymbolsRemaining < 1) mSpacerSymbolsRemaining = 1;
    } else if (mCmd.phase == swi3s::kPhaseWrite) {
        // Write: MP spacer then a single peripheral response (final symbol).
        mState = eWriteSpacer;
        mSpacerSymbolsRemaining = swi3s::kSpacerBitsMP / swi3s::kSymbolBits;
        if (mSpacerSymbolsRemaining < 1) mSpacerSymbolsRemaining = 1;
    } else if (mCmd.phase == swi3s::kPhaseReadSetup) {
        // ReadSetup: MP spacer then peripheral response; data follows only on
        // READ_DATA_NOW (handled in eReadResp).
        mState = eReadMpSpacer;
        mSpacerSymbolsRemaining = swi3s::kSpacerBitsMP / swi3s::kSymbolBits;
        if (mSpacerSymbolsRemaining < 1) mSpacerSymbolsRemaining = 1;
    } else {
        // CalibratePhy / Unblock / ExitDormant: emit at phase granularity.
        emitAndReset();
    }
}

// Size the read-data accumulator from the device's pending read byte count
// (from the original ReadSetup). Falls back to 1 byte if unknown.
void CCommandTransportParser::startReadDataPacket()
{
    int dev = 0;
    for (int i = 0; i < swi3s::kMaxPeripherals; ++i) {
        if (mCmd.deviceMask & (1u << i)) { dev = i; break; }
    }
    int n = 1;
    if (mCmd.hasReadByteCount) {
        n = mCmd.readByteCount;                 // ReadSetup carries it directly
    } else if (mPendingReadCount[dev] > 0) {
        n = mPendingReadCount[dev];             // ReadData inherits from ReadSetup
        mCmd.address = mPendingReadAddr[dev];
        mCmd.hasAddress = true;
    }
    if (n < 1) n = 1;
    mReadBytes.clear();
    mReadBytesNeeded = n + 2;                    // data + 2 CRC bytes
    mState = eReadDataSpacer;
    mSpacerSymbolsRemaining = 1;                 // Mgr-owned 10-bit spacer after response
}

bool CCommandTransportParser::emitAndReset()
{
    mCmd.endSample = mLastSampleNumber;
    mState = eHuntSpm;
    mDecoder.Reset();   // re-hunt the next K.28.7 comma
    mHeaderCount = 0;
    mCommandReady = true;
    return true;
}
