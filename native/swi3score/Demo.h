// Synthetic SWI3S streams for tests and demos: an absolute per-UI data-level plan carrying
// a control sequence that configures and commits data ports, followed by their payload (a
// sine per channel). MakeDemoLevels is the PHY2 reference: two commits (2 -> 8 -> 16
// columns) over four mono ports, two 16-bit PCM and two 1-bit PDM, with DP1 and DP3
// scrambled. Feed a level plan to a MemorySampleSource.

#ifndef SWI3SCORE_DEMO_H
#define SWI3SCORE_DEMO_H

#include <cstdint>
#include <string>
#include <vector>

namespace swi3score {

// The longest stream a generator below builds: each throws std::invalid_argument when its
// samples, intervals or samples per phase argument is above it. One second of 48 kHz audio.
constexpr int kMaxDemoSamplesPerChannel = 48000;

// Returns the per-UI data-line levels (Column 0 = NRZS CDS, other columns = idle then
// audio, scrambled on DP1 and DP3). 'audioSamplesPerChannel' controls the length.
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
// build_capture_from_levels (same as MakeDemoLevels). `fcpScrambled` writes
// FCP_ScramblerEn = 1 (0x0E bit 3) on every port and scrambles each DRQ bit with a
// per-port LFSR seeded when the port is enabled; otherwise the DRQ bit is sent raw.
std::vector<bool> MakeDemoLevelsFlowControl(int audioSamplesPerChannel = 32,
                                            bool fcpScrambled = false);

// Test fixture for the scrambler-seed rule on a channel: one 16-bit PCM port (dp0, Columns
// 1-2, 64-row Interval) on an 8-column bus, in seven phases of `samplesPerPhase` intervals
// (at least 16), channel 0 sending one sample per interval. Channel 1 is enabled by the
// first SSCR, disabled by the second, re-enabled by the third, disabled by a DSCR and
// re-enabled by a second DSCR, each commit point an interval boundary, so the change
// applies to the interval that starts there. Then a single-ranked 0x0B write clears
// ScramblerEn and a second sets it again, both programming errors on a running port that
// the decode flags and applies. Each channel's scrambler is seeded at the phase that
// enables the channel and at the phase that sets ScramblerEn, and continues otherwise; with
// ScramblerEn 0 the wire carries the clear bits. Channel c carries sineSample(c, index,
// 15), its index counting that channel's samples. Returns bus levels for
// MemorySampleSource.
std::vector<bool> MakeChannelReseedLevels(int samplesPerPhase = 32);

// Test fixture for the scrambler-seed rule on an FCP: one RX_CONTROLLED source data port
// (dp0, unscrambled, 16-bit samples, 16-row Interval, FlowControlDelay 0) on a 16-column bus
// with FCP_ScramblerEn = 1, in seven phases of `intervalsPerPhase` intervals (at least 64):
// an SSCR enables channel 0; a second SSCR adds channel 1; a DSCR removes it; then four
// single-ranked 0x0E writes, FCP_ScramblerEn 1 in the first two: TX_CONTROLLED, which
// removes the FCP; RX_CONTROLLED, which enables it again; RX_CONTROLLED with
// FCP_ScramblerEn 0; and RX_CONTROLLED with FCP_ScramblerEn 1. Each 0x0E write changes a
// single-ranked register of a running port: a programming error the decode flags and still
// applies. Each DRQ is a deterministic coin, sent raw while FCP_ScramblerEn is 0; in
// RX_CONTROLLED an interval's TX_PRESENT equals the DRQ one interval earlier, and 1 in the
// first interval after the FCP's enable; the SSCR and the DSCR carry the DRQ history.
// `generatorSeed` says where the wire's
// DRQ scrambler is seeded: "enable" at each FCP enable (the first SSCR and the second 0x0E
// write) and where FCP_ScramblerEn turns on (the fourth); "sscr" also at the second SSCR;
// "dscr" also at the DSCR; "interval" before every DRQ bit; "resume" at the FCP enables
// only; "continue" at the first SSCR only. Any other value throws std::invalid_argument.
std::vector<bool> MakeFcpReseedLevels(int intervalsPerPhase = 96,
                                      const std::string& generatorSeed = "enable");

// Test fixture for a committed FCP placement change: one RX_CONTROLLED source data port (dp0,
// unscrambled, 16-bit samples, 16-row Interval from Row 2, Columns 2..14) on a 16-column bus
// with FlowControlDelay `flowControlDelay` (0 or 1, so d = 1 or 2), its unscrambled DRQ at
// Column 11, Row 12. After `intervalsPerPhase` intervals an SSCR, or a DSCR when `dscr`, commits
// FCP_HorizontalStart `fcpHStart`, FCP_Offset `fcpOffset` and FCP_BitWidth `fcpBitWidth` (the
// DRQ then spans fcpBitWidth + 1 UIs and is sampled on its last) and nothing else, its commit
// point an interval boundary. From there the DRQ is sent at the new cell. The n-th interval
// since the port's enable carries the (n - d)-th DRQ sent since that enable as its TX_PRESENT,
// and 1 for n < d, across the second commit. With `reenable` the second commit disables
// channel 0, and so the port, and a third commit of the same kind, `intervalsPerPhase`
// intervals later, enables it again together with the new FCP placement: the port's sample
// index, its scrambler, its interval phase and its DRQ history all start again there. A DRQ
// outside Columns 1..15 or Rows 0..15, or any of its fcpBitWidth + 1 UIs in Columns 2..14 of
// Rows 2..4, an `fcpBitWidth` outside 0..3, or another `flowControlDelay`, throws
// std::invalid_argument. `scrambled` sets the port's ScramblerEn: TX_PRESENT and every data
// bit, carrying a sample or not, are scrambled by channel 0's LFSR, seeded at each enable and
// continuing across the second commit. `commitRowOffset` (0..15, an SSCR without `reenable`
// only) lands the second commit that many rows past the interval boundary: the SSCR
// re-anchors the port there and its DRQ history starts again, TX_PRESENT 1 for the first d
// intervals. `columnsAfter` (16 or 32, 16 with `reenable`) is the column count the second
// commit also commits; a DSCR that changes it starts the port's interval on Column 0 of its
// commit row and carries the DRQ history. Any other value of either throws
// std::invalid_argument. A count too small to place the commit after the setup (which runs to
// CDS row 8520) throws std::invalid_argument: the smallest is 65 with the default arguments,
// 64 with a `commitRowOffset` of 6 or more, and 75 with `reenable` or a `columnsAfter` of 32.
std::vector<bool> MakeFcpMoveLevels(int intervalsPerPhase = 96, bool dscr = false,
                                    int fcpHStart = 13, int fcpOffset = 12,
                                    int flowControlDelay = 0, bool scrambled = false,
                                    int fcpBitWidth = 0, bool reenable = false,
                                    int commitRowOffset = 0, int columnsAfter = 16);

// Test fixture for the FCP guard and tail: one RX_CONTROLLED (ASYNC when `async`) SINK data
// port (dp0, unscrambled, 16-bit samples, 16-row Interval from Row 2, Columns 2..14) on a
// 16-column bus with FlowControlDelay 0, its unscrambled SOURCE DRQ at Column 11, Row 12 with
// no tail, its guard set by `firstGuardEnable` and `firstGuardPolarity` (off by default).
// After `intervalsPerPhase` intervals (at least 86; a smaller count throws, the setup running
// to CDS row 8520) an SSCR, or a DSCR when `dscr`, commits FCP_HorizontalStart `fcpHStart`,
// FCP_Offset `fcpOffset`, FCP_BitWidth `fcpBitWidth`, FCP_TailWidth `fcpTailWidth`,
// FCP_GuardEnable `fcpGuardEnable` and FCP_GuardPolarity `fcpGuardPolarity` and nothing else,
// its commit point an interval boundary. From there the DRQ is driven on the first of its
// fcpBitWidth + 1 UIs and held, followed in its row by one guard UI driven to fcpGuardPolarity
// when fcpGuardEnable, then fcpTailWidth tail UIs that hold the bus level. The first interval
// after the port's enable carries TX_PRESENT 1 and each later one, across the second commit,
// the DRQ of the interval before it. An fcpBitWidth or fcpTailWidth outside 0..3, or a DRQ,
// guard or tail cell outside Columns 1..15 or Rows 0..15, or in Columns 2..14 of Rows 2..4,
// throws std::invalid_argument.
std::vector<bool> MakeFcpGuardTailLevels(int intervalsPerPhase = 96, bool dscr = false,
                                         int fcpHStart = 8, int fcpOffset = 12,
                                         int fcpBitWidth = 0, int fcpTailWidth = 0,
                                         bool fcpGuardEnable = false,
                                         bool fcpGuardPolarity = false, bool async = false,
                                         bool firstGuardEnable = false,
                                         bool firstGuardPolarity = false);

// PHY1 (FBCSE, "slow" forwarded-clock) demo. A CONSTANT 4-column geometry (CDS at Column 0,
// NRZS) carrying two 16-bit PCM ports interleaved in one column (DP0 Offset 0, DP1 Offset
// 16, DP1 scrambled) at 48 kHz and one PDM port (2 samples/row, 3.072 MHz). DP0 carries
// 44.1 kHz via payload interval skipping (13/160) on its 48 kHz opportunities, sharing its
// column with DP1 at 48 kHz. Halfway through, a commit REPOSITIONS the ports WITHOUT
// changing the column count — the PCM pair moves col 1 -> col 3 and the PDM moves cols 2-3
// -> cols 1-2. Same audio content throughout; exercises a placement-only reconfigure (no
// NumColumns change). Turn into FBCSE clock/data edges via build_capture_from_levels.
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
// `cdsColumn` (1..14, off the data-port columns 4..9; any other value throws
// std::invalid_argument) other than 2 programs CDS_HorizontalStart with the 16-column
// config, so the CDS moves there at that commit.
DemoLevels MakeDemoLevelsPhy3(int audioSamplesPerChannel = 32, int cdsColumn = 2);

// Each single-event test fixture below, up to MakeRunningPortWriteLevels, opens with
// Safe-Lock-2 enumeration Pings and has Pings at least every 2048 Rows to its end.
// MakeSkippingLevels has no enumeration Pings: it opens with a Ping and fills its idle rows
// with keep-alive Pings. The DSCR fixtures (MakeDscrDisableLevels, MakeDscrEnableLevels,
// MakeDscrChannelSetLevels) and the reseed fixtures (MakeChannelReseedLevels and
// MakeFcpReseedLevels, above) place each event's block so the event lands on its documented
// row. A DSCR fixture throws std::invalid_argument when its sample count is too small to
// hold the block there; a reseed fixture raises its count to its stated minimum instead.

// Test fixture: two 16-bit PCM ports on an 8-column bus; a DSCR (synced commit that does
// NOT re-anchor the SSP) disables dp1 mid-stream. Exercises the decode's DSCR handling —
// a correct decode drops dp1's audio at the DSCR and keeps dp0 uninterrupted. With
// `disableAll`, the DSCR disables BOTH ports (the LAST-enabled-port case), which must
// reconfigure the engine to empty rather than keeping the stale port.
std::vector<bool> MakeDscrDisableLevels(int samplesPerChannel = 32, bool disableAll = false);

// Test fixture: dp0 (16-bit PCM, ch0) comes up at the SSCR; a DSCR mid-stream enables dp1,
// which starts on Column 0 of the commit's row with its interval anchored there, or
// (`enableChannel`) channel 1 on dp0, which keeps its phase. A correct decode keeps dp0's
// running channel uninterrupted and decodes the new one from its first sample.
std::vector<bool> MakeDscrEnableLevels(int samplesPerChannel = 32, bool enableChannel = false);

// Test fixture for a DSCR that changes a running port's channel set: one scrambled 16-bit
// PCM port (dp0, Columns 1-7, 64-row Interval, data in its first rows) on an 8-column bus.
// An SSCR enables the channels in `fromMask` (bits 0-2); after `samplesPerChannel` intervals
// a DSCR commits `toMask`, its commit point `rowInInterval` rows into an interval. Both masks
// keep bits 0-2 only, a `fromMask` of 0 enables channel 0, and `rowInInterval` is clamped to
// 0..63. The port
// keeps its interval phase across the DSCR; a channel's scrambler is seeded at its enable.
// Channel c carries sineSample(c, index, 15). Returns bus levels for MemorySampleSource.
std::vector<bool> MakeDscrChannelSetLevels(int samplesPerChannel = 32, unsigned fromMask = 0x3,
                                           unsigned toMask = 0x1, int rowInInterval = 0);

// Test fixture: one scrambled 16-bit PCM port; a single-ranked WriteA32 clears its
// ScramblerEn mid-stream (immediate, no commit). A correct decode reconfigures on that
// write and stops descrambling, so the second half diverges from the generated sine.
// midInterval=true shifts that write half an interval off the sample boundary so the
// reconfigure lands mid-interval (exercises the windowed-replay phase preservation).
std::vector<bool> MakeImmediateScramblerLevels(int samplesPerChannel = 32,
                                               bool midInterval = false);

// Test fixture: one scrambled PCM port carrying (fromSize+1)-bit samples; a single-ranked
// WriteA32 to SampleSizeGrouping sets SampleSize to toSize, and the decode's reconfigure
// lands `rowsIntoSample` rows into a sample. The in-flight sample finishes at the old width
// on the wire; later samples use the new one.
std::vector<bool> MakeImmediateSampleSizeLevels(int samplesPerChannel = 32, int fromSize = 23,
                                                int toSize = 15, int rowsIntoSample = 8);

// Test fixture for the EnableCh_CURR protocol-error rule (SWI3S v1.1: EnableCh is
// dual-ranked; a direct write to its _CURR address is only safe when the port's
// Interval == 1 Row). One PCM port is configured + committed with Interval = `interval`
// (rows/interval - 1; 0 == every row transports == "1 Row"), then a single WriteA32
// writes dp0's EnableCh0/HCount register directly at its _CURR address (0x2000 + 0xC1)
// re-asserting EnableCh0 — legal framing either way, but the decode must flag it as an
// error iff `interval` != 0. An `interval` above 4095, past the 12-bit Interval register,
// or a `samplesPerChannel` x (`interval` + 1) above 64 x kMaxDemoSamplesPerChannel Rows,
// throws std::invalid_argument. Returns bus levels for MemorySampleSource.
std::vector<bool> MakeEnableChCurrWriteLevels(int samplesPerChannel = 32, int interval = 0);

// Test fixture for the running-port write rule: one 16-bit PCM port (64-row Interval),
// committed and running; after `samplesPerChannel` samples a single WriteA32 writes `value`
// to the register at `base` + `offset` (`offset` 0..0xFF; the default base is dp0's block,
// e.g. offset 0x0E FlowControl) while the wire keeps the committed config. With `beforeRun`
// the write is the last setup write before the SSCR. Returns bus levels for
// MemorySampleSource.
std::vector<bool> MakeRunningPortWriteLevels(int samplesPerChannel, int offset, int value,
                                             std::uint32_t base = 0x2000,
                                             bool beforeRun = false);

// Test fixture for Payload Interval Skipping (Section 14.1.10). One unscrambled 16-bit PCM
// source port on an 8-column bus, Interval = 32 Rows, with SkippingNumerator = `numerator`
// and SkippingDenominator = `denominator` — so (D - N) of every D intervals transport and
// the rest leave the bus idle. The payload is a RAMP (sample n has value n), so a decode
// that skips the wrong interval reads an idle interval and drops a real sample, and the
// values no longer count up. `samplesPerChannel` counts TRANSPORTED samples; enough rows
// are generated to reach it. Periodic SSPAs land only where the accumulated skipping is
// back at 0; with `misaligned_sspa` they land one Interval off that — a row where
// row_in_interval is still 0, so the only thing wrong is the skipping phase (the unexpected
// SSP of Section 9.1.6.2.1). The stream is at most 4 * kMaxDemoSamplesPerChannel intervals;
// a ratio that needs more (samples * denominator / (denominator - numerator)) throws
// std::invalid_argument, as does a `numerator` or `denominator` above 4095, past its 12-bit
// register. A `denominator` below 1 is written to the bus as 0, which reads as the
// register's reset value 1. Returns bus levels for MemorySampleSource.
std::vector<bool> MakeSkippingLevels(int samplesPerChannel = 200, int numerator = 13,
                                     int denominator = 160, bool misalignedSspa = false);

} // namespace swi3score

#endif // SWI3SCORE_DEMO_H
