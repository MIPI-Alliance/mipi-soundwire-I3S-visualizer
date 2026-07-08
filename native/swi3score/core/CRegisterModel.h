// Snoops decoded WriteA32 / Commit commands and reconstructs per-device
// register state, so the analyzer can learn bus geometry and per-dataport audio
// layout by observation when a capture includes configuration traffic.
//
// Addressing (Section 15.1/15.2): the target device is the Phase Header Device
// Mask; the 32-bit address is an offset within that peripheral's identical
// register space. Dual-ranked registers stage at _NEXT and are promoted to
// _CURR (= _NEXT + 0x40) by a Commit (SSCR/DSCR); single-ranked and direct
// _CURR writes apply immediately. We normalise every value to the _NEXT-base
// address so reads are uniform.

#ifndef SWI3S_CREGISTERMODEL_H
#define SWI3S_CREGISTERMODEL_H

#include <map>
#include <tuple>
#include <vector>
#include <LogicPublicTypes.h>

#include "CDpConfig.h"

struct SwI3sCommand;

class CRegisterModel
{
public:
    CRegisterModel() = default;

    void Reset();

    // Feed a completed command. WriteA32 stages register bytes for the selected
    // device. Commit promotion is NOT done here -- it happens at the SSP row via
    // Commit(), to match hardware's dual-ranked _NEXT->_CURR at the SSP.
    void OnCommand(const SwI3sCommand& cmd);

    // Promote staged (_NEXT) values to live (_CURR) for the commit groups in
    // 'groupMask'. SLC/geometry registers are Commit Group 0; each data port
    // commits iff (its CommitGroupMemb & groupMask) != 0 (Section 9.1.10.4).
    // Call this at the SSP row for a CONFIRM_COMMIT'd SSCR.
    void Commit(U8 groupMask);

    // Debug what-if: force `value` into the register at `addr` for `dev`, exactly
    // like a WriteA32 (stages at _NEXT for dual-ranked, immediate otherwise). The
    // caller then Commit()s to promote staged values to _CURR. Used to fold UI
    // register overrides into the SAME model the grid + audio decode read from.
    void ForceWrite(int dev, U32 addr, U8 value) { applyWrite(dev, addr, value); }

    // Apply a bus READ's revealed byte: the peripheral reported its live value for
    // `addr` on `dev`. Rank-aware, mirroring the register-map display — a _CURR-alias
    // read updates the committed bank; a _NEXT-base/single read updates the live
    // display bank — and tagged READ (distinct from a manager WRITTEN). Reads apply
    // IMMEDIATELY (never wait for a commit) and are folded ONLY into the register-map
    // snapshot path (registersFromCommands), never the config the grid/audio decode
    // from — a read reveals peripheral state, it is not a configuration write.
    void ApplyRead(int dev, U32 addr, U8 value);

    // Reconstruct a config from observed register state: bus geometry plus every
    // enabled dataport across all devices. Returns false if nothing usable was
    // learned (no enabled dataport seen). Existing 'out' geometry is overwritten.
    bool BuildConfig(SwI3sConfig& out) const;

    // SyncPointOffset (SLC 0x1014) for a device, default if unseen.
    int SyncPointOffset(int deviceNum) const;

    // Committed column count from NumColumns_CURR, or 0 if never observed.
    int ColumnCount() const;

    // ShortProtocolSpacer (SLC 0x1083 _CURR), default false.
    bool ShortProtocolSpacer() const;

    bool AnyConfig() const { return !mDev.empty(); }

    // Every committed (_CURR) register value observed, as (device, base_addr, value)
    // tuples. This is the decode authority's register state — used to parity-check
    // the display register model (Python DeviceRegisterFile) so the two can never
    // silently disagree on how the bus decoded.
    std::vector<std::tuple<int, U32, U8>> CommittedSnapshot() const;

    // Full per-register snapshot for driving the display register model from this
    // one authority: (device, base_addr, cur, has_cur, cur_src, next, has_next,
    // next_src). `cur` is the committed value (has_cur = a commit/immediate write or
    // a _CURR read set it); `next` is the staged (_NEXT) / live-display value
    // (has_next). Each `*_src` is the value's provenance: 1 = written (bus WriteA32 /
    // what-if force), 2 = read (revealed by a bus ReadA32). The display's NEXT column
    // is (next if has_next else cur); its CURR column is cur (dual-ranked). Every
    // touched address (in either bank) is emitted once.
    std::vector<std::tuple<int, U32, U8, bool, U8, U8, bool, U8>> Snapshot() const;

    // Provenance tags for a register value in a Snapshot (parallel to the display
    // model's Provenance): distinguish a manager write from a peripheral-revealed read.
    enum Source : U8 { SrcDefault = 0, SrcWritten = 1, SrcRead = 2 };

    // Spec-defined reset byte for the register at `baseAddr` (the value an
    // un-written register reads as). This is the SINGLE place per-register resets
    // live for the decode path — get() falls back to it, so no decode site
    // hardcodes a reset. Cross-checked against data/registers.json by a test, so
    // a non-zero spec reset can never be silently dropped (the bug that made
    // ScramblerEn default OFF). Address is the normalised _NEXT-base form.
    static U8 resetOf(U32 baseAddr);

private:
    struct DeviceRegs {
        std::map<U32, U8> cur;   // committed values, keyed by _NEXT-base address
        std::map<U32, U8> next;  // staged values awaiting Commit
        // Parallel provenance for cur/next (Source). Only the write/read entry points
        // stamp these and only Snapshot() reads them, so the decode/config maps above
        // stay plain U8 (BuildConfig/get/CommittedSnapshot are unchanged).
        std::map<U32, U8> curSrc;
        std::map<U32, U8> nextSrc;
    };

    void applyWrite(int dev, U32 addr, U8 value);
    void commitDevice(int dev, U8 groupMask);
    // Committed value of a register, or its spec reset (resetOf) if never written.
    U8   get(int dev, U32 baseAddr) const;
    bool has(int dev, U32 baseAddr) const;
    SwI3sDpConfig decodeDp(int dev, int n) const;
    int representativeDevice() const;

    std::map<int, DeviceRegs> mDev;  // by device number (0..11)
};

#endif // SWI3S_CREGISTERMODEL_H
