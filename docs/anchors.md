# Measurement anchors: what a tabulated timing number contains

**Settled 2026-08-13.** This is the reconciliation of record for the `t_DD` / `t_ZD` / `t_DZ`
anchor question, which three artefacts answered differently and which has been re-litigated
at least twice. It is written down so the argument is not had a third time from scratch.

The live calculator is `swi3s_studio/timing/`. The reference analysis is
`timing-analysis/swtiming/` (`emit_ede.py` and the `docs/*.tex` it generates macros for);
`timing-analysis/docs/MODEL_AUDIT.md` is that project's own audit and §2 of it takes the
position this document overturns.

## The ruling

**Figure 174 governs. Table 129's wording is inaccurate.**

`Per_t_DD` and `Per_t_ZD` are measured **from the clock's V_IH,rising / V_IL,falling crossing
to 20 % of the ramp**, as the figure draws them. So a tabulated value *does* contain a slew
portion, and recovering the pure delay the inequalities need means taking it back out:

```
Per_t_DD,pure = Per_t_DD − offset·t_RF,nom      offset = 1/3 (linear), 0.161 (exponential)
              = 20.000 − 1.667 = 18.333 ns      Per_t_DD,min,pure = 0.333 ns
```

**`Per_t_DZ` is exempt and is used raw.** Its meaning is the instant the driver goes high-Z,
which is abrupt — there is no ramp portion inside it to remove.

**A SWI3S Manager needs no conversion.** Figure 176's anchors are symmetric (clock 20 %, data
20 %), so the two portions cancel and the tabulated value is already pure. Only the Peripheral
side (Fig. 174, asymmetric) and any SoundWire side are converted.

## The argument that was made for the other reading, and why it lost

Table 129 says, in words:

- `Per_tZD` — "Time from high-Z to **the start point on the V-Ramp or G-Ramp**"
- `Per_tDD` — "Peripheral Output Hold Time (**earliest change** in data output after edge on
  clock input …)"

Read literally that is a 0 % anchor, which would make the conversion a no-op. Two further
arguments were advanced for it, and both are answered:

| argument for 0 % | answer |
|---|---|
| The G-Ramp case: a conductance ramp has no voltage V_OL waypoint to measure to, and the parameter is defined identically for both ramp types | The figure defines the measurement; the table's prose is loose. A G-Ramp has a 20 %-of-G waypoint, which is what `per_tdd_offset_frac` uses for the exponential shape |
| The keeper leg's addend is the full 0 %-to-rail swing, which is only right if `t_DD` is 0 %-referenced — so the same file contradicted itself | **This was a real contradiction, and the resolution is the opposite one.** The keeper leg was wrong, not the conversion: it charged the raw `t_DD` *plus* the full swing, double-counting the ramp's first fifth. Fixed — the leg now subtracts the pure launch |

That second row is the substantive change this ruling forced. See below.

## What was corrected

| where | was | now |
|---|---|---|
| `keeper_Man` / `keeper_Per` | raw `t_DD` − full swing | **pure** `t_DD` − full swing. `keeper_Per` moves +1.667 (12.76 → 14.42 at 13.2 MHz nominal); `keeper_Man` unchanged, its anchors being symmetric |
| `pure_output_delay` | multiplied by the **operating** `t_RF` | multiplies by the spec's **reference** `t_RF` (5.0 SWI3S, 5.4 SW-1V8). The number was measured at the test condition, so the portion embedded in it is fixed. Corrected 2026-08-13; it had moved margins in both directions |
| `output_anchor_frac`, `per_tdd_offset_frac` docstrings | cited Fig. 174 without addressing Table 129 | state that the figure governs and the table text is inaccurate |

Tests pinning it: `test_the_keeper_leg_charges_the_launch_and_the_swing_without_overlapping_them`
(the two spellings of the interval must agree, and `t_DZ` stays raw) and
`test_the_pure_back_out_is_a_fixed_offset_not_a_function_of_the_operating_slew`.

## The divergence is now a test

`tests/test_timing_vs_reference.py` imports the reference and asserts every leg pair under one
matched configuration: six agree to four decimals, the peripheral-launched ones differ by
exactly this anchor conversion, P→P **hold** differs by the anchor alone, and P→P
**contention** by the anchor plus the one remaining disagreement — the reference splits the
clock's slew between the two peripherals and this model does not, because the clock is driven
from a single Manager pin, so one edge with one slew serves both detections. Each delta is
declared, so one MOVING fails rather than being discovered by a later audit. It skips when the
reference project is not beside this one (override with
`SWI3S_TIMING_ANALYSIS`); it is not vendored, because a stale copy asserting agreement is
worse than no test.

**The same "two peripherals corner independently" argument goes the other way for the
receiver threshold**, and getting the answer right *per quantity* is the point. A threshold is
a per-part property, so two receivers can sit at opposite ends of the V_IH compliance window
at once; the corner search used to collapse that window to one bus-wide value, which credited
an acquiring peripheral 2.38 ns it is not owed on the three legs that net two peripherals'
crossings. Keeping the window open moved P→P contention back to failing and onto the paper's
side of that leg (−1.08 here against the reference's −1.077 at a single slew), leaving the
slew split as the only live divergence.

It also pins one finding about the reference: it prices only the t_ZD launch, so it has no
continuous-transmission setup/hold leg at all.

**A retracted finding, kept because the reasoning is the trap.** The reference's
`keeper_per_conventional` has no `t_DZ` term where this project's `keeper_Per` carries
`1·UI + Per_t_DZ`, and that was first written up as a 9.00 ns omission. It is not one. The
keeper leg wants the EARLIEST release, and t_DZ is specified only as a maximum — so t_DZ = 0
is legal and is the corner that starves the keeper. The reference hardcodes that corner by
omitting the term; this project makes it a corner the search finds, and it does find it
(`picks["Per_t_DZ"] == "min"`). At that corner the two legs differ by the anchor conversion
alone. Comparing a hardcoded worst case against a term evaluated at its NOMINAL is what
manufactured the 9 ns.

## Still open, and owned elsewhere

`timing-analysis/swtiming/emit_ede.py` removed its `offset()` layer on the 0 % reading — see
its ANCHOR note, and `MODEL_AUDIT.md` §2 "Acted on in `emit_ede.py`". Under this ruling that
removal is wrong and should be reverted, which moves every peripheral-launch leg in the EDE
paper by 1.667 ns and regenerates `docs/ede_numerics.tex`. The Timing Analysis paper
(`SWI3S_Timing_Analysis.tex` §2.3) was never changed and already agrees with this ruling.

**That revert is not made here.** It belongs to the paper's author and changes a published
numeric appendix; this document exists so the decision is on the record when it is made.
