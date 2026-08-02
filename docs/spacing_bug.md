# Channel-group spacing leaks across the row boundary — root cause & fix

Status: **fixed in Studio** — both of its placement engines
(`native/swi3score/core/CDataPort.cpp` and `swi3s_studio/swviz/models/dataport.py`).
**The standalone v2 visualizer patch is identified but not applied**
(`mipi-soundwire-I3S-visualizer/src/models/dataport.py`).

Reported against `spacing_question.csv` (AppVersion 2.1.12): with `Spacing_REG=2`,
`HorizontalStart_REG=1`, `HorizontalCount_REG=1`, the spacing that should have been
terminated at the end of row 0 carried into row 1, so row 1's first data UI landed in
**column 2 instead of column 1**.

> **Three engines carry this algorithm, and the first fix only reached one of them.**
> Studio runs two — the C++ decode core (a capture on the wire) and the vendored
> `swviz` (a config CSV in the Visualizer tab) — and the standalone v2 visualizer is a
> third. The initial fix landed in the C++ core alone, so the user re-opened the same
> CSV and still saw a wrong grid: the Visualizer tab is rendered by `swviz`. Any fix
> here must be applied, and tested, in every engine. See §3 and §6.

The reference implementation is
`mipi-soundwire-I3S-visualizer-1.74/swi3s-visualizer-1.74.py` (`DataPort.try_bit`) —
except for the last-column geometry in §5, where we deliberately diverge from it.

---

## 1. Symptom

Config (DP0 of `spacing_question.csv`): `EnableCh=0b11` (2 channels), `SampleSize=0`,
`SampleGrouping=1`, `ChannelGrouping=1`, `Spacing=2`, `Interval=11`,
`HorizontalStart=1`, `HorizontalCount=1`, 16 columns.

| row | 1.74 (golden) | v2.1.12 | Studio C++ core | Studio swviz |
|-----|---------------|---------|-----------------|--------------|
| 0   | cols 1, 2     | cols 1, 2 | cols 1, 2     | cols 1, 2 |
| 1   | cols 1, 2     | **col 2** | **col 2**     | **col 2** |
| 2   | cols 1, 2     | **col 1** | **col 1**     | **col 1** |

(All "before fix".) The error alternates rather than staying constant: row 1 loses
column 1 to the stale countdown, which re-accrues and shifts the following row back
again.

---

## 2. Root cause

**Channel-group spacing is a within-row gap, but none of the three rewritten engines
terminates it at the row boundary.**

Spacing only counts down *inside* the transport window
(`HorizontalStart <= column <= HorizontalStart + HorizontalCount`). If the window closes
while `spacing_slots_remaining > 0`, the remainder survives into the next row and is
consumed by that row's first in-window UI — which therefore carries no data.

The state trace makes the mechanism explicit (DP0 config above):

```
row col |    phase spac | claimed
  0   1 |   ACTIVE    0 | DATA      [window]
  0   2 |   ACTIVE    0 | DATA      [window]
  0   3 |  SPACING    1 |           <- gap armed, window has closed
  ...
  0  15 |  SPACING    1 |           <=== ROW BOUNDARY: spacing survives
  1   1 |  SPACING    1 |           [window]  <- eaten by the stale countdown
  1   2 |   ACTIVE    0 | DATA      [window]  <- data one column late
```

### What 1.74 did

1.74 carries a `done_with_row` latch that fires when the column passes
`HorizontalStart + HorizontalCount`, and clears the spacing counter as it fires
(`swi3s-visualizer-1.74.py:2521-2524`):

```python
elif column_number > self.horizontal_start_REG + self.horizontal_count_REG :
    self.done_with_row = True
    self.channel_group_is_spacing = 0     # <-- spacing terminates AT the row end
```

1.74 clears `channel_group_is_spacing` in three places: initialisation (2145), this
row-end latch (2524), and the SRI path of `new_row` (2619).

### What the rewrite changed

The v2 rewrite folded `done_with_row` into the `in_transport_window` predicate
(`dataport.py:141-147`). That reproduces the **gating** correctly — a port outside its
window owns nothing — but silently dropped the **side effect** of clearing spacing.
`_advance_row` resets `column`, `guard_pending` and `tail_remaining`, and handles the
`PENDING` phase, but never touches `SPACING` / `spacing_slots_remaining`.

Studio's C++ core is a faithful port of that v2 structure, and Studio's `swviz` is the
v2 engine vendored directly — so all three inherited the same omission from one rewrite.
The FCP (`flow_control_port.py`, `CFlowControlPort.cpp`) has no spacing concept and is
**not affected**.

---

## 3. Fix

The same clear, in each engine's row-advance, **before** the `PENDING → ACTIVE`
promotion and the `row_in_interval` increment, so the row starts in a clean `ACTIVE`
phase.

### Studio C++ core — applied

`native/swi3score/core/CDataPort.cpp`, in `advanceRow()`:

```diff
@@ -202,6 +202,21 @@ void CDataPort::advanceRow()
     mGuardPending = false;
     mTailRemaining = 0;
 
+    // Channel-group spacing is a WITHIN-row gap and must not survive the row.
+    // ... (see source for the full rationale comment)
+    if (mPhase == kSpacing) {
+        mPhase = kActive;
+        mSpacingSlotsRemaining = 0;
+    }
+
     if (mPhase == kPending && mChannelGroupBaseChannel < mNumChannels) {
         mPhase = kActive;
     }
```

### Studio swviz — applied

`swi3s_studio/swviz/models/dataport.py`, in `_advance_row()`:

```python
# Channel-group spacing is a WITHIN-row gap and must not survive the row.
if s.transport_phase == TransportPhase.SPACING:
    s.transport_phase = TransportPhase.ACTIVE
    s.spacing_slots_remaining = 0
```

### Standalone v2 visualizer — identified, not applied

`mipi-soundwire-I3S-visualizer/src/models/dataport.py`, in `_advance_row()` — the same
Python block as swviz above. That repo is currently unfixed; it was restored to original
after the golden investigation.

---

## 4. Validation

### 4.1 Sweep against the 1.74 golden model

1.74's `DataPort` class was extracted and driven directly with matching registers
(1.74 encodes `channels_REG` as **N−1** — `channels_REG=2` means 3 channels; getting
this wrong invalidates the comparison). Sweep: channels 1-4 × spacing 0-3 ×
HorizontalStart 0-2 × HorizontalCount 0-3 × SampleGrouping 1-2 × SampleSize 0-1,
4 rows × 16 columns, `SubRowInterval=False`.

| engine | configs matching 1.74 |
|---|---|
| v2 as-is | 588 / 768 (**180 mis-place data**) |
| v2 patched | **768 / 768** |
| Studio C++ as-is | 588 / 768 (same 180) |
| Studio C++ patched | **768 / 768** |
| Studio swviz patched | **768 / 768** |

Identical failure counts across engines confirm a single shared root cause.

> This sweep does **not** cover the last-column geometry in §5 (its `HorizontalStart`
> range is 0-2 on a 16-column bus, so the window never abuts the row end). That case is
> covered by the `spacing_overflow` directed test instead.

### 4.2 Studio test suite

- **510 passed, 8 skipped** (the 8 skips are pre-existing missing-Box-fixture skips,
  unrelated).
- `tests/test_spacing_row_boundary.py` — **12 tests, parametrized over both Studio
  engines**. Mutation-checked in each: reverting the C++ clear fails 5 of the `cpp`
  variants, reverting the swviz clear fails 5 of the `swviz` variants. The remaining
  case is `Spacing=1`, which has no gap and correctly stays green — a control, not a
  miss.
- `tests/test_visualizer_placement.py` (90 configs, C++ core) and
  `tests/test_visualizer_engine.py` (95 configs, swviz) both now **fail** with the fix
  reverted in the corresponding engine — see §5.

### 4.3 Golden impact — none from the fix itself

All 89 pre-existing visualizer example configs were regenerated with the fix applied and
diffed against baseline: **89/89 identical, 0 changed.** The only golden additions are
the new `spacing_overflow` config's own entries (§5).

> Method note: the first attempt at this diff produced "0 changed" with the patch
> accidentally reverted. It was re-run with the patch verified present both before and
> after generation; the result held.

---

## 5. The coverage gap, and the last-column decision

### Why no pre-existing golden caught this

The bug needs spacing to still be counting down *when the row ends*. Two independent
properties of the original 89-config corpus prevented that.

**Every config that reached the trigger was SRI with `Interval_REG = 0`.** Instrumenting
the real headless runs, exactly five configs entered `_advance_row` while in `SPACING`:

```
6 crossings  directed_tests/sri_sample_grouping
6 crossings  directed_tests/PDM_SRI_with_sample_grouping_and_channel_grouping
6 crossings  directed_tests/PDM_SRI_with_channel_grouping
4 crossings  directed_tests/sri_1
3 crossings  spec_figures/Figure_160_..._SRI_..._PHY1_FBCSE
```

All are `SubRowInterval_REG=True, Interval_REG=0`. **SRI is `Interval = 0` by
definition** — one interval per row is what sub-row interval means — so this covers the
SRI path in general, not just the configs that happen to exist. With `Interval = 0` the
row boundary always satisfies `row_in_interval > Interval_REG`, so `_start_interval()` →
`initialize_transport()` runs and resets phase and spacing anyway. The clear is
redundant there, which is why the fix is a no-op for SRI.

**The 20 non-SRI configs with `Spacing >= 2` all completed their spacing inside the
row.** Their windows close well before the last column (e.g. `Figure_11` DP2: window
ends at column 6 of 16). The two apparent exceptions — `handover_logic` DP5/DP6
(HStart 11, HCount 4, window ending at column 15 of 16) — were tested directly rather
than assumed: both place data at columns 11 and 14, so the 2-channel transport completes
one column before the window closes and spacing never crosses.

### The new directed test

`visualizer_examples/directed_tests/spacing_overflow.csv` closes the gap, with goldens in
both sets. It carries two ports:

- **DP0** — `HorizontalStart=1, HorizontalCount=1`: the ordinary in-row case.
- **DP1** — `HorizontalStart=14, HorizontalCount=1` on a 16-column bus: the window
  **ends at the last column**, forcing spacing to overflow the row.

Mutation-checked: with the fix reverted in either engine, both
`test_visualizer_placement` and `test_visualizer_engine` fail on this config.

### Deliberate divergence from 1.74 on DP1

For a window that ends at the last column, 1.74 and Studio disagree, and Studio's
behaviour was chosen knowingly:

| | DP0 (window 1-2) | DP1 (window 14-15, ends at last column) |
|---|---|---|
| 1.74 | r0 `[1,2]`, r1 `[1,2]` | r0 `[14,15]`, r1 `[15]`, r2 `[14]` |
| Studio (fixed) | r0 `[1,2]`, r1 `[1,2]` | r0 `[14,15]`, r1 `[14,15]` |

1.74's latch fires on `column > HorizontalStart + HorizontalCount` — here `> 15`, which
no column on a 16-column bus can satisfy. The latch **never fires**, so 1.74 never
clears the counter and it runs to **−1** and stays there. Its DP1 output is therefore a
consequence of the latch failing, not an intended rule: a countdown passing zero is a
bug, and spacing is defined as a within-row gap. Studio terminates it at the row end
uniformly, for every geometry.

1.74's pattern is at least stable (it repeats per interval and does not drift
unboundedly), so it is reproducible — but reproducibly wrong. **This divergence was
confirmed with the maintainer before the goldens were generated.** If the spec is later
read as mandating 1.74's literal behaviour, the fix would need to clear spacing only
when the latch would have fired (`horizontalEnd < num_columns - 1`), and this golden
would change.

---

## 6. Lessons

- **A placement fix must land in every engine that implements the algorithm.** The C++
  core was fixed and the report was closed; the user re-opened the same CSV and saw the
  same wrong grid, because the Visualizer tab renders through `swviz`. Grep for the
  behaviour, not the file.
- **A regression test must exercise every engine too.** The first
  `test_spacing_row_boundary.py` drove only `swi3score.grid_from_csv`, so it went green
  while the UI was still wrong. It is now parametrized over both.
- **A golden corpus can be large and still blind.** 89 configs, none able to reach the
  defect. Instrumenting which configs actually *hit* the code path was more informative
  than the pass/fail result.

---

## 7. Follow-ups

1. **The standalone v2 visualizer stays unpatched — deprecated.** The fix is recorded in
   §3 should that change.
2. **Consider porting the sweep harness into the suite.** The 1.74-vs-current comparison
   runs from a scratch extraction of 1.74's `DataPort`; as a permanent cross-model oracle
   it would catch the next divergence of this kind directly, rather than relying on a
   golden that happens to cover the case. It would need the §5 divergence encoded as a
   known, justified exception.
