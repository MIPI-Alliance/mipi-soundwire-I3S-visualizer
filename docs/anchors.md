# Measurement anchors: what a tabulated timing number contains

This is the reconciliation of record for where each tabulated timing number is measured from
and to, and how the calculator places it.

The live calculator is `swi3s_studio/timing/`. The reference analysis it is cross-checked
against is vendored as `tests/fixtures/timing_reference.json` (the values it computes, with the
module and revision they came from), and `tests/test_timing_vs_reference.py` states each
declared delta.

## The ruling

The SWI3S draft's figures and tables are cited by caption, not number, because the numbers
move between drafts; its section numbers are a guide to where they sit. The SoundWire 1.3
figure and table numbers are those of a published document. The figures are the "PHY1 & PHY2 Measurement" figures for Peripheral Output Timing,
Manager Output Timing, Peripheral Input Timing and Manager Input Timing (§11.2.7). The tables
are "PHY1 & PHY2 Output Timing", "PHY1 & PHY2 Input Timing" and "PHY1 & PHY2 Output Ramp
Time" (§11.2.3).

**The figures govern. The Output Timing table's wording is inaccurate.** "20 %" below is the
V_OL crossing on a rising edge and the V_OH crossing on a falling edge.

| times | clock end | data end | slew it holds at |
|---|---|---|---|
| `Per_t_DD`, `Per_t_ZD` | clock input at V_IH / V_IL | 20 % of the data ramp | every t_RF of the ramp setting |
| `Per_t_DZ` | clock input at V_IH / V_IL | 10 % V_SEOS from the settled level | no ramp |
| `Man_t_DD`, `Man_t_ZD` | clock output at 20 % | 20 % of the data ramp | every t_RF of the ramp setting |
| `Man_t_DZ` | clock output at 20 % | 10 % V_SEOS from the settled level | no ramp |
| `Per_t_IS`, `Per_t_IH` | clock input at V_IH / V_IL | data at V_IH / V_IL | a clock of exactly 5.0 ns |
| `Man_t_IS`, `Man_t_IH` | clock output at 20 % | data at V_IH / V_IL | every clock t_RF of the setting |

- **Output timing holds at every t_RF of the ramp setting**, 3.0–8.3 ns for the 5 ns setting.
  The 20 % point is therefore bounded whatever the device's split between delay and ramp,
  and the ramp from there is whatever t_RF is.
- **The Manager's clock end is its own output**, 20 % into its clock ramp (V_OL rising, V_OH
  falling), at every clock slew of the setting, for its output and its input timing alike: the
  Manager sources the clock. It is measured at the clock output under the ideal test load
  (the "PHY1 & PHY2 Output Timing" table's note), not at a clock fed back from the pin.
- **The UI runs between two consecutive Manager clock references.** Every edge is placed by
  its 20 % point: a launch at its anchor, a clock edge at its reference, and one clock edge's
  references at two pins a flight apart, whatever each ramp's slew. The spec gives the clock a
  duty cycle (45–55 %, "PHY1 & PHY2 Clock Frequencies") with no measurement point, and no high
  time, low time or edge spacing; the calculator's UI is that duty floor measured between
  references.
- **A Peripheral's input timing is measured with a 5.0 ns clock**, the one slew an input-only
  test can always supply. Both of its ends are threshold crossings, so the crossing envelopes
  charge the operating slew.
- **Hold ends where the input could first toggle**: falling data at V_IL,max, rising data at
  V_IH,min, each widened by the supply and noise corners.
- **One V_SEOS per device.** The Manager, the Peripheral (A on P→P) and Peripheral B each
  sit at one supply in a leg: a crossing takes its transmitter's supply as its ramp's rail and
  its receiver's for the threshold. Every leg is evaluated at each assignment of the three
  supplies to V_SEOS ± tolerance and keeps its smallest margin; the assignment is the leg's
  supply corner.
- **A releasing driver holds its level for the keeper's response, counted at the Manager.**
  The keeper is in the Manager, so its spec sets the rule, and the "PHY1 & PHY2 Bus Keeper
  Behavior" table's Man_tKeeper_Response (shown on the page as Man_t_Keeper_Response, the t
  subscripted as in every timing symbol) is measured from the keeper's input crossing V_IH /
  V_IL to its output current changing sign. The keeper's input is at the Manager pin, so its
  threshold is the Manager's, at the Manager's supply, at the end that starves the keeper:
  V_IH,max on rising data and V_IL,min on falling data, the latest crossings after the launch's
  anchor. A Peripheral's data reaches that input swinging to the Peripheral's supply and offset
  by the ground shift α, as every P→M crossing is priced; the Manager's own data carries no
  ground shift. The margin is the release less the data's crossing of that threshold and the
  keeper's response, both counted from the edge that opens the driven UI: `X_K,rise,late ·
  <dev>_t_RF,DATA` (`X_K,fall,late` on falling data) is the crossing from the anchor. No t_PD
  enters: the level and the high-Z both reach the Manager one flight later. The release is set by the releasing driver's mode:
  conventionally 1·UI + t_DZ, and with EndDriveEarly t_DD + its own ramp to V_OH / V_OL + the
  EDE Delay, so the EndDriveEarly leg charges the crossing and credits the ramp. A SoundWire
  keeper takes its own thresholds, V_TH_Keeper,max (65 % of the larger of the keeper's Vdd and
  VddNom, rising) and V_TL_Keeper,min (35 % of the smaller, falling), for t_Keeper_Delay,max
  (SoundWire 1.3 Figures 18 and 19, Tables 12–14), the same way. t_OH_Data applies to a
  self-timed turn-off, which the calculator does not model.
- **A contention turn-on is the acquirer's 20 % point**, where t_ZD is measured to.
  Conduction before it is not counted.
- **t_DZ has no ramp in it** and is used as tabulated.
- **The per-edge slew tolerance τ applies to the whole edge.** One lane's two edges ramp at
  its t_RF row times (1 + τ) (late) and (1 − τ) (early), and no edge leaves its spec's range
  (3.0–8.3 ns for the 5 ns setting; t_Slew_Clock's min and max for SoundWire): at an end that
  edge stays at the end and the other takes the whole 2τ mismatch. A crossing and the ramp from
  an anchor to V_OH take the factor of the edge they are on.
- **The ramp from an anchor to V_OH / V_OL is the ramp's own crossing time**: one t_RF on a
  linear ramp, which t_RF spans from V_OL to V_OH; on an exponential ramp, whose t_RF is its
  20-80 % time, the time the ramp takes between those two levels, which differs from one t_RF
  where V_OL / V_OH is not 0.20 / 0.80 and differs by polarity where V_OH + V_OL ≠ 1.
- **A Peripheral timing counts from that Peripheral's own detection of its clock edge.** Per_t_DD, Per_t_ZD and Per_t_DZ are each measured from the Peripheral's input crossing V_IH (a rising clock edge) or V_IL (a falling one); the "PHY1 & PHY2 Measurement" figure for Peripheral Output Timing shows all three. Where a leg counts two of them across two edges, as keeper_Per counts Per_t_DD from the edge that opens the UI and Per_t_DZ from the closing edge, the leg carries the spacing of the two detections: `1·UI` between the Manager references plus the closing detection's crossing less the opening one's. The keeper is starved by the opening edge detected late and the closing edge, of the other polarity, early, both at the Peripheral's one supply and ground shift; the leg is evaluated at both polarities of the closing edge and reports the worse. A Manager's timings count from its clock reference, so keeper_Man carries no such term, and an EndDriveEarly keeper leg counts its release and the keeper's crossing from its own launch's anchor.

## The Output Timing table's wording

The Output Timing table says, in words:

- `Per_tZD` — "Time from high-Z to **the start point on the V-Ramp or G-Ramp**"
- `Per_tDD` — "Peripheral Output Hold Time (**earliest change** in data output after edge on
  clock input …)"

Read literally that is a 0 % anchor. The figures draw both to 20 % of the ramp, and define
the measurement. The anchor is the ramp's V_OL crossing on a rising edge and its V_OH crossing
on a falling one (`per_tdd_offset_frac`), which is the 20 % point at V_OL 0.20 and V_OH 0.80.

## Where the model applies it

Every crossing coefficient counts from its edge's 20 % point, so a crossing term `X · t_RF` is
the ramp from that point to the receiver's threshold. The ramp before the 20 % point is f_A · t_RF:

```
f_A · t_RF      rising:  linear V_OL / (V_OH − V_OL),   exponential −ln(1 − V_OL) / ln 4
                falling: linear (1 − V_OH) / (V_OH − V_OL),  exponential −ln(V_OH) / ln 4
```

At V_OH 0.80 and V_OL 0.20 that is f_A = 1/3 (linear) and 0.161 (exponential), the values
the rest of this page quotes. The calculator reads V_OH and V_OL from its inputs. f_A enters
no leg: the crossing geometry is computed from the start of the ramp and counted from f_A of
the way into it.

| where | what it does |
|---|---|
| `_launch_anchor_ns`, `_launch_terms` | a launch is its row value, the instant its ramp reaches 20 %, at every slew. A SoundWire side adds `sw13_anchor_correction` (below), at the polarities of the launched data edge and of the Manager clock edge it is launched from (`sw_anchor_ns`). The term is `<dev>_t_<DD/ZD>,anchor` and names the edges it is placed on (`anchor_symbol`), whether or not their corrections differ (they differ where V_OH + V_OL ≠ 1): a Peripheral's data edge, `,anchor,rise` / `,anchor,fall`, and a Manager's clock edge then data edge with the case letters, `,anchor,rr` / `,rf` / `,fr` / `,ff` (r rising, f falling), then `,fast` on the max corner where it is priced on the fast data edge (the side's DATA lane in `CalcInputs.fast_data_edge`, `_sw_anchor_slews`). One symbol has one value at a corner |
| `edge_factor` | one edge's slew factor, (1 ± τ) inside its spec's range (`trf_range_ns`); `tau_adj` corrects the envelope's own (1 ± τ) where the range binds |
| `pp_clock_edges` | the slew factor of each P→P clock detection's edge: one factor for both, the worse for the leg, where they detect one edge (`pp_shares_clock_edge`), each corner's own otherwise |
| `CalcInputs.envelope` | every crossing coefficient from its edge's 20 % point (`from_anchor`): a data crossing from the launch's anchor, a clock detection from the Manager's clock reference on the same edge |
| `UI_ns` | the duty floor, between two consecutive Manager clock references |
| `timeline.leg_timeline` | places each ramp so its 20 % point is where the calculator puts it |
| keeper legs | the release waits for t_DD, the anchor, then a late edge's crossing of the keeper's threshold at the Manager (`keeper_corners`, `_keeper_case`): the Manager's V_IH,max rising and V_IL,min falling at its supply, or a SoundWire Manager's V_TH_Keeper,max / V_TL_Keeper,min, on a ramp to the releaser's supply, a Peripheral's data offset by the ground shift α, `X_K,rise,late · <dev>_t_RF,DATA` on rising data and `X_K,fall,late` on falling; and then the keeper's response. An EndDriveEarly release also credits its ramp to V_OH / V_OL (`_to_voh_ns`) and its EDE Delay; keeper_Per counts the Peripheral's t_DD and t_DZ from its own detections of the opening and closing edges, so it carries the spacing of the two, `−(X_MP,rise,late − X_MP,fall,early)·Man_t_RF,CLK` when the closing edge is falling and `−(X_MP,fall,late − X_MP,rise,early)·Man_t_RF,CLK` when it is rising, each detection named for its direction, Manager to Peripheral, and its edge |
| `to_voh_frac`, `valid_edge_rising`, `to_voh_symbol` | the ramp from a launch's anchor to V_OH rising / V_OL falling, in units of the edge's t_RF: 1 on a linear ramp; on an exponential ramp, whose t_RF is its 20-80 % time, the ramp's own crossing time, (ln(1 − V_OL) − ln(1 − V_OH))/ln 4 rising and (ln V_OH − ln V_OL)/ln 4 falling (`ramp_to_valid_frac`), 1 at V_OL / V_OH = 0.20 / 0.80. An EndDriveEarly release on a contention leg takes it on the data edge that reaches V_OH / V_OL the later after the clock reference: the edge whose ramp is the longer, or on a SoundWire side the edge whose anchor plus ramp is the later. A keeper leg takes its data edge from the keeper's crossing: a conventional release the edge whose anchor plus keeper crossing is the later, rising on a tie, and an EndDriveEarly leg the edge whose keeper crossing less this ramp is the larger (`_keeper_case`). The term is `<dev>_t_RF,DATA,rise` or `<dev>_t_RF,DATA,fall`, naming the edge it is priced on whether or not the two polarities' crossing times differ (they differ on an exponential ramp at V_OH + V_OL ≠ 1), then `,fast` where it is priced on the fast data edge (a lane in `CalcInputs.fast_data_edge`), so one symbol has one value |
| `_ede_release_terms` | an EndDriveEarly release is `t_DD + (ramp to V_OH / V_OL) + EDE Delay − UI` from the closing edge, on either side; a Manager's is measured from its clock reference. A contention leg shows it as its parts in time order from the edge that opens the driven UI: `<dev>_t_DD` (and its `<dev>_t_DD,anchor,…` on a SoundWire side), the ramp `<dev>_t_RF,DATA,rise` / `,fall` to 80 %, `<dev>_t_EDE`, then `UI` back to the closing edge. `_ede_release_vs_edge_ns` is its closed form, which the tests check the terms against; no leg reads it |
| contention legs | the turn-on is the acquirer's t_ZD (its 20 % point) and, for a Manager, its clock reference. On a SoundWire acquirer the turn-on takes the data edge whose anchor is the earlier (`turn_on_rising`), rising on a tie |
| `sw13_anchor_correction` | a SoundWire side's rows hold its SWI3S equivalents, derived at the slews `sw13_equivalent_slews` names: on the max side k = s = t_Slew_Clock,max for the system size (5.0 ns small, 6.0 ns large); on the min side k = t_Slew_Clock,min, and s the reference slew on a Peripheral or t_Slew_Clock,min on a Manager. The correction is 0 at those slews and moves each launch's 20 % point to the operating slews by SoundWire's own anchors: t_OV ends at 80 % of the data transition, t_ZD_Data at low impedance, and a Manager's are measured from its clock's 65 % / 50 % point. Each ramp is placed at its own polarity (`sw13_ramp_fracs`): the data ramp's anchor is its V_OL crossing rising and its V_OH crossing falling, 80 % of the transition is 0.80 of the rail rising and 0.20 falling, and the clock's 65 % / 50 % points are V_TP_Clock rising and 35 % / 50 % (V_TN_Clock) falling. The two polarities agree where V_OH + V_OL = 1, where the rising placement is taken for both. A setup or hold case gives the data and clock edges; the P→M envelopes select their case with each case's anchor added; an EndDriveEarly release on a contention leg takes the edge that leaves the later valid level, a keeper leg the edge whose keeper crossing is the later, and keeper_Man the opening edge that places its launch the later (`keeper_open_rising`) |

A receiver threshold may lie on either side of a launch's anchor: on a SoundWire data input at
the supply and noise extreme, falling data crosses V_IL before the transmitter's 80 % point.
`t(V) = t_A + (V − V_A)/swing · t_RF` holds either side, and the corner search finds the slew
that binds.

Tests pinning it, in `tests/test_timing_cross_spec.py`:
`test_a_launch_is_placed_at_its_anchor_at_every_slew`,
`test_a_clock_crossing_counts_from_the_manager_s_clock_reference`,
`test_the_keeper_leg_charges_the_launch_at_its_anchor_and_its_crossing_at_the_manager`,
`test_an_ede_release_is_the_launch_plus_t_rf_plus_the_ede_delay`,
`test_an_ede_release_shows_its_parts_in_time_order`,
`test_an_exponential_ramp_to_v_oh_is_its_true_crossing_on_keeper_and_ede_legs`,
`test_a_keepers_ede_leg_takes_its_crossing_and_ramp_on_one_data_edge`,
`test_a_keepers_crossing_is_its_closed_form_on_either_shape`,
`test_one_ramp_symbol_has_one_value_on_every_leg_at_every_supply_assignment`,
`test_one_anchor_symbol_has_one_value_on_every_leg_at_every_supply_assignment`,
`test_every_soundwire_anchor_is_its_launchs_placement_at_the_edges_it_is_drawn_on`,
`test_an_edge_never_leaves_its_ramp_setting` and
`test_an_anchor_correction_note_reproduces_its_value`.

## The cross-check against the reference

`tests/test_timing_vs_reference.py` reads the reference's values from the fixture and asserts
every leg pair but the reference legs `_UNCOMPARED_LEGS` declares (keeper_per_ede, a constant
in the reference; keeper_response_man, the Bus Keeper Behavior table's form of keeper_man, which is compared;
keeper_man_retired_launch and keeper_window_opens, which are not legs of this model) under one
matched configuration (12.288 MHz, 30 cm, 8.3 ns slews except where a leg's own defaults say
otherwise), each delta declared. The reference places every launch at the start of its ramp
and every Manager time at the start of the Manager's clock, and moves each edge ±τ with no
range limit, so the file compares with this model's range limit lifted. The declared deltas,
each placement on its edge's (1 ± τ):

| divergence | size | legs |
|---|---|---|
| a peripheral launch's anchor | t_RF/3 at the leg's slew | every peripheral-launched leg |
| the Manager's clock reference | t_RF,CLK/3 | P→M setup and hold, the mixed contention legs |
| a Manager launch's clock reference less its data anchor | 0 on edges of one slew factor; 2τ·t_RF/3 with the reference on a late edge and the anchor on an early one | Manager-launched setup (0) and hold (2τ·t_RF/3) |
| the EDE release | (1/3 − τ)·t_RF: the reference reaches 80 % 4/3·t_RF after the tabulated launch, this model a late edge's (1 + τ)·t_RF after it | P→M and P→P contention with a releasing EDE peripheral |
| the keeper's threshold criterion | with the anchor, (5/3 − c_K·(1 + τ))·t_RF, c_K the crossing of the keeper's threshold from the anchor in units of t_RF: 0.75 on keeper_Man, the Manager's own V_IH,max on its own rail, and 0.964 on keeper_Per at the matched configuration, the Manager's V_IL,min − α at 0.95 V_SEOS on a Peripheral rail of 1.05 | both keeper legs: the reference counts the keeper's response from the rail, this model from the data's crossing of the Manager's V_IH,max / V_IL,min at the Manager |
| the keeper_Per detection spacing | −(c_late,open − c_early,close)·t_RF,CLK at the leg's supplies; −7.92 ns at the matched configuration | keeper_Per with a conventional release: this model counts the Peripheral's t_DD and t_DZ from its own detections of the opening and closing edges, the reference takes them one UI apart |
| the P→P slew split | 2.617 ns | P→P contention |
| the clock's edge spacing | f_A·(t_RF,late − t_RF,early) = 2τ·t_RF,CLK/3 | M→P, P→M and P→P setup, and P→P contention with a releasing EDE peripheral: the reference spaces ramp starts, this model 20 % points |
| a shared edge's slew factor | [f·(c_credit − c_charge) − ((1 − τ)·c_credit − (1 + τ)·c_charge)]·t_RF,CLK, each c counted from the ramp's start and f the edge's factor; 0.48 ns at the matched configuration | P→P hold (`hold_pp`) and P→P contention with a conventional release: their two clock detections are of one edge, which this model takes at its one slew factor, the worse for the leg, and the reference at each corner's own |
| the supply split | the reference's crossings at this model's one-supply-per-device assignment less those at per-crossing supply corners, ≥ 0; at the matched configuration 1.75 ns on P→M setup, 0.99 on P→P setup, 0.43 on P→P hold and contention, 1.05 on M→P contention and on P→P contention with a releasing EDE peripheral | P→M setup, P→P setup, hold and contention: the reference corners each crossing's supplies on their own, so one device can sit at both extremes; M→P contention and EDE P→P contention, whose reported case is another polarity at that case's worst supplies |
| the polarity case | the leg's reported crossings less the reference's construction of them, at the leg's supplies; at the matched configuration −4.15 ns on P→M setup, −1.38 on M→P contention and on P→P contention with a releasing EDE peripheral | P→M setup: Sigma_cross,PM,setup's cross case, a rising clock with falling data, whose ground shift delays both crossings; the contention legs: a falling closing edge. The reference puts every clock crossing on a rising edge and P→M setup's data on the clock's polarity |

- **Manager-launched setup** (`MP_setup_ho`) differs by the edge spacing alone: the Manager's
  reference and its data anchor cancel while its two lanes share a slew and both sit on late
  edges. On Manager-launched hold the anchor is on an early edge, 2τ·t_RF/3 apart.
- **The slew split** is the one disagreement rather than a placement. The reference corners
  the two peripherals of a P→P leg at opposite ends of the clock's slew band; this model takes
  one slew, because the clock is driven from a single Manager pin, so one edge with one slew
  serves both detections. Where the two detections are of one edge, this model also takes
  both at that edge's one slew factor, (1 + τ) or (1 − τ), whichever leaves the leg the less,
  each with its own crossing coefficient; the reference takes each at its corner's own
  factor. P→P contention reads −18.48 ns in the reference, and −14.96 here with the
  slew-range limit lifted and −14.63 with the limit in force; the lifted gap is the slew split (2.62), the
  shared edge's slew factor (0.48) and the supply split (0.43) together.
- **The EDE release.** The reference holds an EDE peripheral's level for a hold interval
  after the ramp reaches the rail; this model holds it for the EDE Delay after the ramp's 80 %
  point. With the EDE Delay set to the reference's hold plus the ramp's last 20 % (t_RF/3),
  the release here is (1/3 − τ)·t_RF earlier: the reference reaches 80 % 4/3·t_RF after the
  tabulated launch, this model one late edge's (1 + τ)·t_RF after it. The reference's mid-UI
  grid placement of a Manager EDE release has no counterpart here.

Each delta is declared, so one that moves fails. The fixture holds every value a test reads
from the reference, so the legs are checked wherever this tree is. When the reference checkout
is present (`SWI3S_TIMING_ANALYSIS` names it, else the directory named timing-analysis beside this tree; a swtiming directory there that does not import fails the check rather than skipping it),
`test_the_fixture_matches_the_live_reference` also checks the fixture against it and fails,
naming the values that differ, when it is stale. The fixture is rewritten only by running
`PYTHONPATH=. python3 tests/test_timing_vs_reference.py --regenerate`, and the difference is
read before it is kept.

Rules the cross-check depends on, and what it pins:

- **A matched configuration names the specification's parameter**, not a value read out of
  the artefact under test: two copies of one error are indistinguishable from a cross-check
  passing. `Man_t_DZ` is its own tabulated parameter at 0–10 ns, not the reference's launch
  range `E.MAN_ANALOG_MAX`.
- **The receiver threshold corners per part.** Two receivers can sit at opposite ends of the
  V_IH compliance window at once, so the corner search keeps that window open on the three
  legs that net two peripherals' crossings.
- **The reference prices only the t_ZD launch**, so it has no continuous-transmission
  setup/hold leg.
- **`PP_hold_ho` (the reference's `hold_pp`) bounds the bus.** The inequality names only the
  device turning on out of high-Z and the device whose hold window that ends, so when the
  **Manager** released, the level is Manager→peripheral data and a peripheral sink must hold
  it. At zero handover UIs it reads −5.69 ns at its worst corner in the Timing view's EDE
  Proposed column, both sides on SWI3S PHY2 EDE, at any rate (the leg has no UI term there).
  At the cross-check's matched configuration, with Per_t_ZD at its minimum, it reads −5.04
  with the slew-range limit and −5.32 without it, against the reference's −3.60. The matched
  pair differs by three declared deltas: the peripheral's anchor on an early edge (−2.63), the
  shared edge's slew factor (+0.48) and the supply split (+0.43).
  `tests/test_timing_cross_spec.py::test_the_out_of_scope_split_matches_the_legs_own_terms`
  states the rule and checks it against the terms. `PP_hold` is excluded from what bounds the
  bus, because its data is peripheral-to-peripheral and not guaranteed. B samples on either
  polarity and the leg reports the worse case, as every setup and hold leg does. A launches
  on the edge its count gives after B's: B's polarity at an even count and the other at an
  odd one, detected early at V_IH (`X_MP,A,rise,early`) when rising and at V_IL
  (`X_MP,A,fall,early`) when falling. The figures above are at zero handover UIs, where A
  launches on B's edge.
- **`keeper_Per`'s `1·UI + Per_t_DZ` is evaluated at its corner.** The keeper leg wants the
  EARLIEST release, and t_DZ is specified only as a maximum, so t_DZ = 0 is legal and is the
  corner that starves the keeper; the corner search finds it (`picks["Per_t_DZ"] == "min"`).
  The reference's `keeper_per_conventional` hardcodes that corner by omitting the term. At
  that corner the two legs differ by the declared threshold criterion and the detection spacing alone; compare
  them there, not a hardcoded worst case against a term at its NOMINAL.

## Still open

The spec's clock duty cycle ("PHY1 & PHY2 Clock Frequencies") names no measurement point; this
model's reading of it (between clock references) is an assumption until the spec states one.
