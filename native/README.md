# `swi3score` — SWI3S decode core (pybind11)

The verified C++ SWI3S PHY2 decode, **vendored in-repo** under `swi3score/core/`
so this repository builds standalone. The sources originated in the Saleae plugin
(`SwI3sAnalyzer`); only the SDK-coupled bits are replaced: `compat/LogicPublicTypes.h`
is a tiny SDK-free type shim, and `ISampleSource` + `Decoder` replace
`CBitstreamDecoder` + `WorkerThread` (same pipeline, fed from a `.sal`/CSV/in-memory
source instead of `AnalyzerChannelData`). See `swi3score/core/README.md` for the
vendored file list and how to keep it in sync with the plugin.

## Build

**Standard (needs PyPI):**
```bash
pip install ./native        # scikit-build-core + pybind11
```

**Offline (no PyPI)** — compiles straight to an importable extension, discovering
pybind11 headers (including the copy bundled with an installed `torch`):
```bash
./native/build_local.sh     # -> swi3s-studio/swi3score.cpython-3xx-*.so
```

## Use

```python
import swi3score
levels = swi3score.make_demo_levels(32)                 # synthetic PHY2 stream
src    = swi3score.MemorySampleSource(levels, 98_304_000, 4)
dec    = swi3score.Decoder(src, swi3score.DecoderSettings())
dec.run()
dec.commands()   # list[dict]: phase, opcode, address, data, crc_valid, response, samples…
dec.audio()      # list[dict]: dp, channel, value, index, sample_size, samples…
dec.column_count, dec.row_rate_khz, dec.measured_ui_rate_hz
```

`MemorySampleSource` is the first `ISampleSource`; the `.sal` and CSV readers
(Milestone 1) implement the same interface so the decode core is unchanged.

## Test

```bash
PYTHONPATH=. python3 tests/test_swi3score.py     # end-to-end: control + audio round-trip
```
