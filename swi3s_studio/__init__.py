"""SWI3S Studio — desktop MIPI SoundWire I3S bus analyzer.

This package wraps the verified C++ decode core (`swi3score`, built from the
Saleae plugin sources) with capture ingestion, an out-of-core results store, and
(later) a PySide6 UI. See ../docs/architecture.md.
"""
# App version, shown in the window title. Kept in sync with pyproject.toml.
# 3.0.17: Saleae .sal version 4 opens, and four separate performance defects are fixed. Logic 2
#        bumped its internal blob version with no format change; the fix is small, but it was
#        VERIFIED against a v0 export of the same capture rather than accepted because the parse
#        looked plausible, and the accepted version set is now pinned by a test. The four are one
#        class. The memory guard's budget scaled with free RAM while its prediction under-read
#        the real peak by up to 3.6x, so a machine with more memory opened more without asking —
#        the failure grew with the hardware. A cursor move rebuilt 48,000 table items. A windowed
#        open of a long capture cost its START OFFSET rather than its window. And a region scan
#        sized for "sub-0.3s" was freezing the GUI thread for ~550 ms. What a user sees: a time
#        window reachable on purpose at any size, a prompt that fires on every machine rather
#        than only small ones, and navigation that no longer stalls.
#        Performance is now guarded by COUNTS as well as clocks — a ceiling on a function
#        cannot say "and not on the GUI thread", which is precisely how the worst of these
#        shipped green. Gated on every supported platform before tagging; a second platform
#        rejected one of the new tests twice before its measurement was right.
#        • THE NEW MEMORY TEST MEASURED A HIGH-WATER MARK AS IF IT WERE A DELTA, and only a
#          second platform said so. `ru_maxrss` is the peak for the whole PROCESS, so an
#          after-minus-before taken inside one process measures the load only when the load is
#          the biggest thing that process ever did. Three attempts, and the two that used maxrss
#          were each wrong on a platform nobody was looking at: building the fixture in the same
#          process reported 0.164 GB for a load that truly took 0.580 GB (it would have passed a
#          3x under-prediction), and measuring before/after around the load gave exactly 0.000 GB
#          on another platform, where IMPORTING peaks at ~0.9 GB and then releases it so a
#          0.2 GB load never exceeds the earlier transient. Subtracting two such processes does
#          not help, because they share that transient — which is why the second attempt reddened
#          again with 0.897 GB baseline against 0.897 GB loaded.
#          The measurement now SAMPLES CURRENT RSS across the load on a 2 ms thread and takes the
#          max, which is immune to anything that happened before the load and is precisely what
#          `est_peak_bytes` models. macOS: 0.034 GB before, 0.242 GB at peak, 0.207 GB actual
#          against 0.264 GB predicted, ratio 1.27. Both mutations still redden it (bare sum
#          2.88x under, a 16x transient 5.45x over), and a machine without psutil SKIPS with a
#          stated reason rather than passing silently. A multi-platform gate found this twice;
#          a single-platform one would have shipped a hollow assertion that reads as coverage.
#        • PERFORMANCE IS NOW GUARDED BY COUNTS, NOT ONLY BY CLOCKS — `tests/test_cursor_cost.py`.
#          Every per-move stall this project has shipped got past the perf suite for one of two
#          structural reasons, and neither is fixed by adding more ceilings. A ceiling on a
#          FUNCTION cannot say "and not on the GUI thread": `test_tx_persist_columns_ceiling`
#          allowed 30 M UIs at 1.5 s and stayed green while a 4.7 s capture froze ~550 ms per
#          move, because the defect was WHICH THREAD the call landed on. And a wall clock has
#          to be loose enough to survive a shared runner, so it catches cliffs and never drift.
#          The new assertions are therefore counts — rows built per cursor move, which thread a
#          region scan runs on, hidden docks costing nothing — deterministic and identical on a
#          busy runner. Six of them, each mutation-tested against the defect it exists to catch:
#          restoring the 32 M threshold, making the in-window Samples path rebuild, dropping the
#          hidden-dock guards, routing everything async, and running a big scan inline each fail
#          exactly one test and nothing else.
#          The pattern that emerged is worth naming: each cost needs a MECHANISM test and a
#          CALIBRATION test. Both defects were a constant set too high, and a bound stated
#          relative to the constant moves with it — so `_SAMPLE_HALF_WINDOW <= 1000` and
#          `_TX_PERSIST_SYNC_MAX_UIS <= 4_000_000` are asserted ABSOLUTELY, while the routing
#          and windowing tests check the mechanism still works at whatever the constant says.
#        • THE CURSOR CASCADE NOW HAS A WALL-CLOCK BUDGET, and its docstring states what it
#          cannot see. `test_cursor_move_budget_with_every_dock_visible` drives `_on_cursor`
#          with all ten docks open: baseline 22-29 ms median / 34 ms worst on this machine,
#          ceilings 150 ms and 350 ms. It is a coarse backstop by necessity — mutation-tested to
#          catch a +120 ms regression and to MISS a +40 ms one, and on the demo capture the
#          Samples defect that cost ~99 ms on a real 4.7 s capture adds only ~19 ms, so no
#          ceiling on this fixture could have caught it. That limit is written into the
#          docstring rather than left for the next person to assume otherwise; extending it to
#          a realistic multi-region capture with audio and commands is the piece still missing.
#        • THE TX-PERSISTENCE INLINE THRESHOLD WAS SET 16x ABOVE ITS OWN STATED BUDGET.
#          `_TX_PERSIST_SYNC_MAX_UIS` decides whether a config region's persistence scan runs
#          on the GUI thread or a worker, so the constant IS the freeze a cursor move can
#          cause. Its comment claimed "sub-~0.3s" and it was 32 M UIs; the scan measures
#          ~32 ns/UI (19.2 M UIs of a real 4.7 s capture took 616 ms), so it admitted ~1.0 s.
#          Worse, this bit SHORT captures rather than long ones: a 4.7 s capture's own
#          18.9 M-UI region sat UNDER the threshold, so the first cursor move into it froze
#          for ~550 ms with TX persistence on -- measured 530 ms median per move against
#          28 ms with it off, and 557 ms worst on a realistic walk. Now 2 M UIs, which is the
#          inline budget rather than what the scan can eventually manage; that region goes to
#          the worker and the same walk's worst move is 240 ms. The async path was always the
#          designed route for big regions, not a fallback to avoid. The constant had NO test:
#          `test_perf.test_tx_persist_inline_threshold_stays_inline` now drives a region of
#          exactly the threshold size and holds it to a ceiling, so raising it reddens the
#          gate. `test_tx_persist_columns_ceiling` guards the scan's own O(#edges) behaviour
#          and could not see this, because it tests the function rather than the gate.
#        • THE MEMORY GUARD UNDER-PREDICTED BY 2-3.6x AND ITS BUDGET GREW WITH THE MACHINE,
#          which together let a 103 GB box load a capture to ~60 GB resident with no prompt at
#          all. Two separate defects pointing the same way:
#          `_STREAM_TRANSIENT = 4.0` was applied ONLY to the windowed branch of `load_capture`
#          — the path that allocates LEAST — while the whole-file branch, which allocates most,
#          used the bare `uncompressed + edges` sum. Measured on real captures: predicted
#          0.314 GB against 0.598 actual, 0.596 against 2.125, 0.275 against 0.921. The
#          transient belongs on the EDGE term only, since building the arrays holds the delta
#          run, its cumsum, the mask and the concatenate at once while the inflated blob is one
#          flat allocation; modelled that way the same three predict 0.733 / 2.134 / 0.948 —
#          within a few percent, erring high, which is the safe direction for a guard.
#          `memory_budget` returned 0.6 x FREE memory with no ceiling, so the more capable the
#          machine the more the app loaded before asking a human — which inverts the guard: the
#          better machine was the slower one. 16-32 GB machines got the window prompt for a
#          291 MB capture and stayed fine; a far larger one loaded it whole and sat in continuous
#          macOS memory compression, which is a minute-long stall no amount of per-move Python
#          tuning reaches. `_MAX_AUTO_BUDGET` caps the computed budget at the same 8 GiB the
#          unknown-memory fallback uses, so "memory unknown" and "memory plentiful" now agree on
#          the largest load taken without asking. An explicit `max_bytes` still overrides,
#          because a caller naming a number has already made the decision. That capture now
#          predicts 74.4 GB against 20.3 GB before, and the prompt fires at every free-RAM level
#          from 4 GB to 1 TB.
#        • A WINDOW IS NOW REACHABLE ON PURPOSE: Analyzer ▸ Open Capture Time Window…. The
#          size-triggered prompt was the ONLY route to one, so a capture the app was willing to
#          open whole could not be sliced at all — and with plenty of free memory that was every
#          capture, including the one a user had already decided they wanted 20 s of. "It fits"
#          answers a question about capacity, not about staying interactive.
#        • A WINDOWED .sal OPEN COST ITS START OFFSET, NOT ITS WINDOW, and the whole of that
#          cost bought ONE BIT. `_decode_v3_window_streaming` skips blocks that end before the
#          window, but the only thing a skipped block contributes is the LINE LEVEL at the
#          window's start, and that was derived by decoding every delta in every preceding
#          block. On the reported 291 MB / 176 s / 2.25 G-transition capture, the same 0.5 s
#          window cost 0.88 s at the start, 10.51 s at 88 s, 19.18 s at 132 s and 27.53 s at
#          175.5 s on the clock channel alone — and a window is what the app offers when a
#          capture is too big to open whole, so the remedy for a large capture was itself
#          O(size). Terminator counting cannot replace the decode (a code whose MSB digit is
#          < 0x40 encodes as two bytes both < 0x80, and an odd over-count inverts the level
#          silently), which is why the decode was there.
#          The bit is already in the file. Every block header carries `level`, the state at
#          that block's START, and the block spanning s0 is decoded ANYWAY because it overlaps
#          the window — so its own header plus its own transitions before s0 give the level at
#          s0, and no earlier block needs decoding at all. Both channels of every window tried
#          now return the parity path's initial state AND samples byte-for-byte, with the
#          per-window total falling 42.0 s -> 3.25 s at 175.5 s and 29.2 s -> 2.9 s at 132 s.
#          What remains is the sequential inflate of a DEFLATE zip member, which cannot be
#          seeked past.
#        • THE LEVEL FIELDS ARE ONLY TRUSTWORTHY ON A LOGIC-SHAPED BLOB, and the signal for
#          that is the METADATA HEADER, not the level values. `build_channel_v3` — the minimal
#          internal writer — stamps 0 on every non-first block, so trusting it would invert the
#          level; but the values cannot discriminate, because with an even block size and a low
#          initial state every block legitimately starts at 0. The first-block OFFSET can: a
#          minimal blob puts it at magic+version+type and nothing else, while a Logic-shaped
#          writer puts a metadata header first — 51 bytes for `build_logic2_channel_v3`, 67 on
#          the real capture. An unrecognised blob raises `_UnstampedBlockLevels` and the loader
#          retries by parity, so an unfamiliar writer loses the speed and never the answer.
#          `build_logic2_channel_v3` had no test coverage at all before this; the stamped path
#          now has a test that booby-traps `_count_and_discard`, so decoding a skipped block
#          fails the suite rather than merely slowing it down.
#        • THE DECODED-SAMPLES PANE COST ~99 ms OF EVERY CURSOR MOVE, and the cost was the
#          WINDOW SIZE rather
#          than any algorithm. The pane is a QTableWidget refilled wholesale, so a cursor move
#          that leaves the loaded window discards every item and builds
#          2 * _SAMPLE_HALF_WINDOW * 6 fresh ones; a move INSIDE the window is only a
#          select_sample and costs nothing. At the original half-window of 4000 that rebuild was
#          48,000 items and profiled at ~99 ms of a 142 ms cursor move on a 2520x1350 dpr-2.0
#          display — which is exactly the reported shape, "the cursor jumps instantly, then it
#          hangs", because jumping OUT of the window is what triggers the rebuild. Hiding the
#          dock took the same move to 43 ms, and that is what identified the pane rather than the
#          decode. Now 500/1000: the rebuild is 6,000 items, measured 21.5 ms -> 2.4 ms offscreen
#          (linear in item count, so ~99 ms -> ~12 ms on a real display). A screenful is ~40 rows,
#          so several hundred still absorbs ordinary arrow stepping without rebuilding. The size
#          must not be raised to buy fewer rebuilds: the rebuild is O(window) and the size only
#          buys a CHANCE of staying inside it, so that trade runs the wrong way.
#        • SHRINKING THE WINDOW UNCOVERED A RE-ENTRANT EDGE LOAD that the old constants had been
#          hiding by arithmetic. `prepend_samples` re-anchors the view with `sb.setValue()` after
#          inserting rows; the scrollbar is a SEPARATE OBJECT from the table, so the
#          `blockSignals()` around the insert never covered that call, and the new value can land
#          within `_EDGE_PAD` of either end — which `_on_scroll` read as the user scrolling there
#          and answered with another `edgeReached`. A scroll-to-TOP extend therefore also appended
#          a chunk at the BOTTOM: double the row building, and at `_MAX_LOADED` it evicted the
#          very rows the user had scrolled up to reach. `_sample_range` and the pane's real
#          contents drifted apart as a result. The re-anchor now happens under the scroll guard.
#          `test_window_is_bounded_and_lazily_extends` could not see this: with a 4000-row window
#          12000 + 4000 was EXACTLY `_MAX_LOADED`, so the stray append's own eviction landed the
#          row count back on the expected number and the test stayed green. The new test asserts
#          the absence of the cascade itself and that `_sample_range` still describes what is
#          loaded, and `test_perf` now holds a ceiling on the rebuild plus a bound on the
#          half-window — the perf suite had ceilings on `tx_persist_columns` and the command
#          filter, both the same family, but nothing on this pane, which is why it shipped green.
#        • `PP_hold_ho` NOW BOUNDS THE BUS RATE, and it is a SCOPE correction rather than a
#          change to any arithmetic — the leg's number did not move. Three of the four P->P data
#          legs are excluded from `F_max_binding` because satisfying them would require one
#          peripheral to READ a level another drove to be read, which the specification does not
#          promise. `PP_hold_ho` was excluded with them, and its terms say it should not have
#          been: the inequality names the device turning ON out of high-Z and the device whose
#          hold window that ends, and THE OWNER OF THE LEVEL AT RISK DOES NOT APPEAR IN IT. The
#          per-leg note in `compute` had already said so in as many words — "a third device,
#          which appears in this leg only as the owner of the level at risk" — which is what
#          makes this a reading failure rather than a modelling one. When the MANAGER is the
#          releaser, that level is Manager-to-peripheral data and every peripheral sink is
#          obliged to read it, so the leg is a requirement on guaranteed traffic. It fails at
#          −7.67 ns at zero handover UIs and no Manager parameter appears in it, so the Manager
#          cannot fix it: a handover UI is still owed wherever a peripheral acquires. One
#          allocated UI answers it (+28.95).
#          Raised from the bus side — "in EDE with no handover UIs, Manager to
#          Peripheral A's hold time is determined by when Peripheral B starts driving in the next
#          UI" — and named the row: inequality 11.
#        • WHAT THE SCOPE EXCLUSION LACKED WAS A TEST ON ITS OWN CLAIM. An out-of-scope
#          declaration is an assertion about an inequality's TERMS, and nothing checked it
#          against them; `PP_hold_ho` sat excluded on the strength of its NAME for as long as it
#          existed, here and in the reference analysis both. There is now a test that states the
#          rule (out of scope iff satisfying the leg needs B to read what A drove to be read),
#          pins the resulting split, and checks the behavioural half — that a failing
#          `PP_hold_ho` really does drive `F_max_binding` to zero and name itself, which needs a
#          configuration where it is the ONLY failing bounding leg. The first version of that
#          assertion passed while `binding_inequality` reported `MP_hold`, whose own −0.74 ns
#          was pinning the rate; a lifted Manager launch isolates the leg.
#        • The test file's own copy of the exclusion list is GONE — it is imported from
#          `CalcResults._PP_DATA_LEGS` now. Two definitions of one set is how the "short legs"
#          equality assertion came to subtract a leg the model no longer excluded, and it would
#          have absorbed this change silently.
#        • THE REFERENCE ANALYSIS USED A LAUNCH PARAMETER AS A RELEASE MAXIMUM, and this
#          project's cross-check mirrored it. `Man_t_DZ` is its own tabulated parameter at
#          0–10 ns; `emit_ede.py` fed the analog launch's 9–23 ns in as `Man_t_DZ,max`, and
#          `tests/test_timing_vs_reference.py`'s matched configuration set
#          `Man_t_DZ_max_ns=E.MAN_ANALOG_MAX` to agree with it. Two copies of one error look
#          exactly like a cross-check passing. Fixed on both sides; the reference's no-mechanism
#          M->P baseline moves from −12.90 to +0.10 ns, which is the number that had made a
#          Manager EDE mechanism look necessary on that leg.
#        • The Manager-side reading both models now carry: a timed move to
#          high-Z AT the UI boundary is `Man_t_DZ,max = 0` — the bottom of the ratified range,
#          not a new parameter, and no 4× clock. `Man_t_DZ` is a pure delay from the Manager's
#          internal timing reference for its own clock edge, so the release can be gated from
#          that edge.
#        • THE EDE COLUMN NOW MODELS THAT BOUNDARY RELEASE. The Manager's launch goes back to
#          the revision's analog 9–23 ns and the 4× clocked grid (launch 0.25 UI, release
#          0.75 UI) is retired.
#        • `Man_ede` IS FALSE ON THAT COLUMN, and that is the load-bearing detail rather than
#          a tidy-up. The flag means one specific thing — `Man_t_DZ` is referenced to the
#          START of the last driven UI, Table 130's mid-UI convention — so with it set and a
#          row of 0.0 the release would land a whole UI early. The Manager is not doing
#          EndDriveEarly here: it releases on time under the ordinary reference, which is also
#          why Table 130 no longer bounds it and why every launch mode now leaves the release
#          alone. The flag stays in `CalcInputs` because a mid-UI Manager release is still
#          expressible from code, and the tests exercise it.
#        • WHAT MOVED ON SCREEN: MP contention +11.16 → +3.48 (the release is LATER, and the
#          margin that leg needs is the ACQUIRER's own lateness — a flight, a detection and
#          its own t_ZD,min — which the Manager cannot spend by holding on longer), MP
#          setup_ho +15.07 → +1.23, MP hold_ho −0.74 → −0.89, keeper_Man +1.48 → −3.21.
#        • THAT KEEPER FAILURE IS THE PROPOSAL'S ONE RESIDUAL ASK, not a cost of the
#          placement. Rearranged, the keeper is a ceiling on the LAUNCH:
#          `Man_t_DD,max ≤ 19.79 ns` with the keeper observing, 22.79 forced, against the
#          revision's 23. The EDE paper asks the working group to settle Table 125 as a FORCED
#          keeper, worth the 3 ns; this model charges it, so the column shows the pessimistic
#          reading and the results tree names the Manager as the short device.
#        • The keeper is MONOTONE IN THE RELEASE — `t_DZ − t_DD,max − t_swing` — so a later
#          release is strictly better and the boundary is the best the UI contains. The grid
#          argument had it backwards: 0.75 UI is 9.16 ns *worse* on the very leg it was chosen
#          for, and the sweep that picked it excluded the boundary by construction, so an
#          interior point was the best of what remained rather than the best available. Now
#          asserted rather than argued, at one input set with one field changed.
#        • `_EDE_UI_RELATIVE_ROWS` is EMPTY and the refresh machinery stays. No Manager value
#          is a UI fraction any more, but the 0.25/0.75 grid is still selectable and any
#          future fractional placement has to re-enter that list or it silently carries across
#          a rate change. So the LIST empties, not the code that reads it, and the test
#          asserts the invariant — a row named in the list must scale with the UI, a row
#          absent from it must not move — which is vacuous today and live the moment it is not.
#        • Five tests pinned the retired placement and each needed a different answer rather
#          than a relaxation. Two are worth naming:
#          — THE 3:1 RULE SPLITS. It asserted 1:1 on all three Manager rows, which was correct
#            while every one was a grid point. The launch is unclocked now, so it is governed
#            like a peripheral value (2.56:1 — wider than 3:1, the safe direction) while the
#            release stays deterministic, for a reason that needs no multiplied clock: the UI
#            boundary is the one instant the bus clock marks by itself.
#          — the keeper's grid test read −24.33 ns where the placement gives +1.48, because
#            `apply_row_picks` writes each ROW's own range into the inputs: a `Man_t_DD` pinned
#            in `CalcInputs` is overwritten by the ratified row's typ the moment the picker
#            runs, and filtering that pick out does not protect it — it picks typ instead.
#            Worth knowing generally: pinning a value and then running the corner search
#            silently measures a different device.
# 3.0.16: A second test platform, and the defects only a second platform — or a review — was
#        ever going to find. Little here is new behaviour. The FIRST successful build on a second
#        platform immediately exposed three faults in the gate's own tooling, and a ten-subsystem
#        code review found six more, each landing with the test that would have caught it. What
#        a user sees change: a corrected DRQ clash verdict in Bus-Visualizer authoring, a
#        command filter that no longer freezes for seconds on its first keystroke, and a config
#        CSV that can no longer divide the decoder by zero. Gated on every supported platform
#        before tagging — which is now one command, unattended.
#        • A TEN-SUBSYSTEM CODE REVIEW, and the six defects worth acting on. Each is listed
#          below with what made it survive; the review's own value was mostly in the second
#          question — not "is this wrong" but "why did nothing catch it".
#        • A DRQ WAS CLASH-CHECKED AGAINST ITS PARENT PORT'S DIRECTION, WHICH IS THE OPPOSITE
#          OF ITS OWN. `Device._drq_direction()` states the rule ("PortDirection_REG: True =
#          SINK DP -> DRQ SOURCE"): a Sink DP's flow control port DRIVES its DRQ to ask for
#          data, a Source DP samples it. The engine read PortDirection_REG anyway, so the
#          verdict inverted in both directions — two devices genuinely driving one DRQ column
#          (a physical two-driver collision) was filed as a benign read overlap and never even
#          registered as a write, while several devices legally sampling one DRQ column was
#          reported as a bus clash. Every other slot type takes its direction FROM that
#          register, so only DRQ could diverge. What let it live: none of the 96 vendored
#          configs carries a DRQ collision, so the authoritative parity test — bits +
#          bus_clashes + device_clashes + read_overlaps + warnings over the whole corpus —
#          passes identically either way. Fixing it moves no golden, which is exactly why the
#          case is now pinned by hand in `tests/test_drq_clash.py`.
#        • A MALFORMED CONFIG CSV COULD DIVIDE THE DECODER BY ZERO, and the platforms disagreed
#          about what that means. `NumColumns_REG` arrives from `parseInt` unvalidated, and
#          `Decoder::run` computes `(mColumnCount - phaseOffset % mColumnCount) % mColumnCount`
#          — so `NumColumns_REG,-1` made that a modulo by zero. ARM64's sdiv quietly yields 0
#          and the capture mis-decodes; x86_64's idiv raises SIGFPE and kills the process. This
#          machine and both test platforms are ARM64 and the CI runners are x86_64, so the crash
#          landed where nobody was looking while local runs saw only a wrong answer. A more
#          negative value skipped the crash and poisoned every row/column calculation instead.
#          Clamped to the protocol's own limits in `SwI3sConfig::columnCount()` — the accessor,
#          so the live decode and the audio engine (which passes the same value to
#          CDataPort/CFlowControlPort::Configure) are both covered by one guard, while the raw
#          register value stays readable for round-tripping. The grid paths already applied
#          this floor to their own local copy, which is why only the decode was exposed.
#        • THE RELEASE GATE'S OWN TESTS COULD NOT FAIL. Every assertion in
#          `tests/test_release_gate.py` was a substring search over `tools/gate.py` and
#          `tools/mypy_gate.py`, and nothing imported or called either. Demonstrated by
#          changing `main()` to print its failures and then `return 0`, leaving the literal
#          "return 1" as a comment for the grep: all 17 tests still passed in 0.08 s, and
#          `bash tests/gate.sh` would have printed FAIL and exited 0. Exit 0 IS the gate, so
#          this was the most consequential finding in the review and the only one about the
#          process rather than the product. The file now has a behavioural half that EXECUTES
#          both tools with their slow parts stubbed and asserts the exit code from a seeded
#          outcome: that mutation fails four of the new tests, and a regression predicate that
#          can never be true fails three more — including the one holding
#          `--allow-incomparable` to the environment case it exists for.
#        • THE THRESHOLD FRACTIONS WERE ACCEPTED, DOCUMENTED, THREADED IN, AND IGNORED.
#          `compute_delta_tpd_envelope` takes `V_OH_frac`/`V_OL_frac`, the module writes the
#          differential as `(V_IH_eff + V_IL_eff - V_OH - V_OL) / swing_TX · t_RF`, and
#          `CalcInputs.envelope()` passes them — but the linear crossing helpers divided by a
#          hardcoded `0.60`, which is just the default 0.80 - 0.20. Meanwhile `calculator.py`
#          reads the same two fields for real (`span = V_OH_min_frac - V_OL_max_frac`), so the
#          two halves of one model already disagreed about one knob: change it and the swing
#          legs move while every setup/hold crossing term does not. No shipped number was
#          wrong — the default hides it exactly and nothing sweeps the pair, neither field
#          having a CalcRow — which is the whole reason it survived. The swing is now derived
#          from the parameters and required at every crossing call site, so a future one cannot
#          silently reintroduce the constant. Computing it as the subtraction rather than the
#          literal shifts the defaults by ~2 ULP (0.80 - 0.20 is 0.6000000000000001), six
#          orders of magnitude below anything the margins resolve; all 123 other timing tests
#          are unmoved. The exponential ramp stays independent of the swing BY DESIGN — its
#          shape is set by a time constant, so there is no slope for the swing to define.
#        • LOCATE SUB-CAPTURE COULD ORPHAN ITS OWN WORKER. Both other workers refuse to start a
#          second run (`_load_async`, `_start_tx_persist_worker`); this one did not, so
#          re-invoking during the multi-second FFT search overwrote _locate_thread/_worker/
#          _dlg/_path while the first was still running. Its still-connected done/failed slots
#          then read the SECOND search's state — a result reported against the wrong file — and
#          the abandoned QThread was never joined, which is the destroyed-while-running abort
#          `join_worker_threads()` exists to prevent. Refused before the file dialog now, so
#          nobody picks a file only to be told no, and NOT queued: the other two coalesce to
#          "latest wins" because their input is derived state, while this one's input is a file
#          the user chose.
#        • THE COMMAND FILTER FROZE THE UI FOR SECONDS ON ITS FIRST KEYSTROKE. The row-text
#          cache is cold after every model reset, and QSortFilterProxyModel re-tests every
#          source row on the first invalidateFilter — so the first character typed rendered the
#          whole table on the GUI thread: 0.58 s at 10k commands, 2.9 s at 50k, 11.5 s at 200k,
#          recurring after every load, re-decode and Clear All Filters (the second keystroke
#          was 0.14 s, because by then it was warm). The cost was not the formatting but the
#          route to it: twelve QModelIndex constructions and twelve role dispatches per row.
#          The DisplayRole formatter is now one function both `data()` and `search_text()` call
#          — so the filter still matches exactly what the cell displays, asserted row by row —
#          and the same 200k sweep costs 1.19 s. Typing is debounced on top, since each
#          character otherwise queued its own whole-table sweep.
#        • THE OFFLINE CORE BUILD HAD NEVER WORKED ON LINUX, and nothing could have told us.
#          `native/build_local.sh` called itself a Unix build path while assuming macOS twice
#          over: it invoked `clang++` by name, which a Debian/Ubuntu toolchain does not provide
#          (that is `g++`), and it passed `-undefined dynamic_lookup`, a Mach-O linker flag GNU
#          ld rejects outright. It now takes `$CXX` if set, else clang++, else g++, and adds
#          the Mach-O flag only on Darwin — ELF needs no equivalent, since undefined symbols in
#          a shared object are permitted there by default. Neither the macOS gate nor CI could
#          expose this: on macOS both assumptions hold, and CI builds the core through pip.
#        • THE BUILD TOOK ITS PYTHON FACTS FROM THE WRONG INTERPRETER. That script asks a
#          Python for `EXT_SUFFIX`, the C API include path and pybind11's headers, and found it
#          by looking up `python3` on PATH — which under a venv-driven gate is the SYSTEM
#          interpreter, a different build from the one that will import the result. The visible
#          symptom is a build that fails because the system Python has no pybind11; the silent
#          one, where the two versions differ, is an extension named for an interpreter that
#          cannot load it. `tools/gate.py` now exports `$PYTHON` naming the interpreter it is
#          running, for every child process it spawns.
#        • A FAILED NATIVE BUILD WAS HIDING A SKIPPED ASSERTION. The gate returns early when
#          the build fails, which skips the score_abi assert — and the suite then runs against
#          whatever core happens to be installed, the precise stale-core hazard that assert
#          exists to catch. One red check was standing in for two problems: a broken build, and
#          a thousand tests whose subject was unverified. The aggregating design stays (one
#          failure must not hide the rest), but the skipped assert is now reported in the
#          NOT RUN HERE list rather than passing unmentioned.
#        • A SECOND TEST GUEST, on Linux, and the three findings above are its first run's
#          entire yield — the gate's own tooling, not the product. Fonts and a non-CoreText
#          rasteriser are the reason it exists (a 3.0.15-era test asserted a rendered pixel
#          equalled a palette colour, which freetype antialiases and CoreText does not), and
#          the suite is green there: 976 passed, 6 skipped, the six being the reference
#          cross-check that needs a sibling project absent from this tree.
#        • THE GATE BUILT THE FILE THAT THEN FAILED ITS OWN TIER CHECK, and only a working
#          Linux build could expose it. `test_publish_tiers` lists paths with `git ls-files`
#          and falls back to a filesystem walk wherever there is no git — which is every test
#          platform, and deliberately a published tree. `.gitignore` keeps
#          `/swi3score*.{so,pyd,dylib}` out of the git listing, but the walk had no such rule,
#          and the gate REBUILDS that file into the tree root before the suite runs. So the
#          first Linux build that worked produced a platform-tagged `swi3score` shared object
#          which the classification test then reported as an unclassified path — failing twice,
#          once in the suite and once in the per-suite pass, for one cause. The walk now skips
#          the three patterns `.gitignore` already names, matched by NAME rather than by bare
#          suffix so a binary someone genuinely commits still has to be classified. Checked
#          against that condition and not the development machine's — a non-git tree with the
#          artefact present fails before the change and passes after. The other platform never
#          saw this: it rebuilds too, and its product does not land in the walked tree.
# 3.0.15: The release that actually ran on Windows first. 3.0.13 and 3.0.14 were both tagged
#        and published on a macOS-only gate and both were red on Windows within minutes; this
#        one fixes the second of those defects, tools and documents that run so the step
#        cannot be dropped again, and was gated on a second platform (Python 3.14) BEFORE
#        being tagged. No product change beyond the dialog layout.
#        • THE SECOND-PLATFORM GATE IS NOW A CHECKLIST STEP WITH A RUNNER, because the
#          checklist kept losing it and that cost two releases. It is step 3 of the release
#          checklist, BEFORE tagging, and scripted rather than remembered: one command gates
#          the exact commit on every configured platform and returns a single verdict, nonzero
#          if any fails. A platform with nothing configured reports UNTESTED, which is not a
#          pass — the same principle as NOT RUN HERE. The runner and its setup notes live in
#          the maintainers' tier, since their subject is other machines.
#          ONE FALSE POSITIVE FIXED THE DOCUMENTED WAY. The leak scan flagged `ssh -i "$KEY"`
#          as naming a key file. A variable names nothing, and the rule is to narrow the
#          pattern rather than allowlist the string, so the pattern now ignores a `$`-prefixed
#          argument and still flags a literal path — checked both ways.
#          AND THE CASE FOR A LINUX DESKTOP PLATFORM IS RECORDED with the one thing that
#          decides whether it is worth having: ci.yml's Linux jobs install NO FONTS, so a
#          desktop Linux platform would pass rendering tests CI fails and recreate the blind
#          spot it exists to remove.
#        • THE SETTINGS DIALOG'S WIDTH WAS A CAP, NOT A FLOOR, which is the same defect as
#          3.0.14's button widths one level up — and both Windows CI jobs caught it where
#          macOS could not. `setFixedWidth(max(470, title))` pinned the window at 470, so on a
#          wider font the label column was squeezed to 219 px against the 272 "Enforce
#          Handover" needed, and clipped. It now takes `max(470, sizeHint, title)`: the
#          constant is a floor, and on this platform's metrics the answer is still exactly
#          470, so nothing moves where it was tuned.
#          THAT NEEDED `_ElidingLabel` TO STOP ASKING FOR ITS FULL TEXT. QLabel derives both
#          sizeHint AND minimumSizeHint from the string, so an unbounded summary drove the
#          dialog's own hint — reintroducing the resize-on-selection defect the fixed width
#          existed to prevent, by another route. Both accessors now report the minimum;
#          overriding only sizeHint left the summary driving it through the other one, which
#          the equal-widths assertion caught.
#        • AND THE TEST'S STRESS FONT IS NOW DERIVED FROM METRICS, which is why 3.0.14 shipped
#          with the defect still in it. A point size is not a width: 13pt stresses the Windows
#          runner and macOS passes it at 21pt, so a fix verified locally failed CI twice. The
#          test now grows the size until the widest label reaches the width that runner
#          actually produced (272 px), whatever family the platform provides, and fails rather
#          than skipping if it cannot get there.
# 3.0.14: A CI-fix release, cut immediately after 3.0.13 because that release's published
#        pull request was red on two of the six matrix jobs — and both failures were outside
#        what the hand-run gate covers, which is the case DEVELOPMENT.md's "a green CI is not
#        a green gate" was written for. One was product code (dialog widths that were pixel
#        constants tuned on one platform's font), one was a test of mine that was wrong twice
#        over about code that was right. No behaviour changes beyond the dialog layout.
#        • THE INK TEST ASSERTED A RASTERISATION DETAIL, and the Ubuntu CI job failed on a
#          tree whose rendering was correct. It required a pixel equal to VizTheme.TEXT;
#          freetype there draws this text entirely in antialiased mid-tones (#92969c, #94999f
#          between the background and the ink) and never lands on the full colour, where macOS
#          does. The property under test is that ANCHORING CHANGES NOTHING, so it now compares
#          the anchored render against the plain one — which is what catches the bug, since
#          `inherit` made one near-black while the other stayed light — and SKIPS where no
#          glyphs rasterise rather than passing vacuously.
#          WORSE, IT COULD NOT HAVE CAUGHT THE BUG AT ALL: `ink()` built its own document with
#          the correct stylesheet hardcoded, so it exercised the test's own string and not the
#          delegate. Re-injecting `color: inherit` left it green. It now builds the document
#          through `_RichTextDelegate._doc`, and the injection check that should have been run
#          when it was written now fails as it should.
#        • THE CDS DIALOG WIDTHS WERE CONSTANTS, and the Windows CI job caught it: "Guard 0"
#          needs 80 px in that runner's font where `BTN_W = 76` gave it 76, so Qt shrank the
#          button and clipped its own label. macOS passed, which is exactly the gap the release
#          checklist warns about — the hand-run gate covers neither Linux nor the CI's Python
#          range, and CI covers neither the perf gate nor a hand-run Windows VM.
#          A button group now takes the widest sizeHint IN THE GROUP (`_fit_uniform`), which
#          keeps the three properties the geometry test pins — nothing narrower than its own
#          content, all equal so the value column stays aligned, and independent of the
#          selected value, since the selected and unselected stylesheets differ only in
#          colour. And the LABEL column is sized from its own labels: widening the buttons
#          handed the grid's spare width to the button columns, so column 0 collapsed to the
#          widest DEVICE label and "All sources" — which is wider than "Device 11" — clipped
#          by 3 px at the very font the dialog was designed on. Found by fixing the first half.
#          The test now runs every CDS dialog at 13pt as well, verified by re-injecting the
#          constant. Rendered at 9pt and 13pt and inspected.
# 3.0.13: Mostly a TIMING cycle. The Timing page stops being a single-spec SWI3S calculator:
#        it models a mixed SWI3S ↔ SoundWire 1.3 bus, carries 17 numbered inequalities
#        (MP/PM/PP setup and hold in both launch forms, three handover legs, the keeper per
#        releasing device), reads them in physical time order, and lets a reader click a term
#        to trace it. Two model corrections change published margins — Table 130 now bounds
#        an EndDriveEarly release, and the receiver threshold window is no longer collapsed
#        by the corner search. The CDS gains two per-source registers and crosses to the
#        Analyzer in both directions (score_abi 10); the audio fix and the demo/transport
#        items are separate.
#        • CLICK A TERM TO TRACE IT. Clicking any symbol or substituted number on the Timing
#          page highlights every occurrence of the same parameter across all 17 inequalities,
#          in both the equation and the Specification/Example rows, and washes its INPUT ROW
#          above; clicking it again, or any blank space, clears. Several may be lit at once.
#          Requested, and the scoping question that decided the design was what "the same term"
#          means: the key is the INPUT ROW, not the printed symbol. So `Man_t_DD,pure` and
#          `Man_t_DD` are one key (`,pure` is an anchor conversion of one row's value, not a
#          second parameter), and a crossing keys on its t_RF LANE — clicking
#          `X_PM,late · Man_t_RF,CLK` lights the Manager's clock slew in fifteen legs and the
#          `t_RF Man CLK` row. That is the same rule the symbols already follow (`_trf_sym`:
#          name the lane so the term points at the row a reader would edit), and it is what
#          makes the click answer "where does this parameter act?" rather than "where else is
#          this string?". Four terms have no row and key on themselves; they are LISTED, so a
#          new row-less term fails a test instead of silently matching nothing.
#          HOW IT WORKS, because the mechanism is not the obvious one. Terms are text inside
#          one tree item, not widgets, so there was no hit target: each is now an HTML anchor,
#          Qt's `anchorAt` resolves a click to one, and the wash is applied by CSS CLASS
#          through the document's stylesheet. That means a toggle is a REPAINT — no recompute,
#          no rebuild, and the fitted column width cannot move (the wash has no padding),
#          which a test pins by asserting the item text is byte-identical across a toggle.
#          THE CLICK REGIONS DRIFTED FROM THE GLYPHS ON THE FIRST CUT, exactly as the guard
#          predicted and one level above where the guard sat. Sharing `_doc`/`_origin` was
#          not enough: the hit-test built its OWN QStyleOptionViewItem with `initFrom(view)`,
#          which copies the palette and the font METRICS but leaves `opt.font` at Qt's
#          default 9 pt, while paint gets the view's 15 px font. The two documents laid out
#          at 512.8 against 606.5 on leg 1, so the error grew with x and clicking `DATA` in
#          `Man_t_RF,DATA` selected `Per_t_IS` two terms along, as reported. The option now
#          comes from the view's own `initViewItemOption`, the same one paint receives, and
#          the invariant is tested the only way that catches it: at every sample point on
#          every row, what PAINT draws must equal what CLICK resolves. 51 rows, >5000 points,
#          no hardcoded coordinates — verified by re-injecting the defect.
#          A CLICK BETWEEN TERMS IS INERT, and that is a second thing the fix exposed. The
#          operators are not click targets, so a `−` is on the text but on no term; clearing
#          needs genuinely empty space. Clearing on any non-term click meant a 2-pixel miss
#          wiped every highlight.
#          ONE WASH, NOT A COLOUR PER TERM: an AMBER HIGHLIGHTER (#6f5410 dark, #ffe7a3
#          light), chosen off rendered swatches rather than from hexes. Amber is the one
#          emphasis colour free on this page — green already means "active" and red "fail",
#          so a traced term must read as SELECTED and not as a verdict, and blue is the
#          accent the chrome already uses. An accent-derived wash was tried first and judged
#          too subtle, which the side-by-side render made obvious. `HILITE_BG` is
#          in both palettes, which the three theme tests enforce.
#          `a { color: inherit }` COST TEN MINUTES AND HAS ITS OWN TEST. It reads as
#          obviously right and is wrong: Qt's rich-text CSS resolves `inherit` to BLACK, not
#          to the paint context's Text role, so on the dark palette every anchored symbol went
#          near-invisible while the substitution numbers stayed bright (their table cell sets
#          its own colour) — a half-dimmed page that reads as a font bug. Naming the colour
#          fixes it. Caught by rendering and looking, and the test checks INK in both palettes
#          because a document's charFormat reports a default black brush either way, so
#          querying the format cannot tell the broken case from the working one.
#        • EVERY LEG NOW READS IN PHYSICAL TIME ORDER, which was requested and is the change that
#          makes the equations followable rather than merely correct. The terms are a
#          NARRATIVE, and the order was whatever the arithmetic had been written in. Four
#          rules, each violated somewhere before:
#          — THE RECEIVER'S WINDOW IS LAST. t_IS on every setup leg, t_IH on every hold leg.
#            It is the requirement the whole leg is spent satisfying, and it used to sit two
#            terms in, ahead of the bus and the crossings that happen long before it.
#          — CLOCK LANE BEFORE DATA LANE. The clock edge is what a device detects and the data
#            crossing is judged against it. Sorted inside `_cross_terms`, so it holds for
#            every leg at once — and it had to be a rule rather than four edited constants,
#            because Delta_cross,MP SWAPS which lane carries the late crossing between setup
#            and hold, so the two families were printing their lanes in opposite orders on
#            adjacent rows of one page.
#          — A PERIPHERAL LAUNCHER DETECTS, THEN LAUNCHES. On the eight PM/PP legs the launch
#            now sits BETWEEN the two crossings: the peripheral cannot launch before it has
#            recognised the edge, and the sampler cannot judge the result before the data has
#            crossed back. `_split_cross_terms` is what allows it — the envelope's two halves
#            belong at two different instants, so it can no longer be splatted as one block.
#          — THE TWO TRAVERSALS ARE TWO TERMS, bracketing that launch, each naming its own
#            flight: the clock reaching the launcher, then the data coming back. `2·t_PD` off
#            to the side stated a total and hid that it is two events. So inequality 5) reads
#            UI − t_PD − X_clk − Per_t_DD − t_PD − X_data − Man_t_IS, which is the trace.
#            NOT applied to the contention legs' bus term, which stays NETTED for the reason
#            already recorded there: that pair is a skew and an allowance that CANCEL on two
#            of the three legs, and printing them apart invited a reader to hunt for a bus
#            effect that is not there. Here they do not cancel — both charged on setup, both
#            credited on hold.
#          The contention trio gets the same treatment in its own shape: both devices detect
#          the shared edge, then the releaser lets go and the acquirer turns on, allowance
#          last. Their two detections stay ADJACENT deliberately — on P→P they gather into
#          one exact product, and one edge read at two thresholds is nearly simultaneous
#          anyway, so little order is lost where the real instants are the release and the
#          turn-on. Same reason the gathered P→P clock term sits at the earlier of the two
#          instants it spans, A's, which is the one the launch follows.
#          NO NUMBER MOVES — a margin is a sum — which is exactly why this needed its own
#          test: nothing else would notice a reordering, the whole benefit is to a reader,
#          and the whole cost of losing it is silent. Rendered and inspected at all 17.
#        • THE INEQUALITIES ARE NUMBERED 1) … 17), in display order, on the equation rows and
#          in `summary_text()`. For reference: naming a leg in conversation took
#          "Peripheral A Holding for Peripheral B, the t_ZD variant". ONE map (`_INEQ_NUMBER`,
#          derived from the display grouping) feeds both renderers — two independent
#          numberings would drift the moment either order changed, and a stale number is
#          worse than no number, so a test also pins that the display order and INEQUALITIES
#          still agree. ")" AND NOT ".", also requested: the equations are full of the "·"
#          operator and decimals like "1.44·8.30", so a leading "1." reads as arithmetic.
#        • THE SETUP/HOLD HEADINGS NAME THE OBLIGATION, not a direction arrow. Every one of
#          these legs pairs ONE device's output timing against ANOTHER's receiver window, and
#          "Manager-to-Peripheral Hold" stated the data direction while leaving the reader to
#          infer which device owed what — the part that gets got wrong. Now: "Manager Setup
#          for Peripheral" / "Manager Holding for Peripheral", "Peripheral Setup for Manager"
#          / "Peripheral Holding for Manager", "Peripheral A Setup for Peripheral B" /
#          "Peripheral A Holding for Peripheral B" — the requested wording, including the
#          ruling that setup reads "Setup" rather than "Launching": the launch is a step on the
#          way and the requirement is the goal. The P→P pair carries the same letters its terms
#          already use. Rendered and measured — the widest is 277 px against a 695 px column,
#          so nothing elides.
#        • A DRIVES AND B SAMPLES ON EVERY P→P LEG, and that is now a test rather than two
#          note strings. Nothing in the arithmetic prevents a fifth P→P leg from lettering the
#          sampler A: both letters read the same `Per` rows, so the margin would be identical
#          and the equation self-consistent while telling the reader the wrong device to
#          change — the `_LEGAL_LANES` failure mode exactly, which is why this one also needs
#          a human's declared statement of intent. Verified by injecting the swap.
#          AND "A DRIVES" DOES NOT SETTLE WHOSE LEVEL IS AT RISK, which the notes now say per
#          leg. On a hold leg A's launch is the AGGRESSOR: on PP_hold (continuous) it ends A's
#          own previous bit, but on PP_hold_ho A has just ACQUIRED the bus and the level it
#          destroys belongs to the peripheral that just RELEASED — a third device, present in
#          the leg only as the owner of the level at risk. That also separates the leg from
#          PP_contention beside it: same two drivers, different failures. Contention asks
#          whether they are ever driving AT ONCE (a current, damaging drivers); this asks
#          whether A's turn-on ends the releaser's LEVEL before B finished sampling it (a lost
#          bit). Either can fail with the other passing.
#          One crossing note was muddled and is corrected while here: A's detection enters the
#          hold leg as a credit BECAUSE A's launch is referenced to it, so detecting later
#          holds the level longer and the EARLY end is the worst corner — the smallest credit.
#          The old note read as though launching sooner were the helpful direction.
#        • A RECEIVER THRESHOLD IS A PER-DEVICE CORNER, so the compliance window must stay
#          OPEN. `CalcRow` gained `is_window`, and V_IH / V_IL now keep both ends in force
#          instead of being collapsed to one picked value. The old behaviour was stated
#          plainly in `default_swi3s_rows` — "picking a value collapses the band to that
#          value" — and is sound for a parameter ONE device realises (a t_DD corner is that
#          device at that corner). A threshold is not: each part's own switching point sits
#          somewhere inside [V_IH,min, V_IH,max], so two receivers can be at opposite ends
#          at the same time, and a single bus-wide value cannot express that. On a leg that
#          CHARGES one receiver's crossing and CREDITS another's, collapsing let the two
#          partially cancel — the acquirer's credit rose with the releaser's charge, 0.4938
#          → 0.7804, worth 2.38 ns it is not owed.
#          EXACTLY THREE LEGS MOVE, all of them the ones that net two peripherals:
#          PP_contention +1.30 → −1.08, PP_hold and PP_hold_ho −5.29 → −7.67. The other
#          fourteen are unchanged to two decimals, and the reason is the physics rather than
#          luck: within ONE receiver what matters is the V_IH-to-V_IL separation, which a
#          collapse preserves, while ACROSS two receivers it is the window's WIDTH, which a
#          collapse destroys. The direction was one-way too — collapsing could only ever
#          read at or above the spec window's own answer — so the worst-corner search was
#          picking its worst out of a subspace that excluded the worst corner.
#          A window row now gets NO PICK, which is not a gap: the envelope already corners
#          each ROLE at the end that role needs, so the intact window IS the independent
#          per-device cornering. `_read_rows` had to carry the flag too, or the UI path
#          would have re-collapsed it after the model stopped doing so.
#          AND IT LANDS THE APP ON THE PAPER'S SIDE of that leg: PP_contention is now −1.08
#          against the reference's −1.077 with a single slew, so the only remaining
#          divergence there is the declared t_RF split (`_PP_SLEW_SPLIT`, 2.617 ns).
#          READ THAT SIGN CORRECTLY, and this is a bus-behaviour note rather than a derivation: on
#          PHY2, P→P IS NOT EXPECTED TO CLOSE WITHOUT A UI OF HANDOVER. PHY2 allocates one
#          to every handover by default (§11.1.1.1) and that UI is how it separates two
#          peripherals' drivers, so a negative P→P margin at N_HO = 0 is the specified
#          behaviour, not a finding — the leg prices REMOVING the UI, and that price is a
#          key motivation for EDE. Allocation is per-transition, so a P→P boundary that
#          does not close costs one UI at that transition while the mixed ones stay free.
#          Recorded at the leg itself, since the number invites the other reading.
#          Reported from the bus side, and it corrected the initial diagnosis twice. That
#          diagnosis first called the collapse a consequence of having "one row", which is
#          wrong — a row is a BAND and each part draws from it — and then proposed that a pick
#          should "shift the window", which is incoherent, since the window is the spec limit
#          and there is nothing outside it to shift into. The fix is simply not to perturb it.
#        • TABLE 130 NOW BOUNDS THE EDE RELEASE, and it is the only thing that ever did.
#          Nothing in the model forbade a release on the UI boundary, and neither of the
#          Manager's own legs objects to one:
#          — M→P CONTENTION CANNOT BIND at any placement inside the UI. The acquiring
#            peripheral owes a flight, a detection and its own t_ZD,min before it may
#            drive — about 10 ns at 12.288 MHz over 30 cm — so the gap is the ACQUIRER's
#            lateness and the Manager cannot spend it by releasing later. Raised as a question
#            from the bus side, and it holds: at the boundary the leg reads +10.10 and every
#            nanosecond of that is the peripheral being unable to start at the edge. The leg
#            goes negative only WITHOUT EDE, where the release moves 23 ns PAST the closing
#            edge, 0.63 UI into the acquirer's own UI (−12.90).
#          — THE KEEPER GETS BETTER toward the boundary. Release and launch trade 1:1, so
#            each 0.25 UI step later is 9.155 ns off contention and onto the keeper:
#            (0.50, 0.75, 1.00 UI) → MP contention +28.41/+19.25/+10.10 against keeper
#            −7.68/+1.48/+10.63.
#          So 1.00 UI read as a comfortable pass on both while being outside
#          0.60·UI + 10 ns (0.873 UI here) and not an early release at all. Worse, on a
#          1/2 UI grid that is where nearest-point snapping PUT it — so the tool reported
#          a 2× EDE column with every leg green, contradicting this file, `_ede_ranges`
#          and the paper, all three of which say 2× cannot express EDE. `snap_to_launch_grid`
#          gained `ceil_ns` (the mirror of the `floor_ns` that stopped Man_t_DD,min
#          rounding to zero) and `ede_release_ceiling_ns` owns the table; the release is
#          now the LATEST LEGAL placeable point, annotated when it moves. At 2× that is
#          0.50 UI — the launch's own point — so the keeper fails at −16.83 and the
#          impossibility reaches the reader as a number instead of a comment. The 4×
#          column does not move: 0.75 UI was already inside the cap.
#          WHERE THE CEILING AND THE FLOOR CONFLICT THE CEILING WINS, and that case is
#          not pathological — on a 1/2 UI grid nothing sits between the 0.75 UI seed and
#          the 0.873 UI cap, which IS the statement that 2× cannot do it. The cap also
#          applies with no grid at all (an analog EDE release is still bounded by the
#          table) and NOT to a conventional release, which Table 130 does not govern.
#          The cap is rate-dependent the unhelpful way: the 10 ns turn-off allowance is
#          absolute, so above ~18 MHz it sits past the boundary and stops binding, and
#          the definition of "early" is all that is left there.
#        • ONE Man_t_DZ, read by BOTH families. The keeper leg took `inp.Man_t_DZ_max_ns`
#          RAW while the contention legs took it snapped to the launch grid, so under a
#          clocked mode one output stage had two release times and each family kept the
#          end that suited it — the same hybrid `_man_tzd_ns` already documents for t_ZD,
#          and the same one `_man_tdz_max_ns`'s own docstring complained about. Now
#          `_man_tdz_parts` is the single definition and returns the note with the value,
#          so the keeper leg also SAYS when the release was moved. Found while adding the
#          ceiling, not by a test, which is why one now exists.
#        • THE KEEPER'S SEPARATION RULE WAS MISSTATED IN FIVE PLACES, all of them the
#          reason for the 4× clock rather than the arithmetic that produces it. The
#          requirement is `t_DZ − t_DD ≥ swing + t_keeper` = 13.83 + 3.00 = 16.83 ns
#          (0.46 UI). Three said `t_keeper` alone — which ONE 0.25 UI step of 9.155 ns
#          satisfies, so the stated rule admitted the very (0.25, 0.50) pairing the leg
#          rejects at −7.68 — and two gave the swing without the keeper's response, which
#          has been charged unconditionally since Table 125 was read as a demand on the
#          LANE. One test docstring went further and named `t_DD = 0.50 UI with
#          t_DZ = 0.75 UI` as the pairing that satisfies everything: the placement
#          retired when the addend became the full swing, and the case the test two above
#          it proves fails. Every one of those docstrings was contradicted by inline
#          comments in the same file, and in one case in the same function.
#        • A SUBSET ASSERTION HID A LEG LEAVING IT. `test_ede_needs_a_quarter_UI_grid`
#          allowed the short-leg set to be a SUBSET of {MP_hold, MP_hold_ho,
#          PP_contention} with a comment explaining that P→P contention is short because
#          its two peripherals realise the t_RF tolerance independently. Both halves went
#          stale in the same change one bullet up: the band is gone and the leg closes at
#          +1.30. An upper bound cannot see a leg LEAVE it, so nothing failed and the
#          explanation stayed. Asserted by EQUALITY now, with P→P contention's sign
#          checked separately and pointed at the declared reference divergence.
#        • ONE CLOCK PIN HAS ONE SLEW, so the legs that read it twice now GATHER. Five legs
#          carry `Man_t_RF,CLK` twice — the three contention legs and the four P→P data legs —
#          and two of them cornered the pair INDEPENDENTLY, at opposite ends of a
#          `tRF_CLK_tol_ns` band, on the argument that two unrelated peripherals share the slew
#          SETTING but not the tolerance around it. That is wrong about the physics, not the
#          modelling: t_RF on the clock lane is the MANAGER's output slew, driven from one pin,
#          and at any instant that edge has one slew. Both peripherals detect the SAME edge.
#          What genuinely differs between them is where their thresholds sit on it — already
#          the V_IH/V_IL spread inside the envelope — and their internal delays, which are
#          their own parameters. Worse, on P→P setup one crossing read a BAND END while the
#          other read the row's picked value: two slews for one edge, in one equation.
#          So the band is gone, `tRF_CLK_tol_ns` with it, and a leg reading one lane twice
#          shows ONE PRODUCT OF A PARENTHESISED PAIR — `(X_PP,A,late − X_PP,B,early) ·
#          Man_t_RF,CLK`, substituted as `(1.44 − 0.45)·5.00` so all THREE numbers are on the
#          row and the netting can be checked against the symbol beside it. Two spellings were
#          tried and dropped first: joining the heads bare is an expression wearing a symbol's
#          place and breaks the comma grammar the naming gate parses, and collapsing to one
#          role (`X_PP,net`) is a valid symbol that says only that SOME netting happened —
#          which pair netted went into a note nobody has to read. The bracket is also the
#          grouping the arithmetic needs, since the operator is hoisted out.
#          Collapsing is EXACT here and nowhere else, which is the
#          condition `_cross_terms` had already written down: it refuses to collapse an
#          envelope "because that is equal only when the lanes happen to share a t_RF" —
#          precisely what `_gather_lane_terms` now tests for. Two terms on different rows
#          (Man CLK vs Per DATA) are still shown apart, so the per-lane form that made a
#          wrong-lane bug visible is untouched. Applied in `InequalityBreakdown.__post_init__`
#          so a leg added later cannot opt out of it.
#          IT MOVES A CONCLUSION — AND THEN THE THRESHOLD RULING MOVES IT BACK. This gains
#          P→P contention 2.6 ns, which closed it; the `CalcRow.is_window` fix below then
#          costs it 2.38 and it fails again at −1.08. The two are the same question asked of
#          two different quantities, and the answers differ for a reason: the clock is one
#          Manager pin driving one edge, so both peripherals detect one slew, while the
#          receiver threshold is a per-part property and two receivers sit wherever they sit
#          inside the window. The net of the pair is +0.24 ns on that leg. Ordering
#          MP > PM > PP is what has survived all four rewrites, so that is what is pinned
#          and each sign is stated as its own movable fact.
#          THE REFERENCE IS SPLIT ON IT, which is why the cross-check gained a leg rather than
#          losing one. `emit_ede.hold_pp` uses a single slew — so P→P hold now agrees with it
#          up to the anchor alone, and that divergence is retired. But `nc_pp` DOES split, so
#          P→P contention gets its own test and a declared 2.617 ns gap, with the reasoning on
#          the record: the reference is internally inconsistent about it, and the geometry its
#          own docstring describes (releaser far, acquirer near) would justify a slew
#          difference by EDGE DEGRADATION ALONG THE BUS — a real effect, but not the
#          part-to-part tolerance it cites, and neither model carries a distance-dependent
#          slew term. The sign is asserted too: declining the split is the GENEROUS direction
#          and must never pass unremarked.
#          THE DISPLAYED DECOMPOSITION NEEDED ITS OWN TEST. Hoisting the operator out of a
#          gathered pair without dividing the contributions by it prints "1.44 late + 0.45
#          early" under a minus, which reads as −1.89 where the leg is −0.99 — the same
#          "minus a negative" trap `_cross_terms` avoids by keeping an operator per leg, met
#          one level up. The term's VALUE is right either way, so no margin assertion catches
#          it; the new test parses the note, applies its own signs, and requires the result to
#          equal the coefficient claimed.
#        • THE CDS CELL IS TWO LINES, AND ITS COLUMN GROWS TO FIT. `CDS_SPx` needed 58 px in
#          a 44 px cell and overflowed, and end-drive-early had no grid representation at all
#          — a per-device setting with real decode consequences, invisible. The cell now reads
#          `CDS` over a flags line: `SP`/`SPx` for a Special drive type and `EDE`/`EDEx` for
#          end-drive-early, the trailing `x` meaning the sources disagree, which is the `Gx`
#          convention the guard labels already use. A merged run's ` x{n}` goes on the FIRST
#          line beside the name; appending it to the flags would read as a third flag.
#          The flags line is dropped entirely when there is nothing unusual to report, which
#          is every one of the 89 example configs — so their grids are untouched.
#          ONLY THE CDS COLUMN WIDENS. The grid is a uniform _CW per column and now has one
#          exception, sized from the RENDERED text rather than a guessed constant, because a
#          view whose job is fitting 32 columns cannot spend the width everywhere. `_col_extra`
#          is empty whenever there are no flags, so those paths render exactly as before; a
#          wide-bit CDS spanning three columns already has the room and grows by nothing.
#          `_cx`/`_colw`/`_span_w` make the drawing sites width-aware, including the bit
#          dividers inside a merged cell — those stepped by _CW and would have drawn away from
#          the cell's own bit edges once a column stopped being _CW wide.
#          Two-line labels are drawn LINE BY LINE: a QGraphicsSimpleTextItem holding a newline
#          renders the block but left-aligns each line inside it, so "CDS" sat flush against
#          its wider "SPx EDEx" instead of centred over it.
#        • TWO PER-DEVICE CDS FIELDS, AND THE CDS SETTINGS COLLAPSE INTO ONE ROW. The
#          Visualizer now carries CDS_DriveType (CDS 0x86 bit 7) and CDS_EndDriveEarly
#          (0x87 bit 4), both PER SOURCE — each register sits in a device's own CDS block, so
#          each is a 13-entry list on the same index convention as the guard and the tail
#          (0 = Manager, i = Device i-1), NOT one bus-wide bit.
#          Drive type: 1 Normal, both levels actively driven; 0 Special, where a CDS bit of 1
#          is left high-Z and the Manager's bus keeper holds the level, which NRZS decode
#          sees. The grid marks those cells `CDS_SP`, and `CDS_SPx` when sources disagree —
#          the CDS is one cell driven by whoever has it, so the `x`-means-they-differ
#          convention the guard labels already use (`Gx`) applies. Normal stays plain `CDS`:
#          annotating the ordinary case would suffix all 89 example configs and say nothing.
#          End drive early: "Full" / "Early", NAMING NO DURATION. The first version said
#          "1/2 UI early" from the r06 extract; r08 removed that idea outright — its revision
#          history records "remove the idea of the time being explicitly a 'half UI'", the
#          granularity is BITS not UIs (a Wide Bit / guard / tail group gets one early
#          release), and the point is ImpDef inside a PHY bound. The spec's verbatim encoding
#          is on the buttons' tooltips instead.
#          THE TWO DEFAULT OPPOSITELY RELATIVE TO THEIR RESETS, on purpose. CDS_DriveType
#          defaults to 1 AGAINST its reset of 0, because a file with no such row must keep
#          reading as it always did — seeding Special would have relabelled every existing
#          config. CDS_EndDriveEarly defaults to 0 WITH its reset, full-UI drive being the
#          ordinary behaviour. Each looks like a bug beside the other, so one test asserts
#          both together.
#          THE INTERFACE COLUMN HAD FOUR CDS ROWS IN THREE CONTROL SHAPES and these would
#          have made six — a third of the column on one register block. All of it now sits
#          behind one "CDS Settings" row opening `CdsSettingsDialog`: two bus-wide scalars
#          (bit width, enforce handover) then three per-source rows, each a "Per source…"
#          button with a summary. Nothing commits until OK, including a change made two
#          dialogs deep — the per-source dialogs edit copies handed back to the outer one, so
#          cancelling discards them.
#          THE THREE PER-SOURCE DIALOGS ARE ONE CLASS NOW (`_CdsPerSourceDialog`), which is
#          what made the UI defects fixable once instead of three times. See the UX entry
#          below; adding drive type as a third hand-written copy is what forced the issue.
#          BOTH CSV ENGINES CARRY BOTH FIELDS, as `CDS_DriveTypePerSource` /
#          `CDS_EndDriveEarlyPerSource` 13-wide rows. A field in one engine and not the other
#          round-trips through a save and vanishes on load, so the plain 13-wide rows are now
#          driven off ONE TABLE per engine (`_CDS_PER_SOURCE_INT_ROWS`, `_CDS_INT_PER_SOURCE`)
#          and a sixth per-source field is one line each rather than four scattered edits.
#          That refactor exposed a latent bug: a truncated row was read through a plain int
#          parse, giving 0. Right for the tail; on drive type 0 means SPECIAL, so a row cut
#          short by a hand edit would have silently relabelled the whole bus as passively
#          driven. Each field now carries its own default for the gap case.
#          A file predating either field loads clean on both engines (an interface field falls
#          back with a warning; only DP/FCP fields are fatal), so no golden moved.
#          AND THE CDS NOW CROSSES TO THE REGISTERS, BOTH WAYS (score_abi 10). The gap was
#          total rather than partial: the C++ CSV loader recognised NO `CDS_*` field and
#          `registersFromConfig` emitted only the DP block plus NumColumns/SkippingDenominator,
#          so Import Visualizer CSV produced no CDS register state at all — not the two new
#          fields, and not the pre-existing bit width / guard / tail either. Both directions
#          are wired: an authored config emits 0x1186 (drive type) and 0x1187 (bit width,
#          end-drive-early, guard enable+polarity, tail) per device, and `BuildConfig` recovers
#          them from snooped writes so a decoded capture exports an authoring CSV with its CDS
#          intact. `SwI3sConfig` gained the per-source lists and its loader the 13-wide rows,
#          which also stops a THIRD CSV reader from disagreeing with the other two.
#          THE MANAGER'S SLOT CANNOT CROSS and is asserted to stay at its default. Only
#          peripherals have an addressable CDS block, so nothing on the wire says how the
#          Manager drives the CDS; inferring it from a peripheral would invent a fact the
#          capture does not contain.
#          A device that wrote nothing keeps the CONFIG default (Normal) rather than the
#          register RESET (0 = Special) — they differ on purpose, and reporting only what was
#          actually programmed avoids relabelling every CDS-indifferent capture as passive.
#          ABI 10 EVEN THOUGH NO RETURN SHAPE MOVED, on ABI 8's precedent: an ABI-9 module
#          silently omits the CDS registers, so Compare would report the whole block as
#          missing from the expected config — a difference a user would chase as a real
#          defect rather than a stale binary. The `configToDict` keys alone would not have
#          justified a bump (the Python side reads them with `in`).
#        • THE CDS DIALOGS WERE FIXED BY LOOKING AT THEM, which had not been done. The
#          previous revision asserted widget text and tooltips offscreen — enough to verify
#          wiring, blind to layout — and shipped a dialog nobody had seen. Rendering each one
#          to an image found seventeen issues:
#          CLIPPED AND JUMPY. `addRow("", w)` parked the note and the checkbox in the narrow
#          value column, so "Enforce CDS Handover" was cut to "Enforce CDS Handc…" and the
#          note wrapped to three lines with half the dialog empty beside it. The window also
#          re-widened when the note changed length, so picking a value made it jump. Now one
#          two-column grid, labels right-aligned, notes spanning both, width FIXED.
#          ONE OF THE SEVENTEEN WAS NOT A DEFECT, and the fix for it was worse than the
#          thing it fixed. A selected "Off" rendering in the same green as a selected setting
#          was called out as "green reads as on, so thirteen green Off buttons look entirely
#          enabled", and given its own blue-grey. But green marks the CHOSEN OPTION IN A
#          GROUP throughout these dialogs — GuardSelectorDialog has always shown a green
#          "Off" for the identical Off/G0/G1 choice, PortModeSelectorDialog a green
#          "Normal (Off)", DeviceSelectorDialog a green device number with no on/off sense
#          at all. So the second colour did not add a distinction, it broke a convention,
#          and it broke it INCONSISTENTLY: the per-source CDS guard went blue-grey while the
#          per-DP guard one click away stayed green. Reverted to one selected colour. The
#          label carries the value and the colour carries the selection; where a bus-wide
#          "nothing is set" reading is wanted it belongs in the summary TEXT, which the CDS
#          Settings rows already show ("off" / "all Normal" / "All Full"). The test now pins
#          the CONVENTION across the per-source dialogs AND the older per-DP one that set it,
#          because the failure was two dialogs disagreeing rather than one being wrong.
#          THE SCROLL AREA CAUSED THREE DEFECTS AT ONCE — its bar drew over the right-hand
#          button column, a horizontal bar appeared over the bottom row scrolling content that
#          already fitted, and its flat 320 px height sliced the last visible row in half. It
#          is gone: 13 rows flat is a 553 px dialog, and one grid cannot misalign with itself.
#          FIFTY-TWO CLICKS FOR A UNIFORM CONFIG, with no way to say "all of them". An "All
#          sources" strip now sets every row at once.
#          Also: the platform's square bevel on the unstyled buttons (the panel stylesheet
#          covers QLineEdit and QCheckBox but not QPushButton, so "Open…" drew a light frame
#          on three sides and a dark one on the fourth); hardcoded greys that would have
#          stayed dark under the Light theme; a QSpinBox that drew no arrows and read as a
#          text field, replaced by the toggle strip every other small range already uses; and
#          ragged per-source buttons whose summaries did not line up.
#          THE SUMMARIES NOW ELIDE with the full string in the tooltip, because their length
#          is unbounded — "Manager tail 3; 12 devices tail 3" needs 186 px in 184, found by
#          the new test rather than by eye. Letting it widen the window would restore the
#          jumpiness; letting Qt squeeze it truncates with no ellipsis, which is how the
#          clipped checkbox shipped.
#          TWO TESTS THAT MEASURE WHAT THE EYE SEES, since the old ones could not. Content vs
#          WIDGET width, not widget vs dialog: at a fixed width Qt shrinks a control and clips
#          the text inside it rather than letting it overhang, so an overflow check passes the
#          clipping — confirmed by re-injecting it. And the checkbox is measured at its
#          INDICATOR, not its rectangle: the panel sets `subcontrol-position: center` for its
#          own 64 px bool rows, this dialog inherits it, and centring makes the drawn square's
#          position depend on the widget's width — so it drifted off the column edge while
#          every widget-level assertion still passed.
#        • MANAGER WINS WHICHEVER ROW COMES FIRST, a real defect in the CSV read. A config
#          encodes a Manager data port as DeviceNumber_REG = 0 plus ManagerDataport = True,
#          because the register's legal values are 0-11 and "in the Manager" is not one of
#          them — the flag decides whether the number applies at all rather than competing
#          with it. `BusConfig.from_csv_text` applied the flag the instant it read it, on the
#          stated assumption that it is "written after DeviceNumber_REG": true of files it
#          writes, false of a hand-edited or third-party one, where the later DeviceNumber row
#          silently turned a Manager port into a peripheral. swviz's loader was always
#          order-independent, so THE SAME FILE decoded two ways depending on the engine — the
#          divergence CLAUDE.md warns about. The flag is now resolved after the loop, and a
#          test pins both orders in both engines plus the flag-false case.
#        • REGISTER MAP RE-EXTRACTED TO r08 (was r06). The field data moved in six places and
#          the chapter moved wholesale. Data: `DPn_CommitPointDelay[3:0]` added at DP 0x0F
#          (previously all-Reserved, {ASW5702}/{ASW5801}); CDS_EndDriveEarly,
#          DPn_EndDriveEarly and DPn_FCP_EndDriveEarly are read-only 0 in PHY1 and their
#          encoding no longer names a half UI; IntCascade_SDCA / _ImpDef / _DP<n> OR the
#          IntActive_* terms (IntStat AND IntEn) rather than IntStat_* alone, {ASW6001};
#          NumColumns gained an encoding example. No register moved offset, no block base
#          changed, no reset value changed, nothing was removed.
#          BUT EVERY CITATION DID: the register content moved from Chapter 15 to Chapter 14
#          (Interrupts became Chapter 15), sections 15.2.1-15.2.5 became 14.2.1-14.2.11, and
#          tables 163-171 became 165-182 — without a single register moving. So the extract
#          now records that TABLE AND SECTION NUMBERS ARE NOT STABLE KEYS and is keyed on
#          name + block + offset + bit range, which were stable. One field added (224 -> 225),
#          none lost. IntCascade_DFI<n> is the same family as the three corrected fields but
#          was not named in the revision history, so it is left as extracted and said so.
#        • A FOURTH P→P PLACEMENT, BOTH PERIPHERALS FAR, which was missing rather than
#          excluded. Two ends and two devices is four layouts; the selector enumerated the
#          three that had come up in the derivation, so the absent one read as a case not
#          modelled when the answer is that it costs nothing — t_PD + 0 − t_PD, both devices
#          take the clock equally late and the data has no distance left to cover between
#          them. Three of the four are now free and only driver-far/sampler-near pays its
#          2·t_PD, unchanged. The brute-force placement test no longer lists the free ones by
#          hand: it derives them from `PP_PLACEMENT_LABELS` and fails if the set moves, so a
#          fifth cannot be added without a bracket being decided for it. `pp_net_pd` also
#          falls through to the bounding worst-per-leg answer on an unrecognised key instead
#          of to zero — a key added and forgotten is now conservative rather than free.
#          The picker read "unknown (worst per leg)", burying what it does in a parenthetical
#          after a word that sounds like missing information; it is now "Worst Per Leg",
#          stated directly. The PERSISTED token is still `unknown`, so saved sessions load
#          unchanged. It was also the one combo box on that page with no minimum width, so it
#          sized to the current item and elided the longer placements once one was chosen.
#        • EVERY t_RF LANE IS QUALIFIED, and the style rules became tests. `Man_t_RF` now
#          reads `Man_t_RF,CLK` / `Man_t_RF,DATA` on every term, crossing and swing alike. The
#          qualifier used to be dropped wherever the role was thought to imply it, which was
#          true only for a reader who already knew how the envelope was built — and was never
#          uniform anyway, since X_MP had to qualify both of its Manager-driven lanes.
#          ONE CLOCK, ONE LANE NAME. The Manager is the only source of the forwarded clock, so
#          every device's detection of it is a crossing on `Man_t_RF,CLK` and there is nothing
#          for a second spelling to distinguish. A peripheral end does read that row at an END
#          of its min/max rather than at the corner the search picked — two unrelated parts
#          share the slew SETTING but not the tolerance — and that is a CORNER of the lane,
#          not a different lane, so the NOTE says it and the symbol stays single.
#          Briefly spelled `t_RF,CLK,band`, which invented a second name for the one clock and
#          implied the row was not read at all. It is: `tRF_CLK_tol_ns` is seeded straight off
#          the `t_RF Man CLK` row, so narrowing that row narrows the band with it. The note
#          also briefly INFERRED which end was in force by comparing the value against the
#          row's ends — wrong whenever the picker had simply chosen the row's max, which it
#          does on most worst-corner legs, so `PM_contention` was labelled "the SLOW end" when
#          it was reading the picked corner. Independence is now passed by the caller, which
#          knows, rather than deduced from the number.
#          THE ROLE IS MANDATORY AND LAST, which `X_PP,A` was not. Both P→P legs named the
#          device whose clock crossing they charge and then stopped, leaving the reader to
#          infer from the sign whether it was A's late crossing or its early one — the exact
#          thing the role suffix exists to say, and the two are 11.96 vs 4.10 ns apart. Now
#          `X_PP,A,late` (setup) and `X_PP,A,early` (hold), matching `X_PP,B,*` beside them.
#          The gate read the qualifiers as an UNORDERED BAG, and `A` is a legal token, so a
#          symbol carrying only the device letter satisfied every rule while saying nothing.
#          Checked positionally now: the last token must be late/early, and only a device
#          letter may sit between the pair and the role. Re-injecting the old spelling fails
#          it, which is the check the bag version could never have made.
#          A LETTER MEANS A ROW READ TWICE, not "belongs to a different chip". `Per_t_DD` and
#          `Per_t_IS` on a P→P leg are A's and B's respectively and the two ARE independent
#          parts — each realises its own value, exactly as each realises its own slew. But the
#          independence only COSTS something where one row is read twice with opposite signs,
#          because then a single cornered value serves as both the charge and the credit; that
#          is the clock lane, worth 2.6 ns on P→P hold. Every device parameter appears at most
#          once per leg, so the ordinary corner search already drives it to the end that hurts,
#          and `Per_A_t_DD` would send a reader looking for a `Per_B_t_DD` row the input grid
#          does not have. The device goes in the NOTE instead ("A, the driver: its launch").
#          A first draft of that reasoning claimed the two devices SHARE the parameter, which
#          is false; the condition is read-twice, not shared, and
#          `test_no_pp_leg_reads_one_per_row_for_both_devices` fails the day a leg does it —
#          so the argument expires by itself rather than being taken on trust later.
#          THE RULES ARE NOW FOUR TESTS plus a checklist in `calculator.py` naming which test
#          enforces what: symbols (naming scheme + every symbol subscripts), LANES (a declared
#          per-leg table — `_LEGAL_LANES`), arithmetic (terms sum to the margin), corners
#          (affine, or the one-row-at-a-time search stops being exact), and the reference
#          cross-check. The lane table is the one that matters: a wrong-lane bug is CONSISTENT
#          — symbol, factors and behaviour all agree on the wrong row — so no invariant over
#          the symbol can catch it, and re-injecting the original P→P bug proved the naming
#          gate passes it. Only a statement of what each leg SHOULD read fails, which makes
#          adding a leg a review step rather than a copy-paste.
#        • ONE VOCABULARY FOR THE DISPLAYED SYMBOLS, and a P→P lane bug found by writing it
#          down. A crossing now reads `X_<pair>,<role> · <lane>_t_RF`, with the role always
#          `late`/`early` — setup is eroded by a late crossing and hold by an early one, which
#          is what Δ_cross,MP already said. `X_PM,setup,clk · Man_t_RF` became `X_PM,late ·
#          Man_t_RF`: it named the clock twice and still left the reader to know that Σ's clock
#          leg is the Manager's. The lane is named by the t_RF the term multiplies, so it is
#          never repeated in the role — except on X_MP, which keeps `,CLK`/`,DATA` because BOTH
#          its legs are Manager-driven and which one carries the late crossing SWAPS between
#          setup and hold. The contention legs, which name the same two quantities, now spell
#          them the same way.
#          EVERY SYMBOL SUBSCRIBES NOW. `X_PP` had no token in the subscript regex, so the P→P
#          legs rendered `X_PP,B,late` upright with a bare underscore; `Per_t_hold,min` failed
#          on its `hold` token. Both added. `Man_tKeeper_Response` was the last, and it was
#          misspelled rather than unlucky: the spec writes `Man_tDD` where this file writes
#          `Man_t_DD`, so the symbol is `Man_t_Keeper_Response` and the marker rule holds
#          without an exception. A test asserts the whole scheme, including that no rendered
#          symbol keeps a stray underscore.
#          THE BUG: the P→P legs reused the MP crossing tuples verbatim, whose data lane is
#          `Man DATA` — so they charged the MANAGER's data slew on a link the Manager does not
#          drive. The data reaching B comes from peripheral A, so the lane is `Per DATA`.
#          Invisible while every lane sat at 5 ns; with the rows split 5.0 / 8.3 the legs
#          tracked the wrong row entirely. Found by asking what `X_MP,late · Man_t_RF,DATA` was
#          doing inside a leg with no Manager in it, which is the argument for having one
#          naming scheme in the first place.
#        • THE MEASUREMENT ANCHOR IS SETTLED, AND IT COST THE KEEPER LEGS 1.667 ns. Figure 174
#          measures Per_t_DD / Per_t_ZD from the clock's V_IH,rising / V_IL,falling crossing to
#          20 % OF THE RAMP, so a tabulated value contains the first fifth of the transition
#          and the pure back-out is real. Table 129's wording ("to the start point on the
#          ramp", "earliest change in data output") reads as a 0 % anchor and is inaccurate;
#          the drawing is what the parameter means. t_DZ is EXEMPT and used raw everywhere —
#          it marks the instant the driver goes high-Z, which is abrupt.
#          The keeper legs were the casualty. They subtracted the RAW t_DD and then charged
#          the FULL rail-to-rail swing, paying for the ramp's first fifth twice —
#          `offset·t_RF,nom` = 1.667 ns too much on keeper_Per (12.76 → 14.42 at 13.2 MHz
#          nominal). keeper_Man does not move: Fig. 176's anchors are symmetric, so its pure
#          value IS its tabulated one, which is why the error could only show on Per.
#          NO TEST CAUGHT IT — the conventional keeper_Per margin was never pinned to a
#          number, only asserted positive, which it was either way. The new test pins the
#          IDENTITY instead: pure launch + full swing must equal raw launch + the 20 %-to-rail
#          remainder, two spellings of one interval, and t_DZ must stay raw.
#          This overturns `timing-analysis/docs/MODEL_AUDIT.md` §2, which read the same
#          contradiction the other way and removed `emit_ede.py`'s offset layer instead. The
#          contradiction was real; the resolution is that the KEEPER leg was wrong, not the
#          conversion. Recorded in `docs/anchors.md` with the argument that lost, so it is not
#          re-litigated a third time; reverting the paper's side is the author's call and is
#          named there as open.
#        • THE PURE-DELAY BACK-OUT WAS SCALING WITH THE OPERATING SLEW, and it should never
#          have. A tabulated clock-to-output time was converted to the pure delay the
#          inequalities need by subtracting `offset · t_RF` at the t_RF IN FORCE ON THE BUS.
#          But the tabulated number was MEASURED at the spec's reference test condition
#          (Fig. 174), so the V=0→V_OL portion embedded in it is the one realised at THAT
#          slew — offset · 5.0 ns = 1.667 ns for SWI3S. It is a fixed offset:
#          Per_t_DD,max,pure is 18.333 ns and Per_t_DD,min,pure 0.333 ns at every slew.
#          `pure_output_delay` no longer takes an operating t_RF; it reads each SIDE's own
#          reference condition, which is what a mixed bus needs.
#          THE ERROR MOVED MARGINS IN BOTH DIRECTIONS, which is why no reviewer's sense of
#          "this looks pessimistic" would have caught it. At the 8.3 ns slow corner it
#          reported Per_t_DD,max,pure as 17.233 — 1.10 ns of delay the device never saves,
#          credited on exactly the corner where the leg is thinnest — and on the min corner
#          it flowed the other way. Spec column, worst corners: PM_setup −8.48 → −9.58,
#          PM_setup_ho −6.48 → −7.58, PM_hold +3.84 → +3.18, PP_setup −10.54 → −9.87,
#          PP_hold −11.12 → −10.02. The MP legs and the keeper legs do not move: a SWI3S
#          Manager's t_DD is measured between symmetric anchors and was never converted.
#          Every direct test of the conversion kept its expected value unchanged, because
#          each already passed the spec's own reference slew — the arithmetic it asserted
#          was right; the value the model fed it was not.
#        • P→P DATA LEGS, four of them, reversing a deliberate omission. A peripheral
#          receiving while another peripheral drives had no setup/hold inequality, on the
#          grounds that P→P communication is not guaranteed — the standalone tool prices the
#          same pair as `setup_pp`/`hold_pp` and labels them HYPOTHETICAL for that reason.
#          They are legs now: "Peripheral-to-Peripheral Setup" and "… Hold", each carrying
#          its t_ZD launch variant, which is the launch-method half of the 2x2 this file
#          treats as structural (carrying three quarters of it is what hid MP_hold_ho).
#          What they report is the PRICE of promising P→P, and on PHY2 it is not paid.
#          — THREE CROSSINGS ENTER, and the construction is ported rather than invented:
#            clk@A (A's clock detection, where its launch is referenced), data@B, and clk@B
#            (B's detection, which IS its sample edge). clk@B and data@B are the same die at
#            nearly the same instant, so they are CORRELATED and their difference is exactly
#            the Δ_cross,MP construction — which is why the legs reuse DELTA_SETUP/HOLD
#            verbatim. clk@A is an unrelated part and is charged in full at the band end that
#            hurts, since two peripherals may share the slew SETTING but not the tolerance.
#            A test asserts that identity, not the paper's magnitudes: this model's envelope
#            inputs (V_IH/V_IL, supply, noise) are its own.
#          — THE FLIGHT TERM IS A PLACEMENT, and only two of the three placements differ.
#            The leg pays `t_clk(driver) + t_data(driver→sampler) − t_clk(sampler)`: ZERO
#            with both peripherals near, ZERO with driver near and sampler far (data that
#            chases the clock down the same line arrives exactly as the clock does — the
#            reason MP setup pays no t_PD), and 2·t_PD with the driver far and the sampler
#            near, where the data runs back against the clock. A cost for setup and a credit
#            for hold, so `unknown` (the default) gives each leg its own end.
#          — A SELECTOR, NOT A CORNER ROW, and the first attempt got that wrong. As a row its
#            value multiplies the bus-length row, and `find_worst_corner_rows` moves one row
#            at a time — exact only for a margin AFFINE in each parameter. The search duly
#            missed the worst corner: on PP_setup it left the bus at 15 cm where 30 cm is
#            2 ns worse, because with the placement at its typical of zero the leg looked
#            bus-insensitive. Same failure mode the keeper split documents. A brute-force
#            test over the geometric rows is the guard.
#          — THE HEADLINE RATE STILL EXCLUDES THEM. A failing P→P hold would otherwise pin
#            F_max_binding to 0.0 for every configuration and name itself the binding
#            constraint, reporting "no clock rate works" for a bus whose guaranteed
#            directions are fine. Their own margins and F_max are shown; what is excluded is
#            the claim that they bound the BUS. Six tests that asserted "the proposal closes
#            every inequality" now say every GUARANTEED one, which is what it claimed — the
#            proposal scoped P→P out explicitly.
#        • MAN_TKEEPER_RESPONSE IS NOW CHARGED ON EVERY KEEPER LEG. Table 125 requires the
#          data lane to be STABLE AT THE MANAGER PIN for 3 ns — a demand on the wire, not a
#          property of the keeper — and a `keeper_forced` flag was zeroing it on the
#          argument that a Manager which tells the keeper its value spares it from watching
#          the bus. The flag defaulted to forced, so the term contributed nothing to any
#          published margin; PHY2 has no forced keeper in the first place. The flag is gone
#          (its only reader was that one line), and the term is displayed under the spec's
#          own name with the requirement as its note instead of a bare `t_keeper`.
#          Two results follow, both recorded in tests rather than smoothed over:
#          — THE PROPOSED REVISION'S WORST CORNER NO LONGER CLOSES ON SLEW ALONE. At
#            12.288 MHz with the earliest legal release (t_DZ → 0, legal because t_DZ is a
#            maximum), the 7 ns edge that used to leave +2.79 now leaves −1.05. Any ONE of
#            these closes it: an edge of 6.4 ns or better, Man_t_DD,max of 21.95 instead of
#            23 (the leg is affine: margin = 21.95 − t_DD), or a STATED Man_t_DZ minimum of
#            1.05 ns, which the spec does not have. 23 ns of launch + a slow-corner
#            transition + the keeper's 3 ns does not fit a 36.62 ns UI.
#          — UNDER EDE THE PERIPHERAL-RELEASING LEG IS EXACTLY ZERO, because PHY2 sets the
#            EDE hold minimum and Man_tKeeper_Response to the same 3 ns. "Satisfied by
#            construction" is now literal and has nothing spare: a pass by the model's
#            criterion, knife-edge in practice, since anything eating the settled window at
#            the far pin (ringing, settling, a hold interval AT its minimum) takes it
#            negative. The tests assert the equality rather than a `>= 0` so that either
#            number moving is a failure and not a silent gain of slack.
#        • NO HANDOVER UI, NO UI TERM. With none allocated, the five legs that carry the
#          allocation (MP/PM hold on a t_ZD launch, all three contention legs) printed
#          `0·UI`, kept on the argument that a term which becomes `1·UI` the moment one IS
#          allocated says something a missing term cannot. It does — but not as a row of
#          arithmetic contributing nothing, least of all on the legs whose whole point is
#          that NOTHING separates the release from the acquirer's turn-on. Display only: the
#          dropped term was worth 0.0 ns and each margin is the sum of its terms, so nothing
#          moves. A FRACTIONAL allocation still shows (§11.1.1.1 lets a handover be
#          programmed longer, and the field is a float), and the zero that still shows is
#          `Man_t_IH` — a parameter reading 0.00 because that is PHY2's value, which is a
#          different kind of zero from an allocation nobody made.
#        • THE SWING TERM'S t_RF IS EXPLICIT IN THE SUBSTITUTION ROW: `1.67·8.30` where it
#          printed the pre-multiplied 13.83. The rule for expanding a product was "where the
#          coefficient is not already visible in the symbol", which reads the wrong half —
#          `1.67·Man_t_RF` names its multiplier, and that is precisely why the slew in force
#          was nowhere on the row and could only be recovered by dividing. The rule is now
#          "where an OPERAND's value would otherwise appear nowhere", so crossing and swing
#          terms both expand. Carries the same two-decimal rounding the crossing terms
#          already do (1.67·8.30 reads 13.86 against the 13.83 in force, since the
#          coefficient is 1/(V_OH,min−V_OL,max) shown to two places); the note holds the
#          exact arithmetic. One `_swing_coeff` now feeds both the symbol and the factors,
#          so the printed multiplier cannot drift from the one the product was built with.
#        • BUS KEEPER, ONE HEADING PER RELEASING DEVICE — "Bus Keeper — Manager Releasing"
#          and "Bus Keeper — Peripheral Releasing", where the pair shared one title. Still
#          ONE keeper, and it is still in the Manager ({ASW3805}): the heading names the
#          device LETTING GO. Two headings because they are two inequalities with their own
#          corners and their own binding device, and one title over two margins made a
#          reader work out whose corner they were looking at before knowing what to change.
#        • THE DEMO CAPTURE IS NO LONGER DECODED AT STARTUP. It was preloaded in
#          MainWindow.__init__ so the first switch to Bus Analyzer was instant, and that
#          one decode was 2.4 GB of the 2.6 GB the app sat at on launch: a 1 s synthetic
#          capture at 500 MS/s is ~25M UIs, whose retained cost is 238 MB of uint64 edge
#          arrays, a 449 MB C++ AudioSample vector (6.24M samples × 72 B) and the 349 MB
#          NumPy copy of that same audio, on top of ~750 MB of synthesis temporaries that
#          macOS never returns to the OS. Every session paid it — including the DEFAULT
#          Bus Visualizer ones that never open the Analyzer, and every File ▸ Open that
#          threw it away seconds later. It now decodes on first entry to Bus Analyzer,
#          through the same worker + progress dialog as a real capture. Launch drops
#          2,616 → 198 MB; the demo costs the first switch and nothing after it.
#          `load_session` clears the pending flag and an in-flight load is left alone, so
#          the deferred demo can never land on top of a capture the user opened (that
#          matters because opening from the Visualizer switches modes, which is the same
#          hook). Side effect: the test suite halves, 274 s → 138 s, because 61 MainWindow
#          constructions stopped decoding a capture each.
#        • THE DECODE'S AUDIO VECTOR IS RELEASED ONCE THE SESSION HAS COPIED IT. The core
#          keeps every decoded sample in a C++ vector (72 B/sample) and Session copies the
#          whole thing into NumPy columns (56 B/sample) at load — 449 MB beside 349 MB on
#          the demo — for a second reader that never arrives: the audio store, playback,
#          the plots, PDM decode and WAV export all read the columns, `Session.audio`
#          builds its dict list from them, and a re-decode constructs a NEW Decoder rather
#          than re-reading the old one. New `Decoder::releaseAudio()` (bound as
#          release_audio / audio_released) drops it with shrink_to_fit; _refresh_audio calls
#          it after every decode, via getattr so an older core just keeps holding it (NOT an
#          ABI bump — purely additive, no return shape moved).
#          WHAT IT IS WORTH, measured rather than assumed: nothing on a single load (macOS
#          keeps freed pages resident — even a 450 MB NumPy free returns 0 MB to the OS, and
#          building the store + dict list after a release peaked identically, 4,997 MB
#          either way). It pays on the path the app actually walks: decoding capture B while
#          capture A is still held — which is what _load_async does, since the window keeps
#          the old Session until load_session swaps it — where the peak drops 2,847 → 2,398
#          MB, the vector's size exactly. Reading decoder.audio()/audio_columns() after the
#          release RAISES rather than returning an empty list: silently-empty audio is
#          indistinguishable from a capture that carried none. Six test sites that read a
#          SESSION's decoder audio directly now read `Session.audio`, which is the same
#          dicts from the copy the app itself uses.
#        • DISPLAY POLISH, three places where the app said something other than what the
#          spec says. (a) The Timing page's equation and substitution rows multiplied with
#          `×` while the same file's notes and this change log already used `·` — one
#          notation now, the middle dot, on the term symbols (`2·t_PD`, `1·UI`,
#          `X_MP,late · Man_t_RF,DATA`), their `coefficient · t_RF` notes, the numeric
#          substitution and the `·V_DD` unit. The `2×`/`4× CLK` launch-mode names keep
#          their `×`: there it is "double-rate", a multiplier's NAME, not an operator.
#          (b) Statistics ▸ Regions leads with Bus configs instead of ending with it — it
#          says how many regions exist and what geometries they use, which is the frame the
#          per-region rates are read against; last, it read as a footnote to the final
#          region. (c) Peripheral Reports called an ImpDef bit "vendor": the count row is
#          now named for what fired (ImpDef, SDCA, or both — they share a bucket because
#          the base spec defines neither, but they are different terms), and the trailing
#          "(vendor-defined)" after each field name is gone. The FIELD NAME already carries
#          ImpDef/SDCA, so the suffix restated the name in a word the specification does
#          not use — dropped in the report rows and in the command-row fault label.
#        • PAYLOAD INTERVAL SKIPPING (§14.1.10) decoded the wrong intervals. A skipping
#          port transports (D − N) of every D intervals, chosen by a fractional accumulator
#          that every SSP re-initializes. CDataPort::SyncToSSP cleared that accumulator but
#          never started the interval BEGINNING at the SSP, so that interval never spent its
#          `A += Numerator` step and every later decision landed one interval late: the
#          decode read the interval the device had skipped (idle bus) and skipped the next
#          one, discarding the sample really transported there. Found on a 44.1-over-48
#          loopback capture (N=13, D=160), where 2,177 of 24,918 samples came back as the
#          idle pattern; the same capture now decodes 22,331 consecutive samples of its
#          ramp with every step exact. Initialize() had always been right — SyncToSSP now
#          agrees with it, which makes the two paths' skip phase the same by construction.
#          A port with Numerator 0 is untouched, so nothing else in the suite moves.
#        • A skipping port's transport pattern repeats every Interval × SkippingDenominator
#          Rows, not every Interval, and those are the only rows an SSP may land on (§4.2.3.1).
#          Both places that computed the period from the Interval alone — the manual-SSP row
#          reduction and the demo generator's SSPA cadence — now use SwI3sConfig::patternRows,
#          which mirrors the visualizer's Interface.interval_lcm.
#        • NEW ERROR: an SSP that arrives while a skipping port is part-way through its
#          pattern is an unexpected SSP (§9.1.6.2.1) and is now reported on the command that
#          generated it, rather than silently re-phasing the audio the analyzer goes on to
#          show. `make_skipping_levels` is the fixture, carrying a RAMP instead of a sine so
#          a mis-phased skip reads as a wrong VALUE rather than as plausible audio.
#        • NEW STATISTICS SECTION, "Peripheral Reports": what each device said about
#          ITSELF. The analyzer decodes the wire and forms its own opinion; a peripheral
#          also reports through its IntStat_*/DevStat_* latched bits and its saturating
#          EC_* error counters, and those replies were already on the wire and already
#          resolved to field names by data/registers.json — nothing read their VALUES. So
#          a 44.1 kHz loopback capture in which DP1 raised IntStat_PortImpDef_1 decoded
#          clean and said so. Faults and vendor-defined interrupts are top-level rows (a
#          raised bit has to prompt investigation without anyone expanding anything) and
#          feed the Commands error count; live state folds away per port. Classification is
#          by field NAME, so the System & Link Control block's link-error interrupts
#          (BadCRC, Bad8b10b, LostLock, MissedRowSync …) come along without a second table.
#          A reply that failed CRC is ignored rather than allowed to invent a fault.
#        • REGISTER ARRAYS were being dropped on the floor. The spec's tables collapse a
#          run of consecutive addresses into one row, and the loader skipped any row whose
#          offset was a range — so DPn_EC_TestFailCh (Table 166, 0x20-0x2F: sixteen
#          per-channel test-fail counters), SLC_IntStat_SDCA (0x40-0x47, 64 bits),
#          SLC_IntStat_ImpDef (0x4C-0x4F, 32 bits), their IntEn peers and
#          SLC_IntCascade_DP (0x28-0x2B) resolved to NOTHING. They now expand to one
#          register per address with correctly numbered fields, ascending with the
#          lowest-numbered member at the lowest address and at bit 0 (Table 166/169's own
#          layout). This capture's manager clears IntStat_ImpDef00 and IntStat_ImpDef08 —
#          previously invisible, now decoded in the register view.
#          Table 169 prints IntCascade_DP11 at 0x2A bit 1, a duplicate of the label at 0x29
#          bit 3; every other bit of both bytes ascends unbroken, so we generate DP17.
#          Reserved/ImpDef filler runs have nothing to index, so they stay the ONE row the
#          table shows (a new RegisterSpec.span) while every address inside still resolves —
#          a write into Reserved space is identifiable instead of unknown. Such a row shows
#          its address RANGE and no value, because the byte at its first address is not the
#          range's value and pretending otherwise would hide a stray write further in.
#        • The Timing page models a MIXED bus. Each side — Manager and Peripheral — is set
#          independently to SWI3S PHY1, SWI3S PHY2, or SoundWire 1.3 at either supply, and
#          contributes the timing its OWN spec promises. The inequalities are untouched:
#          both specs describe the same physical event graph. What differs is what their
#          tabulated numbers MEAN, so a new normalisation layer (timing/spec_source.py) puts
#          each side's clock-to-output on the common pure-delay basis the inequalities assume
#          — clock event to the START of the physical slew, since the slew itself is already
#          inside Δ_cross/Σ_cross and counting it twice inflates the setup requirement on
#          whichever leg the SoundWire device transmits. Selecting a SoundWire side also
#          surfaces its f_Clock ceiling (no SoundWire envelope reaches PHY2's mandatory
#          13.2 MHz) and the cost of hysteresis SoundWire does not guarantee on its Data
#          receiver. The all-SWI3S path is bit-identical to before and pinned by a test.
#        • THREE corrections to that mapping, each found by a question rather than by a test,
#          and all three the same class of error: an anchor read off the wrong point of a
#          waveform. Worth listing individually because the third reversed the second.
#          — SoundWire t_OV ends at V_OH (80 %) where SWI3S t_DD ends at V_OL (20 %), exactly
#            one t_RF apart. Feeding t_OV in raw double-counted 7.20 ns of output slew.
#          — The hold-side bound is t_ZD_Data,Min, NOT t_OH_Data. t_OH_Data is a Permission,
#            measured from data-valid, and bounds drive-before-handover rather than validity
#            at the sampling instant; SoundWire's declared hold mechanism is the bus keeper.
#          — t_OV and t_ZD START at the clock's V_TP threshold, not at its V=0. Re-anchoring
#            the clock side too is worth +5.85 ns in the OPPOSITE direction, so the net
#            conversion is −0.25·t_RF (−1.35 ns), not the −7.20 ns the first fix implied.
#          The reference plane was the open question behind the third: §5.2.1 settles it —
#          timing is specified at the external Clock node, which for a Manager is its own
#          clock output pin, so no propagation delay enters and the two specs are directly
#          comparable. Consequence worth stating plainly, because it inverted a finding twice
#          before it settled: MP hold tracks WHICH SPEC THE MANAGER IS BUILT TO, not how much
#          of the link is SoundWire — a SoundWire Manager's re-anchored bound beats SWI3S's.
#        • The Manager data launch is an ARCHITECTURE choice, not a tighter number, and is now
#          selectable. An analog delay line gives Man_t_DD as a range, and the inequalities
#          absorb its min-to-max spread on BOTH MP legs at once — the max erodes setup while
#          only the min funds hold — so tightening the range cannot improve both. A
#          clock-launched edge (2× or 4×) lands on a known clock phase, so min == max and the
#          spread disappears; Man_t_ZD follows it, a turn-on out of high-Z being launched from
#          the same clock. The two are bounded from opposite ends (4× by hold, 2× by setup),
#          so which is viable is rate-dependent and the hint under the selector says so.
#        • The two columns are now CURRENT spec versus a PROPOSED revision, not spec versus a
#          blank slate. The read-only Specification column shows the spec as it stands and the
#          editable Example column seeds from the proposal, so the page opens on the
#          comparison rather than requiring one to be typed. The proposal is deliberately NOT
#          a selectable spec source: it is unratified, and a picker entry would let it be
#          quoted as specification.
#        • The PHY1/PHY2 picker is GONE, folded into the per-side spec selector. It was a
#          global switch while the spec source was per side, so the two controls answered
#          overlapping questions and a PHY1 Manager talking to a PHY2 Peripheral could not be
#          expressed at all. It can now. Establishing which parameters that mixing is legal
#          for corrected a claim this changelog would otherwise have inherited: Table 129
#          (OUTPUT) prints EVERY parameter as a separate PHY1 row and PHY2 row and never
#          collapses them — three differ (Per_tDD, Per_tZD, Man_tZD) and three merely AGREE
#          (Man_tDD, Man_tDZ, Per_tDZ). Table 128 (INPUT) is the genuinely shared one, a
#          single row per parameter. So a revision to Per_tIH revises it for both PHYs, while
#          one to an output row does not — the opposite of the split first implemented. The
#          PHY1 Example column therefore carries PHY1's OWN revised output maxima, Per_tDD and
#          Per_tZD 50 -> 44 ns, rather than PHY2's proposed 9: both are revisions, but 44 is a
#          value stated for this PHY where 9 would be one invented by pasting. That closes
#          PM setup on an all-PHY1 bus, −4.79 -> +1.21 ns, the 6 ns being exactly 50 − 44.
#        • Handover NON-CONTENTION is modelled, on all THREE legs — MP_contention,
#          PM_contention and PP_contention (peripheral to peripheral):
#          N_HO·UI + t_ZD,min(acquiring) − t_DZ,max(releasing) + clock skew ≥ 0. A different
#          question from the existing handover SETUP variants, which ask whether the acquiring
#          driver gets valid data to the receiver in time; this asks whether the releasing
#          driver let go first. Positive is an undriven float gap the Manager's bus keeper
#          holds; negative is two drivers on the bus at once, which nothing permits for an
#          audio-mode data handover. The handover-UI count is a PHY property with a
#          programmable default (0 for PHY1, 1 for PHY2), so it is an input, not a constant:
#          PHY1 needs no UI because its own numbers satisfy the inequality, and the model
#          reproduces that +4.00 ns independently on a zero-length bus and reports it as
#          rate-independent. PHY2 forced to zero UIs shows −8.00 ns and no legal clock rate,
#          which is the case the scheduled UI exists to buy.
#        • The CLOCK-SKEW term in those inequalities was missing from the first version, and
#          the peripheral-to-peripheral leg is what surfaced it. Two peripherals have no
#          privileged reference plane between them, which forces the question of what each
#          t_DZ/t_ZD is measured against — and the answer applies to all three legs. Each
#          device's parameters are referenced to ITS OWN clock, and the clock propagates from
#          the Manager, so a peripheral sees it t_PD late; contention being a conflict between
#          two drivers in absolute time, the margin carries t_clk(acquirer) − t_clk(releaser).
#          That is +t_PD for MP (merely pessimistic to omit) but −t_PD for PM and PP, so the
#          first version reported a PM margin no bus could deliver: +4.00 where a 30 cm bus
#          gives +2.00. PP assumes both peripherals may sit anywhere on a bus of the
#          configured length, so its skew is the full −t_PD — a bounding case, not a
#          measurement of a specific topology.
#        • A 'Handover UI' CHECKBOX selects whether the N_HO·UI term applies, in
#          preparation for EndDriveEarly. Seeded from the selected PHYs (PHY2 schedules a
#          UI, PHY1 hands over intra-UI) and overridable both ways: allocating one on PHY1
#          is legal (§11.1.1.1 permits longer handovers), and CLEARING it on PHY2 is the
#          EDE case. Cleared on PHY2 the handover legs FAIL at the current numbers — t_ZD,min
#          2 ns cannot cover t_DZ,max 10 ns without a UI to separate the reference edges —
#          and that is the true answer, not a defect: EDE reaches a zero-UI handover by
#          releasing ~half a UI early, i.e. by changing t_DZ, so it needs timing values of
#          its own before the legs can close. A hint under the checkbox says so, so a red
#          margin reads as the open question it is. `CalcInputs.handover_UIs` stays a float,
#          so a longer programmed allocation is still expressible from code.
#        • EndDriveEarly is now MODELLED, as a selectable spec source (SWI3S PHY2 EDE) —
#          and it needed no new inequality. EDE moves the releasing device's t_DZ reference
#          from the END of its last driven UI to the START, one UI earlier, so in an
#          end-of-UI-referenced inequality it is simply a NEGATIVE effective t_DZ. Held as
#          per-side `Man_ede`/`Per_ede` flags, because the two ends reach it differently: a
#          Manager can clock a mid-UI release, a peripheral has no mid-UI reference and can
#          only aim at the UI boundary the clock already marks.
#        • Selecting it SHIFTS THE COMPARISON UP a step. The two columns become PHY2
#          Proposed (baseline) versus EDE Proposed (candidate) — the current spec is settled
#          enough to stop being what EDE is measured against; what matters is what EDE adds
#          on top of the revision. Column headings follow, in the grids and in the results
#          tree, because "Specification" over proposed-revision values would be false.
#        • The values are the COMBINED position of the EDE white paper, which beats either
#          contributing analysis alone: a Manager clocked at mid-UI on a 1/4 UI grid (forced
#          onto the 4x launch by the bus keeper's 3 ns), a peripheral released by the UI
#          boundary (the IFX proposal's mechanism, which needs no mid-UI reference), and
#          Per_t_ZD,min at the 3:1 statement's 3 ns. All THREE handover legs then close at
#          zero allocated UIs — M->P +12.3, P->M +10.2, P->P +1.0 — where the proposed
#          revision alone fails all three (-8.0, -3.0, -10.0). MP setup also improves
#          +1.2 -> +15.0. MP hold is untouched at -0.7: EDE does not address it.
#        • Four EDE rows are UI FRACTIONS, so they follow F_CLK rather than sitting on a
#          stale absolute value — "0.50 UI" carried across a rate change is not
#          stale-but-usable, it is wrong at both rates. Narrowly scoped to those four rows,
#          so an edit to an absolute row survives a rate change, which is the reseed trap
#          `_on_launch_changed` exists to avoid.
#        • A BUS KEEPER inequality, added because its absence was a false PASS. Selecting
#          EDE with the analog Manager launch showed all three handover legs green while the
#          keeper was starved by 7.7 ns — the releasing driver let go before the level had
#          been valid long enough to be held, so the bit is lost and nothing said so.
#          Contention cannot catch it: it reads t_DZ and t_ZD, never t_DD, so it is
#          indifferent to how the Manager launched. The keeper is the only leg coupling them,
#          which is *why* EDE and the launch architecture are not independent choices.
#          release_earliest − t_DD,max ≥ 3 ns (Table 125's Man_tKeeper_Response), with t_DD
#          read AFTER the launch mode so the selector actually bites: analog −4.7, 4x
#          clocked +9.1. Expressed in UI-start-referenced terms so a non-EDE bus gets a
#          genuine (one-UI-larger) margin rather than a vacuous special case. t_DZ enters at
#          its MIN corner here and its max in the contention legs — the same field pulled
#          both ways, resolved per inequality by the auto-worst solver. Caveat in the code:
#          t_DZ and t_ZD are NOT symmetric, and the asymmetry is what makes the keeper leg
#          EXACT rather than a bound: there is no turn-off ramp, so a driver drives at
#          strength right up to t_DZ and then stops, while t_ZD is where the impedance ramp
#          BEGINS. So t_DZ is precisely when driving ends and t_ZD precisely when it starts —
#          which is also why the contention legs pair the right two events with nothing left
#          over. SWI3S impedance control is sawtooth — Figure 166: ramped on, instant off.
#          A useful consequence: a NEGATIVE contention margin is not merely a fail, its
#          magnitude IS the overlap duration, because contention begins at the acquirer's
#          t_ZD and ends at the releaser's t_DZ. (An earlier version of this note claimed the
#          keeper leg was optimistic by a turn-off ramp. There is no turn-off ramp.)
#        • The LAUNCH MODE is a live trade on an EDE bus, not a formality, and the hint now
#          says which way. It applies to BOTH columns — an earlier version of that hint
#          claimed it drove the baseline only, which was simply false since launch_mode is
#          shared. At 12.288 MHz: 4× leaves the MANAGER's keeper leg +9.16 but MP hold −0.74;
#          2× closes MP
#          hold at +8.42 and puts the keeper at EXACTLY 0, because t_DZ,min − t_DD,max
#          reduces to turn_min, which equals the keeper's 3 ns identically. So 2× is
#          marginal, not excluded — the white paper had said excluded, having measured the
#          keeper to the release REFERENCE rather than to the actual release. Figure 166 is
#          instant-off at t_DZ, so the driver holds until t_DZ and the true ceiling is
#          turn_min higher. MP hold at 4× is bound by Δ_cross, so tightening Per_t_IH cannot
#          reach it; 2× funds it by brute force with 9.16 ns more t_DD,min. (That +9.16 is
#          the MANAGER's leg only, and the hint used to quote it as the keeper margin 4×
#          buys. It is not — see the keeper split below.)
#        • THE EDE RECOMMENDATION MOVED FROM 2× TO 4×, and the route there is worth
#          recording because three successive positions were each undone by the next, all
#          by the same underlying mistake: treating the Manager's outputs as independently
#          placeable parameters rather than as one clocking decision.
#          — First position: 2× at a 0.00 UI t_ZD, argued as "the only 1/2 UI point that
#            closes both legs", with the corollary that 2× was "only coherent BECAUSE the
#            peripheral releases by the UI boundary". Both retired.
#          — What killed it: t_DD and t_ZD are the two DATA LAUNCH METHODS — from an
#            already-driving output, or out of high-Z — and both answer the same setup and
#            hold requirements, because the receiver cannot tell which produced the edge.
#            So they take the same value, which is why the revision states them identically
#            (9–23 ns or 0.5 UI 2× / 0.25 UI 4×) and why Man_t_ZD,min moved 2 → 9 ns for
#            exactly the reason Man_t_DD,min is 9: hold at the peripheral. There is no grid
#            to trade, and 0.00 UI is not on offer for t_ZD at all.
#          — Then the same error on t_DZ: it was carrying a 3–9 ns analog turn-off on top of
#            its grid point while t_DD and t_ZD were deterministic. One output stage clocks
#            all three or none. With t_DZ deterministic too, 2× CANNOT EXPRESS EDE: its one
#            interior point cannot be both the launch and the release, 0.00 UI fails hold by
#            ~10 ns, and 1.00 UI is the boundary, so the release is not early.
#          — Settled position: a 1/4 UI grid, launch (t_DD = t_ZD) at 0.50 UI = 18.31 ns,
#            release at 0.75 UI = 27.47 ns, every value deterministic. That pair is UNIQUE —
#            hold rules out the two earliest points, the keeper rules out a release on the
#            launch's own point, and Table 130's 0.60 UI + 10 ns rules out the boundary.
#            Every leg closes at the auto-worst corner; P→P at exactly 0.00 and the
#            peripheral's keeper at +0.21 are the two to watch.
#          An analog delay on top of a grid launch remains available and is the fallback if
#          no fully clocked configuration exists. One does, so it is unused — and if it were
#          needed, 2× + analog would be preferred to 4× + analog, since the analog term is
#          what costs margin and the cheaper clock is then the better buy.
#        • THE LAUNCH SELECTOR SETS THE CLOCK'S RESOLUTION, NOT A PLACEMENT. It used to pin
#          Man_t_DD to one point per multiplier — 0.50 UI at 2×, 0.25 UI at 4× — so "4×"
#          could only ever mean 0.25 UI. A 4× clock in fact offers 0.25, 0.50 and 0.75 UI,
#          and the settled EDE design wants a 4× RESOLUTION with the launch at 0.50 UI. The
#          old pinning reported that design as failing MP hold by 0.74 ns when the design
#          does not place t_DD there. Now the mode says which points exist and the rows say
#          which are used: all three Manager outputs are editable placements, snapped to the
#          grid, none read-only. Labels follow ("4× CLK (1/4 UI grid)"). Snapping is stateless
#          — reseed-then-snap — because snapping the displayed value accumulates damage: a
#          0.75 UI release visited at 2× rounds to 1.00 UI, and returning to 4× finds it
#          already on-grid and keeps it, so a detour silently destroys the placement.
#        • THE 3:1 TEST HAD AN UNFALSIFIABLE CARVE-OUT, and that is why the 21.31–27.31 ns
#          t_DZ passed it. The test subtracted an ASSUMED grid point and checked only the
#          remainder: 27.31/21.31 is 1.28:1 as a whole value, but (27.31−18.31)/(21.31−18.31)
#          is 3.0. The test chose which part of the value to excuse, so any range could be
#          made to pass by nominating a suitable grid point. Caught by inspection, not by a test.
#          Replaced with a rule that cannot be gamed: a clocked value is deterministic (1:1),
#          an unclocked one carries 3:1 over the WHOLE of itself, nothing in between and no
#          subtraction.
#        • The 3:1 rule applies to the WHOLE of a peripheral value but only to the ANALOG
#          PORTION of a Manager one, and conflating the two seeded Per_t_DZ at 1.33:1
#          (27.6–36.6 ns) — asking for better than PVT delivers, i.e. an unbuildable part
#          rather than a tight one. A Manager t_DZ may be narrower in total legitimately,
#          because its 0.50 UI grid point is clocked and carries no PVT while its turn-off
#          is 3–9 = 3:1; a peripheral has no grid, so its t_DZ is analog end to end. IFX
#          Proposal 1 fixes the MAX at the UI boundary, so PVT sets the min at max/3:
#          Per_t_DZ is 12.21–36.62 = exactly 3:1. Only the keeper leg reads t_DZ,min, and the
#          Manager binds it at 0.00 at the design point while the peripheral's leg sits at
#          +0.21 — which looked like "no reported margin moves", and was not: see the keeper
#          split below, where that +0.21 turns out to be the proposal's binding rate ceiling.
#          A test now asserts the discipline, and asserts it as a
#          FLOOR: wider than 3:1 is merely conservative, tighter is unbuildable. Per_t_DD
#          sits at 4.5:1 (the revision's own 2–9), which is the safe direction.
#        • A t_ZD anchoring change was made and then REVERTED, recorded because the revert
#          is the correct state and a reader should not re-derive the wrong one. It removed
#          Per_t_ZD's V=0→V_OL back-out, reading "t_ZD ends where the impedance ramp begins"
#          as meaning it sits before the voltage slew. It does not: the 20% is 20% OF THAT
#          RAMP, the same endpoint Per_t_DD is measured to, so both carry the same slew
#          portion and a 9 ns Per_t_DD,max and 9 ns Per_t_ZD,max MUST give the same pure
#          value. Removing the back-out made them differ by t_RF/3, which is how it was caught.
#          Reverted; PM_setup_ho is back to +6.185 and MP hold is again the only short
#          leg at the all-extremes corner. Per and Man differ here by anchor symmetry
#          (Figures 174 vs 176), which is consistency per side, not a disagreement — that had
#          been misdiagnosed too.
#        • THREE "the value on screen is not the value in force" defects, two of them reported
#          from inspection and the third found by chasing the first. All the same class as the
#          false PASSes above: a check existed and could not see the failure mode.
#          — CLOCKED OR ANALOG, NOT A MIX. The launch mode reached Man_t_DD only, so at 2× a
#            PHY2 Manager was modelled with t_DD collapsed to the 18.31 ns clock grid point
#            while its t_ZD kept 9–23 ns and its t_DZ kept 0–10 — one output stage described
#            as two different devices. It also took the FAVOURABLE HALF OF EACH: the clocked
#            data launch, which helps setup, together with the analog 10 ns release, which
#            helps MP contention. All three now follow the mode, and on the default seeding the
#            correction COSTS MP contention 8.3 ns at 2× (+28.62 →
#            +20.31) while the keeper gains — a clocked release at 18.31 ns is LATER than a
#            10 ns analog one, so the Manager holds the bus longer. That they are all clocked
#            or all analog is not optional.
#          — The mode fixes the RESOLUTION, not one shared placement, and conflating those
#            two was a second error on the same fix. A 4× clock offers 0.00, 0.25, 0.50
#            and 0.75 UI and the three timings may each pick a DIFFERENT one; a 2× clock
#            offers 0.00 and 0.50. Forcing all three onto the data edge's point is a real
#            restriction on the design space, not a conservative simplification — and the EDE
#            proposal is the proof, since t_DD 0.50 / t_ZD 0.00 / t_DZ 0.50 is exactly such a
#            design and a model that cannot express it cannot evaluate it. So Man_t_DD is
#            fixed at the placement the selector names ("4× CLK launch (0.25 UI)") and stays
#            read-only, while t_ZD and t_DZ are seeded there but remain EDITABLE and are
#            snapped to a point the selected clock can place. The snap is reported in the
#            term's note, since a silent one is its own display-versus-value mismatch, and
#            the note is ordered snap-then-anchor-conversion — feeding the tabulated value to
#            _conv_note had made it attribute the whole move to a conversion that does
#            nothing at all on a SWI3S Manager. Ties round half AWAY FROM ZERO rather than
#            through the built-in round(), whose half-to-even would land an exactly-halfway
#            t_ZD on 0.00 UI from one side of the grid and 0.50 UI from the other.
#          — The ROWS now follow it, in both columns, min == max and read-only with a tooltip
#            naming what fixed them, and the hint names every row it fixes rather than only
#            Man_t_DD. This was the reported symptom: the picker moved the margins while the
#            parameters they are computed from sat unchanged on screen, so the number in force
#            was nowhere on the page. Returning to analog re-seeds from the spec tables rather
#            than restoring a prior edit — deliberate, and noted in the code: while the mode
#            held, that edit was being ignored by the model, so there is no value to honour.
#            Guarded from both sides, because neither guard alone suffices: the model tests
#            construct CalcInputs directly and catch a missing override, the view test asserts
#            row == term and catches a missing row sync. A mutation of either passes the other.
#          — EDE was at first exempted from the mode beyond t_DD, on the reading that it
#            stated all three placements itself. Withdrawn: EDE redefines what t_DZ MEANS —
#            the reference moves to the UI start, inside Table 130's window — not whether the
#            selected clock can place it, and a release the clock cannot generate is not a
#            proposal. All three are governed on every column, which is what surfaces 2×
#            being unable to express EDE rather than hiding it behind an exemption.
#          — The KEEPER is now TWO inequalities, one per releasing device, which is a
#            correctness fix and not presentation. find_worst_corner_rows moves one row at a
#            time, exact only for a margin monotone in each parameter; a min over two legs is
#            not. The Manager's leg sits at exactly 0.00, which SATURATES that minimum, so no
#            single peripheral row could lower min(0.00, per) and the search read them all as
#            insensitive. Reaching the peripheral's own worst needs Per_t_DZ,min and
#            Per_t_DD,max TOGETHER. It reported +0.00 at 12.6 MHz where the peripheral leg is
#            −0.10, and +0.00 at 13.2 MHz — PHY2's mandatory maximum — where it is −0.64.
#            Iterating the search to a fixed point was tried and does NOT fix it: coordinate
#            descent cannot leave a local minimum that needs two coordinates to move at once.
#            Split, each leg is monotone and the search is exact again — checked against brute
#            force over ~53k corner vectors — and the results tree names which device is short.
#            A new inequality that is not monotone in this sense must be SPLIT, not answered
#            by a cleverer search; that is now written where the search is.
#        • One caveat on the clocked model, stated rather than buried: collapsing a row to
#          min == max assumes NO analog turn-on or turn-off after the clock edge. That is the
#          optimistic direction on whichever leg reads the min corner. It happens not to
#          matter on PHY2, where the affected legs pass by more than 20 ns, but it is exactly
#          what the EDE column declines to assume — EDE models 3–9 ns of analog turn on top of
#          each grid point, at the 3:1 PVT spread. If the plain columns should carry the same
#          spread rather than collapsing to a point, that is a one-line change to the same
#          helper and would make the two columns consistent.
#        • Consequence for the EDE proposal: the boundary release does not
#          remove the rate ceiling, it MOVES it from contention to the keeper.
#          Per_t_DZ,max = UI (IFX Proposal 1) plus the 3:1 floor fixes Per_t_DZ,min at UI/3,
#          which must cover Per_t_DD,max + t_keeper = 12 ns — a fixed number, while UI/3
#          shrinks with the rate. So UI ≥ 36 ns, i.e. F_CLK ≤ 12.50 MHz, which CLEARS the
#          12.288 design point. That is the finding, not a defect. An earlier write-up had it
#          backwards, reading it as failing against Table 126's mandatory 13.2 MHz and
#          recommending tightening Per_t_DD,max to 8.36 ns to reach it. 13.2 predates the timing
#          analysis that produced the revision this builds on, and that analysis already puts
#          PHY2 beneath it — its Finding 3 is that PM setup binds F_max BELOW the mandatory
#          rate. So a 12.50 MHz keeper ceiling is one more measurement of the same thing: PHY2
#          lands near the audio design point, and the zero-UI handover is not what caps it.
#          The 8.36 ns figure stays as arithmetic, explicitly NOT as a recommendation — paying
#          a hard-fought Per_t_DD,max to reach a rate the rest of the analysis says PHY2 cannot
#          reach anyway spends real margin on a number the physics does not support. The white
#          paper, its generated numerics and its smoke check carry the corrected reading, which
#          now asserts the ceiling CLEARS 12.288 rather than that it falls short of 13.2 — and
#          the paper's claim that "the rate ceiling essentially disappears" is corrected too.
#          The 13.2 MHz check survives for a different reason: a reader can dial that rate in,
#          and must see a red margin instead of the false PASS the single keeper leg gave.
#        • The paper's 2×-vs-4× reasoning is corrected with it: 4× was described as buying
#          +9.2 ns of keeper margin for −0.7 ns of MP hold. The +9.2 is the MANAGER's leg; at
#          4× the binding leg is the PERIPHERAL's at +0.21, which no Manager launch choice
#          moves. So 4× buys 0.2 and pays 0.7. The recommendation is unchanged; the reasoning
#          behind it was wrong.
#        • Changelog hygiene, since this entry is the release note: a 51-line run of 3.0.13
#          bullets appeared TWICE verbatim, one copy spliced into the middle of the bus-keeper
#          bullet and orphaning its second half. Repaired. Two further bullets described the
#          Per_t_ZD back-out removal as shipped, including "it moved a published number:
#          PM_setup_ho +6.185 → +4.519" and a second failing leg it was said to expose — that
#          change was REVERTED (see above) and the baseline test pins +6.185, so both were
#          describing code that does not exist. Removed rather than reworded.
#        • A residual left unmodelled rather than approximated: t_ZD sits 20% into the G-ramp,
#          so it is fractionally later than a true start-of-slew anchor, and the contention
#          legs are optimistic by the same sliver (conduction begins just before t_ZD).
#          Sizing it needs the G-ramp duration, which is not a parameter here, and it is far
#          smaller than the t_RF/3 it replaces.
#        • Defaults now describe the design point rather than the worst corner. F_CLK opens at
#          12.288 MHz (256 × 48 kHz) instead of PHY2's 13.2 MHz mandatory maximum, still
#          clamped by whatever the selected pair may legally clock. Bus length is 0–30 cm
#          rather than a 5–60 cm sweep: 30 cm is the target, and 0 cm is a REAL corner, not a
#          placeholder — t_PD funds PM hold, so a co-packaged Peripheral is that leg's worst
#          case. Pinning 30 cm had hidden it, and PM hold reads +3.84 ns where it read +7.84.
#        • Two tooling defects this work walked into, both in checks that are supposed to
#          catch things. `tools/leak_scan.py` failed the gate on the citation "§10.1.4.4.2",
#          which matches a private-IP pattern; narrowed rather than allowlisted, per
#          DEVELOPMENT.md, and the first narrowing then dropped a GENUINE address written at
#          the end of a sentence — there was no test coverage of the structural patterns at
#          all, and there is now, in both directions. And `tools/publish_tree.py` ran the
#          suite on a tree with no native core, so Python silently imported a months-old copy
#          from site-packages: four tests failed on the assembled tree while passing in the
#          repo, reported as "something load-bearing was pruned". That is the stale-binary
#          failure the release gate exists to prevent, in the one check the gate does not
#          cover, and it could as easily have gone the other way. The core is now staged and
#          its score_abi asserted before the suite runs.
#        • The demos now carry a READ, and until this cycle none did — so the ReadSetup /
#          ReadData transport had no coverage at all. Both shapes are emitted: an IMMEDIATE
#          read (peripheral answers READ_DATA_NOW, one command record) and a DEFERRED one
#          (REMOTE_READ_DEFERRED with no data, then a ReadData delivery inheriting the
#          setup's address and byte count — two records for one logical read). Appended
#          before the idle fill so fillIdleWithPings absorbs them: the demo's row count,
#          audio timing and SSP rows are unchanged, only the command list grows.
#        • That CONFIRMED a suspected display bug and fixed it. Per-command statistics
#          grouped by the raw wire name, so a deferred read showed as "ReadA32 / 0 bytes
#          read" beside an unconnected "ReadData" group holding the real bytes — two rows a
#          reader cannot join up, for one read they asked for. Now folded on logical identity
#          (measurements._fold_deferred_reads), keyed on the DEFERRED response token rather
#          than "carries no data", because a READ_FAILED setup also carries none and must not
#          swallow an unrelated later delivery. An orphan delivery still counts.
#        • Writing the demo frame found two things unrelated to statistics: a read payload
#          must be closed by the PM spacer AND the Manager Response symbol (the parser only
#          emits the record on that response) — a first version stopped at the data CRC and
#          the immediate read produced NO record and NO error, the next comma abandoning the
#          phase mid-spacer; and test_sspa_demo's CRC check lacked the has_manager_packet
#          guard that analysis/errors.py has, so the first legitimate ReadData ever emitted
#          read as corrupt. A ReadData has no Manager Packet and so no CRC to validate.
#        • The resync Column-0 guard is DOWNGRADED, not built. Losing sync does not imply the
#          column count changed — it is assumed unchanged, and a peripheral that cannot
#          rejoin needs a full bus reset, which is the already-covered cold-start path. The
#          register's "re-locks at a different width" failure mode was inferred from the code,
#          not the protocol. The defensive branch stays; the synthetic demo does not get
#          built, because it would freeze a wrong model of the bus into a permanent fixture.
#        • A grid cell's `dp` is now the logical DataPortNumber in BOTH engines. The C++ core
#          always reported it (Decoder.cpp: dpNumber >= 0 ? dpNumber : slot index); swviz's
#          internal SLOT INDEX reached the grid unmapped, so one key meant a number from one
#          engine and an index from the other — device 1's DP0 tooltipped as "DP5". Fixed at
#          the adapter (model/viz_engine.py::render_payload), NOT in the engine: the index is
#          a real index inside swviz (_detect_truncated_drq groups bits by it and looks them
#          up by enumerate() position), so redefining it there would have silently stopped
#          that detection for exactly the configs being fixed. render_payload is the single
#          boundary both engines' cells pass through, so mapping there fixes the grid, the
#          tooltips and the cross-engine comparison at once. The published reference model
#          (dataport.py, flow_control_port.py) knows nothing about port numbering and was not
#          touched. Proven a no-op for the existing corpus — all 89 example configs number
#          their ports by position, so every golden passed UNCHANGED, which is also why 89
#          configs never caught it. Uniformity is its own blind spot.
#        • Comparing (device, dp) pairs while fixing that surfaced a SECOND bug, in the C++
#          CSV reader, whose worse symptom was not the labelling that exposed it. A config
#          encodes the manager as device 0 + ManagerDataport=True, because DeviceNumber_REG
#          holds 0..11 and cannot carry the -1 sentinel; the reader took only the first half
#          and documented the flag as ignored. Since 0 is a REAL peripheral address, every
#          manager data port impersonated device 0 — and registersFromConfig therefore emitted
#          the manager's port configuration as register writes ADDRESSED TO device 0: 103
#          writes to device 0 against 23 for each peer on the flow-control fixture, including
#          two conflicting values for the same DP0 FlowMode register. One port's configuration
#          was silently overwriting another's. The emitter was never wrong (both its loops
#          already skip deviceNum < 0 — a manager port is not addressable as a peripheral
#          register write); only the sentinel had to survive the parse. Decoded now in a pass
#          AFTER the parse loop, so the result cannot depend on which of the two CSV rows a
#          writer emits first. After: 23 writes per device, zero duplicate (device, address)
#          pairs, and both engines agree on all eight ports. The corpus sweep could never have
#          caught it — its comparison key carries no device field at all.
#        • The published reference model reads as hardware, not as notes to a maintainer.
#          dataport.py and flow_control_port.py ship inside the MIPI specification as a
#          standalone functional model, and test_reference_model_clean already said what that
#          means — "the spec text is the explanation, and implementation rationale belongs in
#          docs/ or in the tests, not in the deliverable" — but it enforced that only against
#          `#` tokens. The DOCSTRINGS had accumulated seven references a spec reader cannot
#          follow: two `tests/test_perf.py` citations, a `utils/validators.py` path, a
#          `mirrors C++ CDataPort`, a `Device._channel_from_index` (a method of a module the
#          spec does not publish, so a dangling pointer), and two mentions of "the engine".
#          A docstring is MORE prominent in the published artefact than a comment would have
#          been, so the enforced half of the rule was the less important half. All seven are
#          now stated as behaviour, and a test rejected them in docstrings too — for the rest
#          of this cycle only. The next external drop re-added two of them and the test is
#          withdrawn; see the entry above for why a rule over externally-authored prose could
#          not hold. Requirement tags ({ASW####}) were PERMITTED throughout — they point into
#          the spec the model ships in, which is the one cross-reference that belongs — though
#          after this cycle neither file contains one. Device/Interface survive as type names,
#          which are structural rather than prose.
#        • Where the external reviewer's docstring trims were taken over ours. The same review
#          drop that prompted this had cut four docstrings to one-liners. Three of those cuts
#          are accepted as-is, deliberately including one fact we would have kept:
#          _txp_enabled's RX_CONTROLLED note with its {ASW5203} tag. It is not a codebase
#          reference, so it did not have to go; taking them is a concession to a reviewer who
#          was right about the larger point and had been pushed back on for it three times. The
#          polarity inversion came back in the reviewer's own shape rather than ours —
#          `"""PortDirection_REG=True means SINK"""` is the
#          whole docstring, because the property name already says what it returns and the
#          inversion is the only surprising part. It also matches the wording already in
#          bus_config.py. What is NOT conceded is the clamp below — the residual disagreement
#          is now one line of code plus three docstrings that name this repository, which is
#          a reviewable diff rather than an argument.
#        • Speed is not a goal of the Python model, and saying so cost 7%. swi3score is the
#          fast implementation; the swviz model optimises for a spec reader. Three hoists that
#          existed purely for speed are gone (a `_num_cols` cached in initialize(), and locals
#          in clock_tick() and _effective_channel_grouping()), reading the properties at the
#          point of use instead. The engine build goes 67.1 -> 72.0 ms against a 15 s ceiling,
#          i.e. three orders of magnitude clear. test_perf.py's docstring had argued the
#          opposite — it named the three hoists and warned they "read as redundant, so they
#          get removed by well-meaning cleanups" — which sent three successive external review
#          drops into the same argument before the intent was written down. That ceiling is a
#          runaway guard (an accidental O(n^2), a re-decode per tick), not a defence of
#          micro-optimisation. The partial-channel-group clamp in _advance_channel is NOT in
#          that category and is untouched: without its `min`, a 5-channel port at
#          ChannelGrouping 2 emits one extra DATA cell per interval.
#        • The only CSV at the repository root is gone. flow_control.csv was a test fixture
#          read by exactly one test, via a CWD-RELATIVE path — and registers_from_csv returns
#          [] for a path it cannot open rather than raising, so running pytest from anywhere
#          else failed as `assert []` ("no 0x0E registers emitted"): a missing file reported
#          as a decode regression. Now tests/fixtures/flow_control_demo.csv, resolved from
#          __file__ with an explicit isfile() assertion. Its tier comment had also claimed
#          "referenced by Demo.cpp + 3 tests"; Demo.cpp does no file I/O at all and one test
#          reads it — the count came from grep hits on five unrelated files.
#        • The governance layer is owned UPSTREAM, not here. Publishing is a tree replacement
#          onto a repository with its own history, so a file its maintainers added is
#          invisible to us and a naive replacement DELETES it. That nearly shipped: an
#          upstream pull request had aligned that repository with its organisation's project
#          template — adding a project-identity file and a config-check workflow, moving
#          CODEOWNERS to the root under a renamed team, and removing six community-health
#          documents now inherited from organisation-level defaults. A tree built from here
#          would have reverted all of it silently. This tree now carries the root CODEOWNERS
#          and drops the six inherited documents (their links are rephrased, since an
#          inherited default has no stable in-repository path), and `tools/publish_tree.py`
#          declares the two remaining upstream-owned paths and GRAFTS them in at publish time
#          — refusing to build a tree if the upstream ref is unavailable, if one of those
#          files is missing there, or if this tree carries a copy that would overwrite theirs.
#          Ownership is one-directional on purpose: carrying our own copy is worse than
#          dropping it, because dropping is visible in a diff and overwriting is not.
#        • THE GROUND SHIFT DOES NOT CANCEL AS WIDELY AS THE MODEL CLAIMED, so
#          Σ_cross,PM,setup was understating a penalty. Reported as a question — why α does
#          nothing to some cross terms and makes others GROW — and the first half of the
#          answer is that the model was right: the three cross terms do not share a sign.
#          Δ_cross,MP and Σ_cross,PM,setup are penalties that shrink as α falls, while
#          Σ_cross,PM,hold is a CREDIT (it is how long the data stays valid at the Manager),
#          so LESS noise makes it GROW. Both say the same thing — more margin — and reading
#          term magnitudes as if they moved together is what makes the response look
#          inconsistent. The invariant now documented and tested is on the margins:
#          ∂margin/∂α ≤ 0 for all 13 inequalities, verified across both ramp shapes.
#          The second half is a real error. PM setup sends a signal out and gets one back, so
#          TX and RX roles swap and ONE physical δ = V_Per_gnd − V_Man_gnd raises the forward
#          leg's threshold and lowers the backward leg's. The model took that antisymmetry as
#          exact cancellation and dropped α entirely. It cancels only when t_cross is AFFINE
#          in the threshold (a linear ramp) AND the two legs carry EQUAL t_RF:
#          — Under the EXPONENTIAL ramp t_cross is convex, so by Jensen the ±δ pair sums to
#            strictly MORE than the δ=0 value: +0.57 ns at α = 0.10, t_RF = 5 ns.
#          — Under ANY shape, unequal per-leg t_RF weights the ±δ terms differently and
#            leaves a residue. A mixed SWI3S ↔ SoundWire bus never has equal legs, so this
#            one bit the interop path even on the linear ramp its scenarios use.
#          Σ_setup is a penalty, so the binding δ MAXIMISES the sum; convexity puts that
#          maximum at an endpoint δ = ±α, so the fix evaluates both polarities and takes the
#          worse — exact and analytic, no interior search. A per-leg coefficient can no longer
#          express the total (the max couples the legs), so sigma_pm_setup is now marked
#          reporting-only and callers go through sigma_pm_setup_ns(); the two analysis scripts
#          that were scaling the raw coefficient by (t_RF_Man + t_RF_Per) were updated with it.
#          What moved: linear at equal t_RF is unchanged, so the ALL-SWI3S default path does
#          not move at all. The exp-ramp PM setup penalty grows 9.60 → 10.17 ns and drops
#          F_max,PM from 12.6 to 12.4 MHz — still clear of the 12.288 MHz design point, but on
#          0.11 MHz of headroom rather than 0.31. The four mixed-bus PM setup margins each lose
#          0.03–0.07 ns; both interop conclusions survive, since the shift is a common offset —
#          Scenario A stays exactly revision-invariant and B's revision is still worth exactly
#          +11.00 ns, which is now asserted rather than left to inspection.
#          The sibling hold-rr term keeps its δ=0 form deliberately: rr's per-leg coefficients
#          dominate rf's on BOTH legs, so min(rr, rf) always selects rf and the simplification
#          is inert. That is asserted rather than assumed, out to a 20:1 leg asymmetry, because
#          the docstring leans on it — if a corner change ever made rr bind, a silent
#          simplification would become a live error of the same kind as this one.
#        • Δ_cross,MP PUT THE WRONG LANE'S t_RF ON THE LATE EDGE FOR SETUP, found while
#          scoping a rename that looked purely cosmetic. Both MP lanes are Manager-driven, so
#          the differential is a LATE crossing minus an EARLY one — but WHICH LANE carries the
#          late edge flips with the direction. MP setup is worst when the DATA arrives late and
#          the clock samples early; MP hold is worst when the CLOCK samples late and the data
#          transitions early. The model scaled the late crossing by t_RF_Man_CLK in both, which
#          is right for hold and wrong for setup. At equal per-lane t_RF the two coincide — the
#          reason one coefficient served both — but unequal they diverge, and the setup
#          direction is a PENALTY, so it was understated: 5.69 ns at t_RF (CLK 5, DATA 8) and
#          9.48 ns at (3, 8). `compute` now derives both and each MP inequality takes the one
#          for its direction. A max over the two would be wrong the other way, making hold
#          pessimistic, so the caller states which it needs rather than guessing safely.
#          LATENT, NOT LIVE: both fields default to 5.0 and the UI never separates them, and
#          the one test that made them unequal exercises MP_hold_ho — the direction the old
#          assignment was right for. So no published number moves, and the equal-lane default
#          is arithmetically identical to before.
#        • The NAMES were what hid it, so they changed too: mp_clk_slow/mp_data_fast →
#          mp_late_rise/mp_early_fall. A data edge's direction follows the BIT PATTERN, so
#          either lane can rise or fall on any UI; the coefficients describe edge POLARITY and
#          a threshold, never a lane. Naming them for lanes made a lane-dependent assumption
#          read as settled fact. The polarity choice itself needs no runtime search — the
#          rising-late/falling-early pairing has both a later late crossing AND an earlier early
#          one than its alternative, so it dominates for every positive t_RF pair, which is now
#          asserted rather than inherited from a comparison made only at equal t_RF. PM's _clk
#          and _data suffixes are deliberately UNCHANGED: there the forward leg really is the
#          clock (Mgr→Per, t_RF_Man) and the backward leg really is the data (Per→Mgr,
#          t_RF_Per), two distinct legs with independent slew, so the lane names are accurate.
#          Renamed through to the memo's macros and table rows, whose "clock (slow rising)" /
#          "data (fast falling)" labels carried the same error into print. Regenerating moved
#          macro NAMES only — every emitted value is byte-identical, which is the check that
#          the rename is a rename.
#          Both findings this cycle came from reading the model rather than from a test:
#          the α question exposed the ground-shift residue, and a dimensional slip in the memo
#          (t_cross·t_RF, implying s²) exposed this. The memo's structural inequalities were
#          right and its worked numerics multiplied by t_RF a second time; they now carry
#          Δ_cross,MP and Σ_cross,PM, which are the dimensionless coefficients those lines
#          already substituted. Worth recording that the misnamed symbol and the dimensional
#          slip were the same defect seen from two directions.
#        • Bus-Visualizer DP COLOURS key on the config SLOT again, not the DataPortNumber.
#          The model permits several slots to share a number — a device may not reuse one,
#          but the same number across different devices is fine — so a smart-amp config
#          where four devices each own a DP0 and a DP1 has eight distinct ports carrying
#          two numbers. Colouring by the number merged them: eight ports rendered in two
#          colours under a two-swatch key, so the colours did not identify what the key
#          named. This was collateral from 24d658a, which correctly made a cell's `dp` the
#          logical number for LABELS and cross-engine comparison, and thereby changed what
#          `palette[dp % 12]` meant — right in the reference Visualizer, where `dp` WAS the
#          slot index. Cells now carry both (`dp` logical, `dp_index` slot); the grid
#          colours by slot, and the key labels each swatch with the port's name, falling
#          back to a device-qualified number ("Mgr·DP0", "D1·DP0") rather than a bare
#          "DP0" that four ports would share. The engine-mode key gained names it never
#          had. Caught on a real config, not by a test: all 89 corpus examples use
#          the identity mapping, the same blind spot 24d658a itself noted.
#        • The external reviewer's next drop CONCEDES THE CLAMP, so the code disagreement
#          above is closed. Their `_advance_channel` now sizes the trailing group with a `min`
#          against the remaining enabled channels — the partial-channel-group fix, in the
#          reviewer's own shape. Taken as authored. What it also drops is the `max(0, ...)`
#          around the seed, and that is safe: the transport-window gate already requires
#          channel_group_base_channel < enabled channels before a tick can reach here, so
#          the min's second term is at least 1 and the seed cannot go negative. Swept 648
#          register combinations (channels x grouping x sample-grouping x sample-size)
#          through the model with the seeding branch instrumented — 11,664 visits, minimum
#          term 1, minimum seed 0 — so the clamp was unreachable rather than merely unused.
#          CDataPort keeps its defensive `(groupChannels > 1) ? groupChannels - 1 : 0`; the
#          two engines now differ in defensiveness only, which no capture can observe.
#          Not conceded, and now withdrawn rather than enforced: three docstrings still name
#          this repository (a `utils/validators.py` path, `mirrors C++ CDataPort`, a
#          `Device._channel_from_index`, two mentions of "the engine"), which is what the
#          docstring rule above existed to reject. That rule is GONE. It was right about the
#          artefact and wrong about who writes it — the model is authored externally and
#          arrives as a whole file, so enforcing prose here means rewriting the author's words
#          every drop or carrying a divergent copy, and the next drop undoes either. It was
#          first tried as a pinned exemption list, which is worse than no rule at all: it
#          reads as coverage while the exemptions are where the content is. What stays
#          enforced is what a whole-file drop cannot silently undo and what this tree does
#          own — no `#` comments (the drops ship none; OUR edits added all 25), and the
#          module/class docstrings existing, so the comment rule is never satisfied by
#          deleting documentation instead of moving it. The content ask goes to review of the
#          next drop, where it can be acted on. Same call on the drop's other regression:
#          _is_source loses `PortDirection_REG=True means SINK` for "Return True if this is a
#          source DP", accepted in context — the property sits two lines under
#          PortDirection_REG's declaration, so the inversion is legible without the sentence.
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
#        every partial channel group — plus a build-prerequisite pass prompted by a Linux
#        build failing mid-compile. Native ABI 7 → 8 (DecoderSettings
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
#        • Build prerequisites, after a Linux build died ~20 lines into
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
__version__ = "3.0.17"

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
