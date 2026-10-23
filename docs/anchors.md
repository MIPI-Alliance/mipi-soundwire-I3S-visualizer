# Measurement anchors: what a tabulated timing number contains

This is the reconciliation of record for the `t_DD` / `t_ZD` / `t_DZ` anchor question.

The live calculator is `swi3s_studio/timing/`. The reference analysis is
`timing-analysis/swtiming/` (`emit_ede.py` and the `docs/*.tex` it generates macros for);
`timing-analysis/docs/MODEL_AUDIT.md` is that project's own audit, and its §2 takes the 0 %
reading this document rules against.

## The ruling

Figures and tables are cited by caption, not number, because the numbers move between
drafts. The Peripheral Output Timing figure and the Manager Output Timing figure are "PHY1 &
PHY2 Measurement: Peripheral Output Timing" and "PHY1 & PHY2 Measurement: Manager Output
Timing" (§11.2.7). The Output Timing table is "PHY1 & PHY2 Output Timing" (§11.2.3).

**The Peripheral Output Timing figure governs. The Output Timing table's wording is
inaccurate.**

`Per_t_DD` and `Per_t_ZD` are measured **from the clock's V_IH,rising / V_IL,falling crossing
to 20 % of the ramp**, as the figure draws them. So a tabulated value *does* contain a slew
portion, and recovering the pure delay the inequalities need means taking it back out:

```
Per_t_DD,pure = Per_t_DD − offset·t_RF,nom      offset = 1/3 (linear), 0.161 (exponential)
              = 20.000 − 1.667 = 18.333 ns      Per_t_DD,min,pure = 0.333 ns
```

**`Per_t_DZ` is exempt and is used raw.** Its meaning is the instant the driver goes high-Z,
which is abrupt — there is no ramp portion inside it to remove.

**A SWI3S Manager needs no conversion.** The Manager Output Timing figure's anchors are symmetric
(clock 20 %, data 20 %), so the two portions cancel and the tabulated value is already pure. Only
the Peripheral side (the Peripheral Output Timing figure, asymmetric) and any SoundWire side are
converted.

## The Output Timing table's wording

The Output Timing table says, in words:

- `Per_tZD` — "Time from high-Z to **the start point on the V-Ramp or G-Ramp**"
- `Per_tDD` — "Peripheral Output Hold Time (**earliest change** in data output after edge on
  clock input …)"

Read literally that is a 0 % anchor, which would make the conversion a no-op. The figure
defines the measurement. A G-Ramp has a 20 %-of-G waypoint, which is what
`per_tdd_offset_frac` uses for the exponential shape.

## Where the model applies it

| where | what it does |
|---|---|
| `keeper_Man` / `keeper_Per` | **pure** `t_DD` − full swing, so the ramp's first fifth is not counted twice. `keeper_Man` is unaffected by the conversion, its anchors being symmetric |
| `_per_ede_release_vs_edge_ns` | a peripheral's EDE release is **pure** `t_DD` + full swing + hold interval − UI, the same pairing as the keeper legs. It is the `Per_t_DZ,EDE` term of `PM_contention` and `PP_contention`, and that term's note lists the four parts |
| `pure_output_delay` | multiplies by the spec's **reference** `t_RF` (5.0 SWI3S, 5.4 SW-1V8), not the operating one: the number was measured at the test condition, so the portion embedded in it is fixed |
| `_conv_note` | prints the conversion with that reference `t_RF` and its coefficients as numbers, so the note recomputes to the pure value it shows |
| `output_anchor_frac`, `per_tdd_offset_frac` docstrings | state that the figure governs and the table text is inaccurate |

Tests pinning it: `test_the_keeper_leg_charges_the_launch_and_the_swing_without_overlapping_them`
(the two spellings of the interval must agree, and `t_DZ` stays raw),
`test_the_peripheral_ede_release_charges_the_launch_and_the_swing_without_overlapping_them`,
`test_the_pure_back_out_is_a_fixed_offset_not_a_function_of_the_operating_slew` and
`test_a_conversion_note_reproduces_the_pure_value_it_shows`, all in
`tests/test_timing_cross_spec.py`.

## The cross-check against the reference

`tests/test_timing_vs_reference.py` imports the reference and asserts every leg pair under one
matched configuration: four agree to four decimals, the Manager keeper leg agrees at *both*
release placements, the peripheral-launched ones differ by exactly this anchor conversion,
P→P **hold** differs by the anchor alone, and P→P **contention** with a conventional release
differs by the slew split alone. The slew split is the one remaining disagreement: the
reference splits the clock's slew between the two peripherals and this model does not, because
the clock is driven from a single Manager pin, so one edge with one slew serves both
detections. With a releasing EDE peripheral, P→M and P→P contention also differ by the anchor
conversion, because the EDE release contains the launch; P→P carries the slew split on top.
Each delta is declared, so one that moves fails. It skips when the reference project is not beside this one
(override with `SWI3S_TIMING_ANALYSIS`); it is not vendored, because a stale copy asserting
agreement is worse than no test.

Rules the cross-check depends on, and what it pins:

- **A matched configuration names the specification's parameter**, not a value read out of
  the artefact under test: two copies of one error are indistinguishable from a cross-check
  passing. `Man_t_DZ` is its own tabulated parameter at 0–10 ns, not the reference's launch
  range `E.MAN_ANALOG_MAX`.
- **The receiver threshold corners per part.** Two receivers can sit at opposite ends of the
  V_IH compliance window at once, so the corner search keeps that window open on the three
  legs that net two peripherals' crossings. With a releasing EDE peripheral, P→P contention
  reads +0.59 here against the reference's −1.08 at a single slew; the 1.667 ns between them
  is the anchor conversion in the EDE release, and the slew split is the only other live
  divergence.
- **The reference prices only the t_ZD launch**, so it has no continuous-transmission
  setup/hold leg.
- **`PP_hold_ho` (the reference's `hold_pp`) bounds the bus.** The inequality names only the
  device turning on out of high-Z and the device whose hold window that ends, so when the
  **Manager** released, the level is Manager→peripheral data and a peripheral sink must hold
  it. At zero handover UIs it reads −7.67 ns at this model's worst corner in the EDE column,
  and −5.26 at the matched configuration against the reference's −3.60 there; the matched pair
  differs by the declared anchor conversion.
  `tests/test_timing_cross_spec.py::test_the_out_of_scope_split_matches_the_legs_own_terms`
  states the rule and checks it against the terms. `PP_hold` is excluded from what bounds the
  bus, because its data is peripheral-to-peripheral and not guaranteed.
- **`keeper_Per`'s `1·UI + Per_t_DZ` is evaluated at its corner.** The keeper leg wants the
  EARLIEST release, and t_DZ is specified only as a maximum, so t_DZ = 0 is legal and is the
  corner that starves the keeper; the corner search finds it (`picks["Per_t_DZ"] == "min"`).
  The reference's `keeper_per_conventional` hardcodes that corner by omitting the term. At
  that corner the two legs differ by the anchor conversion alone; compare them there, not a
  hardcoded worst case against a term at its NOMINAL.

## Still open, and owned elsewhere

`timing-analysis/swtiming/emit_ede.py` has no `offset()` layer: it follows the 0 % reading
(see its ANCHOR note, and `MODEL_AUDIT.md` §2 "Acted on in `emit_ede.py`"). Under this ruling
the layer belongs back, which moves every peripheral-launch leg in the EDE paper by 1.667 ns
and regenerates `docs/ede_numerics.tex`. The Timing Analysis paper
(`SWI3S_Timing_Analysis.tex` §2.3) already agrees with this ruling.

**That change is not made here.** It belongs to the paper's author and changes a published
numeric appendix.
