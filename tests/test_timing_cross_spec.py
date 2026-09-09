"""Cross-spec (SWI3S ↔ SoundWire 1.3) timing tests.

Three things are pinned here:

1. **Non-regression.** With both sides SWI3S the calculator is bit-identical to
   the single-spec behaviour it had before the spec selector existed. The
   cross-spec work must not perturb the validated SWI3S path.

2. **Agreement with the `swtiming.interop` oracle** on all four setup legs, to
   within the two models' tPD-convention difference.

3. **The anchor conversion that got them there.** Porting the mapping surfaced a
   defect in the oracle: it converted the SWI3S clock-to-output to a pure delay
   but fed SoundWire's t_OV in raw, double-counting 7.20 ns of output slew on
   every leg where the SoundWire device transmits. That moved two published
   margins (A/PM setup −14.28 → −7.08; B/MP setup −3.04 → +4.16, a sign flip)
   and the binding setup leg in Scenario B. Fixed upstream; ORACLE below carries
   the corrected values, and ``test_the_conversion_is_worth_the_whole_anchor_offset``
   pins the mechanism so a partial application cannot pass.

Run: PYTHONPATH=. python3 -m pytest tests/test_timing_cross_spec.py
"""
import math
from dataclasses import replace as dc_replace

import pytest

from swi3s_studio.timing import (
    CalcInputs,
    CalcResults,
    CalcRow,
    ParamRange,
    apply_row_picks,
    compute,
    default_swi3s_rows,
    find_worst_corner_rows,
)
from swi3s_studio.timing.spec_source import (
    SPEC_SOURCES,
    LaunchMode,
    clock_anchor_frac,
    default_handover_UIs,
    fclk_ceiling_MHz,
    is_soundwire,
    launch_mode_max_fclk_MHz,
    launch_mode_tDD_ns,
    output_anchor_frac,
    preset,
    pure_output_delay,
    recommended_launch_mode,
    schmitt_delta_ns,
    snap_to_launch_grid,
    ui_relative_limit_ns,
)

# Baseline matching swtiming's canonical corner: 30 cm, 13.2 MHz, small/1.8 V.
BASE = dict(bus_length_cm=30.0, F_CLK_target_MHz=13.2)


def _mixed(man_spec, per_spec, **kw):
    """A CalcInputs with each side seeded from its own spec's preset."""
    m = preset(man_spec, "Man")
    p = preset(per_spec, "Per")
    return CalcInputs(
        Man_spec=man_spec, Per_spec=per_spec,
        Man_t_DD_min_ns=m.t_DD_min_ns, Man_t_DD_max_ns=m.t_DD_max_ns,
        Per_t_DD_min_ns=p.t_DD_min_ns, Per_t_DD_max_ns=p.t_DD_max_ns,
        Man_t_IS_max_ns=m.t_IS_max_ns, Man_t_IH_max_ns=m.t_IH_max_ns,
        Per_t_IS_max_ns=p.t_IS_max_ns, Per_t_IH_max_ns=p.t_IH_max_ns,
        Man_t_DZ_max_ns=m.t_DZ_max_ns, Per_t_DZ_max_ns=p.t_DZ_max_ns,
        Man_t_ZD_ns=m.t_ZD_ns, Per_t_ZD_ns=p.t_ZD_ns,
        tRF_Man_CLK_ns=m.tRF_ns, tRF_Man_DATA_ns=m.tRF_ns,
        tRF_Per_DATA_ns=p.tRF_ns,
        **{**BASE, **kw},
    )


# ---------------------------------------------------------------------------
# 1. Non-regression
# ---------------------------------------------------------------------------

def test_swi3s_defaults_unchanged_by_the_spec_selector():
    """The default CalcInputs is SWI3S on both sides and must be untouched.

    These margins are the pre-change values, transcribed. The selector defaults
    to SWI3S/SWI3S precisely so the validated single-spec path is the default
    path; if this moves, the cross-spec work has leaked into it.

    """
    r = compute(CalcInputs())
    expect = {
        "MP_setup": +6.955, "MP_setup_ho": +14.955, "MP_hold": -2.636,
        "PM_setup": -3.815, "PM_setup_ho": +6.185, "PM_hold": +9.073,
    }
    for ineq, want in expect.items():
        assert r.breakdowns[ineq].margin_ns == pytest.approx(want, abs=5e-4), ineq
    # No cross-spec advisories fire on an all-SWI3S bus.
    assert r.schmitt_cost_ns == 0.0
    assert r.caveats == ()
    assert r.F_CLK_legal and r.F_CLK_ceiling_MHz == 13.2


def test_every_leg_displays_the_margin_it_reports():
    """The equation on screen must SUM to the margin beside it, on every leg and every spec.

    A setup leg computes its margin from a `*_required` expression and builds its term list
    SEPARATELY — two spellings of one inequality, with nothing tying them together. Change one
    and the page shows an equation whose terms do not add up to the number it prints, which is
    the worst kind of wrong for a tool whose whole output is "here is the arithmetic".

    Found while sabotage-testing the reference cross-check: a 0.5 ns error injected into a
    displayed Term left every margin untouched, so no test noticed. The cross-check compares
    margins, and could not see it either. This closes that side.

    Hold legs sum their terms to get the margin, so for them this is a tautology — asserted
    anyway, because which family a leg belongs to is not a property anyone should have to
    remember before trusting the display.
    """
    for spec in ("SWI3S_PHY2", "SWI3S_PHY2_PROP", "SW13_1V8"):
        for extra in ({}, {"handover_UIs": 0.0}, {"Man_ede": True, "Per_ede": True}):
            r = compute(_same(spec, **extra))
            for name, b in r.breakdowns.items():
                assert sum(t.value_ns for t in b.terms) == pytest.approx(b.margin_ns, abs=5e-9), \
                    (f"{spec} {name}{extra}: terms sum to "
                     f"{sum(t.value_ns for t in b.terms):+.4f} but the margin reads "
                     f"{b.margin_ns:+.4f} — the equation and the number disagree")


def test_the_keeper_leg_charges_the_launch_and_the_swing_without_overlapping_them():
    """A keeper leg must not charge the 0→20 % portion of the ramp twice.

    Figure 174 measures Per_t_DD from the clock crossing to 20 % OF THE RAMP, so a tabulated
    value already contains the first fifth of the transition. The leg then adds the FULL
    rail-to-rail swing to ask when the level is settled — so it must subtract the PURE launch,
    or that fifth is paid for twice. It was: the leg used the raw 20.00 ns with an 8.33 ns
    full swing, over-charging keeper_Per by exactly offset·t_RF,nom = 1.667 ns.

    No test caught it, because the conventional keeper_Per margin was never pinned to a
    number — only asserted positive, which it was either way. This pins the identity instead
    of the value: the two spellings of the same physical interval must agree.

    The Manager side is unaffected by construction — Fig. 176's anchors are symmetric, so its
    pure value IS its tabulated one — which is why the bug could only ever show on Per.
    """
    from swi3s_studio.timing.calculator import _per_tdd_pure_max, _swing_ns

    inp = CalcInputs()
    leg = compute(inp).breakdowns["keeper_Per"]
    launch = next(t for t in leg.terms if t.symbol.startswith("Per_t_DD"))
    swing = next(t for t in leg.terms if t.symbol.endswith("Per_t_RF,DATA"))

    # pure launch + full swing == raw launch + the 20 %-to-rail remainder. Same interval.
    remainder = (0.80 / 0.60) * inp.tRF_Per_DATA_ns
    assert -launch.value_ns - swing.value_ns == pytest.approx(
        inp.Per_t_DD_max_ns + remainder), "the keeper leg double-counts the ramp's first 20 %"
    assert -launch.value_ns == pytest.approx(_per_tdd_pure_max(inp))
    assert -swing.value_ns == pytest.approx(_swing_ns(inp, inp.tRF_Per_DATA_ns))
    assert "pure" in launch.symbol, launch.symbol

    # t_DZ is NOT converted anywhere: high-Z is abrupt, so there is no ramp portion in it.
    release = next(t for t in leg.terms if t.symbol.startswith("Per_t_DZ"))
    assert release.value_ns == pytest.approx(inp.Per_t_DZ_max_ns), \
        "t_DZ marks an abrupt event and must be used raw"


def test_the_pure_back_out_is_a_fixed_offset_not_a_function_of_the_operating_slew():
    """Per_t_DD,pure is 18.333 ns at EVERY slew, and the reason is what the number IS.

    The tabulated 20.00 ns was MEASURED at the spec's reference test condition, so the
    V=0→V_OL portion embedded in it is the one realised at THAT slew: offset·5.0 = 1.667 ns.
    Running the bus at a slower edge does not retroactively change what the measurement
    contained.

    It used to multiply by the OPERATING t_RF, and that moved margins in BOTH directions, so
    no single sign of pessimism made it obvious. At the 8.3 ns slow corner it reported
    Per_t_DD,max,pure as 17.233 — handing back 1.10 ns of delay the device never saves, on
    exactly the corner where PM setup is thinnest (−8.48 where the truth is −9.58) — while
    the min corner flowed the other way and flattered PM hold. The operating slew is
    charged, heavily and correctly, by the crossing envelopes; it must not also be credited
    here.
    """
    from swi3s_studio.timing.calculator import _per_tdd_pure_max, _per_tdd_pure_min

    for trf in (3.0, 5.0, 8.3):
        inp = dc_replace(CalcInputs(), tRF_Per_DATA_ns=trf)
        assert _per_tdd_pure_max(inp) == pytest.approx(18.3333, abs=1e-4), trf
        assert _per_tdd_pure_min(inp) == pytest.approx(0.3333, abs=1e-4), trf
    # ...and the offset is the tabulated value less the pure one, at the reference slew.
    assert 20.0 - _per_tdd_pure_max(CalcInputs()) == pytest.approx(1.6667, abs=1e-4)


def test_swi3s_manager_output_is_already_pure():
    """Man_t_DD is measured between symmetric anchors (Fig. 176), so a SWI3S
    Manager's tabulated value needs no conversion — while a SoundWire one does.

    The expected numbers did not move when `pure_output_delay` stopped taking an operating
    t_RF: every call here already passed the spec's OWN reference slew (5.0 SWI3S, 5.4
    SW-1V8), which is the value the function now reads for itself.
    """
    assert pure_output_delay(18.0, spec="SWI3S_PHY2", side="Man",
                             shape="linear") == 18.0
    # Per is asymmetric (Fig. 174) and does get the offset.
    assert pure_output_delay(20.0, spec="SWI3S_PHY2", side="Per",
                             shape="linear") == pytest.approx(18.3333, abs=1e-4)
    # A SoundWire Manager is NOT exempt — this is the case the old Per-only
    # hardcode silently got wrong. Both ends are re-anchored: −(1+1/3)·t_RF on the
    # data side (t_OV ends at V_OH) and +1.083·t_RF on the clock side (t_OV starts
    # at the clock's V_TP threshold, not its V=0). Net −0.25·t_RF = −1.35 ns.
    assert pure_output_delay(27.6, spec="SW13_1V8", side="Man",
                             shape="linear") == pytest.approx(26.25, abs=1e-9)


# ---------------------------------------------------------------------------
# 2. Anchor geometry
# ---------------------------------------------------------------------------

def test_anchor_gap_is_exactly_one_trf_under_both_ramp_shapes():
    """SoundWire t_OV ends at V_OH (80 %); SWI3S t_DD ends at V_OL (20 %).

    t_RF *is* the 20 %–80 % span, so the two anchors are exactly one t_RF apart
    whatever the ramp shape — while the absolute offset back to the start of
    slew is shape-dependent. Verified against SoundWire v1.3 Table 15, which
    defines t_OV_Data as measured "to: V_Data >= V_OH_Data_Min (High) or
    V_Data <= V_OL_Data_Max (Low)" and t_Slew_Data between those same two
    levels (V_OH_Data_Min = 80 % Vdd, V_OL_Data_Max = 20 % Vdd, Table 8).
    """
    for shape in ("linear", "exp"):
        gap = output_anchor_frac("SW13_1V8", shape) - output_anchor_frac("SWI3S_PHY2", shape)
        assert gap == pytest.approx(1.0, abs=1e-12), shape
    assert output_anchor_frac("SWI3S_PHY2", "linear") == pytest.approx(1 / 3, abs=1e-12)
    assert output_anchor_frac("SW13_1V8", "linear") == pytest.approx(4 / 3, abs=1e-12)
    assert output_anchor_frac("SWI3S_PHY2", "exp") == pytest.approx(0.1610, abs=1e-4)
    assert output_anchor_frac("SW13_1V8", "exp") == pytest.approx(1.1610, abs=1e-4)


# ---------------------------------------------------------------------------
# 3. The oracle comparison
# ---------------------------------------------------------------------------

# swtiming.interop at small/1.8 V, 30 cm, 13.2 MHz, with the SWI3S side on its
# CURRENT numbers. Scenario A = SWI3S Mgr + SW Per; B = the reverse.
#
# NOTE the revision: `interop.run_scenario` now defaults to swi3s_revision=
# "proposed", because the memo's Part II assumes the proposed timings throughout.
# So docs/numerics.tex's \interop* macros are PROPOSED values and no longer match
# this dict — reproduce these by passing swi3s_revision="current". The
# proposed-side equivalents are pinned in section 9 below.
ORACLE = {
    ("A", "MP", "setup"): +10.41, ("A", "MP", "hold"): -2.68,
    ("A", "PM", "setup"): -12.93, ("A", "PM", "hold"): +22.94,
    ("B", "MP", "setup"): -1.69,  ("B", "MP", "hold"): +3.72,
    ("B", "PM", "setup"): -2.01,  ("B", "PM", "hold"): +7.19,
}

# On MP the Manager transmits; on PM the Peripheral does.
#   Scenario A (SWI3S Mgr, SW Per): SoundWire is TX on PM.
#   Scenario B (SW Mgr, SWI3S Per): SoundWire is TX on MP.
SW_IS_TX = {("A", "MP"): False, ("A", "PM"): True,
            ("B", "MP"): True,  ("B", "PM"): False}

SCENARIOS = {"A": ("SWI3S_PHY2", "SW13_1V8"), "B": ("SW13_1V8", "SWI3S_PHY2")}


def _oracle_gap_ns(scenario: str, leg: str) -> float:
    """How far Studio should sit from the oracle on this leg.

    Now ZERO everywhere. It was (1 + offset) * t_RF = 7.20 ns on the legs where
    the SoundWire device transmits, because `interop._sw13_tx` fed the raw t_OV
    into a slot that must hold a pure delay. That defect was found by this port
    and has since been fixed upstream, so the two models converged.

    Kept as a function rather than inlined as 0.0: it is the hook that makes a
    future divergence explicit instead of silently loosening a tolerance.
    """
    return 0.0


@pytest.mark.parametrize("scenario", ["A", "B"])
def test_matches_oracle_where_soundwire_receives(scenario):
    """Where the oracle is sound, Studio must reproduce it.

    These are the legs on which the SoundWire device is the RECEIVER, so no
    clock-to-output normalisation is in play and the two models should agree.
    Setup only: the hold legs additionally depend on the Schmitt treatment,
    which is covered separately.
    """
    man, per = SCENARIOS[scenario]
    r = compute(_mixed(man, per))
    leg = "MP" if not SW_IS_TX[(scenario, "MP")] else "PM"
    got = r.breakdowns[f"{leg}_setup"].margin_ns
    want = ORACLE[(scenario, leg, "setup")]
    assert got == pytest.approx(want, abs=0.75), (
        f"{scenario}/{leg} setup: Studio {got:+.2f} vs oracle {want:+.2f}. "
        "The SoundWire side only receives on this leg, so the two models "
        "should agree to within the tPD-convention difference."
    )


@pytest.mark.parametrize("scenario", ["A", "B"])
def test_matches_oracle_where_soundwire_transmits(scenario):
    """The legs that found the bug, and now agree.

    SoundWire v1.3 Table 15 defines t_OV_Data as ending when the data reaches
    V_OH_Data_Min / V_OL_Data_Max -- 80 % / 20 % of Vdd (Table 8), i.e. the END
    of the slew. SWI3S t_DD ends at V_OL, the START of the valid swing. The
    inequalities need a *pure* delay ending at the start of physical slew,
    because the slew is already carried by Delta_cross / Sigma_cross.

    `swtiming.interop` converted the SWI3S side but fed `_sw13_tx`'s t_OV in
    raw, counting the full V=0 -> V_OH portion twice on any leg where SoundWire
    transmits -- (1 + 1/3) * 5.4 = 7.20 ns at this corner. Two independent
    derivations of the true clock-edge -> V_IH time agreed to 0.36 ns (the
    per-edge slew tolerance) while the oracle sat 7.2 ns above both:

        pure-delay form:   20.40 + 7.78 = 28.18 ns
        t_OV + 80%->V_IH:  27.60 + 0.22 = 27.82 ns
        oracle (was):      27.60 + 7.78 = 35.38 ns

    Fixed upstream, so ORACLE now carries the corrected values (A/PM -14.28 ->
    -7.08; B/MP -3.04 -> +4.16, a sign flip) and the two models agree here.
    """
    man, per = SCENARIOS[scenario]
    r = compute(_mixed(man, per))
    leg = "MP" if SW_IS_TX[(scenario, "MP")] else "PM"
    got = r.breakdowns[f"{leg}_setup"].margin_ns
    want = ORACLE[(scenario, leg, "setup")] + _oracle_gap_ns(scenario, leg)
    assert got == pytest.approx(want, abs=0.75), (
        f"{scenario}/{leg} setup: Studio {got:+.2f} vs oracle {want:+.2f}. "
        "Both models must reduce t_OV to a pure delay; a ~7.2 ns gap here means "
        "one of them has stopped."
    )


def test_the_conversion_is_worth_the_whole_anchor_offset():
    """Pin the mechanism, not just the agreement.

    Relabelling the SoundWire Manager as SWI3S makes its value "already pure"
    and skips both re-anchors. The difference must be the NET of the two:

        data side  −(1 + 1/3)·t_RF   (t_OV ends at V_OH, one t_RF past V_OL)
        clock side +1.083·t_RF       (t_OV starts at the clock's V_TP, not V=0)
        net        −0.25·t_RF        = −1.35 ns at t_RF = 5.4

    Asserted as the net rather than as one term, because for a day the model had
    only the data-side half and −7.20 ns looked like a complete answer.
    """
    inp = _mixed("SW13_1V8", "SWI3S_PHY2")           # Scenario B: SW is Mgr (TX on MP)
    converted = compute(inp).breakdowns["MP_setup"].margin_ns
    raw = compute(inp.__class__(**{**inp.__dict__, "Man_spec": "SWI3S_PHY2"}))
    unconverted = raw.breakdowns["MP_setup"].margin_ns
    net = (output_anchor_frac("SW13_1V8", "linear")
           - clock_anchor_frac("SW13_1V8", "linear")) * inp.tRF_Man_DATA_ns
    assert converted - unconverted == pytest.approx(net, abs=1e-9)
    assert converted - unconverted == pytest.approx(1.35, abs=1e-9)
    # and the two halves individually, so a future edit cannot drop one silently
    assert output_anchor_frac("SW13_1V8", "linear") * 5.4 == pytest.approx(7.20, abs=1e-9)
    assert clock_anchor_frac("SW13_1V8", "linear") * 5.4 == pytest.approx(5.85, abs=1e-9)


# ---------------------------------------------------------------------------
# 4. Advisories
# ---------------------------------------------------------------------------

def test_soundwire_caps_the_clock_below_the_swi3s_mandatory_rate():
    """No SoundWire configuration may legally clock at SWI3S PHY2's 13.2 MHz.

    The interop model had no ceiling at all until 2026-08-03 and happily
    reported margins at a rate the bus may not run at.
    """
    assert fclk_ceiling_MHz("SWI3S_PHY2", "SWI3S_PHY2") == 13.2
    for size, expect in (("small", 12.7), ("large", 10.1)):
        assert fclk_ceiling_MHz("SWI3S_PHY2", "SW13_1V8", size) == expect
        assert fclk_ceiling_MHz("SW13_1V8", "SWI3S_PHY2", size) == expect
    for size, expect in (("small", 12.3), ("large", 11.0)):
        assert fclk_ceiling_MHz("SW13_1V2", "SWI3S_PHY2", size) == expect

    r = compute(_mixed("SWI3S_PHY2", "SW13_1V8"))     # 13.2 MHz target
    assert not r.F_CLK_legal, "13.2 MHz is above every SoundWire ceiling"
    assert r.F_CLK_ceiling_MHz == 12.7
    ok = compute(_mixed("SWI3S_PHY2", "SW13_1V8", F_CLK_target_MHz=12.0))
    assert ok.F_CLK_legal


def test_schmitt_cost_is_reported_for_a_soundwire_receiver():
    """SoundWire guarantees hysteresis only on its Clock pin, so the Schmitt
    margin is not spec-guaranteed on a SoundWire Data RX. Report it."""
    assert compute(CalcInputs()).schmitt_cost_ns == 0.0        # all-SWI3S: n/a
    r = compute(_mixed("SWI3S_PHY2", "SW13_1V8"))
    assert r.schmitt_cost_ns == pytest.approx(0.945, abs=5e-3)
    assert any("guaranteed-recognition" in c for c in r.caveats)
    assert any("t_Slew_Data" in c for c in r.caveats)
    assert any("t_DZ" in c for c in r.caveats)


def test_schmitt_simplification_stays_small_at_the_realistic_corner():
    """Guard the §4.5 decision to treat Schmitt globally rather than per-receiver.

    The decision rests on the cost being sub-nanosecond. That is true at the
    realistic corner — the nominal hysteresis band — and this pins it.
    """
    for tRF, want in ((2.0, 0.350), (5.0, 0.875), (5.4, 0.945)):
        assert schmitt_delta_ns(shape="linear", tRF_ns=tRF) == pytest.approx(want, abs=5e-3)
        # The exponential ramp is cheaper still.
        assert schmitt_delta_ns(shape="exp", tRF_ns=tRF) < want
    assert schmitt_delta_ns(shape="linear", tRF_ns=5.4) < 1.0


def test_schmitt_simplification_is_NOT_small_at_the_widest_legal_corner():
    """...but the "it's under a nanosecond" justification does NOT generalise.

    The delta is (V_IH - V_IL)/swing, so it scales with the hysteresis band.
    SWI3S specifies V_IH in [0.45, 0.65] and V_IL in [0.35, 0.55] and requires
    hysteresis of at least 10 % V_SEOS -- it sets a FLOOR on the band, not a
    ceiling. A compliant receiver may therefore sit at V_IH = 0.65 with
    V_IL = 0.35: a 0.30 band, three times nominal. Combined with the slowest
    legal edge (t_RF = 8.3 ns) the cost reaches ~4.4 ns, which is no longer
    negligible against the margins being reported.

    This does not overturn the decision at the documented baseline, and it
    changes no published verdict there. It does mean the simplification is
    justified *by the corner*, not by the structure -- so it must not be
    silently inherited if the operating point moves. Pinned here so that a
    future change to the threshold window or edge rate surfaces it rather than
    quietly invalidating the reasoning.
    """
    worst = max(
        schmitt_delta_ns(shape=shape, tRF_ns=tRF, V_IH_max_frac=vih,
                         V_IL_max_frac=vil, VDD_tol=tol)
        for shape in ("linear", "exp")
        for tRF in (2.0, 5.0, 5.4, 8.3)
        for vih, vil in ((0.65, 0.55), (0.65, 0.45), (0.65, 0.35))
        for tol in (0.0, 0.05)
    )
    assert worst == pytest.approx(4.36, abs=0.05), (
        f"worst-corner Schmitt delta moved to {worst:.2f} ns — re-read §4.5 "
        "before accepting it; the global-treatment decision assumed this corner."
    )
    # Widest band, slowest edge: the cost is >4x the baseline.
    assert schmitt_delta_ns(shape="linear", tRF_ns=8.3, V_IL_max_frac=0.35) > 4.0


def test_every_spec_source_builds_and_converts():
    """No preset is missing a field, and no combination crashes."""
    for man in SPEC_SOURCES:
        for per in SPEC_SOURCES:
            r = compute(_mixed(man, per))
            assert math.isfinite(r.breakdowns["MP_setup"].margin_ns)
            assert (r.schmitt_cost_ns > 0) == (is_soundwire(man) or is_soundwire(per))


def test_converted_terms_carry_provenance():
    """A converted clock-to-output term explains itself where it is used,
    not only in the design document."""
    r = compute(_mixed("SW13_1V8", "SWI3S_PHY2"))
    term = next(t for t in r.breakdowns["MP_setup"].terms if "Man_t_DD" in t.symbol)
    assert "t_OV" in term.note and "end of slew" in term.note
    assert "27.6" in term.note and "26.25" in term.note
    # A SWI3S Manager term says it needed no conversion.
    r2 = compute(CalcInputs())
    t2 = next(t for t in r2.breakdowns["MP_setup"].terms if "Man_t_DD" in t.symbol)
    assert "already a pure delay" in t2.note


# ---------------------------------------------------------------------------
# 5. UI
# ---------------------------------------------------------------------------

def _view():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    return TimingView()


def test_spec_selector_reseeds_only_its_own_side():
    """Switching the Peripheral to SoundWire must reseed the Peripheral rows from
    the SoundWire tables and leave the Manager's SWI3S values alone — which side
    is which changes the answer."""
    tv = _view()
    # The title is fixed; the SUBTITLE names the selected bus.
    assert tv._title.text() == "SWI3S & SoundWire Timing Calculator"
    assert tv._subtitle.text() == "SWI3S PHY2 Manager and Peripheral"
    # The size envelope is SoundWire-only, and HIDDEN rather than greyed when
    # neither side is SoundWire — on the default all-SWI3S page a disabled row
    # is a permanent question with no answer on screen.
    assert not tv._size_row.isVisibleTo(tv), "size envelope is SoundWire-only"

    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V8"))
    assert tv._size_row.isVisibleTo(tv)
    # Peripheral now carries SoundWire's t_OH_Data / t_OV_Data small.
    assert tv._min[("spec", "Per_t_DD")].value() == pytest.approx(7.9)  # t_ZD_Data,Min
    assert tv._max[("spec", "Per_t_DD")].value() == pytest.approx(27.6)
    assert tv._max[("spec", "t_RF Per DATA")].value() == pytest.approx(5.4)
    # Manager is untouched.
    assert tv._min[("spec", "Man_t_DD")].value() == pytest.approx(7.0)
    assert tv._max[("spec", "Man_t_DD")].value() == pytest.approx(18.0)
    # The subtitle names both sides, Manager first — which side is which
    # changes the answer, so it must not be hidden.
    assert tv._subtitle.text() == ("SWI3S PHY2 Manager  ↔  "
                                   "SoundWire 1.3 (1.8 V) Peripheral")


def test_system_size_switches_the_soundwire_envelope():
    tv = _view()
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V8"))
    tv._size_combo.setCurrentIndex(tv._size_combo.findData("large"))
    assert tv._max[("spec", "Per_t_DD")].value() == pytest.approx(31.6)
    assert tv._max[("spec", "t_RF Per DATA")].value() == pytest.approx(9.0)
    assert tv._fclk.value() == pytest.approx(10.1)


def test_fclk_seeds_to_the_legal_ceiling_not_the_swi3s_rate():
    """A mixed bus may not clock at 13.2 MHz, so reset must not seed it there."""
    tv = _view()
    # All-SWI3S opens on the 12.288 MHz audio design point (256 x 48 kHz), not
    # PHY2's 13.2 MHz mandatory maximum — a worst-case corner no design targets.
    assert tv._fclk.value() == pytest.approx(12.288)
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V8"))
    assert tv._fclk.value() == pytest.approx(12.288)     # below the 12.7 ceiling
    # The large envelope's 10.1 MHz ceiling DOES clamp the design point.
    tv._size_combo.setCurrentIndex(tv._size_combo.findData("large"))
    assert tv._fclk.value() == pytest.approx(10.1)


def test_advisory_warns_above_the_ceiling_and_about_hysteresis():
    tv = _view()
    assert tv._ceiling_label.text() == ""
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V8"))
    # Reset seeded a legal rate, so only the hysteresis advisory shows.
    assert "hysteresis" in tv._ceiling_label.text()
    assert "ceiling" not in tv._ceiling_label.text()
    assert "guaranteed-recognition" in tv._ceiling_label.toolTip()
    # Push the target above the ceiling and the rate warning joins it.
    tv._fclk.setValue(13.2)
    assert "ceiling" in tv._ceiling_label.text()


def test_spec_selection_round_trips_and_legacy_dicts_default_to_swi3s():
    tv = _view()
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V2"))
    tv._size_combo.setCurrentIndex(tv._size_combo.findData("large"))
    d = tv.to_dict()

    tv2 = _view()
    tv2.set_from_dict(d)
    assert tv2._man_spec_combo.currentData() == "SW13_1V2"
    assert tv2._size_combo.currentData() == "large"
    assert tv2._subtitle.text() == tv._subtitle.text()

    # A workspace saved before the selector existed can only have been all-SWI3S.
    for k in ("man_spec", "per_spec", "sw_system_size"):
        d.pop(k)
    tv3 = _view()
    tv3.set_from_dict(d)
    assert tv3._man_spec_combo.currentData() == "SWI3S_PHY2"
    assert tv3._per_spec_combo.currentData() == "SWI3S_PHY2"
    assert tv3._subtitle.text() == "SWI3S PHY2 Manager and Peripheral"


def test_legacy_phy1_workspace_restores_as_a_phy1_bus():
    """The PHY picker is gone; PHY1 is a spec source on each side.

    A workspace written while PHY was one global switch carries `phy: 1` and no
    spec keys. That switch meant "both sides are PHY1", so it must restore that
    way — silently landing on PHY2 would change the answer without saying so.
    """
    # A realistic legacy dict: every row value present (to_dict always writes
    # them), spec keys absent, `phy` present. Row values come from the dict, so
    # this checks the SELECTOR mapping, which is the part that has no other
    # source once the picker is gone.
    src = _view()
    for combo in (src._man_spec_combo, src._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY1"))
    legacy = src.to_dict()
    for k in ("man_spec", "per_spec", "sw_system_size"):
        legacy.pop(k)
    legacy["phy"] = 1

    tv = _view()
    tv.set_from_dict(legacy)
    assert tv._man_spec_combo.currentData() == "SWI3S_PHY1"
    assert tv._per_spec_combo.currentData() == "SWI3S_PHY1"
    assert tv._subtitle.text() == "SWI3S PHY1 Manager and Peripheral"
    assert tv._max[("spec", "Per_t_DD")].value() == pytest.approx(50.0)
    # The margins must match the bus that was saved, not a PHY2 reading of it.
    assert tv.summary_text() == src.summary_text()

    # An explicit spec key always wins over the legacy PHY hint.
    tv2 = _view()
    tv2.set_from_dict({"phy": 1, "man_spec": "SW13_1V8"})
    assert tv2._man_spec_combo.currentData() == "SW13_1V8"
    assert tv2._per_spec_combo.currentData() == "SWI3S_PHY1"


def test_the_size_row_hides_and_returns_with_the_soundwire_selection():
    """Hiding must be reversible, and must survive a workspace load.

    `_sync_spec_controls` runs on selection change AND inside `set_from_dict`;
    if either path missed it, a restored SoundWire workspace would come back
    with no way to pick its envelope.
    """
    tv = _view()
    assert not tv._size_row.isVisibleTo(tv)
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V2"))
    assert tv._size_row.isVisibleTo(tv)
    # Back to all-SWI3S and it goes away again.
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SWI3S_PHY2"))
    assert not tv._size_row.isVisibleTo(tv)

    # Round-trip a SoundWire workspace into a fresh view.
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V8"))
    tv2 = _view()
    assert not tv2._size_row.isVisibleTo(tv2)
    tv2.set_from_dict(tv.to_dict())
    assert tv2._size_row.isVisibleTo(tv2)


def test_phy1_example_column_follows_the_table_structure_not_the_numbers():
    """Which half of the proposal reaches a PHY1 side is decided by how the SPEC
    IS STRUCTURED, not by which numbers happen to coincide (v1.1r06).

    Table 128 (INPUT) is genuinely shared — one row per parameter, no PHY1/PHY2
    distinction — so revising Per_tIH 0-4.5 -> 0-1.5 revises it for both PHYs.

    Table 129 (OUTPUT) prints EVERY parameter as a separate PHY1 and PHY2 row and
    never collapses them, so a revision to PHY2's row implies nothing about
    PHY1's. Man_tDD's two rows agree today, and revising them together is a
    decision the proposal makes. Per_tDD and Per_tZD DIFFER per PHY, so they
    carry PHY1's own revised maximum (50 -> 44) rather than PHY2's 9 — the
    distinction being that 44 is a number stated for this PHY, where 9 would be
    a value invented by pasting. Man_tZD has no PHY1 revision and stays put.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY1"))

    # Carried: Man_tDD (coincident output rows) and Per_tIH (one shared input row).
    assert (tv._min[("spec", "Man_t_DD")].value(),
            tv._max[("spec", "Man_t_DD")].value()) == (7.0, 18.0)
    assert (tv._min[("proposed", "Man_t_DD")].value(),
            tv._max[("proposed", "Man_t_DD")].value()) == (9.0, 23.0)
    assert tv._max[("spec", "Per_t_IH")].value() == pytest.approx(4.5)
    assert tv._max[("proposed", "Per_t_IH")].value() == pytest.approx(1.5)

    # PHY1's OWN revision on the two rows that differ per PHY: 14-50 -> 14-44.
    # NOT PHY2's proposed 2-9, which would be a value invented for this PHY.
    for name in ("Per_t_DD", "Per_t_ZD"):
        assert (tv._min[("spec", name)].value(),
                tv._max[("spec", name)].value()) == (14.0, 50.0), name
        assert (tv._min[("proposed", name)].value(),
                tv._max[("proposed", name)].value()) == (14.0, 44.0), name
        # The min is untouched — only the maximum is revised.
        assert tv._min[("proposed", name)].value() == tv._min[("spec", name)].value()

    # Man_tZD has no PHY1 revision, so both columns hold its Table-129 value.
    for col in ("spec", "proposed"):
        assert tv._min[(col, "Man_t_ZD")].value() == 14.0, col
        assert tv._max[(col, "Man_t_ZD")].value() == 30.0, col

    # The revisions have to actually move margins, or seeding them means nothing.
    spec = tv._per_inequality_worst("spec")
    prop = tv._per_inequality_worst("proposed")
    assert prop["MP_hold"].margin_ns > spec["MP_hold"].margin_ns, \
        "Man_tDD,min 7->9 and Per_tIH,max 4.5->1.5 fund MP hold"
    # Per_tDD,max 50->44 is worth exactly 6 ns of PM setup, and closes it.
    assert prop["PM_setup"].margin_ns - spec["PM_setup"].margin_ns == pytest.approx(6.0)
    assert spec["PM_setup"].margin_ns < 0 < prop["PM_setup"].margin_ns, \
        "the PHY1 revision should close PM setup, which is the point of it"


def test_the_shared_input_table_is_shared_on_both_phys():
    """Table 128 has no PHY1/PHY2 split, so both PHYs must carry identical input
    timing — the invariant that makes carrying Per_tIH to PHY1 correct.

    Asserted against the PRESETS, not against two readings of the same view: the
    UI seeds shared rows from one side, so a view-to-view comparison moves both
    together and passes even if the presets diverge.
    """
    for side, names in (("Man", ("t_IS_max_ns", "t_IH_max_ns")),
                        ("Per", ("t_IS_max_ns", "t_IH_max_ns"))):
        p1 = preset("SWI3S_PHY1", side)
        p2 = preset("SWI3S_PHY2", side)
        for f in names:
            assert getattr(p1, f) == getattr(p2, f), (side, f)

    # And the three Table-129 rows that DO differ must stay different, or the
    # PHY selector is not selecting anything.
    assert preset("SWI3S_PHY1", "Per").t_DD_max_ns != preset("SWI3S_PHY2", "Per").t_DD_max_ns
    assert preset("SWI3S_PHY1", "Per").t_ZD_ns != preset("SWI3S_PHY2", "Per").t_ZD_ns
    assert preset("SWI3S_PHY1", "Man").t_ZD_ns != preset("SWI3S_PHY2", "Man").t_ZD_ns


def test_the_two_swi3s_phys_are_independently_selectable_per_side():
    """A PHY1 Manager with a PHY2 Peripheral — inexpressible with a global PHY
    switch, which is the reason the switch was folded into the spec selector.

    Each side must take its OWN Table-129 rows: PHY1 splits only the Peripheral
    output/high-Z and Manager high-Z values, so a mixed pair mixes those three
    and shares everything else.
    """
    tv = _view()
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SWI3S_PHY1"))
    assert tv._subtitle.text() == ("SWI3S PHY1 Manager  ↔  SWI3S PHY2 Peripheral")
    assert tv._max[("spec", "Man_t_ZD")].value() == pytest.approx(30.0)   # PHY1
    assert tv._max[("spec", "Per_t_DD")].value() == pytest.approx(20.0)   # PHY2
    # Shared PHY1&2 values are unaffected by either side's choice.
    assert tv._max[("spec", "Man_t_DD")].value() == pytest.approx(18.0)
    assert tv._max[("spec", "Per_t_IH")].value() == pytest.approx(4.5)
    # PHY1's 6.6 MHz mandatory max binds the pair, so the target clamps to 6.4.
    assert tv._fclk.value() == pytest.approx(6.4)


# ---------------------------------------------------------------------------
# 6. Same-spec controls
# ---------------------------------------------------------------------------

def test_soundwire_on_both_sides_matches_the_oracle():
    """SW <-> SW is the control that says the MP-hold failure is not about mixing.

    Both same-spec configurations fail MP hold in these inequalities -- SW/SW at
    -2.83 and SWI3S/SWI3S at -2.64 -- so a mixed bus's failure is not caused by
    the specs being different. For SoundWire it is arithmetic: t_OH_Data 6.7 minus
    t_IHold 4.0 leaves 2.7 ns against a 5.33 ns crossing penalty at its own t_Slew.

    (SoundWire's own method does not form this inequality at all; it spends the
    edge uncertainty on clock-high time instead. See the memo's interop finding 4.)
    """
    r = compute(_mixed("SW13_1V8", "SW13_1V8"))
    assert r.breakdowns["MP_setup"].margin_ns == pytest.approx(+2.31, abs=0.05)
    assert r.breakdowns["MP_hold"].margin_ns == pytest.approx(+4.22, abs=0.05)
    assert r.breakdowns["PM_hold"].margin_ns == pytest.approx(+18.87, abs=0.05)


def test_soundwire_hold_bound_is_t_ZD_not_t_OH():
    """The hold-side parameter, and the re-anchor it still needs.

    SoundWire's min-corner value is NOT t_OH_Data. Table 15 measures that "from
    data first becoming valid ... to Data output becoming high impedance" — a
    data-referenced drive-before-handover permission ({1209}), after which the
    BUS-KEEPER holds the level to the sampling edge (§5.1.11, Fig. 16). It is not
    a hold guarantee, and using it penalised SoundWire by 1.2 ns.

    The right parameter is t_ZD_Data,Min — "Time to enable Data output signal
    after positive or negative edge on Clock input signal", limit is a Min — the
    earliest a driver may drive the NEXT value, hence the bound on destroying the
    old level.

    It is clock-referenced, but at the clock's V_TP threshold, so it still takes
    the +1.083·t_RF clock-side re-anchor. It takes no DATA-side term: it ends at
    low-impedance, before the ramp, so no slew is embedded in it.
    """
    # t_ZD 7.9 plus the clock-side re-anchor only.
    assert pure_output_delay(7.9, spec="SW13_1V8", side="Man", shape="linear",
                             corner="min") == pytest.approx(13.75, abs=1e-9)
    # t_OV on the same side takes BOTH re-anchors, netting −1.35 ns.
    assert pure_output_delay(27.6, spec="SW13_1V8", side="Man", shape="linear",
                             corner="max") == pytest.approx(26.25, abs=1e-9)
    # A SWI3S Peripheral min corner IS a clock-referenced delay at a 20 % anchor
    # and keeps its own offset, with no clock-side term (they cancel).
    assert pure_output_delay(2.0, spec="SWI3S_PHY2", side="Per", shape="linear",
                             corner="min") == pytest.approx(0.3333, abs=1e-4)


# ---------------------------------------------------------------------------
# 7. The proposed SWI3S PHY2 revision
# ---------------------------------------------------------------------------

# The P->P DATA legs the spec does not promise: peripheral B READING a level peripheral A drove
# in order to be read. A claim of the form "the proposal closes every inequality" is a claim
# about the GUARANTEED directions, so filtering these keeps it exact; what they actually read is
# recorded by the tests named for them.
#
# IMPORTED FROM THE MODEL, NOT RESTATED. This was a local tuple, and it drifted the moment the
# model's set changed -- it still listed `PP_hold_ho` after that leg became bounding, so the
# equality assertion below went on subtracting a leg the model no longer excludes and could not
# see the change. One definition, read by both.
_PP_DATA_LEGS = tuple(sorted(CalcResults._PP_DATA_LEGS))


def _guaranteed(breakdowns):
    """`breakdowns` less the P->P data legs — the directions the spec promises."""
    return {k: b for k, b in breakdowns.items() if k not in _PP_DATA_LEGS}


def _same(spec, **kw):
    """Both sides built to one spec, seeded from its preset."""
    m, p = preset(spec, "Man"), preset(spec, "Per")
    return CalcInputs(
        Man_spec=spec, Per_spec=spec,
        Man_t_DD_min_ns=m.t_DD_min_ns, Man_t_DD_max_ns=m.t_DD_max_ns,
        Man_t_ZD_ns=m.t_ZD_ns, Man_t_IH_max_ns=m.t_IH_max_ns,
        Per_t_DD_min_ns=p.t_DD_min_ns, Per_t_DD_max_ns=p.t_DD_max_ns,
        Per_t_ZD_ns=p.t_ZD_ns, Per_t_IH_max_ns=p.t_IH_max_ns,
        **{**BASE, **kw},
    )


def test_proposal_closes_every_failing_inequality_at_the_reference_corner():
    """The revision's purpose: MP hold and PM setup both fail today and both close.

    Deltas are exact and attributable, which is why they are asserted rather
    than bounded -- each comes from one parameter change:
      MP hold  +5.00 = Man_t_DD,min +2.0 and Per_t_IH,max -3.0
      PM setup +11.00 = Per_t_DD,max 20 -> 9
      MP setup -5.00 = Man_t_DD,max 18 -> 23, the cost of funding the above
    """
    cur = compute(_same("SWI3S_PHY2"))
    prop = compute(_same("SWI3S_PHY2_PROP"))
    for ineq, before, after in (("MP_hold", -2.64, +2.36),
                                ("PM_setup", -3.81, +7.19),
                                ("MP_setup", +6.95, +1.95)):
        assert cur.breakdowns[ineq].margin_ns == pytest.approx(before, abs=0.01), ineq
        assert prop.breakdowns[ineq].margin_ns == pytest.approx(after, abs=0.01), ineq
    # Every inequality passes under the proposal; none does today.
    assert all(b.margin_ns >= 0 for b in _guaranteed(prop.breakdowns).values())
    assert not all(b.margin_ns >= 0 for b in _guaranteed(cur.breakdowns).values())
    # The binding constraint stops being a hard failure and becomes a rate bound
    # above the mandatory 13.2 MHz.
    assert cur.F_max_binding_MHz == 0.0
    assert prop.F_max_binding_MHz == pytest.approx(14.0, abs=0.1)


def test_proposal_at_the_design_target_leaves_a_sub_ns_worst_corner_deficit():
    """What the proposal is actually asked to do, judged at the real design point.

    Targets are 12.288 MHz (256 x 48 kHz) over a 30 cm bus -- not the 13.2 MHz
    mandatory ceiling the earlier corners used. At that point the proposal closes
    everything except MP hold at the simultaneous worst case, and that residual
    is **0.89 ns**.

    That residual should NOT be read as a failure. The corner stacks every
    parameter at its individual extreme at once -- slowest legal slew AND full
    120 mV noise AND independent supplies at opposite tolerance ends -- which is
    not a configuration a real deployment lands in. Any ONE of those relaxing is
    enough (see the companion test). The honest claim is "closes at the design
    point with a sub-nanosecond all-extremes residual", not "closes every corner".
    """
    prop = compute(_same("SWI3S_PHY2_PROP", F_CLK_target_MHz=12.288))
    assert all(b.margin_ns >= 0 for b in _guaranteed(prop.breakdowns).values()), \
        "the proposal must close at the design point with nominal slew"
    assert prop.breakdowns["MP_hold"].margin_ns == pytest.approx(+2.36, abs=0.02)
    assert prop.breakdowns["MP_setup"].margin_ns == pytest.approx(+4.48, abs=0.02)
    assert prop.breakdowns["PM_setup"].margin_ns == pytest.approx(+9.72, abs=0.02)

    worst = compute(_same("SWI3S_PHY2_PROP", F_CLK_target_MHz=12.288,
                          tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=8.3,
                          tRF_Per_DATA_ns=8.3))
    assert worst.breakdowns["MP_hold"].margin_ns == pytest.approx(-0.89, abs=0.02)
    # MP setup stays positive at the design rate -- the 13.2 MHz corner is what
    # pushed it negative, and 13.2 is above the target.
    assert worst.breakdowns["MP_setup"].margin_ns > 0
    assert worst.breakdowns["PM_setup"].margin_ns > 0


def test_which_relaxations_close_the_all_extremes_corner():
    """The residual is covered by ANY one of three realistic assumptions.

    Pinned because the three are alternatives, not a cumulative requirement, and
    quoting them as a list of things that must ALL hold would overstate the ask
    substantially. Slew is held at its 8.3 ns worst throughout except where varied.

    (This briefly asserted that a SECOND leg failed here, PM_setup_ho at -0.25 ns.
    That was an artefact of removing Per_t_ZD's V_OL back-out, which was itself a
    misreading -- see `_per_tzd_pure_max`. MP hold is again the only short leg.)
    """
    W = dict(F_CLK_target_MHz=12.288, tRF_Man_CLK_ns=8.3,
             tRF_Man_DATA_ns=8.3, tRF_Per_DATA_ns=8.3)

    def failing(**kw):
        r = compute(_same("SWI3S_PHY2_PROP", **{**W, **kw}))
        return {k for k, b in _guaranteed(r.breakdowns).items() if b.margin_ns < 0}

    # All extremes at once: MP hold is the one short leg.
    assert failing() == {"MP_hold"}
    # Any ONE of these covers it.
    assert failing(V_SEOS_tol_frac=0.0) == set()          # Man/Per share a supply
    assert failing(V_noise_pp_frac=0.065) == set()        # noise under ~78 mV of 120
    assert failing(tRF_Man_CLK_ns=7.0, tRF_Man_DATA_ns=7.0,
                   tRF_Per_DATA_ns=7.0) == set()          # not the slowest legal edge

    # THE MANAGER'S KEEPER LEG IS THIN HERE TOO, and it does not show above because
    # this corner leaves t_DZ at its tabulated 10 ns. Drive t_DZ to the other end of
    # its row -- a device that goes high-Z the instant its UI closes, which the spec
    # permits since it states only a maximum -- and the leg fails:
    #
    #     1*UI + Man_t_DZ - Man_t_DD,max - swing = 36.62 + 0 - 23.00 - 13.83 = -0.21
    #
    # i.e. at 12.288 MHz the revision's own Man_t_DD,max of 23 ns plus a slow-corner
    # transition needs 36.83 ns against a 36.62 ns UI. The Manager cannot finish the
    # transition it launched inside the UI it launched it in, so anything it drives is
    # handed to the keeper mid-edge. Same arithmetic as MP hold's shortfall here, one
    # step earlier -- and the auto-worst corner search DOES find it, so this is what
    # the app shows on the proposed column.
    #
    # Unlike MP hold it is NOT covered by two of the three relaxations, because the
    # keeper leg carries no crossing term -- it is pure driver timing, so supply
    # tolerance and noise cannot move it. Only the slew can.
    assert failing(Man_t_DZ_max_ns=0.0) == {"MP_hold", "keeper_Man"}
    assert failing(Man_t_DZ_max_ns=0.0, V_SEOS_tol_frac=0.0) == {"keeper_Man"}
    assert failing(Man_t_DZ_max_ns=0.0, V_noise_pp_frac=0.065) == {"keeper_Man"}
    # AND THE SLEW RELAXATION NO LONGER COVERS IT EITHER, since Table 125's 3 ns became
    # unconditional (PHY2 has no forced keeper, so the observing case is the only case).
    # The 7 ns edge used to leave +2.79 and now leaves -1.05: the keeper wants 3 ns of
    # settled level at the Manager pin and this corner has 1.05 ns less than the whole
    # transition. What the leg now needs at 12.288 MHz, any ONE of:
    #
    #     an edge of 6.4 ns or better        (6.5 -> -0.21, 6.0 -> +0.62)
    #     Man_t_DD,max of 21.95 ns, not 23   (the leg is affine: margin = 21.95 - t_DD)
    #     a STATED Man_t_DZ minimum of 1.05 ns -- which the spec does not have, since
    #       t_DZ is a maximum, so "release the instant the UI closes" stays legal
    #
    # That is a finding about the proposed revision at its own worst corner, not a
    # modelling artefact: 23 ns of launch plus a slow-corner transition plus the keeper's
    # 3 ns does not fit in a 36.62 ns UI.
    assert failing(Man_t_DZ_max_ns=0.0, tRF_Man_CLK_ns=7.0, tRF_Man_DATA_ns=7.0,
                   tRF_Per_DATA_ns=7.0) == {"keeper_Man"}
    assert failing(Man_t_DZ_max_ns=0.0, tRF_Man_CLK_ns=6.0, tRF_Man_DATA_ns=6.0,
                   tRF_Per_DATA_ns=6.0) == set()
    syms = [t.symbol for t in
            compute(_same("SWI3S_PHY2_PROP", **W)).breakdowns["keeper_Man"].terms]
    assert not any("cross" in s for s in syms), \
        f"the keeper leg must carry no crossing term, or the reasoning above fails: {syms}"


def test_proposed_values_are_flagged_as_unratified():
    """A proposed number must never be presented with a spec's authority."""
    r = compute(_same("SWI3S_PHY2_PROP"))
    assert r.caveats and r.caveats[0].startswith("These are PROPOSED")
    # The UI-relative ambiguity is carried alongside it, not buried.
    assert any("0.5 UI" in c for c in r.caveats)
    # A ratified selection carries neither.
    assert compute(_same("SWI3S_PHY2")).caveats == ()


def test_ui_relative_alternative_is_tighter_than_the_fixed_cap_at_13_2MHz():
    """The proposal's '0.5 UI at 2x CLK' alternative is not a relaxation.

    At the mandatory rate it is 17.05 ns against the fixed 23 ns, so a device
    meeting one may fail the other. Which governs is rate-dependent and the
    wording does not say -- pinned so the ambiguity stays visible.
    """
    assert ui_relative_limit_ns(13.2) == pytest.approx(17.05, abs=0.01)
    assert ui_relative_limit_ns(13.2) < 23.0            # tighter, not looser
    assert ui_relative_limit_ns(13.2, ui_fraction=0.25) == pytest.approx(8.52, abs=0.01)
    # They cross below 9.78 MHz, where the UI-relative form becomes the looser one.
    assert ui_relative_limit_ns(9.78) == pytest.approx(23.0, abs=0.05)
    assert ui_relative_limit_ns(6.6) > 23.0


# ---------------------------------------------------------------------------
# 8. Manager launch mode (analog vs clock-launched)
# ---------------------------------------------------------------------------

def test_clock_launch_beats_analog_on_both_mp_legs_at_the_design_rate():
    """A clock-launched edge wins because it is DETERMINISTIC, not because it
    is shorter.

    The analog option spans 9-23 ns, and the inequalities must absorb that
    14 ns spread on both sides at once: the max erodes setup while the min is
    all that funds hold. A clock launch collapses min and max to one value, so
    the spread stops costing anything.

    At 12.288 MHz, 4x CLK improves BOTH MP legs over analog — which a mere
    tightening of the range could not do, since setup and hold pull in
    opposite directions on it.
    """
    # The launch mode sets the clock's RESOLUTION; the placement is a design
    # choice on that grid, so a comparison has to state it. These are the
    # placements the proposal names for each multiplier -- 0.25 UI at 4x, 0.50 UI
    # at 2x -- but they are now inputs rather than something the mode imposes.
    UI = 1000 * 0.45 / 12.288
    base = dict(F_CLK_target_MHz=12.288)
    analog = compute(_same("SWI3S_PHY2_PROP", **base))
    def at(mode, frac):
        inp = _same("SWI3S_PHY2_PROP", launch_mode=mode, **base)
        return compute(dc_replace(inp, Man_t_DD_min_ns=frac * UI,
                                  Man_t_DD_max_ns=frac * UI))

    clk4 = at(LaunchMode.CLK4, 0.25)
    clk2 = at(LaunchMode.CLK2, 0.50)

    assert analog.breakdowns["MP_setup"].margin_ns == pytest.approx(+4.48, abs=0.02)
    assert analog.breakdowns["MP_hold"].margin_ns == pytest.approx(+2.36, abs=0.02)
    # 4x: much more setup room, and hold no worse.
    assert clk4.breakdowns["MP_setup"].margin_ns == pytest.approx(+18.33, abs=0.02)
    assert clk4.breakdowns["MP_hold"].margin_ns == pytest.approx(+2.52, abs=0.02)
    assert clk4.breakdowns["MP_setup"].margin_ns > analog.breakdowns["MP_setup"].margin_ns
    assert clk4.breakdowns["MP_hold"].margin_ns > analog.breakdowns["MP_hold"].margin_ns
    # 2x: trades setup for a large hold margin.
    assert clk2.breakdowns["MP_setup"].margin_ns == pytest.approx(+9.17, abs=0.02)
    assert clk2.breakdowns["MP_hold"].margin_ns == pytest.approx(+11.67, abs=0.02)


def test_the_two_clock_modes_are_bounded_from_opposite_ends():
    """Why the spec needs both, and why the choice is rate-dependent.

    4x is bounded by HOLD: its launch delay is 0.25*UI, which shrinks as the
    clock speeds up until it no longer covers the receiver's hold requirement.
    2x is bounded by SETUP: its 0.5*UI delay eats half the UI, so the remainder
    stops covering t_IS first.

    Because they fail from opposite ends, neither alone spans the rate range —
    4x is the better choice up to ~17 MHz (more setup room at adequate hold),
    and above that only 2x closes.
    """
    f4, bound4 = launch_mode_max_fclk_MHz(LaunchMode.CLK4)
    f2, bound2 = launch_mode_max_fclk_MHz(LaunchMode.CLK2)
    assert bound4 == "hold" and bound2 == "setup"
    assert f4 == pytest.approx(17.0, abs=0.2)
    assert f2 == pytest.approx(24.6, abs=0.2)
    assert f2 > f4, "2x must reach higher, or the rate-dependent guidance inverts"

    # The design point sits comfortably inside 4x's range...
    assert recommended_launch_mode(12.288) is LaunchMode.CLK4
    # ...and 2x takes over once 4x's hold margin runs out.
    assert recommended_launch_mode(18.0) is LaunchMode.CLK2
    assert recommended_launch_mode(24.0) is LaunchMode.CLK2


def test_clock_launch_collapses_the_min_max_spread():
    """The mechanism, pinned directly: min == max for a clock-launched edge."""
    assert launch_mode_tDD_ns(LaunchMode.ANALOG, 12.288) is None
    lo4, hi4 = launch_mode_tDD_ns(LaunchMode.CLK4, 12.288)
    lo2, hi2 = launch_mode_tDD_ns(LaunchMode.CLK2, 12.288)
    assert lo4 == hi4 == pytest.approx(9.16, abs=0.01)     # 0.25 UI
    assert lo2 == hi2 == pytest.approx(18.31, abs=0.01)    # 0.50 UI
    # Analog keeps its 14 ns spread, which is what the inequalities must absorb.
    assert preset("SWI3S_PHY2_PROP", "Man").t_DD_max_ns - \
           preset("SWI3S_PHY2_PROP", "Man").t_DD_min_ns == pytest.approx(14.0)


def test_launch_mode_governs_man_t_ZD_on_an_analog_column():
    """A clocked Manager clocks its output ENABLE too, so t_ZD follows the mode.

    It has the multiplied clock either way, and a t_DD on a clock grid point beside
    an analog t_ZD is not one device. Checked on a realistic column — seeded from
    the PHY2 proposal's own preset — rather than on hand-built inputs, so the
    coupling is exercised through the same path the page uses.

    Asserted as "the analog value does NOT survive, and what replaces it is
    placeable by the selected clock". Not as a direction: the proposal's 23 ns
    tabulated max is nowhere near a 1/4 UI point, so at 4× it snaps UP to 0.75 UI
    and the handover setup leg gets worse, while at 2× it snaps down to 0.50 UI and
    the leg improves. That is what snapping an arbitrary analog number means, and
    it is why the page SEEDS these rows to the data-launch point on a mode change
    instead of leaving a tabulated value to be snapped.
    """
    UI = 1000 * 0.45 / 12.288

    def zd_term(mode):
        r = compute(_same("SWI3S_PHY2_PROP", F_CLK_target_MHz=12.288,
                          launch_mode=mode))
        return next(t for t in r.breakdowns["MP_setup_ho"].terms if "ZD" in t.symbol)

    tabulated = preset("SWI3S_PHY2_PROP", "Man").t_ZD_ns
    assert -zd_term(LaunchMode.ANALOG).value_ns == pytest.approx(tabulated, abs=0.01), \
        "analog: as tabulated, untouched"
    for mode, grid in ((LaunchMode.CLK4, 0.25), (LaunchMode.CLK2, 0.50)):
        t = zd_term(mode)
        used = -t.value_ns
        assert used != pytest.approx(tabulated, abs=0.01), \
            f"{mode}: the analog value must not survive a clocked mode"
        k = used / (grid * UI)
        assert k == pytest.approx(round(k), abs=1e-6), \
            f"{mode}: {used:.4g} ns is not on the {grid:g} UI grid"
        assert "snapped" in t.note, f"{mode}: the move must be reported, got {t.note!r}"


def test_the_launch_mode_governs_every_manager_output_including_ede_t_DZ():
    """EDE redefines what t_DZ MEANS, not whether the clock can place it -- so the
    selector governs all three Manager outputs, on every column.

    This test previously asserted the opposite, and the reasoning was wrong in a
    way worth recording. It read EDE as stating all three Manager outputs itself,
    with t_ZD on the 0.00 UI grid point plus a 3--9 ns analog turn-on, and treated
    that 0.00 UI point as load-bearing.

    Two things kill it. The PHY2 revision proposes Man_t_ZD as "9--23 ns or
    (0.5 UI 2x CLK, 0.25 UI 4x CLK)" -- the SAME options as Man_t_DD, tracking it,
    so 0.00 UI is not on offer for this parameter. And the revision raised its
    analog minimum from 2 ns to 9 ns precisely BECAUSE 2 ns violated hold at the
    peripheral, which `MP_hold_ho` now carries -- so a 3 ns minimum sits below the
    floor the revision exists to establish. EDE says nothing about t_ZD; it moves
    t_DZ to mid-UI (Table 130). Only t_DZ is exempt.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))
    UI = 1000 * 0.45 / tv._fclk.value()

    grid = {LaunchMode.CLK2: 0.50, LaunchMode.CLK4: 0.25}
    for mode, g in grid.items():
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
        step = g * UI
        for name in ("Man_t_DD", "Man_t_ZD", "Man_t_DZ"):
            lo = tv._min[("proposed", name)].value()
            hi = tv._max[("proposed", name)].value()
            assert lo == pytest.approx(hi), f"{mode}: {name} must be deterministic"
            # Compared in NANOSECONDS, not in grid multiples: the spin box carries
            # one decimal, so 36.62 displays as 36.6 and the ratio test would
            # reject a perfectly on-grid value for a display artefact.
            nearest = round(lo / step) * step
            assert abs(lo - nearest) < 0.06, \
                f"{mode}: {name} = {lo} is not a point a {mode.value} clock places " \
                f"(nearest is {nearest:.2f})"
        # t_ZD is a launch method, so it never drops below the revision's 9 ns floor.
        assert tv._min[("proposed", "Man_t_ZD")].value() >= 9.0 - 0.05, mode
        # And the hold-on-acquire legs it exists to satisfy reach at least PARITY
        # with what the revision's own analog minimum delivers.
        #
        # NOT "> 0": nothing the specification offers meets this leg's 9.89 ns floor
        # at the slow corner -- Man_t_ZD,min = 9 ns leaves -0.89 -- so positivity is
        # the wrong test and asserting it would rule out the revision itself. The
        # 0.25 UI launch supplies 9.16 ns, which beats the analog option by 0.16.
        w = tv._per_inequality_worst("proposed")
        analog_residual = 9.0 - 9.89
        for leg in ("MP_hold_ho", "PM_hold_ho"):
            assert w[leg].margin_ns >= analog_residual - 0.05, \
                (mode, leg, w[leg].margin_ns)


def test_the_hold_on_acquire_leg_is_what_the_handover_UI_was_paying_for():
    """`MP_hold_ho` / `PM_hold_ho` were missing until 2026-08-06, and the omission
    was invisible for the same reason the keeper leg's was: an allocated handover
    UI covered it.

    What they bound: the bit sampled at the UI-opening edge is still inside its
    hold window when the ACQUIRING device turns on. Nothing else covers it ---
    non-contention bounds driver OVERLAP (once the releaser is off, the keeper
    holds the level and the acquirer may drive a different one immediately with no
    driver conflict), and the keeper legs bound the RELEASING side.

    Pinned three ways: the leg must be rate-scaled by N_HO (so an allocated UI
    pays for it), it must FAIL on the pre-revision Man_t_ZD,min of 2 ns, and it
    must PASS on the revision's 9 ns. That last pair is the revision's own stated
    reason for the change, so if this test ever stops distinguishing them the leg
    has lost the constraint it was added for.
    """
    UI = 1000 * 0.45 / 12.288
    base = dict(F_CLK_target_MHz=12.288, bus_length_cm=30.0,
                Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP",
                Per_t_IH_max_ns=1.5, tRF_Man_DATA_ns=8.3)

    def leg(zd_min, n_ho):
        return compute(CalcInputs(Man_t_ZD_ns=zd_min, handover_UIs=n_ho,
                                  **base)).breakdowns["MP_hold_ho"].margin_ns

    # The revision's before and after, at ZERO allocated UIs.
    assert leg(2.0, 0.0) < 0, \
        "2 ns must fail -- that is why the revision raised Man_t_ZD,min to 9"
    assert leg(9.0, 0.0) > 0, "9 ns must pass, or the revision does not fix it"
    # One allocated UI pays for it either way, which is why it never surfaced.
    assert leg(2.0, 1.0) > UI - 10, "an allocated handover UI must cover the leg"
    # And it is a hold leg, so it takes t_ZD's MIN corner: more is better.
    assert leg(9.0, 0.0) > leg(2.0, 0.0)


def test_launch_selector_preserves_the_users_clock_rate():
    """Switching launch mode must not reseed or reset F_CLK.

    It routes through its own handler rather than `_on_spec_changed`, because
    that one calls `_reset_to_defaults` and would silently discard a target rate
    the moment the user compared two launch options. Regressed once during
    wiring: 12.288 jumped back to the 13.2 PHY default.
    """
    tv = _view()
    tv._fclk.setValue(12.288)
    for mode in ("4x", "2x", "analog"):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode))
        assert tv._fclk.value() == pytest.approx(12.288), mode


def test_launch_selector_hint_tracks_the_rate_and_flags_the_ceiling():
    """The hint is the only place the rate-dependent bound is visible."""
    tv = _view()
    tv._fclk.setValue(12.288)
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("analog"))
    assert "spread" in tv._launch_hint.text()          # analog: no fixed value
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("4x"))
    assert "9.16 ns fixed" in tv._launch_hint.text()
    assert "17.0 MHz" in tv._launch_hint.text() and "hold" in tv._launch_hint.text()
    # Raising the rate past 4x's limit warns rather than silently going red.
    tv._fclk.setValue(20.0)
    assert "above this mode's limit" in tv._launch_hint.text()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("2x"))
    assert "above this mode's limit" not in tv._launch_hint.text()
    assert "24.6 MHz" in tv._launch_hint.text() and "setup" in tv._launch_hint.text()


def test_launch_selector_is_disabled_and_forced_analog_for_a_soundwire_manager():
    """A SoundWire Manager has no clock-launch option, so the control must not
    offer one — and must not leave a stale clock mode applied to it."""
    tv = _view()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("4x"))
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V8"))
    assert not tv._launch_combo.isEnabled()
    assert tv._launch_combo.currentData() == LaunchMode.ANALOG.value
    # Back to a SWI3S Manager and the choice returns.
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SWI3S_PHY2"))
    assert tv._launch_combo.isEnabled()


def test_launch_mode_round_trips_and_legacy_dicts_default_to_analog():
    tv = _view()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("2x"))
    d = tv.to_dict()
    assert d["launch_mode"] == "2x"
    tv2 = _view()
    tv2.set_from_dict(d)
    assert tv2._launch_combo.currentData() == "2x"
    # A workspace saved before the selector existed can only have been analog.
    d.pop("launch_mode")
    tv3 = _view()
    tv3.set_from_dict(d)
    assert tv3._launch_combo.currentData() == LaunchMode.ANALOG.value


# ---------------------------------------------------------------------------
# 9. The revision carried onto a mixed bus (memo Part II §10.1)
# ---------------------------------------------------------------------------

def test_revision_reaches_only_one_of_two_hold_terms_on_a_mixed_bus():
    """The structural result Part II turns on.

    The revision buys MP hold from two terms that sit on OPPOSITE sides of the
    link: Man_t_DD,min 7->9 (+2.0) needs a SWI3S Manager transmitting, and
    Per_t_IH,max 4.5->1.5 (+3.0) needs a SWI3S Peripheral receiving. A same-spec
    SWI3S bus collects both (+5.0, closes); a mixed bus collects exactly one,
    because the other term belongs to a SoundWire device that does not change.

    So MP hold improves but does not close in either scenario -- not a defect in
    the revision, just the arithmetic of a link whose ends answer to different
    specs.
    """
    F = dict(F_CLK_target_MHz=12.288)
    a_cur = compute(_mixed("SWI3S_PHY2", "SW13_1V8", **F))
    a_prop = compute(_mixed("SWI3S_PHY2_PROP", "SW13_1V8", **F))
    b_cur = compute(_mixed("SW13_1V8", "SWI3S_PHY2", **F))
    b_prop = compute(_mixed("SW13_1V8", "SWI3S_PHY2_PROP", **F))

    # A gains only Man_t_DD,min's +2.0 (its Peripheral is SoundWire).
    gain_a = a_prop.breakdowns["MP_hold"].margin_ns - a_cur.breakdowns["MP_hold"].margin_ns
    assert gain_a == pytest.approx(+2.0, abs=0.02)
    # B gains only Per_t_IH,max's +3.0 (its Manager is SoundWire).
    gain_b = b_prop.breakdowns["MP_hold"].margin_ns - b_cur.breakdowns["MP_hold"].margin_ns
    assert gain_b == pytest.approx(+3.0, abs=0.02)
    # A same-spec bus collects both.
    s_cur = compute(_same("SWI3S_PHY2", **F))
    s_prop = compute(_same("SWI3S_PHY2_PROP", **F))
    gain_s = s_prop.breakdowns["MP_hold"].margin_ns - s_cur.breakdowns["MP_hold"].margin_ns
    assert gain_s == pytest.approx(+5.0, abs=0.02)
    assert gain_s == pytest.approx(gain_a + gain_b, abs=0.02)

    # Both residuals are sub-nanosecond, which is the point of quoting them.
    assert a_prop.breakdowns["MP_hold"].margin_ns == pytest.approx(-0.14, abs=0.02)
    # B closes: the corrected hold bound (t_ZD_Data,Min plus its clock-side
    # re-anchor) rather than t_OH_Data is worth +6.4 ns on a SoundWire-TX leg.
    assert b_prop.breakdowns["MP_hold"].margin_ns == pytest.approx(+6.72, abs=0.02)
    assert b_prop.breakdowns["MP_hold"].margin_ns >= 0


def test_scenario_A_pm_setup_is_not_swi3s_to_fix():
    """A's binding deficit after the revision belongs to SoundWire.

    On A/PM the SoundWire Peripheral transmits, so PM setup is governed by its
    t_OV and slew — no SWI3S revision can move it, and the value must be
    IDENTICAL before and after. B/PM is the SWI3S Peripheral's, and there the
    revision is worth +11 ns. That asymmetry is Part II's conclusion: of the two
    topologies, a SoundWire Manager driving SWI3S Peripherals is the viable one.

    All four values re-baselined by +0.074 ns (2026-08-06) when Σ_cross,PM,setup
    stopped assuming the ground shift cancels across the two legs: cancellation
    needs equal per-leg t_RF, and a mixed bus carries 5.00 vs 5.40 ns. The shift
    is a common offset, so both conclusions survive unchanged — A is still
    revision-invariant, and B's gain is still exactly +11.00 ns.
    """
    F = dict(F_CLK_target_MHz=12.288)
    a_cur = compute(_mixed("SWI3S_PHY2", "SW13_1V8", **F))
    a_prop = compute(_mixed("SWI3S_PHY2_PROP", "SW13_1V8", **F))
    assert a_cur.breakdowns["PM_setup"].margin_ns == pytest.approx(-9.78, abs=0.02)
    assert a_prop.breakdowns["PM_setup"].margin_ns == pytest.approx(-9.78, abs=0.02)
    assert a_prop.breakdowns["PM_setup"].margin_ns == pytest.approx(
        a_cur.breakdowns["PM_setup"].margin_ns, abs=1e-9), \
        "a SWI3S revision must not move a leg the SoundWire side owns"

    b_cur = compute(_mixed("SW13_1V8", "SWI3S_PHY2", **F))
    b_prop = compute(_mixed("SW13_1V8", "SWI3S_PHY2_PROP", **F))
    assert b_cur.breakdowns["PM_setup"].margin_ns == pytest.approx(+1.14, abs=0.02)
    assert b_prop.breakdowns["PM_setup"].margin_ns == pytest.approx(+12.14, abs=0.02)
    # The revision's worth on B is a clean +11 ns, unchanged by the re-baseline —
    # so the ground-shift correction shifted the level, not the conclusion.
    assert (b_prop.breakdowns["PM_setup"].margin_ns
            - b_cur.breakdowns["PM_setup"].margin_ns) == pytest.approx(11.0, abs=5e-3)
    # Scenario B ends up with NO deficit at all under the revision.
    assert all(b.margin_ns >= 0 for b in _guaranteed(b_prop.breakdowns).values())


def test_preset_trf_means_different_things_on_the_two_sides():
    """SWI3S's preset t_RF is a NOMINAL; SoundWire's is a MAXIMUM.

    SWI3S tabulates 3.0-8.3 ns with 5 ns as its reference test condition.
    SoundWire tabulates t_Slew_Clock 2.0-5.4 ns (small/1.8 V) and publishes no
    nominal at all. So a table built from the presets alone compares a SWI3S
    nominal against a SoundWire worst case — which is what the memo's interop
    table did until it was corrected to put each side at its own maximum.

    Pinned so the asymmetry cannot be forgotten again: it is invisible in the
    numbers (5.0 vs 5.4 looks like a rounding difference) and changes every
    cross-spec margin that scales with slew.
    """
    assert preset("SWI3S_PHY2", "Man").tRF_ns == 5.0      # reference condition
    assert preset("SW13_1V8", "Per").tRF_ns == 5.4        # tabulated MAXIMUM
    # SWI3S's own maximum is well above its preset; SoundWire's IS its preset.
    assert preset("SW13_1V8", "Per", "large").tRF_ns == 9.0


def test_mp_hold_at_each_sides_own_slew_maximum():
    """The worst corner the memo's interop table now uses.

    Quoted at the each-side-own-maximum corner (SWI3S 8.3, SoundWire 5.4),
    because that is the only worst case defined for both specs. The same-spec
    figure is ~-6 ns, not the ~-2.6 ns of the 5 ns reference condition — the two
    differ by 3.25 ns and must never be quoted interchangeably.
    """
    from dataclasses import replace as _replace
    F = dict(F_CLK_target_MHz=12.288)
    SWI3S_MAX, SW_MAX = 8.3, 5.4

    def mp_hold(inp, t_man, t_per):
        return compute(_replace(inp, tRF_Man_CLK_ns=t_man, tRF_Man_DATA_ns=t_man,
                                tRF_Per_DATA_ns=t_per)).breakdowns["MP_hold"].margin_ns

    # Same-spec SWI3S: both sides at 8.3.
    s_cur = mp_hold(_same("SWI3S_PHY2", **F), SWI3S_MAX, SWI3S_MAX)
    s_prop = mp_hold(_same("SWI3S_PHY2_PROP", **F), SWI3S_MAX, SWI3S_MAX)
    assert s_cur == pytest.approx(-5.89, abs=0.02)
    assert s_prop == pytest.approx(-0.89, abs=0.02)

    # Mixed: SWI3S side at 8.3, SoundWire side at 5.4.
    a_cur = mp_hold(_mixed("SWI3S_PHY2", "SW13_1V8", **F), SWI3S_MAX, SW_MAX)
    a_prop = mp_hold(_mixed("SWI3S_PHY2_PROP", "SW13_1V8", **F), SWI3S_MAX, SW_MAX)
    b_cur = mp_hold(_mixed("SW13_1V8", "SWI3S_PHY2", **F), SW_MAX, SWI3S_MAX)
    b_prop = mp_hold(_mixed("SW13_1V8", "SWI3S_PHY2_PROP", **F), SW_MAX, SWI3S_MAX)
    assert a_cur == pytest.approx(-5.39, abs=0.02)
    assert a_prop == pytest.approx(-3.39, abs=0.02)
    assert b_cur == pytest.approx(+3.72, abs=0.02)
    assert b_prop == pytest.approx(+6.72, abs=0.02)   # closes at the worst corner

    # The gain decomposition is slew-INDEPENDENT: both revised terms are fixed
    # nanosecond values, so +2.0 / +3.0 / +5.0 hold at this corner too.
    assert s_prop - s_cur == pytest.approx(+5.0, abs=0.02)
    assert a_prop - a_cur == pytest.approx(+2.0, abs=0.02)
    assert b_prop - b_cur == pytest.approx(+3.0, abs=0.02)


def test_example_column_seeds_from_the_proposed_revision():
    """The page opens on a current-vs-proposed comparison.

    Left (Specification, read-only) carries the spec as it stands; right
    (Example, editable) carries the proposed revision. That is what the two
    columns are for, and it makes the delta legible term-by-term without
    labelling anything as proposed.

    The ranges are asserted rather than taken from SpecPreset because the preset
    holds a SINGLE value for t_ZD and t_IH -- fine for SoundWire where they are
    single-valued, but the SWI3S proposal revises them as ranges, and reading
    them from the preset collapsed min onto max (Per_t_IH became 1.5-1.5).
    """
    tv = _view()
    expect = {
        "Man_t_DD": ((7.0, 18.0), (9.0, 23.0)),
        "Man_t_ZD": ((2.0, 18.0), (9.0, 23.0)),
        "Per_t_DD": ((2.0, 20.0), (2.0, 9.0)),
        "Per_t_ZD": ((2.0, 18.0), (2.0, 9.0)),
        "Per_t_IH": ((0.0, 4.5), (0.0, 1.5)),
        "Per_t_IS": ((0.0, 4.0), (0.0, 4.0)),      # unrevised: identical
    }
    for row, (spec, example) in expect.items():
        got_s = (tv._min[("spec", row)].value(), tv._max[("spec", row)].value())
        got_e = (tv._min[("proposed", row)].value(), tv._max[("proposed", row)].value())
        assert got_s == pytest.approx(spec), f"{row} Specification column"
        assert got_e == pytest.approx(example), f"{row} Example column"

    # The delta is the proposal's effect, and it is what the summary reports.
    txt = tv.summary_text()
    assert "MP hold" in txt and "PM setup" in txt

    # Nothing is labelled "proposed" — that is socialised separately.
    assert "PROPOSED" not in tv._title.text()
    assert "PROPOSED" not in tv._ceiling_label.text()
    # ...and the selector offers ratified specs only.
    data = [tv._man_spec_combo.itemData(i) for i in range(tv._man_spec_combo.count())]
    assert not any(d.endswith("_PROP") for d in data), data


def test_proposal_delta_is_visible_in_the_two_columns():
    """The Spec-vs-Example delta must BE the revision's effect.

    +5.00 on MP hold, +11.00 on PM setup, -5.00 on MP setup -- the same
    decomposition the memo quotes. Pinned here because this is now the only
    place in Studio where the proposal is visible, so a reseed regression would
    silently turn the page back into a spec-vs-spec comparison of nothing.
    """
    tv = _view()
    tv._fclk.setValue(12.288)
    spec = tv._per_inequality_worst("spec")
    ex = tv._per_inequality_worst("proposed")
    for ineq, want in (("MP_hold", +5.00), ("PM_setup", +11.00), ("MP_setup", -5.00)):
        delta = ex[ineq].margin_ns - spec[ineq].margin_ns
        assert delta == pytest.approx(want, abs=0.02), ineq
    # PM hold is untouched by the revision.
    assert ex["PM_hold"].margin_ns == pytest.approx(spec["PM_hold"].margin_ns, abs=1e-9)


# ---------------------------------------------------------------------------
# 10. Handover non-contention
# ---------------------------------------------------------------------------
#
# A DIFFERENT question from the "_ho" setup variants already covered above.
# Those ask whether the ACQUIRING driver gets valid data to the receiver in
# time; these ask whether the RELEASING driver let go before the acquiring one
# started -- i.e. whether the two ever drive the bus at once.
#
#   N_HO*UI + t_ZD,min(acquiring) - t_DZ,max(releasing) >= 0
#
# Positive is an undriven float gap the Manager's bus keeper holds ({ASW3805});
# negative is contention, which nothing in the spec permits for an audio-mode
# data handover.


def test_phy1_hands_over_in_zero_uis_because_its_own_numbers_allow_it():
    """The §11.1.5 note, reproduced by the model rather than hard-coded --- and the
    model now says the note is a PEER-TO-PEER statement.

    "EndDriveEarly is typically not used with PHY1 because this FBCSE operating
    mode already allows for handover between outputs without needing extra UIs
    due to the inequality satisfied by the values of the output timing
    parameters (t_ZD_Min_Phy1 >= t_DZ_Max_Phy1)."

    The note's bare difference, 14 - 10 = 4 ns, is EXACTLY what P->P gives: both
    ends are peripherals, so they share a reference convention and the
    clock-detection lead cancels. That is the check below, and it is
    rate-independent, which is the note's actual content.

    THE MIXED LEGS DO NOT GIVE 4 ns, and that is a finding rather than a defect.
    A peripheral's output parameters are measured from ITS DETECTION of the clock
    edge (Table 129, "after edge on clock input"; Figure 174 marks it at
    V_IH / V_IL of the input), while a Manager's are measured from its own internal
    reference (Figure 176) and carry no such offset. So on a mixed handover the two
    ends do not share a zero, and the lead enters with a sign that depends on which
    class is releasing:

        MP  the peripheral ACQUIRES, so its lateness keeps it clear   -> +detect
        PM  the peripheral RELEASES, so its lateness holds the bus    -> -detect

    Checked on a ZERO-LENGTH bus, so none of this is propagation: it is the
    reference-class difference alone, and it does not go away on a short bus.
    """
    assert default_handover_UIs("SWI3S_PHY1", "SWI3S_PHY1") == 0.0
    # The two clock-crossing corners, taken from the envelope rather than written
    # out, so this test cannot drift from the coefficients it is checking.
    _e = CalcInputs(bus_length_cm=0.0).envelope()
    late = _e.sigma_pm_setup_clk_dgnd_pos * 5.0
    early = _e.sigma_pm_hold_rf_clk * 5.0
    # P->P has TWO peripherals, and they read the SAME clock edge: the Manager drives it from
    # one pin, so one slew serves both detections and both crossings scale by the same t_RF.
    # A previous revision cornered them at opposite ends of a tolerance band; withdrawn, see
    # `_gather_lane_terms`. So P->P's pair is the same two numbers as every other leg's.
    early_i, late_i = early, late
    for fclk in (0.032, 6.4, 6.6):
        r = compute(CalcInputs(
            Man_spec="SWI3S_PHY1", Per_spec="SWI3S_PHY1", handover_UIs=0.0,
            F_CLK_target_MHz=fclk, bus_length_cm=0.0,
            Man_t_DZ_max_ns=10.0, Per_t_DZ_max_ns=10.0,
            Man_t_ZD_ns=14.0, Per_t_ZD_ns=14.0,
        ))
        # NO LEG EQUALS THE NOTE'S BARE DIFFERENCE, and that is the finding. Every
        # one of the three has at least one peripheral on it, and a peripheral's
        # t_DZ / t_ZD are referenced to ITS OWN detection of the clock -- so each
        # leg carries the crossing at whichever corner its roles demand:
        #
        #   M->P  the peripheral only ACQUIRES        -> + early
        #   P->M  the peripheral only RELEASES        -> - late
        #   P->P  one of each, both on the same edge   -> + early - late
        #
        #   M->P  the peripheral only ACQUIRES        -> + early
        #   P->M  the peripheral only RELEASES        -> - late
        #   P->P  one of each, two independent parts  -> + early_i - late_i, each at
        #         its own end of the t_RF tolerance band
        #
        # The 4 ns is still in there, as the DEVICE half; what the note omits is
        # where those parameters' own zero sits on the Manager's timeline.
        assert r.breakdowns["MP_contention"].margin_ns == pytest.approx(4.0 + early), fclk
        assert r.breakdowns["PM_contention"].margin_ns == pytest.approx(4.0 - late), fclk
        assert r.breakdowns["PP_contention"].margin_ns == pytest.approx(
            4.0 + early_i - late_i), fclk
        # Rate-independence is the note's content: no UI appears in any of these
        # legs, so no clock rate changes them. A PASSING leg reports inf; a failing
        # zero-UI leg reports 0.0 rather than inf, which says "no legal rate"
        # instead of "any rate" -- both are statements that the rate is irrelevant.
        # A PASSING zero-UI leg reports inf; a FAILING one reports 0.0 rather than
        # inf, which says "no legal rate" instead of "any rate". Both are statements
        # that no clock rate reaches the leg, which is the note's real content.
        assert r.breakdowns["MP_contention"].F_max_MHz == math.inf, fclk
        for leg in ("PM_contention", "PP_contention"):
            assert r.breakdowns[leg].F_max_MHz == 0.0, (leg, fclk)

    # THE FINDING, pinned so it cannot be lost: two of the three are NEGATIVE at
    # zero bus length, so it is not a propagation effect -- it is the crossing
    # alone. PHY1's zero-UI handover does not close on either leg with a releasing
    # peripheral once the two reference classes are put on one timeline.
    #
    # Raised in docs/MODEL_AUDIT.md and NOT resolved by this model: it is a claim
    # about ratified behaviour, and whether the Sec. 11.1.5 note is a device-parameter
    # statement or a bus one is the working group's to settle.
    assert r.breakdowns["PM_contention"].margin_ns < 0
    assert r.breakdowns["PP_contention"].margin_ns < 0
    assert r.breakdowns["MP_contention"].margin_ns > 0, \
        "M->P must still close: its peripheral only acquires, so it gains the crossing"


def test_the_launch_mode_never_governs_the_specification_column():
    """A clocked launch is the revision's option, so it cannot touch tabulated values.

    The wording that offers it -- "9-23 ns or (0.5 UI 2x CLK, 0.25 UI 4x CLK)" -- is in
    the PHY2 revision. Ratified PHY1 and PHY2 tabulate analog output delays and give a
    Manager no multiplied clock, so the Specification column is analog whatever the
    selector says.

    THIS IS A REGRESSION TEST FOR A SILENT, DESTRUCTIVE BUG. The mode was applied to
    both columns, and `snap_to_launch_grid(2.0, 2x)` is 0.00 -- so selecting a 2x clock
    turned the spec column's Man_t_DD (7-18), Man_t_ZD (2-18) and Man_t_DZ (0-10) all
    into 0.00, and every leg reading them reported margins for a Manager that launches
    data with no delay. Nothing failed; the numbers were just wrong. The full gate was
    green over it, which is why this asserts the ROWS and not only the margins.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2"))
    tab = {n: (tv._min[("spec", n)].value(), tv._max[("spec", n)].value())
           for n in ("Man_t_DD", "Man_t_ZD", "Man_t_DZ")}
    assert tab["Man_t_DD"] == (7.0, 18.0), tab      # the tabulated values, for grounding
    for mode in (LaunchMode.CLK2, LaunchMode.CLK4, LaunchMode.ANALOG):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
        for name, want in tab.items():
            got = (tv._min[("spec", name)].value(), tv._max[("spec", name)].value())
            assert got == want, f"{mode.value}: spec {name} moved {want} -> {got}"
        # And the model agrees, or the row on screen and the value in force diverge.
        assert tv.inputs("spec").launch_mode is LaunchMode.ANALOG, mode
        if mode is not LaunchMode.ANALOG:
            assert tv.inputs("proposed").launch_mode is mode, mode


def test_a_clocked_launch_never_snaps_below_the_tabulated_minimum():
    """Nearest-point snapping rounds DOWN through a row's minimum, and on a coarse
    grid it rounds down to zero.

    The revision's Man_t_DD,min of 9.0 ns is 0.246 UI at 12.288 MHz, so a 2x clock's
    nearest point is 0.00 UI. Knife-edge as well as wrong: EDE's 0.25 UI seed is
    9.155 ns, which rounds the other way to 0.50 UI, so 0.155 ns of seed decided
    between 0.00 and 18.31. The floor makes it the lowest placeable point that still
    honours the minimum.
    """
    UI = 1000 * 0.45 / 12.288
    assert snap_to_launch_grid(9.0, LaunchMode.CLK2, 12.288, 0.45) == \
        pytest.approx(0.0), "unfloored nearest-point snapping still rounds to zero"
    assert snap_to_launch_grid(9.0, LaunchMode.CLK2, 12.288, 0.45,
                               floor_ns=9.0) == pytest.approx(0.5 * UI)
    # The floor lifts to a GRID point, not to the floor itself.
    assert snap_to_launch_grid(9.0, LaunchMode.CLK4, 12.288, 0.45,
                               floor_ns=9.0) == pytest.approx(0.25 * UI)
    # A value already at or above the floor is untouched by it.
    assert snap_to_launch_grid(27.5, LaunchMode.CLK4, 12.288, 0.45,
                               floor_ns=0.0) == pytest.approx(0.75 * UI)

    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2"))
    for mode in (LaunchMode.CLK2, LaunchMode.CLK4):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
        for name in ("Man_t_DD", "Man_t_ZD"):
            got = tv._min[("proposed", name)].value()
            assert got > 0.0, f"{mode.value}: proposed {name} snapped to {got}"


def test_the_level_is_owned_by_the_hold_ho_legs_not_by_contention():
    """Why the contention legs may permit an overlap at all.

    The overlap allowance bounds driver CURRENT. It says nothing about the voltage on
    the line while both drivers are on, so permitting an overlap is only defensible if
    another leg owns the level. The `*_hold_ho` legs do: the bit sampled at the
    UI-opening edge must stay valid for t_IH,max past it, and the ACQUIRING device's
    turn-on out of high-Z is exactly what ends it -- the same event the allowance
    brings forward.

    Pinned STRUCTURALLY rather than as margins, because the two families constrain
    different parameter pairs and are not orderable against each other. What must hold
    is that the level leg exists on both guaranteed directions, and that it is funded
    by the acquirer's t_ZD -- if it ever stopped reading t_ZD, contention would be
    allowing an overlap that nothing else bounds.

    And the deliberate gap: there is no PP setup or hold leg, because
    peripheral-to-peripheral communication is not guaranteed. Two peripherals can
    damage each other's DRIVERS -- bounded by PP_contention -- without either being
    obliged to read the other's DATA. That asymmetry is the reason the set of
    inequalities is not symmetric in the three legs, and it reads as an omission
    unless it is written down.
    """
    r = compute(CalcInputs(
        Man_spec="SWI3S_PHY2_EDE", Per_spec="SWI3S_PHY2_EDE", handover_UIs=0.0,
        Man_ede=True, Per_ede=True, F_CLK_target_MHz=12.288,
        launch_mode=LaunchMode.CLK4))
    # Both guaranteed directions carry the level leg, and each is funded by the
    # acquiring device's turn-on out of high-Z.
    for leg, acquirer in (("MP_hold_ho", "Man"), ("PM_hold_ho", "Per")):
        assert leg in r.breakdowns, f"{leg} is what bounds the level; it must exist"
        syms = [t.symbol for t in r.breakdowns[leg].terms]
        assert any(s.startswith(f"{acquirer}_t_ZD") for s in syms), \
            f"{leg} must be funded by {acquirer}_t_ZD, got {syms}"
    # P2P now has BOTH: a contention leg (two drivers can damage each other whether or
    # not either reads the other) and, as of this revision, its own data legs. This
    # assertion used to be the opposite -- "P2P communication is not guaranteed, so it gets
    # no setup/hold leg" -- and it was a correct reading of the scope at the time. The scope
    # widened; the leg that funds the level here is still the acquirer's own turn-on, which
    # is what this test is about.
    assert "PP_contention" in r.breakdowns
    for leg in ("PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho"):
        assert leg in r.breakdowns, f"{leg} is a data leg now, not an omission"
    assert any(t.symbol.startswith("Per_t_ZD") for t in r.breakdowns["PP_hold_ho"].terms), \
        "the P->P level leg is funded by the acquiring peripheral's turn-on, like MP/PM"


def test_the_out_of_scope_split_matches_the_legs_own_terms():
    """Which P->P legs bound the bus, checked against WHOSE LEVEL each one protects.

    An out-of-scope declaration is a claim about an inequality's TERMS, and nothing
    checked it against them -- so `PP_hold_ho` sat excluded on the strength of its name
    for as long as it existed, in this model and in the reference analysis both.

    THE RULE. A leg is out of scope iff satisfying it would require peripheral B to READ
    a level peripheral A drove in order to be read. On the three setup/hold legs the
    aggressor's launch and the level at risk belong to the SAME device (A drives, B
    samples what A drove), so the claim holds. On `PP_hold_ho` they do not: A has just
    acquired the bus, and the level B is holding belongs to whoever RELEASED -- a third
    device that appears nowhere in the arithmetic. When that releaser is the MANAGER the
    level is Manager-to-peripheral data, which a peripheral sink must read, so the leg
    binds and no P->P scope argument reaches it.

    Pinned as the SPLIT and as the reason, because the two halves fail differently: a
    leg wrongly IN the set silently stops bounding F_max, and a leg wrongly OUT of it
    reports "no clock rate works" for a bus whose guaranteed directions are fine.
    """
    assert set(CalcResults._PP_DATA_LEGS) == {"PP_setup", "PP_setup_ho", "PP_hold"}, \
        ("the out-of-scope set moved. `PP_hold_ho` must NOT be in it -- the level it "
         "protects belongs to the releaser, who may be the Manager -- and the other "
         "three must be, because on each of those the aggressor drove the level itself")

    # AT THE WORST CORNER, not at the typicals. The leg reads +3.37 at nominal values and
    # -7.67 once the search has it, so a test written at the midpoint would assert the
    # opposite conclusion and pass.
    rows = default_swi3s_rows()

    def worst(base, leg, rs=None):
        rs = rows if rs is None else rs
        return compute(apply_row_picks(base, rs, find_worst_corner_rows(leg, rs, base)))

    base = _same("SWI3S_PHY2_PROP", handover_UIs=0.0)
    assert worst(base, "PP_hold_ho").breakdowns["PP_hold_ho"].margin_ns < 0, \
        "the premise of the rest of this test: PP_hold_ho fails at zero handover UIs"
    # MEMBERSHIP OF THE BOUNDING SET IS THE MECHANISM, so check it there rather than only
    # in the frozenset above -- `_bounding` is what F_max and the results tree read.
    b = worst(base, "PP_hold_ho")._bounding()
    assert "PP_hold_ho" in b and "PP_hold" not in b, sorted(b)

    # THE BEHAVIOURAL HALF, and it needs a configuration where PP_hold_ho is the ONLY
    # failing bounding leg. Otherwise MP_hold's own -0.74 ns pins the rate to zero and
    # F_max says nothing about this leg at all -- which is how the first version of this
    # assertion passed while naming MP_hold. A launch at 20 ns clears both MP hold legs
    # (the floor is 9.89) and leaves PP_hold_ho untouched, since no Manager parameter
    # appears in it -- which is itself the reason the leg cannot be fixed from that side.
    lift = [CalcRow(r.name, r.binds,
                    ParamRange(20.0, 20.0, 20.0) if r.name in ("Man_t_DD", "Man_t_ZD")
                    else r.range, r.linked_to, r.is_window)
            for r in rows]
    r0 = worst(base, "PP_hold_ho", lift)
    assert r0.breakdowns["MP_hold"].margin_ns > 0, "the construction must clear MP hold"
    assert r0.breakdowns["PP_hold_ho"].margin_ns < 0, "...and leave PP_hold_ho failing"
    assert r0.F_max_binding_MHz == 0.0 and r0.binding_inequality == "PP_hold_ho", \
        (f"a failing PP_hold_ho must bind the bus and name itself, got "
         f"{r0.binding_inequality} at {r0.F_max_binding_MHz}")
    # ...while `PP_hold`, which fails at every allocation, must never do either.
    base_ho = _same("SWI3S_PHY2_PROP", handover_UIs=1.0)
    r1 = worst(base_ho, "PP_hold", lift)
    assert r1.breakdowns["PP_hold_ho"].margin_ns > 0, \
        "one allocated UI answers the leg, which is why the UI is what the paper concedes"
    assert r1.breakdowns["PP_hold"].margin_ns < 0, \
        ("PP_hold has no handover-UI term and stays negative -- so if it were in the "
         "bounding set no allocation could ever clear the bus, which is the reason the "
         "exclusion exists for it and not for its handover variant")
    assert r1.binding_inequality != "PP_hold" and r1.F_max_binding_MHz > 0.0, \
        f"PP_hold must not bound the bus, got {r1.binding_inequality}"


def test_contention_is_overlap_bounded_by_one_flight_not_a_gap():
    """What counts as contention, and what the bus therefore costs each leg.

    A SPICE result, not a reading of the spec: two drivers may be on together for up
    to the flight time between them without contending. The acquirer turns on into a
    Z0 load and sources V/Z0 -- an ordinary switching current -- and the releaser does
    not learn it has company until that wave arrives one traversal later. So the
    criterion bounds OVERLAP at +t_PD, and the terms are absolute driver-enable times:

        overlap = T_off(releaser) - T_on(acquirer) <= t_PD

    THIS TEST USED TO ASSERT THE OPPOSITE SIGN. It pinned an acquirer's-pin frame,
    which charged the releaser's data one traversal to reach the acquirer and then
    required margin >= 0 -- a GAP of one flight where one flight of overlap is free.
    Wrong side of the physics by 2*t_PD, and it read as rigour because the frame was
    argued at length. What it did get right, and what is kept, is that a bare
    difference of two parameter values is a MIXED frame; the fix is to put both events
    in absolute time, not to move them both to one pin.

    The bus then costs each leg only what the clock skew costs, net of the allowance:

        MP  acquirer's clock t_PD late, plus the allowance -> +2*t_PD, so this is the
            one leg that is WORST ON A SHORT BUS
        PM  releaser's clock t_PD late, plus the allowance -> 0, BUS-INDEPENDENT
        PP  same as PM: releaser at the far end, acquirer at the Manager end, which is
            also the pair furthest apart and so the largest allowance

    PM and PP coming out bus-independent is structural, not arithmetic: the distance
    that makes a releasing peripheral late is the distance that licenses the overlap.
    """
    kw = dict(Man_spec="SWI3S_PHY1", Per_spec="SWI3S_PHY1", handover_UIs=0.0,
              Man_t_DZ_max_ns=10.0, Per_t_DZ_max_ns=10.0,
              Man_t_ZD_ns=14.0, Per_t_ZD_ns=14.0)
    near = compute(CalcInputs(bus_length_cm=0.0, **kw)).breakdowns
    far = compute(CalcInputs(bus_length_cm=30.0, **kw)).breakdowns   # t_PD = 2.0 ns

    # The two peripheral-releasing legs: skew and allowance are the same distance.
    for leg in ("PM_contention", "PP_contention"):
        assert far[leg].margin_ns == pytest.approx(near[leg].margin_ns), \
            f"{leg} must be bus-length independent"
    # M->P banks both, so it gains 2 traversals -- and its worst case is the SHORT
    # bus, which is why `find_worst_corner_rows` drives the bus row to 0 for it.
    assert far["MP_contention"].margin_ns - near["MP_contention"].margin_ns == \
        pytest.approx(+4.0)
    # The bus is ONE netted term on every leg, as a signed count of traversals in
    # the same idiom as the 2*t_PD the PM legs already carry. A zero count states the
    # cancellation instead of showing a +t_PD/-t_PD pair at the bus row's typ value --
    # a value that is not a corner, and read as though 15 cm meant something.
    for leg, n in (("MP_contention", +2), ("PM_contention", 0), ("PP_contention", 0)):
        pd = [t for t in far[leg].terms if t.symbol.endswith("·t_PD")]
        if not n:
            # DROPPED, not shown as zero: the count is zero for every input, so the
            # term could never be anything else. `0·UI` is dropped too now, for the
            # weaker but sufficient reason that nothing is allocated -- see
            # test_no_handover_ui_means_no_ui_term. The zero that STAYS is `Man_t_IH`,
            # a parameter whose value happens to be 0 on this spec.
            assert pd == [], f"{leg}: a structurally-zero bus term must be dropped: {pd}"
            continue
        assert len(pd) == 1, f"{leg}: the bus must be one term, got {len(pd)}"
        assert pd[0].symbol == f"{abs(n)}·t_PD", (leg, pd[0].symbol)
        assert pd[0].value_ns == pytest.approx(n * 2.0), (leg, pd[0].value_ns)


def test_pp_data_legs_carry_the_three_crossings_and_only_two_are_correlated():
    """P->P is a third DIRECTION, and its crossing is not either existing envelope.

    Three crossings enter when peripheral A drives and peripheral B samples:

        clk@A    A's detection of the clock, where its launch is referenced
        data@B   B's detection of A's data
        clk@B    B's detection of the clock, which IS its sample edge

    clk@B and data@B are the same die at nearly the same instant, so they are correlated and
    their DIFFERENCE is exactly the Delta_cross,MP construction -- which is why these legs
    reuse it rather than coining an envelope. clk@A is an unrelated part and is charged in
    full, at the band end that hurts.

    Asserted as the identity, not as a number: the construction is ported from the
    standalone tool's `sigma_pp_setup`/`sigma_pp_hold`, while the magnitudes follow this
    model's own envelope inputs (V_IH/V_IL, supply, noise), which are not the paper's.
    """
    from swi3s_studio.timing.calculator import _per_clk_cross_ns

    inp = CalcInputs()
    env, r = inp.envelope(), compute(inp)

    def crossings(leg):
        # `"X_" in`, not `startswith`: a GATHERED pair's symbol opens with a bracket —
        # `(X_PP,A,late - X_PP,B,early) · Man_t_RF,CLK` — because two crossings of one pin net
        # into one product. A `startswith` filter silently returns 0 for it.
        return sum(t.value_ns for t in r.breakdowns[leg].terms if "X_" in t.symbol)

    late = _per_clk_cross_ns(inp, env, corner="late")
    early = _per_clk_cross_ns(inp, env, corner="early")
    d_set = env.delta_cross_MP_ns(inp.tRF_Man_CLK_ns, inp.tRF_Man_DATA_ns, direction="setup")
    d_hold = env.delta_cross_MP_ns(inp.tRF_Man_CLK_ns, inp.tRF_Man_DATA_ns, direction="hold")

    # Setup wants A's clock LATE; hold wants it EARLY, and there it is the one credit.
    assert crossings("PP_setup") == pytest.approx(-(late + d_set))
    assert crossings("PP_hold") == pytest.approx(early - d_hold)
    # A's clock crossing and B's are the SAME PIN's, so they gather into a single product;
    # B's DATA crossing is a different row and stays its own term. Each P->P leg therefore
    # shows TWO crossing terms, not three: the netted clock and the peripheral's data.
    for leg in ("PP_setup", "PP_hold"):
        syms = sorted(t.symbol for t in r.breakdowns[leg].terms if "X_PP," in t.symbol)
        role = "late" if "setup" in leg else "early"
        gathered = ("(X_PP,A,late - X_PP,B,early)" if "setup" in leg
                    else "(X_PP,B,late - X_PP,A,early)")
        assert syms == sorted([f"{gathered} · Man_t_RF,CLK",
                               f"X_PP,B,{role} · Per_t_RF,DATA"]), syms
        # B's DATA lane is the peripheral's: the data reaching B was driven by A, not by the
        # Manager. Reusing the MP tuples verbatim charged Man DATA on a link the Manager does
        # not drive, which only showed once the two lanes were given different values.
        assert not any("Man_t_RF,DATA" in s for s in syms), syms


def test_pp_placement_is_a_selector_and_the_two_legs_read_opposite_ends_of_it():
    """Four placements, two costs, and NOT a corner row.

    A peripheral references its launch to its OWN clock arrival and the sampler its edge to
    its own, so a P->P leg pays `t_clk(driver) + t_data(driver->sampler) - t_clk(sampler)`.
    Three of the four are ZERO -- data that chases the clock down the same line arrives
    exactly as the clock does (the same reason MP setup pays no t_PD), and with BOTH devices
    far the two clock arrivals cancel and the data has no distance left to cover. Only
    driver-far/sampler-near pays, and it pays twice the flight. The bracket is a COST for
    setup and a CREDIT for hold, so `unknown` hands each leg its own end.

    A SELECTOR, because as a corner ROW its value multiplied the bus-length row and the
    margin stopped being affine in each parameter -- which is the one thing
    `find_worst_corner_rows` requires. It duly missed the worst corner: on PP_setup it left
    the bus at 15 cm where 30 cm is 2 ns worse, because with the placement at its typical of
    zero the leg looked bus-insensitive. The brute-force check below is the guard.
    """
    import itertools

    base = CalcInputs()
    t_pd = base.t_PD_ns()

    def pd_terms(placement, leg):
        b = compute(dc_replace(base, pp_placement=placement)).breakdowns[leg]
        return [(t.symbol, round(t.value_ns, 6)) for t in b.terms if t.symbol == "t_PD"]

    # TWO TERMS, NOT ONE `2·t_PD`, since the legs went into time order: the count is two
    # different FLIGHTS -- the clock reaching A at the far end, then A's data running back to
    # B -- so they bracket A's launch instead of sitting together, and each carries a note
    # saying which it is. The contention legs' bus term stays NETTED for the opposite reason
    # (see the test above): there the pair cancels, and printing it apart invited a reader to
    # look for a bus effect that is not there.
    # Unknown: setup takes the cost, hold takes none -- each its own worst.
    assert pd_terms("unknown", "PP_setup") == [("t_PD", -t_pd), ("t_PD", -t_pd)]
    assert pd_terms("unknown", "PP_hold") == []
    # Driver at the far end: setup pays it and hold BANKS it.
    assert pd_terms("driver_far", "PP_setup") == [("t_PD", -t_pd), ("t_PD", -t_pd)]
    assert pd_terms("driver_far", "PP_hold") == [("t_PD", +t_pd), ("t_PD", +t_pd)]
    # And they name their own flights, which is the only thing distinguishing them.
    notes = [t.note for t in compute(dc_replace(base, pp_placement="driver_far"))
             .breakdowns["PP_setup"].terms if t.symbol == "t_PD"]
    assert "clock" in notes[0] and "far end" in notes[0], notes
    assert "data" in notes[1] and "back" in notes[1], notes
    # EVERY OTHER PLACEMENT COSTS NOTHING EITHER WAY, enumerated from the source rather than
    # listed here: two ends and two devices is four placements, and a fifth added to
    # `PP_PLACEMENT_LABELS` without a cost decided would otherwise go untested. `both_far`
    # arrived exactly that way -- it was missing from the enumeration and read as a case not
    # modelled, when t_PD + 0 − t_PD is simply zero.
    from swi3s_studio.timing.calculator import PP_PLACEMENT_LABELS

    free = set(PP_PLACEMENT_LABELS) - {"unknown", "driver_far"}
    assert free == {"both_near", "driver_near", "both_far"}, (
        f"a placement was added or removed: {sorted(free)} — decide its bracket in "
        f"`pp_net_pd` and say so here")
    for placement in sorted(free):
        assert pd_terms(placement, "PP_setup") == [], placement
        assert pd_terms(placement, "PP_hold") == [], placement

    # And the search is exact again: no combination of the geometric/peripheral rows beats
    # what it finds. (A subset, brute-forced -- the full product is 3**17.)
    rows = default_swi3s_rows()
    sub = [r for r in rows if r.name in ("bus length (cm)", "t_PD,mis fraction", "Per_t_DD",
                                         "Per_t_IS", "Per_t_IH", "t_RF Per DATA")]
    for leg in ("PP_setup", "PP_hold"):
        got = compute(apply_row_picks(
            base, rows, find_worst_corner_rows(leg, rows, base))).breakdowns[leg].margin_ns
        brute = min(
            compute(apply_row_picks(base, sub, dict(zip([r.name for r in sub], combo)))
                    ).breakdowns[leg].margin_ns
            for combo in itertools.product(("min", "typ", "max"), repeat=len(sub)))
        assert got <= brute + 5e-3, f"{leg}: search found {got:+.3f}, brute force {brute:+.3f}"


def test_pp_completes_the_launch_method_2x2():
    """Setup and hold, each on both launch methods -- the same structural grid MP and PM
    carry. A receiver cannot tell whether an edge came from t_DD or out of high-Z, so both
    must satisfy both; carrying three quarters of that grid is what hid `MP_hold_ho`.

    The handover UI credits HOLD only: it puts the acquirer's turn-on a UI further from the
    sampling edge, while setup's sampling edge moves with the launch and nets out.
    """
    r = compute(CalcInputs(handover_UIs=1.0))
    for leg in ("PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho"):
        assert leg in r.breakdowns
    def syms(leg):
        return [t.symbol for t in r.breakdowns[leg].terms]
    assert any(s.startswith("Per_t_DD") for s in syms("PP_setup")), syms("PP_setup")
    assert any(s.startswith("Per_t_ZD") for s in syms("PP_setup_ho")), syms("PP_setup_ho")
    assert any(s.startswith("Per_t_DD") for s in syms("PP_hold")), syms("PP_hold")
    assert any(s.startswith("Per_t_ZD") for s in syms("PP_hold_ho")), syms("PP_hold_ho")
    assert "1·UI" in syms("PP_hold_ho") and "1·UI" not in syms("PP_setup_ho")
    # Setup is a UI-consuming leg, so it reports a rate ceiling; hold does not.
    assert r.breakdowns["PP_setup"].is_setup and not r.breakdowns["PP_hold"].is_setup


def test_pp_hold_is_the_tightest_data_leg_and_says_what_it_would_cost():
    """P->P hold is PM hold with the flight credit deleted and a peripheral's t_IH in place
    of the Manager's, which is why it is the leg that decides whether P->P can be promised.

    PM hold banks +2·t_PD because the data returns along the clock's own path; P->P hold's
    worst placement gives nothing back. Per_t_IH,max is 4.5 ns where Man_t_IH,max is 0. So
    the two differences compound, and this records the consequence rather than asserting a
    conclusion: the leg is short, and the shortfall IS the price of guaranteeing P->P.
    """
    rows = default_swi3s_rows()
    base = CalcInputs()
    worst = {}
    for leg in ("PM_hold", "PP_hold"):
        picks = find_worst_corner_rows(leg, rows, base)
        worst[leg] = compute(apply_row_picks(base, rows, picks)).breakdowns[leg]
    assert worst["PP_hold"].margin_ns < worst["PM_hold"].margin_ns, \
        "P->P hold must be tighter than PM hold -- no flight credit, and a peripheral's t_IH"
    assert worst["PP_hold"].margin_ns < 0, \
        "if P->P hold ever closes on PHY2 as specified, this test should say so loudly"
    # PM hold's credit is the term P->P hold does not get: TWO traversals, one per flight,
    # since the legs read in time order (clock out to the launcher, data back to the sampler).
    pm_pd = [t for t in worst["PM_hold"].terms if t.symbol == "t_PD"]
    assert len(pm_pd) == 2 and all(t.op == "+" for t in pm_pd), pm_pd
    assert not any(t.symbol == "t_PD" for t in worst["PP_hold"].terms)


def test_no_handover_ui_means_no_ui_term():
    """With no handover UI scheduled, no inequality shows one.

    `0·UI` used to print on all five legs that carry the allocation, on the argument that a
    term becoming `1·UI` when a UI IS allocated is worth stating at zero. It is worth
    stating -- but not as a row of arithmetic contributing nothing, least of all on the legs
    whose entire point is that NOTHING separates the release from the acquirer's turn-on.

    Display only: the dropped term was worth 0.0 ns and every margin is the sum of its
    terms, so this asserts the margins are bit-identical either way by reconstructing each
    from its remaining terms.

    A FRACTIONAL allocation still shows, which is the guard against "drop it unless it is a
    whole UI": handovers can be programmed longer (§11.1.1.1) and `handover_UIs` is a float.
    """
    legs = ("MP_hold_ho", "PM_hold_ho", "MP_contention", "PM_contention", "PP_contention")

    def terms(n_ho, leg):
        return compute(CalcInputs(handover_UIs=n_ho)).breakdowns[leg]

    for leg in legs:
        none = terms(0.0, leg)
        assert not any("UI" in t.symbol for t in none.terms), \
            f"{leg} still shows a UI term with no handover UI allocated: " \
            f"{[t.symbol for t in none.terms]}"
        # The margin is untouched by the omission.
        assert none.margin_ns == pytest.approx(sum(t.value_ns for t in none.terms))
        # Allocate one and it is back, worth exactly a UI.
        one = terms(1.0, leg)
        ui = [t for t in one.terms if t.symbol == "1·UI"]
        assert len(ui) == 1, (leg, [t.symbol for t in one.terms])
        assert ui[0].value_ns == pytest.approx(CalcInputs().UI_ns())
        assert one.margin_ns - none.margin_ns == pytest.approx(CalcInputs().UI_ns())
        # A part-UI allocation is not rounded away.
        half = [t for t in terms(0.5, leg).terms if t.symbol == "0.5·UI"]
        assert len(half) == 1, (leg, "a fractional allocation must still show")

    # The zero that STAYS: Man_t_IH reads 0.00 ns on PHY2 because that is the spec's
    # value, not because nothing was allocated, so the row keeps it.
    ih = [t for t in terms(0.0, "PM_hold_ho").terms if t.symbol == "Man_t_IH"]
    assert len(ih) == 1 and ih[0].value_ns == pytest.approx(0.0), \
        "a parameter that happens to read zero must still be shown"


def test_peripheral_to_peripheral_handover_is_its_own_leg():
    """A P2P handover has no privileged reference plane between the two devices.

    The DEVICE half is symmetric — both t_DZ and t_ZD come from the Peripheral
    spec — so on a zero-length bus PP equals what a Per-to-Per pair would give.
    What is NOT symmetric is the clock skew, and its worst case is the releasing
    peripheral being the farther of the two.

    ASSUMPTION, stated because it bounds rather than measures: both peripherals
    are taken to be anywhere on a bus of `bus_length_cm`, so the worst-case skew
    is the full -t_PD. A real pair's separation can be shorter.
    """
    r = compute(CalcInputs(
        Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2", handover_UIs=1.0,
        F_CLK_target_MHz=12.288, bus_length_cm=30.0,
        Per_t_DZ_max_ns=10.0, Per_t_ZD_ns=2.0, Man_t_ZD_ns=2.0,
        Man_t_DZ_max_ns=10.0,
    )).breakdowns
    pp = r["PP_contention"]
    # Both device terms are the PERIPHERAL's, unlike either mixed leg.
    syms = [t.symbol for t in pp.terms]
    assert any("Per_t_ZD" in s for s in syms) and any("Per_t_DZ" in s for s in syms)
    # NO MANAGER *DRIVER*, which is not the same as no Manager symbol at all: the
    # Manager drives the CLOCK on a P2P data handover, so `Man_t_RF` belongs here, and
    # an earlier "no Man_ anywhere" check started failing on it the moment the crossing
    # terms named their slew lane. What must be absent is a Manager output-timing or
    # input-timing parameter -- a Manager DRIVER or RECEIVER, not the clock it sources.
    assert not any(s.startswith(("Man_t_DD", "Man_t_ZD", "Man_t_DZ",
                                 "Man_t_IS", "Man_t_IH")) for s in syms), \
        f"a P2P handover involves no Manager driver or receiver: {syms}"
    # PP no longer equals PM, and the difference is the acquiring peripheral's own
    # crossing credit -- taken at the FAST end of the t_RF tolerance band, since the
    # two devices share the setting but not the tolerance, while PM's single
    # peripheral is read at the row the corner search picked. That is what makes PP a
    # separate leg rather than a relabelling of PM.
    _e = CalcInputs().envelope()
    delta = _e.sigma_pm_hold_rf_clk * 5.0
    assert pp.margin_ns - r["PM_contention"].margin_ns == pytest.approx(delta)
    # ...and PP carries BOTH crossing corners — but GATHERED into one product, because both
    # ends read the SAME clock edge. The Manager sources the clock from one pin, so one slew
    # serves both detections; two separate products of one row invited, and previously got,
    # two independently cornered values for one physical edge. The netted term keeps both
    # contributions visible in its note rather than in two symbols.
    xs = {t.symbol: t.value_ns for t in pp.terms if "X_PM," in t.symbol}
    assert {s.split(" · ")[0] for s in xs} == {"(X_PM,late - X_PM,early)"}, xs
    assert all(s.endswith("Man_t_RF,CLK") for s in xs), xs
    net = next(t for t in pp.terms if "X_PM," in t.symbol)
    # The note decomposes it, and the inner signs are relative to the operator hoisted out:
    # "- (late - early)", never "- (late + early)", which would state a different number.
    assert "late" in net.note and "early" in net.note, net.note
    assert " - " in net.note, net.note
    assert abs(net.value_ns) > 0.1, net.value_ns
    # The gathered value IS the difference of the two crossings at one t_RF.
    assert net.value_ns == pytest.approx(
        _e.sigma_pm_hold_rf_clk * 5.0 - _e.sigma_pm_setup_clk_dgnd_pos * 5.0)
    faster_per = compute(CalcInputs(
        Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2", handover_UIs=1.0,
        F_CLK_target_MHz=12.288, bus_length_cm=30.0,
        Per_t_DZ_max_ns=4.0, Per_t_ZD_ns=2.0, Man_t_ZD_ns=2.0,
        Man_t_DZ_max_ns=10.0,
    )).breakdowns
    assert faster_per["PP_contention"].margin_ns > pp.margin_ns
    assert faster_per["MP_contention"].margin_ns == pytest.approx(
        r["MP_contention"].margin_ns), "MP does not depend on Per_t_DZ"


def test_phy2_needs_its_handover_ui_and_the_model_says_why():
    """PHY2's own numbers do NOT satisfy the inequality: t_ZD,min 2.0 <
    t_DZ,max 10.0. The dedicated handover UI is what pays the 8 ns difference.

    Forcing N_HO = 0 must therefore report contention, and report it as
    unfixable by clock rate -- slowing the clock does not widen a gap that has
    no UI in it. This is the case the spec avoids by scheduling a UI, so the
    model has to be able to show it.

    Read on P->P, because that is the leg where the bare arithmetic IS the margin:
    both ends are peripherals so the clock-detection lead cancels. The mixed legs
    carry it (+detect on MP, -detect on PM), so their numbers are not 8 -- see
    test_phy1_hands_over_in_zero_uis_because_its_own_numbers_allow_it.
    """
    kw = dict(Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2", bus_length_cm=0.0,
              Per_t_DZ_max_ns=10.0, Per_t_ZD_ns=2.0)
    forced = compute(CalcInputs(handover_UIs=0.0, **kw)).breakdowns["PP_contention"]
    # The DEVICE half of the deficit is the 8 ns the note is about: t_DZ,max 10 against
    # t_ZD,min 2. On top of it sits the crossing spread — the acquirer's early detection
    # against the releaser's late one, BOTH AT ONE SLEW, because the clock comes from one
    # Manager pin. (A previous revision took them at opposite ends of a tolerance band, which
    # made this ~2.6 ns worse; withdrawn, see `_gather_lane_terms`.)
    _e = CalcInputs(**kw).envelope()
    _trf = CalcInputs(**kw).tRF_Man_CLK_ns
    spread = (_e.sigma_pm_hold_rf_clk - _e.sigma_pm_setup_clk_dgnd_pos) * _trf
    assert forced.margin_ns == pytest.approx(-8.0 + spread)
    assert forced.F_max_MHz == 0.0, "no clock rate fixes a zero-UI deficit"

    # With the scheduled UI it passes, and now the rate DOES matter: the UI has to
    # cover the whole deficit, device half and crossing together.
    ok = compute(CalcInputs(handover_UIs=1.0, F_CLK_target_MHz=12.288,
                            **kw)).breakdowns["PP_contention"]
    assert ok.margin_ns > 0
    assert ok.F_max_MHz == pytest.approx(1000.0 * 0.45 / (8.0 - spread), rel=1e-6)


def test_the_contention_legs_take_the_opposite_t_zd_corner_from_setup():
    """t_ZD is ONE field, and the two uses want opposite ends of it.

    The "_ho" setup variants subtract t_ZD (max hurts); contention adds it (min
    hurts). The auto-worst solver has to find both from the same row -- if it
    drove t_ZD one way for everything, one of the two families would be
    evaluated at its best corner and silently over-report.
    """
    tv = _view()
    base, rows = tv._typ_baseline("spec")
    ho = find_worst_corner_rows("MP_setup_ho", rows, base)
    cont = find_worst_corner_rows("MP_contention", rows, base)
    assert ho["Man_t_ZD"] == "max", "setup handover is hurt by a LATE turn-on"
    assert cont["Per_t_ZD"] == "min", "contention is hurt by an EARLY turn-on"
    assert cont["Man_t_DZ"] == "max", "contention is hurt by a SLOW release"


def test_a_mixed_phy_pair_takes_the_larger_handover_allocation():
    """The allocation is a property of the bus schedule, not of one device.

    A PHY1 Manager does not stop its PHY2 Peripheral needing a scheduled UI, so
    the pair takes the larger of the two defaults. Taking the smaller would
    model a bus that contends.
    """
    assert default_handover_UIs("SWI3S_PHY1", "SWI3S_PHY2") == 1.0
    assert default_handover_UIs("SWI3S_PHY2", "SWI3S_PHY1") == 1.0
    assert default_handover_UIs("SWI3S_PHY1", "SWI3S_PHY1") == 0.0


def test_a_soundwire_side_flags_the_handover_assumption():
    """SoundWire does not allocate SWI3S handover UIs, so a cross-spec
    contention margin rests on an assumption that must be stated, not buried."""
    r = compute(_mixed("SWI3S_PHY2", "SW13_1V8"))
    assert any("handover-UI count" in c for c in r.caveats), r.caveats
    # An all-SWI3S bus makes no such assumption.
    assert not any("handover-UI count" in c
                   for c in compute(CalcInputs()).caveats)


def test_handover_ui_checkbox_selects_whether_the_ui_term_applies():
    """The control that prepares for EndDriveEarly.

    Ticked = one UI scheduled, cleared = none, and the three handover
    inequalities lose their N_HO*UI term with it. Seeded from the selected PHYs
    on reset -- PHY2 schedules one, PHY1 does not -- and overridable in both
    directions, because allocating a UI on PHY1 is legal (11.1.1.1 allows longer
    handovers) and clearing it on PHY2 is the EDE case.
    """
    tv = _view()
    # PHY2 defaults to a scheduled UI.
    assert tv._ho_ui_check.isChecked()
    assert tv.inputs("spec").handover_UIs == 1.0

    # Clearing it must FAIL the handover legs at PHY2's current numbers: t_ZD,min
    # 2.0 cannot cover t_DZ,max 10.0 without a UI. That is the deficit EDE has to
    # close, so the model showing it is the point of the control.
    tv._ho_ui_check.setChecked(False)
    assert tv.inputs("spec").handover_UIs == 0.0
    w = tv._per_inequality_worst("spec")
    for leg in ("MP_contention", "PM_contention", "PP_contention"):
        assert w[leg].margin_ns < 0, leg
        assert w[leg].F_max_MHz == 0.0, f"{leg}: no clock rate fixes a zero-UI deficit"
    assert "EndDriveEarly" in tv._ho_hint.text()

    # PHY1 reseeds it OFF, because PHY1 hands over intra-UI.
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY1"))
    assert not tv._ho_ui_check.isChecked()
    assert tv._per_inequality_worst("spec")["MP_contention"].margin_ns > 0, \
        "PHY1 closes with no UI — that is what makes it the intra-UI PHY"

    # Ticking it on PHY1 is legal and only adds margin.
    before = tv._per_inequality_worst("spec")["MP_contention"].margin_ns
    tv._ho_ui_check.setChecked(True)
    assert tv._per_inequality_worst("spec")["MP_contention"].margin_ns > before


def test_handover_ui_does_not_reseed_and_round_trips():
    """Toggling it must not reset the rate or the edited Example column.

    Same trap as the launch selector: routing through `_on_spec_changed` would
    call `_reset_to_defaults` and silently discard a user's target rate just
    because they compared the two handover cases.
    """
    tv = _view()
    tv._fclk.setValue(9.0)
    tv._max[("proposed", "Per_t_DD")].setValue(7.5)
    tv._ho_ui_check.setChecked(False)
    assert tv._fclk.value() == pytest.approx(9.0), "toggling must not reset F_CLK"
    assert tv._max[("proposed", "Per_t_DD")].value() == pytest.approx(7.5), \
        "toggling must not reseed the Example column"

    d = tv.to_dict()
    assert d["handover_ui"] is False
    tv2 = _view()
    tv2.set_from_dict(d)
    assert not tv2._ho_ui_check.isChecked()
    assert tv2.inputs("spec").handover_UIs == 0.0

    # A workspace written before the checkbox existed falls back to what the
    # selected PHYs imply, which is what that version computed.
    d.pop("handover_ui")
    tv3 = _view()
    tv3.set_from_dict(d)
    assert tv3._ho_ui_check.isChecked(), "legacy PHY2 workspace implied one UI"


# ---------------------------------------------------------------------------
# 12. EndDriveEarly
# ---------------------------------------------------------------------------

def test_ede_is_a_negative_effective_t_dz():
    """EDE's whole content, as one assertion.

    It moves the releasing device's t_DZ reference from the END of its last driven
    UI to the START -- one UI earlier -- so the tabulated value stays positive
    (Table 130's convention) and the model shifts it. In an end-of-UI-referenced
    inequality that is a NEGATIVE effective t_DZ, which is why no new inequality
    was needed to evaluate the feature.
    """
    UI = 1000 * 0.45 / 12.288
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=0.0, bus_length_cm=0.0,
              Man_t_DZ_max_ns=0.50 * UI + 9.0, Per_t_ZD_ns=3.0)
    off = compute(CalcInputs(Man_ede=False, **kw)).breakdowns["MP_contention"]
    on = compute(CalcInputs(Man_ede=True, **kw)).breakdowns["MP_contention"]
    # Turning EDE on is worth exactly one UI on the leg whose releaser has it.
    assert on.margin_ns - off.margin_ns == pytest.approx(UI, abs=1e-9)
    assert off.margin_ns < 0 < on.margin_ns, "EDE must be what flips this leg"
    # The equation names the convention, so a reader cannot mistake which is shown.
    assert any("EDE" in t.symbol for t in on.terms)
    assert not any("EDE" in t.symbol for t in off.terms)


def test_ede_applies_only_to_the_side_that_has_it():
    """Per-side, because the two ends reach EDE differently: a Manager can clock a
    mid-UI release, a peripheral can only aim at the UI boundary."""
    UI = 1000 * 0.45 / 12.288
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=0.0, bus_length_cm=0.0,
              Man_t_DZ_max_ns=0.50 * UI + 9.0, Per_t_DZ_max_ns=UI,
              Per_t_ZD_ns=3.0, Man_t_ZD_ns=0.25 * UI + 3.0)
    man_only = compute(CalcInputs(Man_ede=True, Per_ede=False, **kw)).breakdowns
    per_only = compute(CalcInputs(Man_ede=False, Per_ede=True, **kw)).breakdowns
    # MP's releaser is the Manager; PM's and PP's is the Peripheral.
    assert man_only["MP_contention"].margin_ns > per_only["MP_contention"].margin_ns
    for leg in ("PM_contention", "PP_contention"):
        assert per_only[leg].margin_ns > man_only[leg].margin_ns, leg


def test_the_ede_column_pair_is_proposal_versus_ede():
    """Selecting EDE shifts the comparison UP a step.

    The baseline stops being the current spec and becomes the proposed revision,
    because what matters is what EDE adds ON TOP of the revision. The headings
    must follow, or the left column reads as "Specification" over values that are
    not the specification.
    """
    tv = _view()
    assert tv._column_labels() == ("Specification", "Example")
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))
    assert tv._column_labels() == ("PHY2 Proposed", "EDE Proposed")

    # Left column = the PROPOSED revision, not the current spec.
    assert (tv._min[("spec", "Man_t_DD")].value(),
            tv._max[("spec", "Man_t_DD")].value()) == (9.0, 23.0)
    assert tv._max[("spec", "Per_t_IH")].value() == pytest.approx(1.5)
    # Right column = EDE, and the MANAGER's launch is the revision's own analog range:
    # the proposal asks nothing of it, so the two columns agree on this row.
    assert (tv._min[("proposed", "Man_t_DD")].value(),
            tv._max[("proposed", "Man_t_DD")].value()) == (9.0, 23.0)
    # What DOES change is the release: a timed move to high-Z at the UI boundary, i.e. the
    # bottom of Table 129's own 0-10 ns range, against the ordinary end-of-UI reference.
    assert tv._max[("proposed", "Man_t_DZ")].value() == pytest.approx(0.0)
    assert tv._max[("spec", "Man_t_DZ")].value() == pytest.approx(10.0)

    # ONLY THE PERIPHERAL HAS A MECHANISM, so only its flag is set. `Man_ede` means
    # `Man_t_DZ` is referenced to the START of the last driven UI (Table 130's mid-UI
    # convention); the Manager here releases at the boundary under the ORDINARY reference,
    # so the shift must not apply -- with `Man_ede` true and a row of 0.0 the release would
    # land a whole UI early. See `_man_ede_for`.
    assert tv.inputs("proposed").Per_ede and not tv.inputs("proposed").Man_ede
    assert not tv.inputs("spec").Man_ede and not tv.inputs("spec").Per_ede
    # And no handover UI is allocated, which is the point of the feature.
    assert tv.inputs("proposed").handover_UIs == 0.0


def test_ede_helps_every_leg_it_owns():
    """What EDE buys. The MECHANISM is asserted; where the margins land is recorded.

    THE NAME OF THIS TEST HAS NOW BEEN WRONG TWICE, in opposite directions -- first
    "closes all three handover legs", then "closes only the manager-releasing one" --
    because each time it named the current result rather than the invariant. It is
    named for the mechanism now, and the result lives in the body where it can move.

    * EDE MUST IMPROVE EVERY LEG WHOSE RELEASER HAS IT. That is a statement about the
      mechanism, it holds regardless of where the margins land, and it is what catches
      a sign error or a swapped reference -- the failure mode that once made M->P read
      BETTER without EDE than with it.
    * Which legs reach positive is a RESULT, and is recorded as one rather than
      demanded. Pinning it the other way round is what let an earlier version of this
      file protect an artefact (see the keeper split test).

    As of the overlap criterion all three reach positive, M->P with room and the two
    peripheral-releasing legs with about 1-2 ns. Before it, only M->P did. The two
    verdicts differ by 2*t_PD on every leg and by nothing else, which is why the
    mechanism assertions below are identical across both and only the tail moved.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))
    ede = tv._per_inequality_worst("proposed")
    base = tv._per_inequality_worst("spec")

    # The mechanism: every contention leg is better on the candidate column than on the
    # baseline, which is what catches a sign error or a swapped reference.
    for leg in ("MP_contention", "PM_contention", "PP_contention"):
        assert ede[leg].margin_ns > base[leg].margin_ns, \
            f"{leg}: the releasing device's change must improve the leg it owns"
    # The two PERIPHERAL-releasing legs fail at the baseline, so the peripheral's mechanism
    # is doing real work on each. M->P is NOT in that list any more: at Table 129's own
    # Man_t_DZ,max the leg is already marginal rather than broken, so what closes it is the
    # release tightened inside a ratified range and not a feature. Asserting a failing
    # baseline there is what a 23 ns release maximum -- a LAUNCH range -- used to buy.
    for leg in ("PM_contention", "PP_contention"):
        assert base[leg].margin_ns < 0, f"{leg}: the baseline should fail at zero UIs"

    # The result, recorded: the two MIXED legs close and P->P does not. Both halves of that
    # have been the other way round at some point, so both are worth stating.
    #
    # P->P's sign has now been decided by two separate readings of "two peripherals corner
    # independently", pulling opposite ways:
    #
    #   t_RF   the clock's slew is NOT independent. It comes from one Manager pin, so one
    #          edge with one slew serves both detections, and the two crossings gather into
    #          a single product (`_gather_lane_terms`). Withdrawing that split was worth
    #          +2.6 ns and made the leg close.
    #   V_IH   the receiver threshold IS independent. Each part's own switching threshold
    #          sits somewhere in the compliance window, so two receivers can be at opposite
    #          ends at once. The row picker used to collapse that window to one value for
    #          the whole bus, which credited the acquirer 2.38 ns it is not owed
    #          (`CalcRow.is_window`). Keeping the window open costs the leg its sign back.
    #
    # Both are about which quantities two devices realise separately, and getting the
    # ANSWER right per quantity is the point -- not applying one rule to both. What has
    # survived every revision either way is the ORDERING, so that is what is pinned
    # hardest.
    for leg in ("MP_contention", "PM_contention"):
        assert ede[leg].margin_ns > 0, (leg, ede[leg].margin_ns)
    assert ede["PP_contention"].margin_ns < 0, \
        ("P->P contention does not close once the threshold window stays open",
         ede["PP_contention"].margin_ns)
    assert ede["PP_contention"].margin_ns < min(ede["MP_contention"].margin_ns,
                                                ede["PM_contention"].margin_ns)
    assert ede["MP_contention"].margin_ns > ede["PM_contention"].margin_ns

    # Whatever else moves, the keeper must hold on both releasing devices -- a
    # starved keeper loses the bit outright rather than eroding a margin. The Manager's
    # leg has room; the peripheral's is EXACTLY zero, because its EDE hold minimum and
    # Man_tKeeper_Response are both 3 ns (see the keeper-split test). Asserted as the
    # equality it is rather than folded into a `>= 0`, so if either number moves this
    # says so instead of quietly reading as slack.
    # THE MANAGER'S KEEPER LEG DOES NOT CLOSE, and that is the proposal's one residual ask
    # rather than a defect in the release placement. The leg is
    # `t_DZ - Man_t_DD,max - t_swing - t_keeper`, so with the release at the boundary it is
    # a ceiling on the LAUNCH: 19.79 ns with the keeper observing, which is what this model
    # charges, against the revision's own 23. Recorded with a bound on the magnitude, so a
    # drift into a large failure is caught while the sign stays where the EDE paper says.
    # Settling Table 125 as a FORCED keeper is worth the 3 ns and is the paper's ask.
    assert -4.0 < ede["keeper_Man"].margin_ns < 0, ede["keeper_Man"].margin_ns
    assert ede["keeper_Per"].margin_ns == pytest.approx(0.0), ede["keeper_Per"].margin_ns
    # ...and moving the release OFF the boundary makes it worse, not better, which is the
    # claim that retired the 4x grid. Checked here rather than asserted in a docstring.
    #
    # BOTH SIDES AT THE SAME CORNER. `ede[...]` is each leg's own auto-worst corner and
    # `tv.inputs()` is the typ baseline, so comparing one against the other measures the
    # corner as well as the release -- which is how the first version of this assertion
    # came to compare +35.67 against -3.21 and fail for a reason that had nothing to do
    # with the claim. Isolate the release: one input set, one field changed.
    #
    # NOTE THE `Man_ede=True` too: a 0.75 UI release is a MID-UI-referenced value, so
    # reaching it means flipping the reference as well as the number. Leaving the ordinary
    # end-of-UI reference in force would put the release at UI + 27.47 ns, which is LATER
    # than the boundary and which the keeper duly prefers.
    from dataclasses import replace as _replace
    _typ = tv.inputs("proposed")
    _at_edge = compute(_typ).breakdowns["keeper_Man"].margin_ns
    _at_grid = compute(_replace(_typ, Man_ede=True,
                                Man_t_DZ_max_ns=0.75 * _typ.UI_ns())
                       ).breakdowns["keeper_Man"].margin_ns
    assert _at_grid < _at_edge, (
        f"the keeper is monotone in the release, so the boundary ({_at_edge:+.2f}) must "
        f"beat the retired 0.75 UI grid point ({_at_grid:+.2f})")
    # And setup must not be damaged by any of it.
    for leg in ("MP_setup", "PM_setup", "PM_hold"):
        assert ede[leg].margin_ns > 0, leg


def test_the_threshold_window_is_never_collapsed_by_a_corner_pick():
    """Two receivers may sit at opposite ends of one compliance window, so the picker
    must not close it.

    A DEVICE'S THRESHOLD IS A DEVICE CORNER -- a given part's rising threshold is
    somewhere in [V_IH,min, V_IH,max] -- and that is exactly why one bus-wide value
    cannot express two of them. The row picker used to bind one picked value to BOTH
    ends, so the moment auto-worst ran, every receiver on the bus was given the same
    threshold and a leg that CHARGES one receiver's crossing while CREDITING another's
    saw them partially cancel.

    Worth 2.38 ns on the three legs that net two peripherals, and nothing anywhere else:
    within one receiver what matters is the V_IH-to-V_IL separation, which a collapse
    preserves; ACROSS two receivers it is the window's WIDTH, which a collapse destroys.
    And it could only ever read at or above the spec window's own answer, so the search
    was choosing its worst corner out of a subspace that excluded the worst corner.

    Not the same question as the clock's slew, which went the OTHER way in the same
    revision: one Manager pin drives one edge, so both peripherals detect the same slew
    and collapsing THAT is correct. Independence has to be decided per quantity.
    """
    rows = default_swi3s_rows()
    windows = {r.name for r in rows if r.is_window}
    assert windows == {"V_IH", "V_IL"}, windows

    inp = CalcInputs(F_CLK_target_MHz=12.288)
    # Whatever the picker does, both ends survive into the inputs it hands the model.
    for leg in ("PP_contention", "PP_hold_ho", "MP_setup"):
        picks = find_worst_corner_rows(leg, rows, inp)
        assert "V_IH" not in picks and "V_IL" not in picks, \
            f"{leg}: a window row must get no pick at all"
        got = apply_row_picks(inp, rows, picks)
        assert (got.V_IH_min_frac, got.V_IH_max_frac) == (0.45, 0.65), leg
        assert (got.V_IL_min_frac, got.V_IL_max_frac) == (0.35, 0.55), leg

    # And the consequence, on the leg that shows it: the two crossings land on OPPOSITE
    # ends of the window, so the releaser's charge and the acquirer's credit cannot
    # cancel. Collapsing the window is what let them.
    e = inp.envelope()
    late, early = e.sigma_pm_setup_clk_dgnd_pos, e.sigma_pm_hold_rf_clk
    collapsed = dc_replace(inp, V_IH_min_frac=0.65, V_IH_max_frac=0.65).envelope()
    assert late == pytest.approx(collapsed.sigma_pm_setup_clk_dgnd_pos), \
        "the releaser's late crossing is already at the top of the window"
    assert early < collapsed.sigma_pm_hold_rf_clk, \
        "and the acquirer's early crossing must NOT rise with it"


def test_ede_ui_relative_rows_follow_the_rate():
    """The rate-relative refresh, asserted as an INVARIANT rather than as a row list.

    `_EDE_UI_RELATIVE_ROWS` is EMPTY now: the settled proposal has no rate-relative Manager
    value.  The launch is the revision's analog 9-23 ns and the release is
    `Man_t_DZ,max` = 0 against the end-of-UI reference -- both absolute, so both survive a
    rate change unchanged, and `Per_t_DZ` stopped being a row at all when the peripheral's
    release became an interval from its own ramp.

    So this test asserts two things that stay true whatever the list contains:

      1. every row NAMED in the list is one whose seeded value really is a fraction of the
         UI -- vacuous today, live the moment a fraction is seeded again, and the check that
         stops the list and the seeding drifting apart;
      2. every row NOT in it holds its value across a rate change, edits included.

    The retired version asserted (2)'s opposite for three named rows, which was right while
    they were grid points -- "0.75 UI" carried across a rate change is not
    stale-but-usable, it is wrong at both rates -- and it is what caught this reseed.
    """
    from swi3s_studio.ui.timing_view import _EDE_UI_RELATIVE_ROWS

    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))
    tv._max[("proposed", "Per_t_ZD")].setValue(7.5)      # absolute row, edited

    rates = (12.288, 9.0, 6.0)
    seen: dict[str, list[float]] = {}
    for f in rates:
        tv._fclk.setValue(f)
        for name in ("Man_t_DD", "Man_t_ZD", "Man_t_DZ", "Per_t_DD", "Per_t_ZD", "Per_t_IH"):
            seen.setdefault(name, []).append(tv._max[("proposed", name)].value())

    for name, vals in seen.items():
        if name in _EDE_UI_RELATIVE_ROWS:
            # (1) a declared fraction must SCALE with the UI, so value/UI is constant.
            fracs = [v / (1000 * 0.45 / f) for v, f in zip(vals, rates)]
            assert max(fracs) - min(fracs) < 0.01, (name, vals, fracs)
        else:
            # (2) everything else is absolute and must not move.
            assert max(vals) - min(vals) < 0.05, \
                f"{name} moved with the rate ({vals}) but is not in _EDE_UI_RELATIVE_ROWS"
    assert tv._max[("proposed", "Per_t_ZD")].value() == pytest.approx(7.5), \
        "a rate change must not discard an edit to an absolute row"


def test_table_130_caps_the_ede_release_and_no_inequality_does():
    """The boundary is excluded by Table 130, not by any leg -- so the model must carry
    the table.

    THIS IS THE ONE THING THAT FORBIDS A RELEASE ON THE UI EDGE. Nothing else does, and
    both of the Manager's own legs are content there:

      * M->P contention CANNOT bind at any placement inside the UI. The acquiring
        peripheral owes a flight, a detection and its own t_ZD,min before it may drive
        -- about 10 ns at 12.288 MHz over 30 cm -- so the gap is the acquirer's own
        lateness and the Manager cannot spend it by releasing later. The leg goes
        negative only WITHOUT EDE, when the release moves past the closing edge.
      * the keeper gets BETTER toward the boundary, monotonically: release and launch
        are 1:1 traded, so every 0.25 UI step later is 9.155 ns off contention and onto
        the keeper.

    So a release at 1.00 UI read as a comfortable pass on both, while being outside
    0.60 UI + 10 ns and not an early release at all -- and on a 1/2 UI grid that is
    where the nearest-point snap PUT it, which is how the tool came to report a 2x EDE
    column with every leg green.

    The cap is asserted on the MODEL, not on the seeded row: the row assertion in
    `test_ede_needs_a_quarter_UI_grid_and_is_seeded_for_it` cannot catch a user edit or
    a corner search moving the value.
    """
    from swi3s_studio.timing.spec_source import ede_release_ceiling_ns

    UI = 1000 * 0.45 / 12.288
    cap = ede_release_ceiling_ns(UI)
    assert cap == pytest.approx(0.60 * UI + 10.0)
    assert cap / UI == pytest.approx(0.873, abs=5e-4), "0.873 UI at this rate"

    def dz_in_force(dz, *, mode=LaunchMode.CLK4, ede=True):
        """The Man_t_DZ the contention leg actually uses, back out of its term."""
        b = compute(CalcInputs(
            Man_spec="SWI3S_PHY2_EDE", Per_spec="SWI3S_PHY2_EDE",
            F_CLK_target_MHz=12.288, handover_UIs=0.0, launch_mode=mode,
            Man_ede=ede, Man_t_DD_min_ns=0.25 * UI, Man_t_DD_max_ns=0.25 * UI,
            Man_t_ZD_ns=0.25 * UI, Man_t_DZ_max_ns=dz)).breakdowns["MP_contention"]
        term = [t for t in b.terms if t.symbol.startswith("Man_t_DZ")][0]
        # The term SUBTRACTS the release, so it carries -t_DZ,eff. Under EDE the
        # effective value is referenced to the end of the UI (t_DZ - UI), so the raw
        # placement is UI - term; conventionally it is just -term.
        return (UI - term.value_ns if ede else -term.value_ns), term.note

    # The 0.75 UI placement is inside the cap and is left exactly where it is.
    used, note = dz_in_force(0.75 * UI)
    assert used == pytest.approx(0.75 * UI, abs=5e-4)
    assert "Table 130" not in note, "an admissible release must not be annotated as capped"

    # The boundary is pulled back to the latest legal point the 1/4 UI grid can place.
    used, note = dz_in_force(UI)
    assert used == pytest.approx(0.75 * UI, abs=5e-4), used
    assert "Table 130" in note and "0.60·UI + 10 ns" in note, note

    # On a 1/2 UI grid the latest legal point is 0.50 UI -- the launch's own point --
    # which is why 2x cannot express EDE. The cap, not the keeper, is what excludes it.
    used, _ = dz_in_force(UI, mode=LaunchMode.CLK2)
    assert used == pytest.approx(0.50 * UI, abs=5e-4), used

    # An ANALOG EDE release is bounded by the table too: it has no grid to snap to, so
    # it is clamped at the cap itself rather than at a grid point.
    used, note = dz_in_force(UI, mode=LaunchMode.ANALOG)
    assert used == pytest.approx(cap, abs=5e-4), used
    assert "Table 130" in note, note

    # And a CONVENTIONAL release is NOT capped: Table 130 bounds EndDriveEarly, while a
    # conventional t_DZ is referenced to the closing edge and bounded by its own max.
    used, note = dz_in_force(UI, ede=False)
    assert used == pytest.approx(UI, abs=0.02), used
    assert "Table 130" not in note, note


def test_one_man_t_dz_is_read_by_both_the_keeper_and_the_contention_legs():
    """Two legs, one release time, and it has to be the same number in both.

    THE KEEPER LEG USED TO READ `inp.Man_t_DZ_max_ns` RAW while the contention legs read
    it snapped to the launch mode's grid -- so under a clocked mode one output stage had
    two release times, and each family kept the end that suited it. That is the same
    hybrid `_man_tdz_parts` was written to stop, and the same failure `_man_tzd_ns`
    already documents for t_ZD.

    Checked where it would show: a value deliberately OFF the grid, on a non-EDE column
    (so the keeper takes its `1·UI + Man_t_DZ` form) under a clocked launch.
    """
    UI = 1000 * 0.45 / 12.288
    off_grid = 0.40 * UI                      # between two 1/4 UI points
    b = compute(CalcInputs(
        F_CLK_target_MHz=12.288, handover_UIs=0.0, launch_mode=LaunchMode.CLK4,
        Man_t_DZ_max_ns=off_grid)).breakdowns
    keeper = [t for t in b["keeper_Man"].terms if t.symbol.startswith("Man_t_DZ")][0]
    cont = [t for t in b["MP_contention"].terms if t.symbol.startswith("Man_t_DZ")][0]
    # The contention term is negated (it subtracts the release), the keeper term adds it.
    assert keeper.value_ns == pytest.approx(-cont.value_ns, abs=5e-4), (keeper, cont)
    assert keeper.value_ns == pytest.approx(0.50 * UI, abs=5e-4), \
        "both must read the snapped value, not the off-grid one"
    assert "not on the 0.25 UI grid" in keeper.note, \
        "and the keeper leg must SAY the value was moved, as the contention leg does"


def test_the_keeper_leg_catches_ede_on_an_analog_launch():
    """The leg that stops the model reporting a false PASS.

    Before it existed, selecting EDE with the analog Manager launch showed all
    three handover legs green while the bus keeper was starved by 7.7 ns -- the
    releasing driver let go before the level had been valid long enough for the
    keeper to hold it, so the bit is lost and nothing said so.

    Contention does NOT catch this: it depends on t_DZ and t_ZD, not on t_DD, so
    it is indifferent to how the Manager launched its data. The keeper is the only
    leg that couples them, which is why EDE and the launch architecture cannot be
    chosen independently.
    """
    UI = 1000 * 0.45 / 12.288
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=0.0, Man_ede=True,
              Man_t_DZ_max_ns=0.50 * UI + 3.0, Per_t_DD_max_ns=9.0)
    clocked = compute(CalcInputs(Man_t_DD_min_ns=0.25 * UI,
                                 Man_t_DD_max_ns=0.25 * UI, **kw)).breakdowns
    analog = compute(CalcInputs(Man_t_DD_min_ns=9.0,
                                Man_t_DD_max_ns=23.0, **kw)).breakdowns
    assert clocked["keeper_Man"].margin_ns > 0, "the 4x clocked launch must satisfy the keeper"
    assert analog["keeper_Man"].margin_ns < 0, "the analog launch must NOT — 23 ns is too slow"
    # And the trap: contention is blind to it, so the keeper is doing real work.
    assert analog["MP_contention"].margin_ns > 0, \
        "contention passes either way — which is exactly why the keeper leg is needed"


def test_the_contention_clock_crossing_is_the_same_X_as_the_pm_legs():
    """The contention legs and the PM legs share a symbol, so they must share a value.

    `X_PM,setup,clk` and `X_PM,hold,clk` appear in BOTH families -- in PM setup/hold as
    one addend of Sigma_cross,PM, and in the contention legs as a releasing or acquiring
    peripheral's clock detection. They were separate names (`Sigma_cross,clk,*`) until
    the crossings went per-lane, and giving them one name asserts they are one quantity.

    That assertion is checkable, so it is checked: the contention path reads
    `sigma_pm_setup_clk_dgnd_pos` / `sigma_pm_hold_rf_clk` directly while the PM path
    takes whichever polarity or case its max/min selects, and those could in principle
    disagree. Swept over noise, supply tolerance, slew tolerance and both ramp shapes.
    If it ever fails, the shared name has become a lie and the symbols must split again.
    """
    import itertools
    for a, d, t, shape in itertools.product((0.0, 0.05, 0.10), (0.0, 0.05),
                                            (0.0, 0.05), ("linear", "exp")):
        inp = CalcInputs(V_noise_pp_frac=a, V_SEOS_tol_frac=d, tRF_tolerance_frac=t,
                         Man_shape=shape, Per_shape=shape)
        e = inp.envelope()
        clk_setup, _ = e.sigma_pm_setup_lanes(inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns)
        clk_hold, _ = e.sigma_pm_hold_lanes(inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns)
        why = (a, d, t, shape)
        assert clk_setup == pytest.approx(e.sigma_pm_setup_clk_dgnd_pos), why
        assert clk_hold == pytest.approx(e.sigma_pm_hold_rf_clk), why


def test_crossing_terms_are_dimensionless_coefficients_times_a_named_lane_trf():
    """X is one lane's crossing coefficient, not a nanosecond constant.

    Sigma and Delta name the COMBINING -- a sum over legs, a difference between them --
    so on a single lane they denote nothing, and an addend carrying the sum's name is
    wrong. The composites keep them: Delta_cross,MP = X_MP,late - X_MP,early, and
    Sigma_cross,PM,setup = X_PM,setup,clk + X_PM,setup,data.

    Each is a sum over legs of (dimensionless coefficient) × (that leg's t_RF), and the
    three lanes -- Man CLK, Man DATA, Per DATA -- are independent rows. A single
    pre-multiplied `Δ_cross,MP = 8.19 ns` hid all of that: that the quantity scales with
    slew, that it scales with a DIFFERENT slew per lane, and that on Δ_cross,MP which
    lane carries the late crossing SWAPS between setup and hold.

    IT CANNOT BE ONE PRODUCT. `Σ_cross,PM,hold × t_RF` is only equal to the envelope
    when the two lanes happen to share a t_RF, and both envelopes select their
    coefficient pair by a max (setup) or min (hold) over branches, so the coefficients
    are not even fixed. So this checks the per-lane split, and checks it where it would
    break: three unequal lanes, both ramp shapes.

    The exactness of the split is asserted inside `compute` itself; here we check the
    presentation is what it claims -- every crossing term names a lane that exists, and
    the products still sum to the envelope the results object reports.
    """
    LANES = {"Man CLK": "tRF_Man_CLK_ns", "Man DATA": "tRF_Man_DATA_ns",
             "Per DATA": "tRF_Per_DATA_ns"}
    # CLK/DATA is implied by the term's own role, so the t_RF symbol carries it only
    # where it must: Δ_cross,MP has BOTH Manager lanes on one leg-pair and swaps which
    # carries the late crossing, so there it is explicit.
    # Every lane is qualified now, and a P->P clock crossing names the tolerance BAND
    # (An earlier revision excluded a peripheral clock crossing here because it was cornered
    # at a tolerance-band end rather than read off the Man CLK row. That band is gone -- one
    # pin, one slew -- so every crossing now reads its row and none is excluded.)
    _LANE_OF = {"Man_t_RF,CLK": "Man CLK", "Man_t_RF,DATA": "Man DATA",
                "Per_t_RF,DATA": "Per DATA"}
    for shape in ("linear", "exp"):
        inp = CalcInputs(F_CLK_target_MHz=12.288, Man_shape=shape, Per_shape=shape,
                         tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=6.0, tRF_Per_DATA_ns=3.0)
        r = compute(inp)
        seen = 0
        for leg, b in r.breakdowns.items():
            for t in b.terms:
                # `"X_" in`, not startswith: a gathered pair opens with a bracket. Skipping it
                # would quietly shrink `seen` and stop checking the very legs where one lane is
                # read twice.
                if "X_" not in t.symbol:
                    continue
                seen += 1
                assert " · " in t.symbol, f"{leg}: {t.symbol} hides its t_RF"
                lane = _LANE_OF[t.symbol.split(" · ")[1]]
                assert lane in LANES, f"{leg}: {t.symbol} names lane {lane!r}"
                # The coefficient is recoverable, and is NOT a nanosecond quantity.
                coeff = abs(t.value_ns) / getattr(inp, LANES[lane])
                assert 0.0 <= coeff < 5.0, f"{leg}: {t.symbol} coeff {coeff}"
        assert seen >= 12, f"{shape}: only {seen} crossing terms found"

    # Δ_cross,MP's late leg swaps lane between setup and hold; that is the whole reason
    # the lane is in the symbol, so pin it rather than leaving it to the docstring.
    r = compute(CalcInputs(F_CLK_target_MHz=12.288, tRF_Man_CLK_ns=8.3,
                           tRF_Man_DATA_ns=6.0))
    late = {leg: [t.symbol for t in r.breakdowns[leg].terms
                  if t.symbol.startswith("X_MP,late")][0]
            for leg in ("MP_setup", "MP_hold")}
    assert late["MP_setup"].endswith("Man_t_RF,DATA"), late
    assert late["MP_hold"].endswith("Man_t_RF,CLK"), late

    # And the two sums still equal what the results object reports in ns.
    for leg, attr in (("MP_setup", "delta_cross_MP_setup_ns"),
                      ("MP_hold", "delta_cross_MP_hold_ns"),
                      ("PM_setup", "sigma_pm_setup_ns"),
                      ("PM_hold", "sigma_pm_hold_ns")):
        # These four legs read two DIFFERENT lanes, so nothing gathers and every crossing is
        # its own term; `"X_" in` keeps the sum right even if that ever changes.
        parts = [t.value_ns for t in r.breakdowns[leg].terms
                 if "X_" in t.symbol]
        want = getattr(r, attr)
        assert sum(parts) == pytest.approx(want if leg == "PM_hold" else -want), \
            (leg, parts, want)


def test_no_term_symbol_names_a_corner_the_picker_owns():
    """A displayed inequality names PARAMETERS; the corner search names corners.

    `Man_t_DD,max` in a symbol asserts which end of the row is in force, and that is
    not the symbol's to say: `find_worst_corner_rows` chooses per inequality, and it
    chooses OPPOSITE ends of the same field for different legs -- t_DZ goes to its low
    end for the keeper and its high end for contention. So a baked-in suffix can
    contradict the number printed beside it.

    It was worse than redundant. `apply_row_picks` sets every field a row binds to the
    same picked value, so `Man_t_DD,min` and `Man_t_DD,max` displayed the identical
    number while claiming to be two different quantities. And `Man_t_DZ,min` named a
    corner the spec does not define at all -- t_DZ is given only as a maximum -- so the
    label asserted a guaranteed-earliest release no vendor has promised.

    Suffixes that are not corners are fine and are not caught here: `,pure` is an
    anchor conversion, `,EDE` is a different definition of the parameter, and a corner
    on a field NO row binds (`Per_t_hold,min`, and the `V_OH,min`/`V_OL,max` inside the
    swing term) is a fact rather than a claim, because the picker never touches it.

    Checked over every spec pairing and both launch families, since the symbols are
    built conditionally and a suffix can hide in a branch.
    """
    from swi3s_studio.timing.calculator import default_swi3s_rows
    rows = {r.name for r in default_swi3s_rows()}
    assert "Man_t_DD" in rows and "Man_t_DZ" in rows, rows   # the check has teeth
    offenders = []
    for spec in SPEC_SOURCES:
        for ede in (False, True):
            for mode in (LaunchMode.ANALOG, LaunchMode.CLK4):
                r = compute(CalcInputs(
                    Man_spec=spec, Per_spec=spec, launch_mode=mode,
                    Man_ede=ede, Per_ede=ede,
                    handover_UIs=0.0 if ede else 1.0))
                for leg, b in r.breakdowns.items():
                    for t in b.terms:
                        for row in rows:
                            if (t.symbol.startswith(f"{row},")
                                    and t.symbol.split(",")[1] in ("min", "max")):
                                offenders.append(f"{spec}/{mode.value}/{leg}: {t.symbol}")
    assert not offenders, (
        "these symbols name a corner that the picker owns:\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_the_keeper_leg_is_meaningful_without_ede_too():
    """Expressed in launch-referenced terms so it is not an EDE special case.

    A conventional handover drives its whole UI and goes on driving into the next one
    until t_DZ, so a non-EDE bus has a large genuine margin rather than a vacuous one.
    If this ever went negative on a plain PHY2 bus it would mean the release had
    overtaken data-valid, which is a real defect worth seeing.

    THE TERM STRUCTURE IS WHAT THIS PINS, because the value has been wrong in both
    directions. `1*UI + t_DZ` is the release point: the UI carries the reference change
    (t_DD is measured from the edge that opens the UI, t_DZ from the one that closes
    it), and t_DZ carries the driving that continues past that edge. One version folded
    the UI into the t_DZ term's value and labelled the sum `Man_t_DZ,min`; the next
    dropped t_DZ altogether. Both are visible here as a wrong term count.
    """
    r = compute(CalcInputs(F_CLK_target_MHz=12.288, handover_UIs=1.0))
    assert r.breakdowns["keeper_Man"].margin_ns > 0
    UI = 1000 * 0.45 / 12.288
    ede_in = CalcInputs(F_CLK_target_MHz=12.288, handover_UIs=0.0,
                        Man_ede=True, Per_ede=True)
    ede = compute(ede_in).breakdowns["keeper_Man"]
    # Both read the same t_DZ field, so the conventional leg's advantage is the UI it
    # carries and nothing else.
    assert r.breakdowns["keeper_Man"].margin_ns - ede.margin_ns == pytest.approx(
        UI, abs=0.05)
    # The terms, named: a UI and a t_DZ on the conventional legs, no UI under EDE, and
    # no invented `,min` corner on a parameter the spec gives only a maximum for.
    for leg, dz in (("keeper_Man", "Man_t_DZ"), ("keeper_Per", "Per_t_DZ")):
        syms = [t.symbol for t in r.breakdowns[leg].terms]
        assert "1·UI" in syms and dz in syms, (leg, syms)
        assert not any(s.endswith(",min") for s in syms), (leg, syms)
    assert "1·UI" not in [t.symbol for t in ede.terms]


def test_the_keeper_needs_the_release_two_grid_steps_after_the_launch():
    """What satisfies the keeper is the PLACEMENT of the release, not the choice of
    multiplier -- and the placement it likes best is the one no grid contains.

    RETAINED AS A PROPERTY OF THE CLOCKED OPTION, which the EDE paper still reports and
    which a designer can still select.  It is no longer the seeded position: the settled
    proposal has the Manager stopping AT the UI boundary under the ordinary end-of-UI
    reference (`_ede_ranges`), so this test drives `CalcInputs` directly rather than
    through the view's rows -- `Man_ede` is what makes a release value mid-UI-referenced,
    and the view no longer asserts it.

    The claim: `t_DZ` must clear `t_DD` by the whole rail-to-rail swing PLUS the keeper's
    response time, 13.83 + 3.00 = 16.83 ns at the slow corner (0.46 UI), which on a
    1/4 UI grid is TWO steps.  The docstring once said "at least t_keeper after t_DD,
    which on a grid means a LATER POINT" -- the retired swing-for-t_keeper reading, and it
    admits ONE step, which the body below has always asserted fails.

    AND THE LAST BLOCK IS WHY THE GRID IS RETIRED.  The leg is monotone in the release, so
    the boundary beats every point the grid offers -- which the grid sweep could not show,
    because it excluded the boundary by construction.
    """
    base = _same("SWI3S_PHY2_EDE", F_CLK_target_MHz=12.288, handover_UIs=0.0, Per_ede=True,
                 tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=8.3, tRF_Per_DATA_ns=8.3)
    UI = base.UI_ns()

    def keeper_with(dd, dz, *, ede=True):
        """The Manager's keeper margin with the launch at `dd` and the release at `dz`.

        `ede=True` makes `dz` mid-UI-referenced, which is what a grid placement is.

        NO ROW PICKER HERE, deliberately.  `apply_row_picks` writes each row's own range
        into the inputs, so a pinned `Man_t_DD` is overwritten by the RATIFIED row's typ
        the moment the picker runs -- filtering the pick out does not protect it, it just
        picks typ instead.  The first version of this helper did that and read -24.33 ns
        where the placement gives +1.48, because it was measuring the ratified 7-18 ns
        launch at its midpoint.  The corner that matters to this leg is `t_RF,max`, and
        that is pinned in `base`.
        """
        inp = dc_replace(base, Man_ede=ede, Man_t_DD_min_ns=dd, Man_t_DD_max_ns=dd,
                         Man_t_ZD_ns=dd, Man_t_DZ_max_ns=dz)
        return compute(inp).breakdowns["keeper_Man"].margin_ns

    assert keeper_with(0.50 * UI, 0.50 * UI) < 0, \
        "release on the launch's own point must fail -- no driven valid level"
    # ONE grid step is NOT enough, and that is what set the multiplier. The addend
    # is the full rail-to-rail swing (t_RF / 0.60 = 13.83 ns at the slow corner,
    # 0.3777 UI), not t_keeper -- the keeper is a weak holder and cannot finish a
    # transition. A 0.25 UI step cannot cover 0.3777 UI.
    assert keeper_with(0.50 * UI, 0.75 * UI) < 0, \
        "one grid step is short of the swing, so it must NOT satisfy the keeper"
    assert keeper_with(0.25 * UI, 0.50 * UI) < 0, "...at either pairing"
    # TWO steps do, which is why a clocked EDE release needed a 4x clock.
    assert keeper_with(0.25 * UI, 0.75 * UI) > 0, \
        "two grid steps must satisfy it -- 0.50 UI covers the 0.3777 UI swing"
    # A MID-UI RELEASE IS STILL CAPPED BY TABLE 130, so the boundary is not reachable as
    # an EDE value: the model pulls 1.00 UI back to the latest legal grid point, 0.75 UI,
    # which from a 0.50 UI launch is one step and fails.
    assert keeper_with(0.50 * UI, UI) < 0, \
        "an EDE release at the boundary must be capped to 0.75 UI, one step from 0.50"

    # AND THE BOUNDARY, REACHED THE OTHER WAY, BEATS ALL OF THEM. `ede=False` makes the
    # value end-of-UI-referenced, so 0.0 IS the boundary -- Table 129's own parameter at
    # the bottom of its range, no mid-UI reference and nothing for Table 130 to bound.
    # Same launch on both sides, so this isolates the release.
    at_edge = keeper_with(0.25 * UI, 0.0, ede=False)
    assert at_edge > keeper_with(0.25 * UI, 0.75 * UI), (
        f"the boundary ({at_edge:+.2f}) must beat the best grid pair "
        f"({keeper_with(0.25 * UI, 0.75 * UI):+.2f}) -- the leg is monotone in the "
        f"release, which is what retired the grid")


def test_the_ede_column_seeds_a_boundary_release_and_the_revision_s_own_launch():
    """What the EDE column IS, now that only the peripheral has a mechanism.

    THIS TEST ASSERTED THE OPPOSITE UNTIL 2026-08-18 -- that the column seeds a 1/4 UI
    clocked grid, launch 0.25 UI and release 0.75 UI, and that a 4x clock is therefore
    required.  Both halves are withdrawn, and the reasoning is worth keeping because the
    arithmetic behind the retired position was never wrong:

      * `Man_t_DZ` is its own tabulated parameter at 0-10 ns, referenced to the edge that
        ENDS the last driven UI (Table 129 Note 1) and measured from the Manager's internal
        timing reference for that edge (Note 3).  So stopping AT the boundary is
        `Man_t_DZ,max` = 0 -- the bottom of a ratified range, gated from a clock edge the
        Manager already owns.  Not an EDE release at all, hence `Man_ede` False and no
        Table 130 ceiling.
      * The keeper is `t_DZ - t_DD,max - t_swing`, monotone in the release, so the boundary
        is the best release the UI holds and 0.75 UI is 9.16 ns worse -- on the very leg the
        grid was chosen for (see the test above).  What the keeper constrains is the LAUNCH.
      * The launch therefore stays the revision's analog 9-23 ns.  It is NOT deterministic
        any more, and the 3:1 test below is what governs it instead.

    A clocked launch remains selectable and is an f_max option: it lifts the keeper's
    ceiling from 12.22 to 24.40 MHz and buys 0.16 ns on MP hold, against MP setup giving
    out at 12.21 MHz on the revision's own account.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))

    # The launch: the revision's own analog range, and RANGED rather than collapsed.
    for name in ("Man_t_DD", "Man_t_ZD"):
        lo = tv._min[("proposed", name)].value()
        hi = tv._max[("proposed", name)].value()
        assert (lo, hi) == (9.0, 23.0), (name, lo, hi)

    # The release: at the boundary, and deterministic because it is gated from the clock.
    lo = tv._min[("proposed", "Man_t_DZ")].value()
    hi = tv._max[("proposed", "Man_t_DZ")].value()
    assert (lo, hi) == pytest.approx((0.0, 0.0)), (lo, hi)
    # ...and it must NOT be treated as an EndDriveEarly release, or the reference shift
    # would put it a whole UI early.
    assert not tv.inputs("proposed").Man_ede
    assert tv.inputs("proposed").Per_ede, "the PERIPHERAL is the one with a mechanism"

    w = tv._per_inequality_worst("proposed")
    short = [k for k, b in w.items() if b.margin_ns < -0.005]
    # EDE closes every CONTENTION leg it owns, and both keeper legs. Three legs stay short
    # and none of the three is EDE's to close:
    #
    #   MP_hold / MP_hold_ho   the ordinary legs. MP_hold carries no t_ZD at all, so EDE
    #                          cannot be what moves them. Both sit at about -0.7 ns on the
    #                          revision's own Man_t_DD,min / Man_t_ZD,min of 9 ns against a
    #                          floor of 9.89 at the slow corner.
    #   PP_contention          the expected PHY2 answer at N_HO = 0 (see below).
    #   keeper_Man             NEW, and it is the proposal's one residual ask rather than a
    #                          consequence of the boundary release -- which is the BEST
    #                          release for this leg. Rearranged, eq:keeper is a ceiling on
    #                          the LAUNCH: Man_t_DD,max <= 19.79 ns with the keeper
    #                          observing, against the revision's 23. The EDE paper asks the
    #                          working group to settle Table 125 as a FORCED keeper, which
    #                          is worth the 3 ns; this model charges it, so the column shows
    #                          the pessimistic reading.
    #   PP_hold_ho             NEW TO THIS SET, and it did not move -- the SCOPE did. The
    #                          leg was excluded as P->P data until its terms were read: the
    #                          level it protects belongs to whoever RELEASED, so when the
    #                          Manager released it is Manager-to-peripheral data and the leg
    #                          binds. It is now in the bounding set, hence in this
    #                          subtraction's result. See `test_the_out_of_scope_split_...`.
    #
    # ASSERTED BY EQUALITY, not as a subset, and that is the point of this line. It
    # WAS a subset -- `set(short) - _PP_DATA_LEGS <= {...}` -- which cannot see a leg
    # LEAVING the set. PP_contention duly left it (the clock's slew band was withdrawn,
    # +2.6 ns, leg closed) and came back (the threshold window stopped being collapsed,
    # -2.38 ns), and neither move cost a red test while the bound was one-sided.
    #
    # `_PP_DATA_LEGS` is now IMPORTED from the model rather than restated here, which is what
    # made this line fail on the scope change instead of quietly subtracting a leg the model
    # no longer excludes.
    assert set(short) - set(_PP_DATA_LEGS) == {"MP_hold", "MP_hold_ho", "PP_contention",
                                              "PP_hold_ho", "keeper_Man"}, \
        f"the set of short legs moved: {short}"
    assert w["PP_hold_ho"].margin_ns == pytest.approx(-7.67, abs=0.02), \
        ("the level a releasing MANAGER drove, against an acquiring peripheral's turn-on: "
         "in scope, unclosed, and what still owes a handover UI wherever a peripheral "
         "acquires")
    assert w["PP_contention"].margin_ns == pytest.approx(-1.08, abs=0.02), \
        "P->P contention: two receivers at opposite ends of one threshold window"
    # AND THAT SIGN IS THE EXPECTED PHY2 ANSWER, which is why it is asserted as a value
    # rather than guarded as a failure. PHY2 allocates a UI to every handover by default
    # and that UI is how it separates two peripherals' drivers -- P->P is not expected to
    # close without one. Evaluated at N_HO = 0 this leg prices REMOVING that UI, which is
    # a key motivation for EDE rather than a defect in the bus.
    assert tv._handover_UIs() == 0.0, "this whole column is the zero-UI hypothetical"
    # WHAT THE BOUNDARY RELEASE BUYS AND WHAT IT DOES NOT. M->P contention closes and the
    # peripheral's keeper leg sits at exactly zero (its EDE hold minimum IS
    # Man_tKeeper_Response), so that one is asserted as the equality it is rather than
    # folded into a `>= 0`. The MANAGER's keeper leg does NOT close, and it is the residual
    # ask rather than a cost of the placement -- see the narrative above the short-leg set.
    assert w["MP_contention"].margin_ns > 0, w["MP_contention"].margin_ns
    assert w["keeper_Per"].margin_ns == pytest.approx(0.0), w["keeper_Per"].margin_ns
    assert -4.0 < w["keeper_Man"].margin_ns < 0, w["keeper_Man"].margin_ns
    base_w = tv._per_inequality_worst("spec")
    assert len([k for k, b in base_w.items() if b.margin_ns < 0]) >= 3, \
        "the baseline should fail the handover legs, or the comparison shows nothing"

    # THE LAUNCH MODE NO LONGER TOUCHES THE RELEASE, and that is worth pinning because it
    # used to. While the release was a grid point, selecting 2x snapped it -- Table 130
    # capping it at 0.50 UI, the launch's own point, so the keeper failed and the tool
    # correctly reported that 2x cannot express EDE. The release is not on a grid now: it is
    # Man_t_DZ,max = 0 against the end-of-UI reference, which every clock can produce
    # because the UI boundary is the one instant the bus clock marks by itself.
    for mode in (LaunchMode.CLK2, LaunchMode.CLK4, LaunchMode.ANALOG):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
        assert tv._max[("proposed", "Man_t_DZ")].value() == pytest.approx(0.0), mode
        assert not tv.inputs("proposed").Man_ede, mode
    # ...and what the launch mode DOES touch is the keeper, through the launch's PVT
    # spread. Clocking it collapses Man_t_DD to a point and the leg closes, which is the
    # f_max option the paper offers and declines to require.
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.CLK4.value))
    assert tv._per_inequality_worst("proposed")["keeper_Man"].margin_ns > 0, \
        "a clocked launch must close the keeper -- that is the whole of what it buys here"
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.ANALOG.value))

    # Which legs are thin, named individually -- and the ORDERING has been wrong in
    # this file twice already, so it is pinned as an ordering and not as three numbers.
    #
    #   M->P  comfortable, and by the widest margin of the three. Its peripheral only
    #         ACQUIRES, so the crossing is a credit -- and it is the one leg that
    #         BANKS the overlap allowance rather than spending it against the skew.
    #   P->M  thin. Its peripheral RELEASES, so it pays the late crossing; the skew
    #         and the allowance cancel, leaving the leg bus-independent. Its acquirer
    #         is the Manager, launching from a 0.25 UI grid point, which is a far
    #         larger turn-on delay than a peripheral's t_ZD,min -- so it still has
    #         more room than P->P.
    #   P->P  thinnest. Same late crossing and the same cancellation, but its acquirer
    #         is a peripheral turning on at Per_t_ZD,min, and the early crossing it
    #         gains does not make up the difference.
    #
    # FOUR earlier versions of this block, and the ORDERING is the only thing that has
    # survived all of them. One had P->P with the MOST room ("the detection lead cancels on
    # it" -- it does not). The next had P->M and P->P NEGATIVE, which was the acquirer's-pin
    # frame demanding a gap where one flight of overlap is free. The third had P->P negative
    # because its two peripherals were cornered at opposite ends of the CLOCK's slew band --
    # withdrawn, because the clock comes from one Manager pin and one pin has one slew, so
    # both detections scale by the same t_RF (`_gather_lane_terms`), and the leg closed. The
    # fourth is this one: the same "two peripherals corner independently" argument is CORRECT
    # for the receiver THRESHOLD, which is a per-device property and not a shared edge, so the
    # window stays open (`CalcRow.is_window`) and the leg is negative again.
    #
    # The signs have moved four times and the ordering never has, so the ordering is what is
    # pinned and each sign is stated as its own movable fact. Note what the two slew/threshold
    # rulings have in common: the question is always which quantities two devices realise
    # SEPARATELY, and the answer is per quantity -- a shared signal is one value, a per-part
    # property is two.
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.CLK4.value))
    w = tv._per_inequality_worst("proposed")
    mp, pm, pp = (w[f"{k}_contention"].margin_ns for k in ("MP", "PM", "PP"))
    assert mp > pm > pp, f"expected MP > PM > PP, got {mp:+.2f} {pm:+.2f} {pp:+.2f}"
    assert pp < 0, (
        f"P->P contention does not close: its two peripherals sit at opposite ends of the "
        f"receiver threshold window, which is a per-device corner: {pp:+.2f}")
    # No claim about the SIZE of the gaps: they were ~0.7 ns apart when this was written and
    # the four rewrites above are what a pinned magnitude here would have cost. The ordering
    # is the load-bearing statement.
    #
    # THIS LINE USED TO READ `mp > 10.0`, and it went with the retired 0.75 UI release: the
    # Manager let go a quarter UI early, so the acquirer had that much more room. At the
    # boundary M->P is about +3.5 ns instead, which is the release being LATER and not the leg
    # getting worse in any sense that matters -- the margin it needs is the acquirer's own
    # lateness, and the Manager cannot spend that by holding on longer.
    assert mp > 0.0, "M->P is the comfortable one: the crossing is a credit there"


def test_setup_and_contention_agree_about_man_t_zd():
    """One definition of Man_t_ZD, read by both families.

    They used to disagree: the setup handover variants applied the launch mode
    while the contention legs took `inp.Man_t_ZD_ns` raw, so on a clock-launched
    bus the two modelled different turn-on times for the same device. It was
    pessimistic in contention rather than optimistic, so it hid rather than broke
    -- which is exactly why one definition beats two that happen to be safe.

    Asserted by construction rather than on values: whatever the mode, the t_ZD
    the setup leg used must be the one the contention leg used.
    """
    from swi3s_studio.timing.calculator import _man_tzd_ns
    for mode in (LaunchMode.ANALOG, LaunchMode.CLK4, LaunchMode.CLK2):
        inp = CalcInputs(F_CLK_target_MHz=12.288, launch_mode=mode,
                         Man_t_ZD_ns=14.0, handover_UIs=0.0,
                         Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2")
        want = _man_tzd_ns(inp)
        r = compute(inp).breakdowns
        # PM contention's acquirer is the Manager, so its +t_ZD term is that value.
        got = next(t.value_ns for t in r["PM_contention"].terms
                   if "Man_t_ZD" in t.symbol)
        assert got == pytest.approx(want), mode
        # A clocked mode is deterministic, so the setup leg sees the same number
        # (a SWI3S Manager t_ZD needs no pure conversion).
        if mode is not LaunchMode.ANALOG:
            used = -next(t.value_ns for t in r["MP_setup_ho"].terms
                         if "Man_t_ZD" in t.symbol)
            assert used == pytest.approx(want), mode


def test_ede_peripheral_values_are_not_tighter_than_3_to_1():
    """An unclocked delay shows ~3:1 max/min over PVT and mismatch.

    The rule bites ASYMMETRICALLY, which is why this asserts a floor and not a
    band: a range WIDER than 3:1 is merely conservative, but a range TIGHTER than
    3:1 asks for better than PVT delivers -- an unbuildable part, not a tight one.

    A peripheral has no clock, so every peripheral value is analog end to end and
    the 3:1 spans the whole of it. A MANAGER value may be narrower in total
    without violating anything, because a clocked grid point carries no PVT and
    only the analog turn-on/turn-off portion does -- so the Manager is checked on
    that portion instead.

    This caught Per_t_DZ seeded as (UI - turn_max .. UI) = 1.33:1, from applying
    the Manager's grid-plus-analog shape to a device with no grid.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))

    def ratio(name):
        lo = tv._min[("proposed", name)].value()
        hi = tv._max[("proposed", name)].value()
        return hi / lo if lo else math.inf

    # Wholly-analog peripheral values: not tighter than 3:1.
    for name in ("Per_t_ZD", "Per_t_DZ"):
        assert ratio(name) >= 3.0 - 0.02, \
            f"{name} is {ratio(name):.2f}:1 — tighter than PVT allows"
    # Per_t_DD is 2-9 = 4.5:1, the proposed revision's own value. Wider than 3:1
    # is the safe direction, so it passes; noted so it is not read as an omission.
    assert ratio("Per_t_DD") >= 3.0

    # THE MANAGER'S ROWS SPLIT, and the rule is the same one applied honestly to each: a
    # CLOCKED value is deterministic (1:1) and 3:1 does not apply to it; an UNCLOCKED value
    # carries 3:1 over the WHOLE of itself. Nothing in between, and no subtraction.
    #
    # The launch is unclocked on this column -- the revision's own analog 9-23 ns, which the
    # proposal asks nothing of -- so it is governed like a peripheral value: 2.56:1, wider
    # than 3:1, which is the SAFE direction. An earlier version of this test asserted 1:1 on
    # all three Manager rows, which was correct while every one of them was a grid point and
    # is what caught this reseed.
    assert 1.0 < ratio("Man_t_DD") <= 3.0 + 0.02, \
        f"an ANALOG Man_t_DD must carry a spread, and no tighter than 3:1: {ratio('Man_t_DD'):.2f}"
    assert ratio("Man_t_ZD") == pytest.approx(ratio("Man_t_DD")), \
        "t_DD and t_ZD are the two launch methods and the revision states them identically"
    # The release IS deterministic, and for a reason that does not need a multiplied clock:
    # Man_t_DZ,max = 0 is the UI boundary, the one instant the bus clock marks by itself, so
    # the move to high-Z is gated from an edge the Manager already has. Zero on both ends,
    # which `ratio` would report as inf -- so it is asserted directly.
    assert (tv._min[("proposed", "Man_t_DZ")].value(),
            tv._max[("proposed", "Man_t_DZ")].value()) == pytest.approx((0.0, 0.0))
    #
    # WHAT THE RETIRED VERSION OF THIS TEST CAUGHT is worth keeping, because the failure mode
    # is general. It used to subtract an assumed 0.50 UI grid point from Man_t_DZ and check
    # only the remainder, which came to 9/3 = 3:1 and passed. That made the check
    # UNFALSIFIABLE -- the test chose which part of the value to excuse, so any range could be
    # made to pass by nominating a suitable grid point. Man_t_DZ was 21.31-27.31, i.e. 1.28:1
    # as a whole value, and the test waved it through. Rod caught it by inspection.


# ---------------------------------------------------------------------------
# Three "the value on screen is not the value in force" defects, found together
# ---------------------------------------------------------------------------
#
# All three are the same shape as the false-PASS classes already guarded above
# (the stale native core, the missing clock-skew term, the missing keeper leg):
# a check existed and could not see the failure mode. Two were reported by
# inspection -- a parameter row that did not move with the picker driving it --
# and the third came out of chasing the first.


def test_the_manager_output_timings_are_all_clocked_or_all_analog():
    """CLOCKED OR ANALOG, NOT A MIX — the invariant, stated on all three at once.

    One Manager has one output stage. If its data edge is launched from a
    multiplied clock then it HAS that clock, and its output enable and disable are
    launched from it too. A t_DD on a clock grid point beside an analog t_ZD and
    t_DZ is not a conservative model of anything; it is two different devices.

    That was the state PHY2 was left in: at 2× t_DD collapsed to 18.31 ns while
    t_ZD kept 9–23 and t_DZ kept 0–10. And it was not neutral, it took the
    FAVOURABLE HALF OF EACH — the clocked data launch (which helps setup) together
    with the analog 10 ns release (which helps MP contention). Fixing it costs
    8.3 ns of MP contention at 2×, and that cost is the correct answer.

    What the mode does NOT dictate is which grid point each one sits on — see
    `test_a_four_x_clock_places_the_three_timings_independently`. This asserts only
    that each is on SOME point of the mode's grid, or that all are tabulated.

    Asserted on the three margins that read the three parameters, so it cannot be
    satisfied by a row that displays the right number while the model reads
    another. A per-parameter check would have passed on t_DD and t_ZD alone.
    """
    UI = 1000 * 0.45 / 12.288
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=1.0,
              Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP")

    def terms(mode):
        r = compute(CalcInputs(launch_mode=mode, Man_t_DD_min_ns=9.0,
                               Man_t_DD_max_ns=23.0, Man_t_ZD_ns=23.0,
                               Man_t_DZ_max_ns=10.0, **kw)).breakdowns
        return {
            "t_DD": -next(t.value_ns for t in r["keeper_Man"].terms
                          if "Man_t_DD" in t.symbol),
            "t_ZD": -next(t.value_ns for t in r["MP_setup_ho"].terms
                          if "Man_t_ZD" in t.symbol),
            "t_DZ": -next(t.value_ns for t in r["MP_contention"].terms
                          if "Man_t_DZ" in t.symbol),
        }

    analog = terms(LaunchMode.ANALOG)
    assert analog == pytest.approx({"t_DD": 23.0, "t_ZD": 23.0, "t_DZ": 10.0}), \
        "analog: every one as tabulated, none snapped"
    for mode, grid in ((LaunchMode.CLK2, 0.50), (LaunchMode.CLK4, 0.25)):
        for name, value in terms(mode).items():
            k = value / (grid * UI)
            assert k == pytest.approx(round(k), abs=1e-6), \
                f"{mode}: {name} = {value:.4g} ns is not on the {grid:g} UI grid"


def test_a_four_x_clock_places_the_three_timings_independently():
    """A 4× clock offers 0.00, 0.25, 0.50 and 0.75 UI, and the three timings do
    NOT have to pick the same one. A 2× clock offers only 0.00 and 0.50.

    The coherence rule above is about clocked-vs-analog, not about sharing an edge,
    and the first version of that fix conflated the two: it forced every clocked
    Manager output onto the data edge's own point. That is a real restriction on the
    design space rather than a conservative simplification, and the EDE proposal is
    the proof — t_DD 0.50 UI, t_ZD 0.00 UI, t_DZ 0.50 UI — so a model that cannot
    express different points cannot express the design it exists to evaluate.

    Off-grid values are SNAPPED rather than accepted, because a t_ZD the selected
    clock cannot place reads as a perfectly good number instead of the
    impossibility it is; the snap is reported in the term's note, since a silent
    one would be its own display-versus-value mismatch.
    """
    UI = 1000 * 0.45 / 12.288
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=1.0,
              Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP",
              Man_t_DD_min_ns=9.0, Man_t_DD_max_ns=23.0)

    def zd_dz(**over):
        r = compute(CalcInputs(**{**kw, "launch_mode": LaunchMode.CLK4, **over})).breakdowns
        return (next(t for t in r["MP_setup_ho"].terms if "Man_t_ZD" in t.symbol),
                next(t for t in r["MP_contention"].terms if "Man_t_DZ" in t.symbol))

    # t_ZD on 0.00 UI and t_DZ on 0.75 UI: two different points, one 4x clock.
    zd, dz = zd_dz(Man_t_ZD_ns=0.0, Man_t_DZ_max_ns=0.75 * UI)
    assert -zd.value_ns == pytest.approx(0.0, abs=1e-6), "0.00 UI must survive"
    assert -dz.value_ns == pytest.approx(0.75 * UI, abs=1e-6), "0.75 UI must survive"

    # Off-grid snaps, and says so.
    _, dz = zd_dz(Man_t_ZD_ns=0.0, Man_t_DZ_max_ns=0.75 * UI - 2.0)
    assert -dz.value_ns == pytest.approx(0.75 * UI, abs=1e-6), "snap to the nearest"
    assert "not on the" in dz.note and "snapped" in dz.note, \
        f"a silent snap is its own display/value mismatch; note was {dz.note!r}"

    # A 2x clock cannot place 0.25 UI, so it must land on 0.50.
    r = compute(CalcInputs(**{**kw, "launch_mode": LaunchMode.CLK2,
                              "Man_t_ZD_ns": 0.25 * UI,
                              "Man_t_DZ_max_ns": 0.5 * UI})).breakdowns
    zd2 = -next(t.value_ns for t in r["MP_setup_ho"].terms if "Man_t_ZD" in t.symbol)
    assert zd2 == pytest.approx(0.5 * UI, abs=1e-6), \
        "a 2x clock reaches only multiples of 0.50 UI"


def test_clocking_the_manager_release_costs_mp_contention():
    """The direction that shows the old hybrid was not merely untidy.

    A clocked t_DZ of ~18.3 ns at 2× is LATER than the tabulated 10 ns max, so the
    Manager holds the bus longer and MP contention gets WORSE. The hybrid kept the
    10 ns analog release alongside the clocked 18.31 ns launch — helping setup with
    the clock and helping contention with the analog number, from one device.

    Pinned as a direction rather than a value, because the value is the model's and
    the direction is the argument: if clocking the Manager ever stops costing MP
    contention, the coupling has been quietly dropped again.
    """
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=1.0,
              Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP",
              Man_t_DD_min_ns=9.0, Man_t_DD_max_ns=23.0,
              Man_t_ZD_ns=23.0, Man_t_DZ_max_ns=10.0)
    mp = {m: compute(CalcInputs(launch_mode=m, **kw)).breakdowns["MP_contention"].margin_ns
          for m in (LaunchMode.ANALOG, LaunchMode.CLK2)}
    assert mp[LaunchMode.CLK2] < mp[LaunchMode.ANALOG], \
        f"clocking the release must cost MP contention, got {mp}"
    # And the keeper gains, which is the same coupling read the other way: the
    # release moves later relative to a fixed data-valid point.
    kp = {m: compute(CalcInputs(launch_mode=m, **kw)).breakdowns["keeper_Man"].margin_ns
          for m in (LaunchMode.ANALOG, LaunchMode.CLK2)}
    assert kp[LaunchMode.CLK2] > kp[LaunchMode.ANALOG], kp


def test_every_governed_row_agrees_with_the_term_it_feeds():
    """The reported symptom that started this: the picker moved the margins while
    the parameters they are computed from sat unchanged on screen, so the value in
    force was nowhere on the page.

    Asserted as an IDENTITY between each row and the term it feeds, for all three
    Manager outputs, in both clocked modes. That is the invariant regardless of
    which placement a design picks -- and it is what an earlier pair of tests got
    wrong by pinning the placement the selector used to impose instead. The
    selector supplies the GRID; the rows supply the placements.
    """
    tv = _view()
    for mode in (LaunchMode.CLK2, LaunchMode.CLK4):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
        w = tv._per_inequality_worst("proposed")
        # t_DD via the keeper, t_ZD via the handover setup variant, t_DZ via MP
        # contention -- one leg each, chosen because each reads that row directly.
        for name, leg, sign in (("Man_t_DD", "keeper_Man", -1),
                                ("Man_t_ZD", "MP_setup_ho", -1),
                                ("Man_t_DZ", "MP_contention", -1)):
            row = tv._max[("proposed", name)].value()
            term = next(t for t in w[leg].terms if name.replace("_", r"\_") in t.symbol
                        or name in t.symbol)
            used = sign * term.value_ns
            # EDE shifts t_DZ one UI earlier for the inequality; compare the
            # unshifted magnitude.
            if name == "Man_t_DZ" and "EDE" in term.symbol:
                used += 1000 * 0.45 / tv._fclk.value()
            assert row == pytest.approx(used, abs=0.06), (mode, name, row, used)
        # And every one is editable: none is the mode's output any more.
        for name in ("Man_t_DD", "Man_t_ZD", "Man_t_DZ"):
            assert not tv._min[("proposed", name)].isReadOnly(), \
                f"{mode}: {name} is a placement the design chooses, not a fixed value"


def test_the_keeper_legs_are_split_so_the_corner_search_stays_exact():
    """`find_worst_corner_rows` moves ONE row at a time, which is exact only for a
    margin affine in each parameter. A min over two legs is not affine.

    The keeper used to be reported as the worse of its two legs, and that
    saturated: on the EDE column the Manager's leg sits at exactly 0.00 ns, so no
    single move of a peripheral row could lower min(0.00, per) -- each one alone
    left the reported margin at 0.00 -- and the search stopped with the peripheral
    rows at their typicals. Reaching the peripheral's worst needs Per_t_DZ,min and
    Per_t_DD,max TOGETHER, a two-coordinate move no amount of iterating one
    coordinate at a time will make.

    Checked against brute force over the four rows the keeper reads, at 13.2 MHz
    (PHY2's mandatory maximum) where the peripheral leg genuinely fails. The
    single-leg model reported +0.00 there.
    """
    import itertools
    from dataclasses import replace

    from swi3s_studio.timing.calculator import apply_row_picks, default_swi3s_rows
    from swi3s_studio.ui.timing_view import _ede_ranges, _UI_ns

    f = 13.2
    ede = _ede_ranges(_UI_ns(f))
    rows = [replace(r, range=ede[r.name]) if r.name in ede else r
            for r in default_swi3s_rows()]
    base = CalcInputs(F_CLK_target_MHz=f, duty_min=0.45, bus_length_cm=30.0,
                      Man_spec="SWI3S_PHY2_EDE", Per_spec="SWI3S_PHY2_EDE",
                      Man_ede=True, Per_ede=True, handover_UIs=0.0,
                      launch_mode=LaunchMode.CLK2)
    names = ["Man_t_DZ", "Man_t_DD", "Per_t_DZ", "Per_t_DD"]
    for leg in ("keeper_Man", "keeper_Per"):
        picks = find_worst_corner_rows(leg, rows, base)
        got = compute(apply_row_picks(base, rows, picks)).breakdowns[leg].margin_ns
        brute = min(
            compute(apply_row_picks(base, rows, {**picks, **dict(zip(names, c))}))
            .breakdowns[leg].margin_ns
            for c in itertools.product(("min", "typ", "max"), repeat=len(names)))
        assert got == pytest.approx(brute, abs=5e-3), \
            f"{leg}: search found {got:+.2f}, true worst {brute:+.2f}"
    # THIS TEST USED TO PIN A CONCLUSION, AND THAT WAS THE BUG.
    #
    # It asserted that the peripheral's keeper leg FAILS at PHY2's mandatory
    # maximum, and read that as the EDE proposal's rate ceiling. The failure was an
    # artefact of the input: Per_t_DZ,min was seeded UI/3, a 3:1 turn-off ratio
    # applied to a POSITION in the UI rather than to a delay, which nothing derived.
    # The assertion then protected the artefact exactly as well as it would have
    # protected a real finding -- a test that asserts a conclusion shields it from
    # correction as well as from regression.
    #
    # So this pins the INPUT instead: the peripheral's release is referenced to its
    # own ramp completion, which makes the keeper leg satisfied BY CONSTRUCTION --
    # what is left over is the hold interval, whose minimum is Man_tKeeper_Response.
    # If that stops being true the leg is wrong, and this says so without asserting
    # which way it comes out.
    # The margin is the hold interval MINUS the keeper's demand, and PHY2 sets the two to
    # the same 3 ns -- so "satisfied by construction" is exact and there is NOTHING SPARE.
    # It read +3.00 while Man_tKeeper_Response was gated off by the old keeper_forced flag;
    # with the demand charged (PHY2 has no forced keeper) it is identically zero. A pass by
    # the model's criterion, and a knife-edge one: anything unmodelled that eats into the
    # settled window at the far pin -- ringing, settling, a hold interval at its minimum
    # rather than above it -- takes it negative, and a starved keeper loses the bit.
    per = find_worst_corner_rows("keeper_Per", rows, base)
    leg = compute(apply_row_picks(base, rows, per)).breakdowns["keeper_Per"]
    assert leg.margin_ns == pytest.approx(0.0), \
        "under EDE the peripheral's keeper margin is its hold minimum less the keeper demand"
    syms = {t.symbol for t in leg.terms}
    assert syms == {"Per_t_hold,min", "Man_t_Keeper_Response"}, syms
    assert not any("UI" in t.symbol for t in leg.terms), \
        "the peripheral's release must not be UI-scaled -- that was the artefact"
