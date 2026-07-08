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
std::vector<bool> MakeDemoLevels(int audioSamplesPerChannel = 32);

} // namespace swi3score

#endif // SWI3SCORE_DEMO_H
