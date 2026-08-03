"""SWI3S Studio — desktop MIPI SoundWire I3S bus analyzer.

This package wraps the verified C++ decode core (`swi3score`, built from the
Saleae plugin sources) with capture ingestion, an out-of-core results store, and
(later) a PySide6 UI. See ../docs/architecture.md.
"""
# App version, shown in the window title. Kept in sync with pyproject.toml.
# 3.0.12: a quality-gate cycle. No user-facing behaviour changes and no ABI change
#        (score_abi 8) — this release exists because 3.0.11's first published pull request
#        reported 445 lint findings to outside reviewers, which exposed a release process
#        with two partial gates and nobody owning the union.
#        • The gate is now ONE executable definition: `tools/gate.py`, with `tests/gate.sh`
#          and `tests/gate.ps1` as thin wrappers. It rebuilds the native core and ASSERTS
#          score_abi against session.py's requirement (a stale .pyd has faked a green
#          Windows result before), then runs the suite, the perf gate, a per-suite pass, the
#          linter, the type ratchet and a leak scan — aggregating failures so one red check
#          doesn't hide the rest, and treating a MISSING TOOL as a failure rather than a
#          silent skip. `tests/test_release_gate.py` fails the build if ci.yml declares a
#          check the gate doesn't run, so the two can no longer drift. It was briefly a bash
#          script, which made it Unix-only and re-created the very gap it exists to close;
#          running it on Windows then found three portability bugs that macOS could not show
#          (a lint result that depended on whether .git was present, a fatal stub-parse under
#          an older assumed Python version, and POSIX-only baseline path keys).
#        • Lint is BLOCKING and the tree is clean. 445 ruff findings: 226 auto-fixed, and of
#          the 219 that had no safe fix, 24 were real and fixed by hand — three of those
#          would have been WRONG to auto-fix (a test's `app = QApplication(...)` binding
#          keeps the object alive; six `F401`s in a package `__init__` needed a judgement
#          call, and turned out to be dead re-exports nothing referenced). The remaining 194
#          are pure layout (semicolon one-liners, assigned lambdas) and are now ignored with
#          a written rationale rather than hand-split across 30+ files for no behavioural
#          gain — a clean tree is what makes the gate blockable.
#        • Types measured for the first time: 448 errors in 29 files. `mypy … || true` had
#          discarded the result even when the job ran, so the state was unmeasured rather
#          than clean. Now a per-file non-regression ratchet (a single total lets a fix in
#          one file mask a regression in another) against a recorded baseline. **54% of the
#          debt was one root cause:** `apply_palette()` setattr()s 42 palette keys onto
#          VizTheme, so a checker saw none of them — 243 errors across 15 files. Declaring
#          them as bare class annotations (no runtime change; verified by palette switching)
#          removed all 243 at once: 448 → 205. The per-file totals were a symptom, not the
#          shape of the problem.
#        • "Leak" now means more than secrets, and is enforced by `tools/leak_scan.py` in the
#          gate. The previous check was a grep whose pattern list was itself PUBLISHED in the
#          release docs — a list of the strings you are trying not to publish. The scanner
#          carries only structural patterns (private IP ranges, home paths, key file names,
#          private-key headers), scans file NAMES and printable runs inside binaries as well
#          as content, takes site-specific names from a file outside the repository, and says
#          loudly when that file is absent so a partial scan cannot read as a clean one.
#          Auditing every published branch with it found a capture named after a device
#          codename in six prose sites (docs + test docstrings) — removed, replaced by the
#          capture's characteristics, which is more useful to a reader who doesn't have it.
#        • Version drift: `swviz/version.py::APP_VERSION` sat at 3.0.8 for three releases.
#          The displayed version and saved CSVs were always right (they read the live
#          `__version__`), but the legacy-CSV conversion note read the literal directly and
#          recorded "converted … to v3.0.8" the whole time. That call site now uses the live
#          value, and a test pins all three version literals to each other — a checklist step
#          skipped three times running should not be prose.
#        • Docs corrected where they had gone stale rather than wrong-by-writing: three files
#          claimed CI "executes on no host", which the first published PR disproved. The CI
#          section now states where it runs, and a table shows what each gate does NOT cover,
#          because the failure this cycle addresses was two nets each assumed to be a
#          superset of the other. The matrix is deliberate now too, with windows 3.14 added —
#          the configuration the release gate actually validates by hand was in no CI job.
#        • Content is now tiered by audience, because the repository was publishing all of
#          it. A local-only tier is gitignored; a maintainers' tier holds the eight
#          engineering artefacts maintainers need and users do not — the .sal format notes,
#          the tech-debt register, a bug post-mortem, a scoping study, release archaeology —
#          about half of docs/ by weight; everything else is what a user or an outside
#          contributor reads. The prune is a PATH, not a manifest that can drift. What made
#          it non-trivial: 18 references to the now-internal docs lived in files that stay
#          public, SIX of them in shipping code, so naive pruning would have left dangling
#          links in shipped source; they are rephrased as named topics without paths.
#          `tools/publish_tree.py` builds the published tree and verifies it — the prune
#          happened, no dangling references survive, the leak scan passes on the PRUNED tree,
#          and the suite passes on it too, which is the check a manifest cannot give you
#          (docs are safe to drop, a fixture is not). It found four broken tests and a
#          scanner that crashed on an exported tree on its first run. Consequence accepted
#          deliberately: the published tree is no longer byte-identical to the release tag —
#          it is the release tree minus the maintainers' tier, reproducible via that tool.
# 3.0.11: SSPA (Stream Sync Point Announce) support end to end, expandable per-command
#        statistics, region-scoped bus geometry, and a placement fix that over-transported
#        every partial channel group — plus a build-prerequisite pass prompted by a
#        colleague's Linux build failing mid-compile. Native ABI 7 → 8 (DecoderSettings
#        gains forced_column_sections).
#        • SSPAs in the demo captures, and decode that uses them. All four demos now emit a
#          periodic SSPA (~100 ms) in every region with an active data port. An SSPA
#          generates an SSP without committing config, so it has to land on an
#          already-interval-aligned row — the LCM of (Interval+1) over the enabled ports —
#          or it shifts transport phase for everything: the emitter places the command in
#          the CDS so the SSP its Row_Delay implies (14-15 per kSspRowDelayMin/Max) falls on
#          a data-port sync point, and re-anchors the audio-decode data ports there. Short
#          regions that can't fit the period fall back to a single midpoint SSPA. SSPAs are
#          filterable in the command table alongside every other opcode.
#        • Partial captures can now lock their SSP BACKWARDS from an SSPA. A capture that
#          joins after the setup commit has no data-port commit to anchor on, so audio
#          decoded as noise. An SSPA re-asserts sync for ports already configured, which
#          means it dates a sync point the capture never saw the origin of — and the phase
#          can be projected backwards over the preceding rows. On the join-late demo the
#          span BEFORE the SSPA goes from noise to clean audio (SNR -14 dB → +38 dB).
#          This surfaced a decoder bug affecting any flow-controlled port:
#          CPayloadEngine::SyncToSSP cleared the DRQ history unconditionally, so an SSPA
#          arriving while a port sat at its sync point discarded an in-flight DRQ and broke
#          the d = FlowControlDelay+1 handshake. The pipeline is now preserved when the port
#          is already at its sync point.
#        • Expandable per-command statistics. A command row in the statistics pane now folds
#          open to a second level: Ping breaks down per peripheral into PING_ALERT_BUSY /
#          PING_ALERT / PING_ATTACHED_BUSY / PING_ATTACHED / NO_RESPONSE, and reads/writes
#          break down per device into counts, error counts, and bytes transferred.
#        • Bus geometry can be forced per REGION, not just per capture. A config CSV's column
#          count previously applied from row 0 for the whole decode, which is wrong for a
#          capture whose geometry changes mid-stream: the override is now scoped to the
#          cursor's region and that region is labelled "12col (forced)" in the timeline.
#          Fixing this also cleared a stale seed — a forced count left over from an earlier
#          import kept being applied to a subsequent config-CSV decode.
#        • Placement: a partial channel group over-transported by one slot per interval.
#          Every interval must carry exactly enabled_channels × (SampleGrouping+1) ×
#          (SampleSize+1) data slots — ChannelGrouping decides how they're arranged, never
#          how many there are. A trailing group that is PARTIAL (3 channels in groups of 2
#          leaves a group of 1) reset its countdown to the nominal grouping instead of that
#          group's real size, so it transported as if full: 7 cells where 3×2×1 = 6, walking
#          channel_index past the enabled channels. Reported on partial_group_cross_engine.
#          The bug was in the C++ core only — the vendored swviz model already clamped with
#          a min, which is why the two engines disagreed and why the published reference
#          model was the authority. Guarded by the invariant directly rather than one
#          config's placement (85 parametrized cases), since that is the property a user can
#          state without reading either engine; an ad-hoc sweep of 648 register combinations
#          is clean after the fix, and 96 of them break if the fix is mutated.
#        • Analyzer Bus Grid: per-port cell-label fields. Click a data port's key to choose
#          which fields its cells show, with a live preview on a real cell and an "apply to
#          every data port" shortcut — the Analyzer sibling of the Visualizer's display
#          options.
#        • Demo Ping payloads are realistic. Every demo answered PING_ATTACHED for all
#          twelve peripherals regardless of what was on the bus; each demo now reports only
#          the devices it actually instantiates, with undriven PingInfo slots left at the
#          0x3FF undriven symbol. Also added coverage for the join-late partial-capture
#          workflow (capture starts after the config commit, geometry supplied by CSV).
#        • Build prerequisites, after a colleague's Linux build died ~20 lines into
#          compiling bindings.cpp with "fatal error: Python.h: No such file or directory" on
#          a machine with a working g++. The cause was ours: pybind11 fell back to the legacy
#          FindPythonInterp/FindPythonLibs shim (CMP0148 unset at cmake_minimum_required
#          3.15), which finds the interpreter and the shared library but never checks that
#          the development HEADERS exist, so it configured cleanly and handed the compiler
#          an include directory with no Python.h. Now PYBIND11_FINDPYTHON is ON with
#          Development.Module required, failing at CONFIGURE time with a message that names
#          the package to install. Both launchers pre-flight the headers and stop BEFORE
#          creating a venv and downloading ~200 MB of wheels; run.sh additionally prefers an
#          interpreter that has headers when several are on PATH. Either launcher stands
#          aside if SKBUILD_CMAKE_DEFINE already points the build at headers elsewhere, and
#          native/build_local.sh honours PYTHON_INCLUDE for the offline path. The README
#          documents the prerequisite plus no-root recipes (uv, conda, rpm2cpio extraction)
#          for locked-down build hosts.
#        • The swviz reference model carries no comments. dataport.py and
#          flow_control_port.py are published in the MIPI spec as the golden reference
#          model; v2.1.12 shipped one comment in each and v3 had accumulated ~20 lines of
#          implementation commentary across three unrelated commits. All of it is removed
#          (parsed AST unchanged) and enforced by a test; the rationale moved to docs/ and
#          to the tests that cover the behaviour.
#        • Tests: the suite has zero skips. The remaining eight were gated on real captures
#          held outside the repo — they're replaced by coverage on the four synthetic demo
#          captures, which exercise the same paths and run everywhere. docs/TESTING.md and
#          the README now state plainly that CI is defined but executes on no host, and name
#          the real gate: this suite, by hand, on macOS and Windows at the release commit.
#        • Release review (4 lenses: decode/ingest, swviz/model, UI/rendering, cross-
#          subsystem coupling) found 3 defects, all in this cycle's own new code, all fixed
#          and mutation-checked. (1) A command group's error row escaped its fold: the
#          per-device "errors" row is styled "fail", and the Statistics pane only adopted
#          "detail" children, so a collapsed group left the error row visible AND orphaned
#          every device after it. No demo capture contains a CRC-errored Read/Write, so the
#          whole suite passed over it. (2) "Apply to every data port" scanned only the grid
#          window at the cursor, so it missed ports configured in a later region — and with
#          the cursor in the FIRST region it found no ports at all and silently applied to
#          just the port clicked; it now enumerates the config of every region. (3) The
#          timeline legend described SSP Announce as a plain "(gold tick)" while the painter
#          draws it tall and gold, identical to Commit + SSP, promising a distinction the
#          ribbon cannot make. The review also proved by mutation that the resync
#          Column-0 phase seed (Decoder.cpp:1121) has NO test coverage — the code is
#          correct, the guard is missing, and reaching it needs a command-starved demo
#          variant; deferred with the recipe in the maintainers' tech-debt register.
# 3.0.10: large-capture openability + placement/render correctness from user reports —
#        a refuse-before-OOM guard with windowed .sal load, three user-reported rendering
#        and placement bugs, and a test-infrastructure pass that closed the hole which let
#        one of them ship. Native ABI unchanged (score_abi 7).
#        • Large captures no longer take the machine down. A .sal is a zip of delta-coded
#          transitions, so it expands enormously: a reported 291 MB capture holds 2.25 GB
#          of payload and ~2.25 bn transitions — ~18 GB of uint64 edge arrays, ~20 GB peak
#          — and every stage (zip inflate → blob decode → edge array) was whole-file, so
#          opening it exhausted RAM and left the machine unresponsive with no error.
#          Now the cost is PREDICTED from the zip directory alone (no inflation, returns
#          instantly) and a load that won't fit raises before allocating anything, against
#          60% of AVAILABLE memory rather than total. The dialog then offers a TIME WINDOW:
#          only the requested span is decoded, seeking the v3 block chain by its per-block
#          A_start/B_end headers and streaming the blob past instead of holding it, so
#          cost scales with the window, not the file. On that capture a 2 s window opens in
#          ~3 s at ~1.4 GB; reading the capture LENGTH (176.1 s) costs 1.6 s at 0.17 GB.
#          The window is recorded in the session source, so a workspace reopens the slice.
#          Windowed decode is verified byte-identical to slicing a full decode across 424
#          window/size/level/chunking combinations and on the real capture.
#        • Audio waveform no longer draws through gaps. Two active dataport regions
#          separated by a stretch with no decoded samples (~103→133 s on the reported
#          capture) were joined by a straight line implying audio that was never on the
#          bus. Audio sample INDICES are contiguous across such a hole — only the capture
#          POSITIONS reveal it — so the renderer now breaks the polyline there, and splits
#          any envelope bin that straddles a gap: the render pyramid is built over the
#          contiguous index array, so one decimated bin could aggregate the silence before
#          a dropout with the loud audio after it, drawing a jump from zero at a position
#          whose decoded sample is 0 (reported on dev6 dp2 at 103,667,003.52 µs).
#        • Channel-group spacing no longer leaks across a row boundary. With Spacing≥2 and
#          a transport window that closes before the countdown finishes, the remainder
#          carried into the next row and ate its first in-window UI, so data started one
#          column late and the error alternated as it re-accrued. Spacing is a WITHIN-row
#          gap; 1.74 terminated it via its done_with_row latch and the rewrite dropped that
#          side effect. Fixed in BOTH of Studio's placement engines — the C++ decode core
#          and the vendored swviz that renders the Visualizer tab (the first fix reached
#          only the core, so the same CSV still rendered wrong). Verified 768/768 against
#          the 1.74 golden model in each. New `spacing_overflow` directed test covers the
#          geometry no existing config could reach — a window ending at the LAST column —
#          where we deliberately diverge from 1.74 (whose latch can never fire there, so
#          its spacing counter runs to −1); see the maintainers' spacing row-boundary write-up.
#        • Quitting mid-load can no longer abort the app. closeEvent joined the
#          TX-persistence worker but left the capture-load and sub-capture-search threads
#          running; Qt calls abort() when a running QThread is destroyed. All three are now
#          joined, via an idempotent MainWindow.join_worker_threads() plus an atexit hook.
#        • Demo menu + demo fidelity: the four demo captures are labelled by PHY throughout
#          — PHY1 (FBCSE), PHY2 (FBCSE), PHY2 (Flow Control), PHY3 (DLV) — with flow
#          control next to its PHY instead of a separate trailing section. The demo
#          synthesiser's idle CDS is now the D10.2 0101 pattern per {ASW2201} rather than
#          all-ones, which NRZS-held the FBCSE bus flat: a dead line where a real bus shows
#          the idle zebra.
#        • CDS symbol pane: per-cell selection and copy, matching the Command table — pick
#          one cell or drag out a range and ⌘/Ctrl+C it, instead of only ever copying whole
#          symbol rows. The blocker was that its cursor sync read selectedRows(), which is
#          empty for a partial row; it now follows the current cell's row.
#        • Test infrastructure (this is what let the spacing bug ship). tests/run_all.sh
#          executed each suite as a SCRIPT, so it ran only what that file's __main__ block
#          happened to call: 60 tests were invisible to it and six files with no __main__
#          block ran nothing, exited 0, and were reported "ok" — including a regression
#          guard added days earlier. It now delegates to pytest per suite, so its coverage
#          equals CI's by construction (verified: same test count both ways), keeping the
#          per-suite reporting and process isolation that make it worth having — that
#          isolation is what surfaced the QThread shutdown bug above. tests/test_collection.py
#          fails the build if any test file collects zero tests; the 63 now-vestigial
#          __main__ blocks are deleted (one kept: the authoring-golden --regen tool, now
#          documented). docs/TESTING.md's "No CI yet" and "Performance is untested" notes
#          were false since 3.0.7/3.0.8 and are corrected.
# 3.0.9: flow-control support (TX_PRESENT / DRQ / FCP) end to end, plus a decoder
#        framing fix and visualizer handover corrections found alongside it.
#        • Flow control decode + demo. A new "Flow Control (4 modes)" demo capture carries
#          four peripheral data ports, one per flow mode (NORMAL / TX_CONTROLLED /
#          RX_CONTROLLED / ASYNC): audio is sampled uniformly but the TRANSPORT is jittered
#          on a 2x-oversampled opportunity grid, exercising the handshake. The decode now
#          GATES on TX_PRESENT — a sample is emitted only when its TX_PRESENT bit read 1, so
#          jittered/gated transport de-jitters back to a bit-exact stream (the descrambler
#          LFSR is still advanced on skipped opportunities). The FCP is re-driven during
#          decode to read the DRQ bit off the bus and VALIDATE the DRQ↔TxPresent handshake
#          (SWI3S §14.2.2 {ASW5205}: RX bijective, ASYNC data-only-if-requested) with the
#          d = FlowControlDelay+1 pipeline; results via Decoder.flow_control_stats(). DRQ /
#          FCP guard-tail cells now render on the bus grid (wide SOURCE DRQ drives every UI,
#          wide SINK DRQ occupies only its last-UI sample point). The audio waveform de-
#          jitters flow-controlled streams to the MEASURED average rate (what a receiver FIFO
#          clocks out), instead of plotting at bus transport time / labeling the oversampled
#          opportunity rate. Native ABI 6 → 7 (AudioSample gains flow_mode + drq_sample).
#        • Decoder framing fix (affects ANY capture). A detected K.28.7 SPM no longer re-
#          frames or abandons the phase in progress: per §7.2.2 {ASW1907} / §8.2.1 {ASW2206}
#          an SPM only ARMS header detection, and mid-phase framing is held by counting bits
#          mod 10. Previously an uncontrolled Write/Read payload byte — or the CRC, which no
#          encoder can constrain — that happened to spell the 10-bit comma at a sub-symbol
#          offset would desync the frame and drop the command.
#        • Visualizer placement. A handover landing on a different-device SINK bit is no
#          longer flagged as a bus clash (a sink drives nothing; only a SOURCE driver
#          clashes); sink data ports in RX_CONTROLLED / ASYNC now draw their DRQ-driven
#          handover. TX_PRESENT is present in RX_CONTROLLED too, per {ASW5203}.
#        • Tests/tooling: a bit-exact flow-control round-trip (all four modes) + grid/
#          handshake guards; full-corpus cross-engine (swviz vs C++) parity incl. FCP
#          placement; restored the visualizer round-trip coverage + a stats-report runner
#          (tools/viz_report.py).
# 3.0.8: large-capture performance + interactive responsiveness — native .sal v3 decode,
#        bounded link-control detection, off-thread TX-map persistence; plus a decode/ingest/
#        UI review pass, a formalized perf/UI regression process, and dock/menu/timing polish.
#        • Large-capture speed + responsiveness (from user testing on an 89 MB / 420 M-edge
#          PDM .sal — full open 87 s → ~16 s, all output-preserving/bit-identical):
#          the Saleae v3 transition-delta codec is decoded in the C++ core (score_abi 6),
#          ~8x the Python loop and without its ~11 GB transient list; Cold/Warm-start
#          bring-up detection reads a bounded leading-edge PREFIX instead of sorting/scanning
#          the whole capture (~26 s → ~0); the DLV complementary-pair check rejects on edge
#          counts before copying gigabytes; audio channels group + time-order in one lexsort;
#          and TX-map persistence scans data edges (bincount), not every UI with
#          logical_or.at (~88 s → ~9 s), and runs on a worker thread for large regions so the
#          grid never freezes (placeholder + token-guarded fill; cached regions render
#          instantly). The TX map / Persistence now also show "No PHY Selected" in a
#          cold-start's pre-audio region (like the config layout) instead of rastering the
#          single-ended bring-up into a bogus 2/4-column grid.
#        • UI polish: dock title bars are a themed custom widget (close X flat + on the
#          left, matching macOS) and the tabified-dock tab-bar base is flattened, so no pale
#          native strip shows behind the tabs or buttons. Analyzer File menu reorganised
#          (Import Visualizer CSV rename; Export Capture + Locate Sub-Capture grouped with
#          Open; Save Bus Grid Image under View Bus Grid); a Clear Compare button by Clear
#          Edits in the Register pane (wider device picker); Timing Calculator top bar split
#          to two rows so nothing clips, and the Analyzer Timing hold-plot regained its grid.
#        • Process: the perf/UI regression loop is formalised in the maintainers' performance notes — the
#          recurring whole-capture-scan class, CI cliff-detectors on a large SYNTHETIC
#          capture (tests/test_perf.py), release-time interaction profiling, the periodic
#          deep-review Workflow, and the UI shots checklist. Decode multi-threading was
#          scoped and deferred (the maintainers' decode-multithreading study).
#        • Performance pass (post-review, all output-preserving — see the maintainers' performance review).
#          Interactive latency: the Symbols/Registers panes bulk-fill without a per-item
#          repaint storm; the bus-grid content cache uses a cheap key + addSimpleText
#          labels; register-map, commit-marker, filtered-command and CDS-symbol selection
#          on a cursor move are now cached + bisected instead of rebuilt/scanned; the
#          command-table column fit and timeline commit markers are bounded. Memory /
#          throughput: audio resampling is a bounded polyphase decimator (low-pass FIR +
#          drop samples) instead of a whole-capture FFT — a minutes-long PDM stream no
#          longer builds multi-GB FFT temporaries; the C++ decode reserves its audio
#          vector, the DLV clock recovery walks a forward edge cursor (not a per-UI binary
#          search), and .bin/CSV/WAV export + PDM decode avoid over-wide temporaries. All
#          verified byte-identical / within DSP tolerance against the goldens.
#        • Architectural pass (post-review, no behaviour change): the .sal v3 codec is
#          now a public saleae_binary API (encode_v3_delta / build_logic2_channel_v3)
#          with one shared block-chain emitter instead of two copies + private reaches;
#          the capture source-type dispatch moved onto Session.from_source (co-located
#          with the from_* factories, so adding a format is a one-file change); the
#          bus-grid slot vocabulary is unified behind one GridSlot enum (test-pinned to
#          the C++ SLOT_NAMES so it can't drift); Statistics measurements build lazily on
#          first show instead of eagerly for a hidden tab; and MainWindow's six copies of
#          the re-decode-preserving-state block collapse to one _redecode_preserving
#          helper. Docs (architecture/testing/tech-debt) reconciled with shipped reality,
#          incl. an honest account of the two live placement engines (C++ decode + swviz
#          authoring) with a new direct cross-engine placement guardrail test.
#        • Timing Calculator now covers PHY1 as well as PHY2. A PHY selector in the
#          top bar switches the specification: PHY1 (FBCSE-slow) and PHY2 (FBCSE-fast)
#          share the setup/hold inequality structure exactly — the SWI3S spec (§11.2)
#          documents them together and points both at the same RX/TX timing tables
#          (128–130) and setup/hold measurement waveforms (Figures 174–179) — so only
#          the Specification values differ. PHY1 gets its slower Peripheral output-hold
#          / high-Z-recovery and Manager high-Z-recovery times (Table 129: Per_t_DD and
#          Per_t_ZD 14–50 ns, Man_t_ZD 14–30 ns, vs PHY2's 2–20/2–18) and its lower
#          mandatory F_CLK (Table 126: 6.6 MHz max, 6.4 target). The Specification
#          column is now READ-ONLY for both PHYs — it shows the spec's own min/max — so
#          only the Example column is editable; switching PHY reseeds both from that
#          PHY's tables. The selected PHY persists in the workspace / saved settings.
#          reconfigures mid-stream (multiple regions) now warns first — a single config
#          is imposed from row 0 and can only match one region, misframing the others
#          (turning their commands red); and a non-config CSV (a capture picked by
#          mistake) is rejected with a message instead of silently imposing an empty
#          config (blank bus grid). The SSP nudge shortcuts moved to ⌘K / ⌘L (−1 / +1);
#          ⌘, is macOS-reserved (Preferences) so its glyph never showed.
#        • Export Capture: the separate Export .sal / .bin / CSV menu items are now one
#          File ▸ Analyzer ▸ Export Capture… dialog — pick the format, which signals +
#          their names, the range (whole capture, or a time / bus-row / UI window), and
#          the output file in one place. Ranges slice a rebased sub-capture
#          (Capture.subcapture); .bin/CSV can drop a signal, while a .sal locks the pair.
#        • Partial DLV (PHY3) captures: a mid-stream differential capture with no
#          §5.1.2 cold-start is now auto-detected and decoded — complementing the
#          existing FBCSE partial-capture support. A Logic digital CSV whose two
#          channels are a complementary pair (data == NOT clock) is routed through the
#          recovered-clock DLV path: the true sample rate is taken from the timestamp
#          quantum (an edge export's finest spacing is one UI, not one sample, so the
#          old min-spacing guess under-reported it), the Safe-Lock column count is
#          blind-detected by which framing yields CRC-valid commands, and RSPs, the CDS
#          symbol table and the recovered clock all come out with no PHY-select on the
#          wire (analysis.dlv_detect; Session(dlv=...) / from_digital_csv auto_dlv).
#          The Bus Grid draws the full operational frame (Sync1 at Column 0, CDS at
#          Column 2, Sync0 at the last column) even with no NumColumns commit on the
#          wire, instead of collapsing to a bare 2-column Sync1/Sync0 layout. DP/DN
#          probe swap is auto-corrected: a differential swap inverts every logical
#          level, so detection tries both polarities and flips the pair when the
#          swapped orientation is the one that yields CRC-valid commands. Detection now
#          runs in the Session ctor for EVERY source (.sal / .bin / CSV / workspace
#          reopen), so re-importing an exported partial-DLV .sal decodes like the
#          original. Show Toggles (TX map) flags the CDS at its real column (Column 2
#          for DLV) and draws the full operational width instead of shifting the frame.
#          Opening a partial DLV capture in the Visualizer (or Export Visualizer CSV)
#          shows the operational DLV frame (16 columns, PHY3) instead of "no bus config"
#          — the config geometry is forced from the detected width even with no config
#          commit on the wire.
#        • Timeline responsiveness: the heavy half of the cursor cascade (register
#          replay + CDS-symbol / Decoded-Samples table rebuilds + bus-grid render,
#          ~150 ms on a big capture) is now coalesced behind a single-shot timer, so
#          clicking around the timeline collapses to ONE rebuild at the resting cursor
#          instead of stacking one per click on the GUI thread (the cursor line still
#          tracks instantly). Same pattern as the drag/playback throttles; stays
#          synchronous headless/in tests.
#        • Capture data-out: File ▸ Analyzer now offers Export .bin (one documented
#          version-0 <SALEAE> blob per channel) and Export CSV (Logic-2 digital CSV,
#          a row per transition) alongside Export .sal — portable, tool-readable
#          workarounds that round-trip through the app (ingest.raw_export).
#        • .sal export rewritten for Logic-2 compatibility: version-3 internal blobs
#          (full header — sample rate, capture timestamp, the 10-byte single-region
#          preamble, block at offset 51) and a schema-complete version-22 meta.json
#          carrying all 16 data keys Logic validates (renderViewState, captureSettings,
#          legacyDevice+capabilities, dataTable, analyzerTrigger, timeManager, …),
#          reverse-engineered from several real Logic Pro 16 captures. Earlier exports
#          wrote v0 blobs (Logic: "an older version") or a partial meta.json (Logic:
#          "file schema is invalid"). The capture's sample rate is injected into the
#          device rate menu so Logic's cross-check passes; the ZIP also carries the
#          fixed trigger-store.bin member Logic requires (without it: "Failed to load
#          file"); the transition stream is chunked into blocks with the preamble's
#          block-count (nblocks×256) set to match (a single block / zero count stalls
#          Logic at "Preparing session"); both channels share one capture-end sample;
#          the file still round-trips through SWI3S Studio.
# 3.0.7: dev workflow — CI test matrix (ubuntu/windows/macos) + goldens, perf-regression
#        gate, ruff/mypy/coverage, dev/release workflow docs + tech-debt register.
#        NEW PHYs, demo captures for all three: File ▸ Load Demo Capture ▸ PHY1/2/3.
#        • PHY3 (DLV) differential-pair synthesis + a virtual-PLL decode front-end that
#          recovers the bit clock from the Sync1 row edges, samples mid-UI, reads a plain-NRZ
#          CDS at Column 2, and converges on the shared FBCSE core — bit-identical audio to
#          the PHY2 demo. The recovered clock changes rate 4x at the commit: the row/reference
#          rate is held constant (3.072 MRows/s) and the PLL divider changes, so the bit clock
#          speeds up (Safe-Lock-4 -> 16 columns) rather than the row rate dropping. Raw Capture
#          shows the differential signal + the recovered bit clock (per-row cycle count, so the
#          4x speed-up is visible); the CDS symbol table, Timing eye (setup/hold vs the mid-UI
#          sample point), Statistics rates, and the commit marker are all DLV-correct and agree
#          on the geometry-change row.
#        • PHY1 (FBCSE-slow) demo: a constant 4-column bus whose ports are REPOSITIONED
#          mid-capture (a placement-only commit), matching the reference phy1_bus_config CSVs.
#        Realistic sampling: every demo now samples at 500 MHz (2 ns) — a real analyzer's
#        fixed max, not a multiple of the UI — so there is a non-integer number of samples per
#        UI and edges land on a jittered grid; the decode works from the actual edges.
#        Cold-start bring-up polish: Bus Grid never claims a PHY before PhyStart (shows the
#        sub-phase, then "PHYn Selected" once clocked), grid viewport re-anchors on re-render
#        (fixes the "click twice to sync" stale-scroll bug), the timeline audio band is
#        labelled "PHYn Safe-Lock-N" clear of the link-control bands, and a two-click cursor
#        desync is fixed (the Decoded-Samples / CDS-symbol panes no longer echo their
#        programmatic selection back out as a user selection).
#        Performance & UX: demo edge builders vectorised (NumPy, no per-UI Python loop) and
#        demo load runs through the async worker + progress dialog (same as an external
#        capture) instead of freezing the GUI; faster .sal export (deflate level 1, ~6x);
#        section-UI stats bisect the segment span; Statistics pane regrouped into collapsible
#        sections; Decoded-Samples pane fills without a per-item repaint storm (fixes a
#        multi-second open stall); the macOS startup no longer emits a spurious
#        "modalSession exited prematurely" warning (Cocoa activation deferred past exec()).
# 3.0.6: large-capture performance pass — lazy Samples window with eviction, memoized
#        filtered-sample queries, decimated timeline paint, cached TX raster + persistence,
#        symbols in-window guard; PDM derived-rate estimator fix; SSPA immediate-reconfigure
#        decode fix (Decoder.cpp); run.ps1 no longer aborts on the stale-.so rebuild probe.
# 3.0.5: launchers (run.ps1/run.sh) force-reinstall the native core so an unchanged
#        0.1.0 version can't leave a stale build, and only stamp on a successful build.
# 3.0.4: windowed on-demand Raw Capture port-sample decode (no full re-decode);
#        commit/command cursors anchor at the Row Sync Point (spec-coincident),
#        incl. width-changing commit grid geometry; TX-map Bus Grid raster;
#        Apply Config CSV to an open capture; UI polish (selection copy, hovers).
# 3.0.3: Windows support (x64 wheels, MSVC build, launchers), CDS disparity column,
#        ping-period stats, v0 .sal load memory fix; MIPI OSS release scaffolding.
# 3.0.2: mode-grouped menus; per-mode File submenus; link-control timeline + timing.
# 3.0.1: workspaces persist the TX-map + Hide-Clock view state.
__version__ = "3.0.12"

from .api import DecodeResult, decode, decode_capture
from .ingest import saleae_binary
from .ingest.capture import Capture
from .ingest.transitions import build_capture_from_levels, demo_capture
from .session import Session
from .workspace import Workspace, session_from_source

__all__ = [
    "Capture",
    "build_capture_from_levels",
    "demo_capture",
    "saleae_binary",
    "DecodeResult",
    "decode",
    "decode_capture",
    "Session",
    "Workspace",
    "session_from_source",
]
