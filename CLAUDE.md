# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

**SWI3S Studio** — a desktop MIPI SoundWire I3S (SWI3S) bus analyzer. A PySide6 UI over a
verified C++ decode core (`swi3score`, built from `native/` via pybind11), with capture
ingestion, an out-of-core results store, audio decode, timing analysis, and the SWI3S
Visualizer's authoring/drawing capability as one mode inside it.

It is an application with a full test suite and a two-platform release gate — not a
documentation or requirements-tracking project.

## Where the guidance lives

Read the doc rather than inferring from the code:

| topic | file |
|---|---|
| branch model, CI, code review, release checklist, publishing | `docs/DEVELOPMENT.md` |
| running tests, markers, what each suite covers | `docs/TESTING.md` |
| subsystem map and the decode pipeline | `docs/architecture.md` |
| code conventions | `docs/STYLE_GUIDE.md` |
| packaging and binaries | `docs/PACKAGING.md` |
| the application from a user's side | `docs/USER_GUIDE.md` |
| what a tabulated timing number contains (t_DD/t_ZD/t_DZ anchors) | `docs/anchors.md` |

The changelog is the comment block at the top of `swi3s_studio/__init__.py`, newest first.

## Before committing

```bash
bash tests/gate.sh          # macOS / Linux
.\tests\gate.ps1            # Windows (PowerShell)
```

Both are thin wrappers around `tools/gate.py` — one executable definition, so the platforms
cannot diverge. It rebuilds the native core and asserts its ABI, then runs the suite, the
performance gate, a per-suite pass, the linter, a type-regression ratchet and a content
scan, aggregating failures so one red check cannot hide the rest. Exit 0 is the gate.

**`NOT RUN HERE` in the summary is not a pass.** Some checks are environment-bound and
report NOT COMPARABLE rather than pretending; `docs/DEVELOPMENT.md` says which, and what
each gate does *not* cover. Neither the gate nor CI subsumes the other — run both.

## Adding a timing inequality

`swi3s_studio/timing/calculator.py` opens with a checklist naming the test that enforces each
rule — symbols, lanes, arithmetic, corners, zeros, and the reference cross-check. Read it
before adding a leg. The short version: every style drift so far was invented by copying a
neighbouring leg that was itself the odd one out, so the rules are tests rather than prose,
and `_LEGAL_LANES` in `tests/test_timing.py` is the one that needs a human — a wrong-lane bug
is self-consistent and only a declared statement of intent catches it.

## The timing model has a second home

`swi3s_studio/timing/` is the LIVE calculator, but the same model exists in
`../timing-analysis/swtiming/` (`emit_ede.py`, which generates the EDE paper's numeric
macros). The two have diverged deliberately in places and accidentally in others.

- `docs/anchors.md` is the reconciliation of record for the measurement-anchor question, and
  names what is still open on the paper's side.
- That project's `docs/MODEL_AUDIT.md` is its own audit of the same three artefacts. Read
  both before changing a leg: each has found real defects in the other.
- The two name their legs by OPPOSITE conventions — the reference names a leg for the
  handover direction, this project for the data direction, so `emit_ede.setup_mp` is this
  project's `PM_setup_ho`. Comparing like-named legs "finds" errors that are not there.

## Things that bite

- **Rebuild the native core after editing `native/`:** `bash native/build_local.sh`. The
  built module is gitignored, so nothing rebuilds it for you, and a stale one has faked a
  green result on a release gate before.
- **`swviz/models/dataport.py` and `flow_control_port.py` are VENDORED — do not edit either
  without the maintainer's explicit approval.** They are the transport-placement reference
  model published in the SWI3S specification, authored externally and dropped in whole. No
  local edit is only local: the next drop reverts it or has to be reconciled by hand. They
  also carry no comments at all, which a test enforces — put an explanation in the commit
  message or the covering test. See `docs/DEVELOPMENT.md`, "The swviz reference model is a
  published spec deliverable".
- **Never regenerate golden fixtures without showing the diff first** — which fixtures,
  which fields, old → new — and wait for review.
- **Stage explicitly rather than `git add -A`.** A test rejects file-sync conflict copies,
  because a stale duplicate of a gate wrapper is something a person runs and believes.
- **A config CSV encodes the manager data port as device 0 plus a flag**, since the device
  register cannot hold the −1 sentinel. Both engines decode the pair; treat a bare device
  number from a CSV with suspicion.
