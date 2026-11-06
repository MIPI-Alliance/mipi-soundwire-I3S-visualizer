# `swi3score` — SWI3S decode core (pybind11)

The verified C++ SWI3S decode (PHY1, PHY2 and PHY3/DLV), kept in-repo under `swi3score/core/`
so this repository builds standalone. `compat/LogicPublicTypes.h` is a small SDK-free
type shim, and `ISampleSource` + `Decoder` run the pipeline from an in-memory or edge-array
source. See `swi3score/core/README.md` for the file list.

## Build

**Standard (needs PyPI):**
```bash
pip install ./native        # scikit-build-core + pybind11
```

Needs CMake 3.18 or newer and scikit-build-core 0.11 or later. `native/LICENSE.md` is the
repository's `LICENSE.md`, copied so the wheel and sdist carry it; `tests/test_native_build_config.py`
keeps the two identical.

**Offline (no PyPI), macOS and Linux only** — compiles straight to an importable
extension. The pybind11 headers come from `$PYBIND11_INCLUDE`, else from the `pybind11`
package installed for the interpreter (`pip install pybind11`). On Windows use the standard
build above:
```bash
bash native/build_local.sh     # -> swi3score.cpython-3xx-*.so at the checkout root
```

## Use

```python
import swi3score
levels = swi3score.make_demo_levels(32)                 # synthetic PHY2 stream
src    = swi3score.MemorySampleSource(levels, 98_304_000, 4)
dec    = swi3score.Decoder(src, swi3score.DecoderSettings())
dec.run()
dec.commands()   # list[dict]: command, phase, opcode, address, data, crc_valid,
                 #   peripheral_response, manager_response, start_sample, end_sample…
dec.audio()      # list[dict]: dp, device, channel, value, index, sample_size,
                 #   start_sample, end_sample…
dec.column_count, dec.row_rate_khz, dec.measured_ui_rate_hz
```

`MemorySampleSource`, `TransitionSampleSource` and `DlvSampleSource` are the three
`ISampleSource` implementations. The Python ingest readers (`swi3s_studio/ingest`) turn a
`.sal`, CSV, VCD or scope file into edge arrays and build a `TransitionSampleSource` (forwarded
clock) or a `DlvSampleSource` (recovered clock) from them, so the decode core does not depend on
the input format.

## Test

```bash
PYTHONPATH=. python3 -m pytest tests/test_swi3score.py   # end-to-end: control + audio round-trip
```
