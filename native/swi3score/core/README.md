# swi3score/core — the SWI3S decode core

These C++ sources are the SWI3S decode classes that the `swi3score` Python extension
compiles. They have no SDK dependency, so the repository builds standalone.

## Files

- Sources built into the extension (the list `native/CMakeLists.txt` compiles):
  `C8b10bDecoder`, `CCommandTransportParser`, `CRegisterModel`, `CColumnDetector`,
  `CDpConfig`, `CDataPort`, `CFlowControlPort`, `CPayloadEngine`.
- Header-only: `CCrc16`, `CDescrambler`, `CNrzsDecoder`, `SwI3sProtocolDefs`,
  `SwI3sResponseNames` (`IsReadDataNow` only; the token-to-name tables live in
  `swi3s_studio/analysis/responses.py`).

`../Decoder` and `../ISampleSource` drive the classes above from a sample source, and
`../compat` supplies an SDK-free `<LogicPublicTypes.h>`.

## Maintenance

The sources are maintained in this repository. They carry Studio-only behaviour (register
snapshots and overrides, reconfigure and state save/restore, CSV load warnings, hub depth),
so a file is never replaced wholesale by a copy of a sibling project's version. A change
made to a sibling project's copy of a decode class is merged into the file here by hand.
A decode fix made here is applied to the sibling copy the same way.
