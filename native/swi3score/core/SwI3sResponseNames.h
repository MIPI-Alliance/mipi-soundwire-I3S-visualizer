// Human-readable names for SWI3S Command Transport response tokens.
//
// Peripheral Response tokens (Table 67) and the Manager Response HD10 tokens
// (Table 66 / 91-93) are Robust Token numbers (0..15) whose MEANING depends on
// the PhaseID. Token 0 in particular resolves to WRITE_OK / READ_DATA_NOW /
// COMMIT_READY / CALIBRATE_NOW by phase; tokens 4 and 5 likewise. -1 = no
// (recognised) token / no response.

#ifndef SWI3S_RESPONSENAMES_H
#define SWI3S_RESPONSENAMES_H

#include "SwI3sProtocolDefs.h"

namespace swi3s {

// Peripheral Response token -> name, resolved within the phase (Table 67 + the
// per-phase tables 84/85/86/87/90).
inline const char* PeripheralResponseName(PhaseId phase, int token)
{
    switch (token) {
    case -1: return "";              // no response / unrecognised
    case 0:
        switch (phase) {
        case kPhaseWrite:        return "WRITE_OK";
        case kPhaseReadSetup:    return "READ_DATA_NOW";
        case kPhaseReadData:     return "READ_DATA_NOW";
        case kPhaseCommit:       return "COMMIT_READY";
        case kPhaseCalibratePhy: return "CALIBRATE_NOW";
        case kPhaseGetStatus:    return "PING_ATTACHED";
        default:                 return "RT0";
        }
    case 2:  return (phase == kPhaseCommit) ? "COMMIT_NOT_READY"
                  : (phase == kPhaseGetStatus) ? "?" : "RT2";
    case 4:  return (phase == kPhaseWrite) ? "WRITE_FAILED"
                  : (phase == kPhaseReadSetup || phase == kPhaseReadData) ? "READ_FAILED" : "RT4";
    case 5:  return (phase == kPhaseWrite) ? "REMOTE_WRITE_BUFFERED"
                  : (phase == kPhaseReadSetup || phase == kPhaseReadData) ? "REMOTE_READ_DEFERRED" : "RT5";
    case 6:  return (phase == kPhaseGetStatus) ? "PING_ATTACHED_BUSY" : "REMOTE_WRITE_BUSY";
    case 7:  return "REMOTE_ACCESS_DISABLED";
    case 8:  return (phase == kPhaseGetStatus) ? "PING_ALERT" : "RT8";
    case 10: return (phase == kPhaseGetStatus) ? "PING_ALERT_BUSY" : "RT10";
    case 12: return "PROTOCOL_ERROR";
    case 13: return "COMMAND_ERROR";
    case 14: return "TRANSPORT_ERROR";
    case 15: return "COMMANDS_BLOCKED";
    default: return "RESERVED";       // 1,3,9,11 -> UnexpectedRobustToken
    }
}

// Manager Response (HD10) token -> name. Only tokens 0/15 are valid; meaning is
// per phase (ReadSetup/ReadData vs Commit).
inline const char* ManagerResponseName(PhaseId phase, int token)
{
    if (token == 0) {
        return (phase == kPhaseCommit) ? "CONFIRM_COMMIT" : "READ_DATA_OK";
    }
    if (token == 15) {
        return (phase == kPhaseCommit) ? "CANCEL_COMMIT" : "READ_DATA_ERROR";
    }
    return "";
}

// True if a peripheral response token indicates the read data follows in this
// phase (READ_DATA_NOW = token 0 for ReadSetup/ReadData).
inline bool IsReadDataNow(PhaseId phase, int token)
{
    return token == 0 && (phase == kPhaseReadSetup || phase == kPhaseReadData);
}

} // namespace swi3s

#endif // SWI3S_RESPONSENAMES_H
