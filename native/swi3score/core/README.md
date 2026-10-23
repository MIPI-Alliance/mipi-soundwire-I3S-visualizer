# swi3score/core — vendored SWI3S decode core

These C++ sources are the **verified SWI3S decode classes** that the `swi3score`
Python extension compiles. They are vendored here so this repository builds
**standalone** — no sibling checkout required.

## Vendored files

Upstream is the `SwI3sAnalyzer` logic-analyzer plugin, a separate project. Only
SDK-free sources are vendored:

- `C8b10bDecoder`, `CCommandTransportParser`, `CRegisterModel`, `CColumnDetector`,
  `CDpConfig`, `CDataPort`, `CFlowControlPort`, `CPayloadEngine`,
  `SwI3sProtocolDefs` (+ their headers: `CCrc16`, `CDescrambler`, `CNrzsDecoder`,
  `SwI3sResponseNames`).

The SDK/GUI-coupled files (`CBitstreamDecoder`, `SwI3sAnalyzer*`,
`SwI3sSimulationDataGenerator`) are **not** vendored: `../Decoder` +
`../ISampleSource` take the place of `CBitstreamDecoder`/`WorkerThread`, and
`../compat` supplies an SDK-free `<LogicPublicTypes.h>` so these build with no
analyzer SDK linkage.

## Keeping in sync

If the analyzer plugin's decode logic changes, re-copy the affected files here
(the file list is in `native/CMakeLists.txt`). Port Studio-specific fixes made here
back to the plugin to keep the two in step.
