// Response-token predicate for the SWI3S Command Transport.
//
// Peripheral and Manager response tokens are Robust Token numbers (0..15) whose meaning
// depends on the PhaseID: token 0 resolves to WRITE_OK, READ_DATA_NOW, COMMIT_READY or
// CALIBRATE_NOW by phase. This header holds only the one predicate the decode needs,
// IsReadDataNow; the token-to-name tables are in swi3s_studio/analysis/responses.py, the
// only table.

#ifndef SWI3S_RESPONSENAMES_H
#define SWI3S_RESPONSENAMES_H

#include "SwI3sProtocolDefs.h"

namespace swi3s {

// True if a peripheral response token indicates the read data follows in this
// phase (READ_DATA_NOW = token 0 for ReadSetup/ReadData).
inline bool IsReadDataNow(PhaseId phase, int token)
{
    return token == 0 && (phase == kPhaseReadSetup || phase == kPhaseReadData);
}

} // namespace swi3s

#endif // SWI3S_RESPONSENAMES_H
