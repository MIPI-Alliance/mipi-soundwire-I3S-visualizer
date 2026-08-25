"""Cross-check: this calculator against the standalone reference analysis.

The same timing model lives twice — here in `swi3s_studio/timing/`, and in the
`timing-analysis` project's `swtiming/emit_ede.py`, which generates the EDE paper's numeric
macros. They have diverged deliberately in places and accidentally in others, and until this
file existed the only way to find out which was an audit by hand.

WHAT THIS PINS. Every leg the reference defines, evaluated against its counterpart here under
ONE matched configuration, with each delta declared and explained. A delta that moves fails.
That is the point: a divergence should cost a red test, not a discovery six months later.

TWO TRAPS, both of which cost real time before they were written down:

1. THE TWO MODELS NAME LEGS BY OPPOSITE CONVENTIONS. The reference names a leg for the
   HANDOVER direction, this project for the DATA direction, and they are inverted --
   `emit_ede.setup_mp` says in its own docstring "= Timing Analysis PM setup". Comparing
   like-named legs reports a 15 ns error that does not exist.

2. EACH REFERENCE LEG CARRIES ITS OWN CORNER IN ITS DEFAULT ARGUMENTS, not a global one.
   `hold_mp` defaults to T_RF_FAST and t_pd=0.0 because it is CREDITED 2·t_PD and Σ_hold, so
   its worst case minimises them. `nc_pm` bakes in a 0.25 UI clocked turn-on. Evaluate them
   all at one blanket slow-slew corner and everything looks wrong.

SKIPPED when the reference project is not beside this one, which is the normal case for a
checkout of swi3s-studio alone. It is not vendored: it is another project's source, it moves
on its own schedule, and a stale copy asserting agreement would be worse than no test.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

from swi3s_studio.timing import CalcInputs, compute
from swi3s_studio.timing.spec_source import LaunchMode

# ---------------------------------------------------------------------------
# Locating the reference
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SIBLING = os.path.normpath(os.path.join(_HERE, "..", "..", "timing-analysis"))
_REF_DIR = os.environ.get("SWI3S_TIMING_ANALYSIS", _SIBLING)


def _reference():
    """`swtiming.emit_ede`, or a skip. Importing it is side-effect free — the one
    `write_text` in that module is inside a function, not at import time."""
    if not os.path.isdir(os.path.join(_REF_DIR, "swtiming")):
        pytest.skip(f"reference analysis not found at {_REF_DIR} "
                    f"(set SWI3S_TIMING_ANALYSIS to point at it)")
    if _REF_DIR not in sys.path:
        sys.path.insert(0, _REF_DIR)
    try:
        from swtiming import emit_ede
    except Exception as exc:                       # noqa: BLE001 - any import failure skips
        pytest.skip(f"reference analysis present but not importable: {exc}")
    return emit_ede


def _matched_inputs(E):
    """This model, configured exactly as the reference configures itself.

    Proposed PHY2 numbers, 12.288 MHz, 30 cm, slow slew, no handover UI, analog Manager
    launch. Every value is read from the reference's own constants rather than restated, so
    a constant moving there shows up as a delta here instead of being silently mirrored.
    """
    return CalcInputs(
        Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP",
        F_CLK_target_MHz=E.F_CLK, duty_min=E.DUTY_MIN,
        bus_length_cm=E.BUS_CM, vPCB_cm_per_ns=E.V_PCB, vPCB_err_frac=0.10,
        tRF_Man_CLK_ns=E.T_RF_SLOW, tRF_Man_DATA_ns=E.T_RF_SLOW,
        tRF_Per_DATA_ns=E.T_RF_SLOW, tRF_tolerance_frac=E.TAU,
        V_SEOS_tol_frac=E.DELTA, V_noise_pp_frac=E.ALPHA,
        Man_t_DD_min_ns=E.MAN_ANALOG_MIN, Man_t_DD_max_ns=E.MAN_ANALOG_MAX,
        Man_t_ZD_ns=E.MAN_ANALOG_MAX,
        # MAN_DZ_MAX_T129, NOT the launch range. Man_t_DZ is its own tabulated parameter at
        # 0-10 ns; this line used to read `E.MAN_ANALOG_MAX`, mirroring the same conflation the
        # reference itself carried, so the two models agreed on a number neither should have
        # produced. Two copies of one error look exactly like a cross-check passing.
        Man_t_DZ_max_ns=E.MAN_DZ_MAX_T129,
        Per_t_DD_min_ns=E.PER_DD_MIN, Per_t_DD_max_ns=E.PER_DD_MAX,
        Per_t_ZD_ns=E.PER_ZD_MAX, Per_t_DZ_max_ns=E.PER_DZ_MAX,
        Per_t_IS_max_ns=E.PER_IS_MAX, Man_t_IS_max_ns=E.MAN_IS_MAX,
        Per_t_IH_max_ns=E.PER_IH_MAX, Man_t_IH_max_ns=E.MAN_IH_MAX,
        t_keeper_ns=E.T_KEEPER, handover_UIs=0.0, launch_mode=LaunchMode.ANALOG,
        pp_placement="unknown",
    )


# The one deliberate divergence that shows up as a number, and the reason for it. Figure 174
# measures Per_t_DD / Per_t_ZD to 20 % of the ramp, so a tabulated peripheral value contains a
# slew portion this model takes back out and the reference does not. See docs/anchors.md; the
# reference removed its own offset layer on the opposite reading of Table 129's wording.
_PER_ANCHOR = 1.6667

# The SECOND declared divergence, and this one is a disagreement rather than a conversion.
#
# The reference corners a P→P leg's two peripherals at OPPOSITE ends of the clock's slew band —
# `nc_pp(trf=T_RF_SLOW, trf_acq=T_RF_FAST)` — on the stated grounds that two parts share the slew
# SETTING but each realises its own value inside the tolerance. This model does not, because the
# clock is driven by the MANAGER FROM ONE PIN: at any instant that edge has one slew and both
# peripherals detect the same edge. What differs between them is where their thresholds sit on it
# (already the V_IH/V_IL spread inside the envelope) and their internal delays (their own
# parameters). So both crossings take one t_RF and the pair gathers into a single product.
#
# TWO THINGS SUPPORT THE READING TAKEN HERE. The reference is internally inconsistent about it —
# `hold_pp` next door evaluates its crossings at a single `trf`, so the split appears on the
# contention leg alone. And the geometry `nc_pp`'s own docstring describes (releaser at the far
# end, acquirer at the Manager end) would justify a slew difference by EDGE DEGRADATION ALONG THE
# BUS, which is a real effect but not the part-to-part tolerance it cites; neither model carries a
# distance-dependent slew term, so a tolerance band is a stand-in whose label does not match what
# it stands for.
#
# Worth 2.617 ns, and it moves P→P contention from just failing to just passing — a conclusion,
# so it is declared here rather than left for whoever next compares the two models.
_PP_SLEW_SPLIT = 2.6174


def _margin(inp, leg, **overrides):
    return compute(replace(inp, **overrides)).breakdowns[leg].margin_ns


# ---------------------------------------------------------------------------
# The legs that must agree EXACTLY
# ---------------------------------------------------------------------------

def test_manager_launched_and_contention_legs_agree_exactly():
    """Six legs, zero delta, to four decimal places.

    These are the legs with no peripheral output value in them (Manager-launched setup/hold,
    where Fig. 176's symmetric anchors need no conversion) and the three contention legs
    (which read t_ZD/t_DZ raw on both sides, because they bound driver STATE rather than when
    a level becomes readable). If any of these moves, one model has changed its physics.

    P→P contention is NOT here: it is the one leg where the two models disagree about the
    clock's slew rather than about a conversion, so it has its own test and its own declared
    number (`_PP_SLEW_SPLIT`).
    """
    E = _reference()
    inp = _matched_inputs(E)
    ui = inp.UI_ns()

    assert ui == pytest.approx(E.UI, abs=1e-9), "the two disagree about the UI itself"

    # THE REFERENCE'S DEFAULTS ARE THE PROPOSAL, so each leg is evaluated at the placement the
    # proposal states and this side is configured to match it. The Manager's launch is the
    # revision's ANALOG 9-23 ns (`_matched_inputs` already sets that) and its release is a timed
    # move to high-Z AT the boundary -- Man_t_DZ,max = MAN_DZ_PROP, which is 0 and therefore
    # reads as an end-of-UI-referenced release with no EDE flag anywhere. The clocked grid
    # placement this replaced is still reachable through the reference's keyword arguments, and
    # the keeper check below uses it deliberately.
    exact = [
        # reference (handover-named)      this project (data-named)
        ("setup_pm", E.setup_pm(), "MP_setup_ho", dict(Man_t_ZD_ns=E.MAN_ANALOG_MAX)),
        ("hold_pm", E.hold_pm(), "MP_hold_ho", dict(Man_t_ZD_ns=E.MAN_ANALOG_MIN)),
        ("nc_mp", E.nc_mp(release=E.man_release(E.MAN_DZ_MAX_T129)),
         "MP_contention", dict(Per_t_ZD_ns=E.PER_ZD_MIN,
                               Man_t_DZ_max_ns=E.MAN_DZ_MAX_T129)),
        ("nc_pm", E.nc_pm(ede=False), "PM_contention",
         dict(Man_t_ZD_ns=E.MAN_ANALOG_MIN, Per_t_DZ_max_ns=E.PER_DZ_MAX)),
    ]
    for ref_name, ref_val, leg, overrides in exact:
        got = _margin(inp, leg, **overrides)
        assert got == pytest.approx(ref_val, abs=5e-4), (
            f"{ref_name} vs {leg}: reference {ref_val:+.4f}, here {got:+.4f}")

    # THE MANAGER'S RELEASE AT THE BOUNDARY, which is the proposal rather than a variant, so it
    # gets its own line: Man_t_DZ,max = 0 with the conventional end-of-UI reference.
    assert _margin(inp, "MP_contention", Per_t_ZD_ns=E.PER_ZD_MIN,
                   Man_t_DZ_max_ns=E.MAN_DZ_PROP) == pytest.approx(E.nc_mp(), abs=5e-4)

    # The Manager keeper leg. Checked at BOTH placements, because which one the keeper prefers
    # is the claim that retired the 4x clock: the boundary release beats the 0.75 UI grid point,
    # and the reference must agree with this model on both or the comparison says nothing about
    # the one that matters.
    analog_edge = dict(Man_t_DZ_max_ns=E.MAN_DZ_PROP)
    assert _margin(inp, "keeper_Man", **analog_edge) == pytest.approx(
        E.keeper_man(forced=False), abs=5e-4)
    clocked_grid = dict(Man_ede=True, launch_mode=LaunchMode.CLK4,
                        Man_t_DD_min_ns=E.MAN_LAUNCH_UI * ui,
                        Man_t_DD_max_ns=E.MAN_LAUNCH_UI * ui,
                        Man_t_DZ_max_ns=E.MAN_RELEASE_UI * ui)
    assert _margin(inp, "keeper_Man", **clocked_grid) == pytest.approx(
        E.keeper_man(forced=False, launch=E.man_launch(),
                     release=E.man_release_ede()), abs=5e-4)


# ---------------------------------------------------------------------------
# The legs that differ, each by a declared amount
# ---------------------------------------------------------------------------

def test_peripheral_launched_legs_differ_by_the_anchor_conversion_only():
    """+1.6667 on setup, −1.6667 on hold, and nothing else.

    Every leg with a peripheral OUTPUT value in it differs by exactly the Fig. 174 anchor
    conversion: this model backs the 0→20 % ramp portion out of Per_t_DD / Per_t_ZD, the
    reference uses the tabulated value. A setup leg subtracts the launch, so removing 1.667 ns
    from it makes the margin 1.667 ns better; a hold leg adds it, so hold goes the other way.

    Pinned per-leg rather than as one blanket tolerance: if a SECOND divergence ever appears
    on one of these, it must not hide inside a band wide enough to admit the first.
    """
    E = _reference()
    inp = _matched_inputs(E)

    # setup: this model is 1.667 BETTER (it subtracts a smaller launch)
    for ref_val, leg, overrides in (
        (E.setup_mp(), "PM_setup_ho", {}),
        (E.setup_pp(), "PP_setup_ho", {}),
    ):
        got = _margin(inp, leg, **overrides)
        assert got - ref_val == pytest.approx(+_PER_ANCHOR, abs=5e-4), leg

    # hold: this model is 1.667 WORSE (it adds a smaller launch). The reference's hold_mp
    # carries its OWN corner in its defaults — fast slew and a co-packaged peripheral,
    # because the leg is credited 2·t_PD and Σ_hold and its worst case minimises both.
    got = _margin(inp, "PM_hold_ho", tRF_Man_CLK_ns=E.T_RF_FAST,
                  tRF_Man_DATA_ns=E.T_RF_FAST, tRF_Per_DATA_ns=E.T_RF_FAST,
                  bus_length_cm=0.0, Per_t_ZD_ns=E.PER_ZD_MIN)
    assert got - E.hold_mp() == pytest.approx(-_PER_ANCHOR, abs=5e-4)


def test_pp_hold_differs_by_the_anchor_alone_now_that_one_pin_has_one_slew():
    """P→P hold agrees with the reference except for the anchor conversion.

    IT USED TO CARRY TWO DIVERGENCES and now carries one. This model cornered the leg's two
    peripherals at opposite ends of the clock's slew band, which cost 2.6 ns against the
    reference's single `trf`; that is withdrawn — one Manager pin, one edge, one slew — so
    `hold_pp` and `PP_hold_ho` now differ by the Fig. 174 anchor and nothing else.

    Note which way that resolved: the reference was RIGHT here and this model was wrong, the
    opposite of the anchor question two tests up. Neither side is the authority, which is the
    reason this file exists.
    """
    E = _reference()
    inp = _matched_inputs(E)
    got = _margin(inp, "PP_hold_ho", Per_t_ZD_ns=E.PER_ZD_MIN)
    assert got - E.hold_pp() == pytest.approx(-_PER_ANCHOR, abs=5e-4)


def test_pp_contention_differs_by_the_slew_split_this_model_declines_to_make():
    """The one leg where the reference splits the clock's slew and this model does not.

    `nc_pp` takes the releaser's charge at T_RF_SLOW and the acquirer's credit at T_RF_FAST;
    this model takes both at the one t_RF the corner search picked, because the clock is driven
    by a single Manager pin. Asserted as a NUMBER so a change on either side fails here rather
    than being absorbed as noise — and its SIGN too, because the direction is the point:
    declining the split makes this model more generous, which must never pass unremarked.

    See `_PP_SLEW_SPLIT` for why it is read this way here, including the reference's own
    inconsistency (`hold_pp` beside it does not split) and what its stated reason does not
    cover.
    """
    E = _reference()
    inp = _matched_inputs(E)
    got = _margin(inp, "PP_contention", Per_t_ZD_ns=E.PER_ZD_MIN,
                  Per_t_DZ_max_ns=E.PER_DZ_MAX_T129)
    ref = E.nc_pp(ede=False)
    assert got - ref == pytest.approx(_PP_SLEW_SPLIT, abs=5e-3), (
        f"the P→P slew-split divergence moved: reference {ref:+.4f}, here {got:+.4f}")
    assert got > ref, "declining the split is the GENEROUS direction — never silently"


def test_the_keeper_per_legs_agree_once_both_are_at_the_earliest_release():
    """They differ by t_DZ only because they read it at different corners, not by an omission.

    The reference's `keeper_per_conventional` is `UI − Per_t_DD − swing − keeper`, with no
    t_DZ term. That looks like a dropped term next to this project's leg, which carries
    `1·UI + Per_t_DZ` explicitly — and it was briefly written up as one. It is not.

    THE KEEPER LEG WANTS THE EARLIEST RELEASE. t_DZ is specified only as a MAXIMUM, so a
    device may go high-Z the instant its UI closes; t_DZ = 0 is legal and is the corner that
    starves the keeper. The reference HARDCODES that corner by omitting the term. This
    project makes it a corner the search finds, and drops the term when it lands on zero (the
    same rule that drops `0·UI`). Two spellings of one worst case.

    Both are then evaluated the same way, and what is left is the anchor conversion alone.
    Asserted at BOTH ends of the row so the direction is unambiguous: at t_DZ,max this
    project is 9 ns more generous, which is the leg correctly reporting that a device holding
    the bus longer feeds the keeper better — not a disagreement about the inequality.
    """
    E = _reference()
    inp = _matched_inputs(E)
    ref = E.keeper_per_conventional(forced=False)

    at_earliest = _margin(inp, "keeper_Per", Per_t_DZ_max_ns=0.0)
    assert at_earliest - ref == pytest.approx(_PER_ANCHOR, abs=5e-4), (
        "at the earliest release the two keeper legs should differ by the anchor conversion "
        "and nothing else")

    at_tabulated = _margin(inp, "keeper_Per", Per_t_DZ_max_ns=E.PER_DZ_MAX)
    assert at_tabulated - at_earliest == pytest.approx(E.PER_DZ_MAX, abs=5e-4), (
        "driving t_DZ longer must feed the keeper exactly that much more")

    # ...and the corner search finds t_DZ = 0 on its own, which is what makes the two
    # equivalent rather than merely reconcilable by hand.
    from swi3s_studio.timing import apply_row_picks, default_swi3s_rows, find_worst_corner_rows

    rows = default_swi3s_rows()
    picks = find_worst_corner_rows("keeper_Per", rows, inp)
    assert picks["Per_t_DZ"] == "min", picks["Per_t_DZ"]
    worst = compute(apply_row_picks(inp, rows, picks)).breakdowns["keeper_Per"]
    # The zero release term STAYS, and that is the third kind of zero this model
    # distinguishes: `0·t_PD` is dropped (structurally zero), `0·UI` is dropped (nothing
    # allocated), but a PARAMETER sitting at zero is shown — like Man_t_IH on PHY2. Here it
    # carries the whole finding, so `_release_note` spells it out rather than letting a
    # vanished term read as an oversight.
    release = [t for t in worst.terms if t.symbol.startswith("Per_t_DZ")]
    assert len(release) == 1 and release[0].value_ns == 0.0, release
    assert "may go high-Z the instant its UI closes" in release[0].note


def test_the_reference_prices_only_the_handover_launch():
    """The structural gap, asserted as a fact about coverage rather than a number.

    The reference defines setup/hold ONLY for the t_ZD (handover) launch — `setup_mp`,
    `hold_mp`, `setup_pm`, `hold_pm`, `setup_pp`, `hold_pp` all take a `t_zd`. It has no
    continuous-transmission leg at all, so its "the proposal closes everything" claim rests
    on the handover cases. This model carries the full launch-method × (setup, hold) grid.

    If the reference ever grows a t_DD-launched leg, this fails and the pairs above should
    gain a row rather than the new leg going unnoticed.
    """
    E = _reference()
    import inspect

    for name in ("setup_mp", "hold_mp", "setup_pm", "hold_pm", "setup_pp", "hold_pp"):
        params = inspect.signature(getattr(E, name)).parameters
        assert any("zd" in p for p in params), f"{name} no longer takes a t_ZD"
        assert not any("t_dd" in p for p in params), \
            f"{name} grew a t_DD parameter — the reference now prices the other launch too"

    ours = compute(_matched_inputs(E)).breakdowns
    for leg in ("MP_setup", "MP_hold", "PM_setup", "PM_hold", "PP_setup", "PP_hold"):
        assert leg in ours, f"{leg} is the t_DD-launched half of the 2x2 and must stay"
