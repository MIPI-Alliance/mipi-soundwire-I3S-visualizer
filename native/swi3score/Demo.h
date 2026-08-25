// Synthetic SWI3S PHY2 stream for tests and demos: an absolute per-UI data-level
// plan carrying a control sequence that configures + commits one stereo 8-bit
// scrambled audio data port, followed by scrambled audio payload (a sine per
// channel). The same plan the Saleae plugin's simulation produces, validated by
// test/test_simulation_roundtrip.cpp. Feed it to a MemorySampleSource.

#ifndef SWI3SCORE_DEMO_H
#define SWI3SCORE_DEMO_H

#include <cstdint>
#include <vector>

namespace swi3score {

// Returns the per-UI data-line levels (Column 0 = NRZS CDS, other columns =
// idle then scrambled audio). 'audioSamplesPerChannel' controls the length.
// DP0 carries 44.1 kHz via PAYLOAD INTERVAL SKIPPING on its 48 kHz transport
// opportunities (13 of every 160 intervals skipped); DP1 stays at 48 kHz.
std::vector<bool> MakeDemoLevels(int audioSamplesPerChannel = 32);

// Flow-control demo. Four PERIPHERAL data ports (the ones a sniffer sees — the
// manager's own DPs are configured off-bus), one per flow mode on four devices:
// dev0 NORMAL (48 ksps reference), dev1 TX_CONTROLLED, dev2 RX_CONTROLLED, dev3 ASYNC.
// The flow-controlled ports run 96 k transport opportunities/s carrying 48 ksps audio
// with 0/1 interval of transport jitter (uniform sampling, jittered transport, gated by
// TX_PRESENT), so the decode's TX_PRESENT gating de-jitters back to a bit-exact 48 ksps
// stream. Single 16-column geometry after one commit. Turn into edges via
// build_capture_from_levels (same as MakeDemoLevels).
std::vector<bool> MakeDemoLevelsFlowControl(int audioSamplesPerChannel = 32);

// PHY1 (FBCSE, "slow" forwarded-clock) demo. A CONSTANT 4-column geometry (CDS at Column
// 0, NRZS) carrying two 16-bit PCM ports interleaved in one column (DP0 Offset 0, DP1
// Offset 16, DP1 scrambled) at 48 kHz and one PDM port (2 samples/row, 3.072 MHz). Halfway
// DP0 carries 44.1 kHz via payload interval skipping (13/160) on its 48 kHz
// opportunities, sharing its column with DP1 at 48 kHz. Halfway
// through, a commit REPOSITIONS the ports WITHOUT changing the column count — the PCM pair
// moves col 1 -> col 3 and the PDM moves cols 2-3 -> cols 1-2 (matching the phy1_bus_config_1
// -> _2 reference CSVs). Same audio content throughout; exercises a placement-only reconfigure
// (no NumColumns change). Turn into FBCSE clock/data edges via build_capture_from_levels.
std::vector<bool> MakeDemoLevelsPhy1(int audioSamplesPerChannel = 32);

// PHY3 (DLV) demo. Same audio content as MakeDemoLevels (16-bit PCM: DP0 @44.1 kHz by
// payload interval skipping, DP1 @48 kHz;
// 2x 1-bit PDM @3.072 MHz; DP0/DP2 unscrambled, DP1/DP3 scrambled) but over DLV
// framing: per-UI LOGICAL differential levels (1 = DP high/DN low). Each row is
// Sync1 (Col 0) + a plain-NRZ CDS bit at Column 2 + payload + Sync0 (last column);
// geometry is Safe-Lock-4 then a single commit to 16 columns. The Python side
// (transitions.build_dlv_capture_from_levels) turns these into complementary DP/DN
// edge arrays; the DLV decode front-end recovers the clock from the Sync1 edges.
//
// `rowColumns` gives each row's column count (levels are row-major), so the capture
// builder can place edges at a CONSTANT row period (the PLL's reference stays 3.072
// MRows/s across the commit) — the UI/bit-clock rate then RISES at the commit as the
// row subdivides into more columns (4 -> 16), 12.288 -> 49.152 MHz.
struct DemoLevels {
    std::vector<bool> levels;
    std::vector<int> rowColumns;
};
DemoLevels MakeDemoLevelsPhy3(int audioSamplesPerChannel = 32);

// Test fixture: two 16-bit PCM ports on an 8-column bus; a DSCR (synced commit that does
// NOT re-anchor the SSP) disables dp1 mid-stream. Exercises the decode's DSCR handling —
// a correct decode drops dp1's audio at the DSCR and keeps dp0 uninterrupted. With
// `disableAll`, the DSCR disables BOTH ports (the LAST-enabled-port case), which must
// reconfigure the engine to empty rather than keeping the stale port.
std::vector<bool> MakeDscrDisableLevels(int samplesPerChannel = 32, bool disableAll = false);

// Test fixture: one scrambled 16-bit PCM port; a single-ranked WriteA32 clears its
// ScramblerEn mid-stream (immediate, no commit). A correct decode reconfigures on that
// write and stops descrambling, so the second half diverges from the generated sine.
// midInterval=true shifts that write half an interval off the sample boundary so the
// reconfigure lands mid-interval (exercises the windowed-replay phase preservation).
std::vector<bool> MakeImmediateScramblerLevels(int samplesPerChannel = 32,
                                               bool midInterval = false);

// Test fixture for the EnableCh_CURR protocol-error rule (SWI3S v1.1: EnableCh is
// dual-ranked; a direct write to its _CURR address is only safe when the port's
// Interval == 1 Row). One PCM port is configured + committed with Interval = `interval`
// (rows/interval - 1; 0 == every row transports == "1 Row"), then a single WriteA32
// writes dp0's EnableCh0/HCount register directly at its _CURR address (0x2000 + 0xC1)
// re-asserting EnableCh0 — legal framing either way, but the decode must flag it as an
// error iff `interval` != 0. Returns bus levels for MemorySampleSource.
std::vector<bool> MakeEnableChCurrWriteLevels(int samplesPerChannel = 32, int interval = 0);

// Test fixture for Payload Interval Skipping (Section 14.1.10). One unscrambled 16-bit PCM
// source port on an 8-column bus, Interval = 32 Rows, with SkippingNumerator = `numerator`
// and SkippingDenominator = `denominator` — so (D - N) of every D intervals transport and
// the rest leave the bus idle. The payload is a RAMP (sample n has value n), so a decode
// that skips the wrong interval reads an idle interval and drops a real sample, and the
// values no longer count up. `samplesPerChannel` counts TRANSPORTED samples; enough rows
// are generated to reach it. Periodic SSPAs land only where the accumulated skipping is
// back at 0; with `misaligned_sspa` they land one Interval off that — a row where
// row_in_interval is still 0, so the only thing wrong is the skipping phase (the unexpected
// SSP of Section 9.1.6.2.1). Returns bus levels for MemorySampleSource.
std::vector<bool> MakeSkippingLevels(int samplesPerChannel = 200, int numerator = 13,
                                     int denominator = 160, bool misalignedSspa = false);

} // namespace swi3score

#endif // SWI3SCORE_DEMO_H
