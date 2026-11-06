"""Cross-spec (SWI3S ↔ SoundWire 1.3) timing tests.

The core of what is pinned here:

1. **Non-regression.** With both sides SWI3S the calculator gives the pinned
   single-spec SWI3S margins.

2. **The anchor placement.** Every launch sits at its anchor, the ramp's 20 % point, at
   every slew; a Manager's timing is measured from its clock reference; every crossing
   counts from its edge's 20 % point.

3. **The SoundWire equivalents.** A SoundWire side carries its Table 17 values as SWI3S
   equivalents (`sw13_equivalents`), rounded outward, and is evaluated as a SWI3S side with
   the same rows but for three things: its data input's threshold window
   (`data_input_window`), the anchor correction that moves each launch from the
   equivalents' slews to the operating ones (`sw13_anchor_correction`), and a SoundWire
   Manager's keeper threshold (`sw13_keeper_thresholds`). A Peripheral's clock detection is
   charged once.

Sections 4 to 14 follow in the file: advisories, the UI, the proposed PHY2 revision, the
Manager launch mode, the revision on a mixed bus, handover non-contention, crossing symbols,
shared edges and polarities, EndDriveEarly, the value on screen being the value in force,
and the corner search.

Run: PYTHONPATH=. python3 -m pytest tests/test_timing_cross_spec.py
"""
import itertools
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
    HANDOVER_CROSS_SPEC_NOTE,
    HANDOVER_SW_PAIR_NOTE,
    REF_TRF_NS,
    SNAP_TOL_NS,
    SPEC_SOURCES,
    THRESHOLD_NOTE,
    TRF_NOTE,
    LaunchMode,
    default_handover_UIs,
    fclk_ceiling_MHz,
    is_soundwire,
    launch_mode_max_fclk_MHz,
    launch_mode_tDD_ns,
    preset,
    recommended_launch_mode,
    snap_to_launch_grid,
    sw13_anchor_correction,
    sw13_equivalent_slews,
    sw13_equivalents,
)

pytestmark = pytest.mark.gui

# Baseline corner: 30 cm, 13.2 MHz, small/1.2 V.
BASE = dict(bus_length_cm=30.0, F_CLK_target_MHz=13.2)


def _edge(trf, lo=3.0, hi=8.3, tau=0.05):
    """(late, early) slew factors of one lane's two edges: 1 ± tau, kept inside [lo, hi] by
    moving the whole 2·tau window, so at an end one edge stays at the end and the other takes
    the full mismatch, and the range's two ends where it is narrower than the window."""
    late, early = 1 + tau, 1 - tau
    if trf * late > hi:
        late = hi / trf
        early = late - 2 * tau
    if trf * early < lo:
        early = lo / trf
        late = min(early + 2 * tau, hi / trf)
    return late, early


def _one_edge(inp, credit_ns, charge_ns):
    """Two P->P clock detections of ONE Manager edge, from their values at their corners' own
    slew factors (the credit early, at (1 − τ); the charge late, at (1 + τ)), re-taken at the
    edge's one factor: whichever of (1 + τ) and (1 − τ) leaves the leg the less."""
    tau = inp.tRF_tolerance_frac
    raw = credit_ns / (1 - tau) - charge_ns / (1 + tau)
    return min(raw * (1 + tau), raw * (1 - tau))


def _mixed(man_spec, per_spec, size="small", **kw):
    """A CalcInputs with each side seeded from its own spec's preset for system size `size`;
    `kw` overrides any field."""
    m = preset(man_spec, "Man", size)
    p = preset(per_spec, "Per", size)
    return CalcInputs(**{
        **dict(
            Man_spec=man_spec, Per_spec=per_spec, system_size=size,
            Man_t_DD_min_ns=m.t_DD_min_ns, Man_t_DD_max_ns=m.t_DD_max_ns,
            Per_t_DD_min_ns=p.t_DD_min_ns, Per_t_DD_max_ns=p.t_DD_max_ns,
            Man_t_IS_max_ns=m.t_IS_max_ns, Man_t_IH_max_ns=m.t_IH_max_ns,
            Per_t_IS_max_ns=p.t_IS_max_ns, Per_t_IH_max_ns=p.t_IH_max_ns,
            Man_t_DZ_max_ns=m.t_DZ_max_ns, Per_t_DZ_max_ns=p.t_DZ_max_ns,
            Man_t_ZD_ns=m.t_ZD_ns, Per_t_ZD_ns=p.t_ZD_ns,
            tRF_Man_CLK_ns=m.tRF_ns, tRF_Man_DATA_ns=m.tRF_ns,
            tRF_Per_DATA_ns=p.tRF_ns),
        **BASE, **kw,
    })


def _same(spec, **kw):
    """Both sides built to one spec, each seeded from its preset (`_mixed`)."""
    return _mixed(spec, spec, **kw)


# The bus pairs with a SoundWire side: a SoundWire Manager, a SoundWire Peripheral, and both.
_SW_PAIRS = (("SW13_1V2", "SWI3S_PHY2"), ("SWI3S_PHY2", "SW13_1V2"), ("SW13_1V2", "SW13_1V2"))


def _crossing_coefficients(breakdowns) -> dict:
    """{(crossing symbol, lane symbol): {leg: coefficient}} over every crossing term.

    A lone term's coefficient is `factors[0]`. A GATHERED term,
    `(X_MP,A,rise,late − X_MP,B,fall,early) · Man_t_RF,CLK`, lists its crossings in the order
    its `factors` carry their signed coefficients, the row value last (`Term.factors`).
    """
    import re
    out: dict = {}
    for leg, b in breakdowns.items():
        for t in b.terms:
            if "X_" not in t.symbol or " · " not in t.symbol or t.factors is None:
                continue
            head, _, lane = t.symbol.partition(" · ")
            if head.startswith("("):
                heads = [h.strip() for h in re.split(r" [−+] ", head[1:-1])]
                coeffs = [abs(c) for c in t.factors[:-1]]
            else:
                heads, coeffs = [head], [abs(t.factors[0])]
            assert len(heads) == len(coeffs), (leg, t.symbol, t.factors)
            for h, c in zip(heads, coeffs):
                out.setdefault((h, lane), {}).setdefault(leg, c)
    return out


# ---------------------------------------------------------------------------
# 1. Non-regression
# ---------------------------------------------------------------------------

def test_swi3s_defaults_unchanged_by_the_spec_selector():
    """The default CalcInputs is SWI3S on both sides and must be untouched.

    The selector defaults to SWI3S/SWI3S, so the validated single-spec path is
    the default path; these are its pinned margins.
    """
    r = compute(CalcInputs())
    # At the 5.0 ns slews of the default inputs, every edge placed by its 20 % point and every
    # crossing counted from there, each leg at one V_SEOS per device.
    expect = {
        "MP_setup": +7.121, "MP_setup_ho": +15.121, "MP_hold": -2.470,
        "PM_setup": -3.429, "PM_setup_ho": +6.571, "PM_hold": +7.573,
    }
    for ineq, want in expect.items():
        assert r.breakdowns[ineq].margin_ns == pytest.approx(want, abs=5e-4), ineq
    # No cross-spec advisories fire on an all-SWI3S bus.
    assert r.caveats == ()
    assert r.F_CLK_legal and r.F_CLK_ceiling_MHz == 13.2


def test_every_leg_displays_the_margin_it_reports():
    """The equation on screen must SUM to the margin beside it, on every leg and every spec.

    A setup leg computes its margin from a `*_required` expression and builds its term list
    SEPARATELY — two spellings of one inequality, with nothing tying them together.

    Hold legs sum their terms to get the margin, so for them this is a tautology, asserted
    anyway.

    THE LANES ARE ALSO RUN APART. With Man CLK, Man DATA and Per DATA at one t_RF, a leg that
    computes its margin from the wrong data lane still sums correctly, so each configuration
    is repeated with the three lanes at three different values, in every order.
    """
    lane_splits = [{}] + [
        dict(zip(("tRF_Man_CLK_ns", "tRF_Man_DATA_ns", "tRF_Per_DATA_ns"), order))
        for order in itertools.permutations((3.0, 5.0, 8.3))
    ]
    for spec, extra, lanes in itertools.product(
            ("SWI3S_PHY2", "SWI3S_PHY2_PROP", "SW13_1V2"),
            ({}, {"handover_UIs": 0.0}, {"Man_ede": True, "Per_ede": True}),
            lane_splits):
        cfg = {**extra, **lanes}
        r = compute(_same(spec, **cfg))
        for name, b in r.breakdowns.items():
            assert sum(t.value_ns for t in b.terms) == pytest.approx(b.margin_ns, abs=5e-9), \
                (f"{spec} {name}{cfg}: terms sum to "
                 f"{sum(t.value_ns for t in b.terms):+.4f} but the margin reads "
                 f"{b.margin_ns:+.4f} — the equation and the number disagree")


def _displayed_sum(terms) -> float:
    """The number the substitution row reads, evaluated as a reader would: the row's text
    with its markup removed, `−` as minus and `·` as times."""
    import re

    from swi3s_studio.ui.timing_view import TimingView
    text = re.sub(r"<[^>]+>", "", TimingView._numeric_only(terms))
    expr = text.replace("−", "-").replace("·", "*")
    assert re.fullmatch(r"[0-9.+\-*() ]+", expr), text
    return float(eval(expr))                            # digits, operators and parentheses only


def _row_rounding_ns(terms) -> float:
    """The most the substitution row's two-place rounding can move its sum: 0.005 on a single
    number, and on a product of coefficients c and a row value v, each printed to 0.005,
    Σ(0.005·|v| + 0.005·|c| + 0.005²)."""
    bound = 0.0
    for t in terms:
        if not t.factors:
            bound += 0.005
            continue
        *coeffs, v = t.factors
        bound += sum(0.005 * abs(v) + 0.005 * abs(c) + 0.005 ** 2 for c in coeffs)
    return bound + 1e-9


@pytest.mark.parametrize("spec", SPEC_SOURCES)
def test_every_leg_s_substitution_row_reads_its_margin(spec):
    """The substitution row's numbers, as printed, add up to the margin beside them: a term
    whose own value is negative keeps its sign (`− (−2.00)`), on every spec including the
    EDE source on both sides. Each number is printed to two places, so the row is within the
    rounding of its printed numbers of the margin (`_row_rounding_ns`)."""
    for extra in ({}, {"Man_ede": True, "Per_ede": True}, {"handover_UIs": 0.0}):
        r = compute(_same(spec, **extra))
        for name, b in r.breakdowns.items():
            got = _displayed_sum(b.terms)
            assert got == pytest.approx(b.margin_ns, abs=_row_rounding_ns(b.terms)), \
                f"{spec} {name}{extra}: the row reads {got:+.2f}, the margin {b.margin_ns:+.2f}"


def test_the_pp_setup_legs_follow_the_peripheral_data_lane():
    """PP_setup and PP_setup_ho read the Per DATA lane and never the Man DATA lane.

    A Peripheral drives the data on a P->P leg, so its late data crossing is on `Per DATA`.
    Asserted three ways: moving Man DATA alone leaves both margins unchanged, moving Per DATA
    alone moves them by the late crossing from the launch's anchor, which the envelope counts
    from the edge's 20 % point, on the late edge's slew factor -- and the worst-corner search
    drives the Per DATA row to its slow end.
    """
    base = CalcInputs()
    for leg in ("PP_setup", "PP_setup_ho"):
        b0 = compute(base).breakdowns[leg]
        m0 = b0.margin_ns
        m_man = compute(dc_replace(base, tRF_Man_DATA_ns=8.3)).breakdowns[leg].margin_ns
        per = compute(dc_replace(base, tRF_Per_DATA_ns=8.3)).breakdowns[leg]
        m_per = per.margin_ns
        assert m_man == pytest.approx(m0, abs=1e-12), (leg, m0, m_man)
        # The Per DATA crossing at the leg's own supplies, the same at both slews.
        assert per.supplies == b0.supplies, (leg, b0.supplies, per.supplies)
        env = base.envelope(b0.supplies)
        x0 = env.mp_late_rise_per / 1.05            # the crossing without its slew factor
        cost = lambda t: x0 * _edge(t)[0] * t                    # noqa: E731
        assert m0 - m_per == pytest.approx(cost(8.3) - cost(5.0), abs=1e-9), (leg, m0, m_per)
        picks = find_worst_corner_rows(leg, default_swi3s_rows(), base)
        assert picks["t_RF Per DATA"] == "max", (leg, picks["t_RF Per DATA"])


@pytest.mark.parametrize("man_shape,per_shape", [("linear", "exp"), ("exp", "linear")])
def test_the_pp_data_crossing_takes_the_peripheral_ramp_shape(man_shape, per_shape):
    """On every P->P leg peripheral A drives the data, so B's data crossing on
    `Per_t_RF,DATA`, `X_PP,B`, takes Per_shape; the clock lane is the Manager's and takes
    Man_shape, B's detection of it `X_MP,B`.

    Pinned with the two shapes different, the only case where the choice shows: each
    coefficient equals the single-shape envelope's, counted from the 20 % point, for the
    device driving that lane, at the leg's supplies -- the transmitter's as the rail, B's for
    the threshold.
    """
    from swi3s_studio.timing.delta_tpd import compute_delta_tpd_envelope

    def single(shape, tx, rx):
        return compute_delta_tpd_envelope(ramp_shape=shape, from_anchor=True,
                                          tx_supply=tx, rx_supply=rx)

    inp = CalcInputs(Man_shape=man_shape, Per_shape=per_shape)
    b = compute(inp).breakdowns
    coeffs = _crossing_coefficients(b)
    # (shape of the lane's driver, transmitter's index in `supplies`, envelope field).
    # Supplies are (Manager, Peripheral A, Peripheral B); B receives every one of these.
    # B's clock detection shares its symbol with PP_contention's acquirer where that detects the
    # same edge, so only the data legs' entries are read here.
    want = {
        ("X_PP,B,rise,late", "Per_t_RF,DATA"): ((per_shape, 1, "mp_late_rise"),
                                                {"PP_setup", "PP_setup_ho"}),
        ("X_MP,B,fall,early", "Man_t_RF,CLK"): ((man_shape, 0, "mp_early_fall"),
                                                {"PP_setup", "PP_setup_ho"}),
        ("X_PP,B,fall,early", "Per_t_RF,DATA"): ((per_shape, 1, "mp_early_fall"),
                                                 {"PP_hold", "PP_hold_ho"}),
        ("X_MP,B,rise,late", "Man_t_RF,CLK"): ((man_shape, 0, "mp_late_rise"),
                                               {"PP_hold", "PP_hold_ho"}),
    }
    for key, ((shape, tx, field), legs) in want.items():
        assert legs <= set(coeffs[key]) <= legs | {"PP_contention"}, (key, coeffs[key])
        for leg, got in ((leg, coeffs[key][leg]) for leg in legs):
            s = b[leg].supplies
            value = getattr(single(shape, s[tx], s[2]), field)
            assert got == pytest.approx(value, rel=1e-12), (key, leg, got, value)
    # The margin moves with the Peripheral's shape through that lane. Against the same bus
    # with the Manager's shape on both devices, PP_hold_ho differs by the change in the data
    # crossing FROM THE ANCHOR -- each envelope's coefficient, counted from its own shape's
    # 20 % point -- times the lane's t_RF. Both legs are evaluated at the mixed bus's supply
    # assignment, so their clock crossings are the same.
    from swi3s_studio.timing.calculator import _compute_at
    mixed = b["PP_hold_ho"]
    same = _compute_at(CalcInputs(Man_shape=man_shape, Per_shape=man_shape),
                       mixed.supplies).breakdowns["PP_hold_ho"]
    _, a, b_ = mixed.supplies
    shift = ((single(per_shape, a, b_).mp_early_fall - single(man_shape, a, b_).mp_early_fall)
             * inp.tRF_Per_DATA_ns)
    assert mixed.margin_ns - same.margin_ns == pytest.approx(shift, abs=1e-9)


# ---------------------------------------------------------------------------
# 2. The anchor placement
# ---------------------------------------------------------------------------


def _keeper_thresholds(inp, man_supply):
    """The keeper's (rising, falling) input thresholds at the Manager's supply, by hand: a
    SoundWire Manager's 65 % of the larger of its supply and nominal and 35 % of the smaller,
    a SWI3S Manager's V_IH,max and V_IL,min times its supply."""
    if inp.Man_spec == "SW13_1V2":
        return 0.65 * max(man_supply, 1.0), 0.35 * min(man_supply, 1.0)
    return inp.V_IH_max_frac * man_supply, inp.V_IL_min_frac * man_supply


def test_the_keeper_leg_charges_the_launch_at_its_anchor_and_its_crossing_at_the_manager():
    """A keeper leg waits from the launch's anchor to the data's crossing of the keeper's
    threshold at the Manager: t_DD, then on a linear ramp X_K·t_RF on a late edge, X_K the
    later of the crossings of V_IH,max rising and V_IL,min falling at the Manager's supply,
    counted from the anchor, on a ramp to the releaser's supply, a Peripheral's data offset by
    the ground shift α. The leg charges no ramp to V_OH / V_OL.

    t_DD ends at 20 % of the ramp, so the crossing is the ramp from there, at every slew of the
    setting, the edge's t_RF taking the per-edge slew tolerance inside the setting's range. The
    Manager's release and launch are both measured from its clock reference, so `keeper_Man`
    carries no clock term; `keeper_Per` carries one, the spacing of its Peripheral's two
    detections.
    """
    for trf in (3.0, 5.0, 8.3):
        inp = CalcInputs(tRF_Man_CLK_ns=trf, tRF_Man_DATA_ns=trf, tRF_Per_DATA_ns=trf)
        r = compute(inp)
        swing = inp.V_OH_min_frac - inp.V_OL_max_frac
        for leg, side in (("keeper_Per", "Per"), ("keeper_Man", "Man")):
            b = r.breakdowns[leg]
            m, p = b.supplies[:2]
            tx, alpha = (m, 0.0) if side == "Man" else (p, inp.V_noise_pp_frac)
            th, tl = _keeper_thresholds(inp, m)
            rise = (th + alpha) / (swing * tx) - inp.V_OL_max_frac / swing
            fall = (tx - (tl - alpha)) / (swing * tx) - (1.0 - inp.V_OH_min_frac) / swing
            terms = b.terms
            launch = next(t for t in terms if t.symbol == f"{side}_t_DD")
            # The later crossing's edge, rising on a tie (`_keeper_case`).
            edge = "rise" if rise >= fall - 1e-12 else "fall"
            (x_k,) = [t for t in terms if t.symbol == f"X_K,{edge},late · {side}_t_RF,DATA"]
            assert -launch.value_ns == pytest.approx(getattr(inp, f"{side}_t_DD_max_ns"))
            assert -x_k.value_ns == pytest.approx(max(rise, fall) * _edge(trf)[0] * trf,
                                                  abs=1e-9), (leg, trf)
            assert not any(t.symbol.startswith(f"{side}_t_RF,DATA") for t in terms), leg
            clk = [t for t in terms if "Man_t_RF,CLK" in t.symbol]
            assert len(clk) == (1 if side == "Per" else 0), (leg, trf, clk)
            # t_DZ has no ramp in it and is used as tabulated.
            release = next(t for t in terms if t.symbol == f"{side}_t_DZ")
            assert release.value_ns == pytest.approx(getattr(inp, f"{side}_t_DZ_max_ns"))


def test_an_ede_release_is_the_launch_plus_t_rf_plus_the_ede_delay():
    """On a linear ramp an EndDriveEarly release is `t_DD + t_RF + EDE Delay − UI` from the
    closing edge, on either side and at every slew: t_DD ends at the ramp's 20 % point, t_RF
    -- a late edge's, with the per-edge slew tolerance inside the range -- takes it to 80 %,
    and the EDE Delay runs from there. A Manager's release is measured from its clock
    reference, the 20 % point its clock edge is placed by, so its contention leg's clock term
    is the peripheral's crossing alone and no term names `f_A`. Checked on the helper, and on
    the parts each releasing contention leg shows: `<dev>_t_DD`, the ramp
    `<dev>_t_RF,DATA,fall` (falling on a linear ramp's tie, `ede_release_rising`),
    `<dev>_t_EDE` and `UI`.
    """
    from swi3s_studio.timing.calculator import _ede_release_vs_edge_ns

    for trf in (3.0, 5.0, 8.3):
        inp = CalcInputs(Man_ede=True, Per_ede=True, handover_UIs=0.0,
                         tRF_Man_DATA_ns=trf, tRF_Per_DATA_ns=trf, Per_t_EDE_ns=6.0,
                         Man_t_EDE_ns=4.0)
        ramp = _edge(trf)[0] * trf
        per = inp.Per_t_DD_max_ns + ramp + 6.0 - inp.UI_ns()
        man = inp.Man_t_DD_max_ns + ramp + 4.0 - inp.UI_ns()
        assert _ede_release_vs_edge_ns(inp, "Per") == pytest.approx(per)
        assert _ede_release_vs_edge_ns(inp, "Man") == pytest.approx(man)
        r = compute(inp)
        for leg, want, side, ede in (("PM_contention", per, "Per", 6.0),
                                     ("PP_contention", per, "Per", 6.0),
                                     ("MP_contention", man, "Man", 4.0)):
            ramp_sym = f"{side}_t_RF,DATA,fall"
            parts = {t.symbol: t for t in r.breakdowns[leg].terms
                     if t.symbol in (f"{side}_t_DD", ramp_sym, f"{side}_t_EDE", "UI")}
            assert len(parts) == 4, (leg, list(parts))
            assert -parts[f"{side}_t_DD"].value_ns == pytest.approx(
                getattr(inp, f"{side}_t_DD_max_ns")), (leg, trf)
            assert -parts[ramp_sym].value_ns == pytest.approx(ramp), (leg, trf)
            assert -parts[f"{side}_t_EDE"].value_ns == pytest.approx(ede), (leg, trf)
            assert parts["UI"].value_ns == pytest.approx(inp.UI_ns()), (leg, trf)
            assert -sum(t.value_ns for t in parts.values()) == pytest.approx(want), (leg, trf)
        clk = next(t for t in r.breakdowns["MP_contention"].terms
                   if t.symbol.endswith("· Man_t_RF,CLK"))
        # At zero handover UIs the acquirer detects the closing edge, and a falling one's
        # early detection is the smaller credit.
        assert r.breakdowns["MP_contention"].closing_rising is False, trf
        assert clk.symbol == "X_MP,fall,early · Man_t_RF,CLK", clk.symbol
        assert not any("f_A" in t.symbol for t in r.breakdowns["MP_contention"].terms), trf


def _exp_to_valid(v_oh, v_ol, rising):
    """An exponential ramp's time from its anchor to its valid level, in units of its t_RF
    (the 20-80 % time, so the ramp is 1 − 4^(−t) rising and 4^(−t) falling): the closed
    form."""
    if rising:
        return math.log(1.0 - v_ol, 4.0) - math.log(1.0 - v_oh, 4.0)
    return math.log(v_oh, 4.0) - math.log(v_ol, 4.0)


def _ramp_term(terms, side):
    """The lone ramp from `side`'s launch anchor to V_OH / V_OL among `terms`:
    `<side>_t_RF,DATA,rise` or `,fall`, then `,fast` where it is priced on the fast edge."""
    (t,) = [t for t in terms if " · " not in t.symbol
            and t.symbol.split(",")[:2] == [f"{side}_t_RF", "DATA"]]
    return t


def _ramp_symbol(side, rising):
    """The ramp's symbol, priced on the edge `rising`: it names that edge whatever the two
    polarities' crossing times."""
    return f"{side}_t_RF,DATA,{'rise' if rising else 'fall'}"


@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.20), (0.90, 0.10), (0.80, 0.15), (0.85, 0.25)])
def test_an_exponential_ramp_to_v_oh_is_its_true_crossing_on_keeper_and_ede_legs(v_oh, v_ol):
    """An EndDriveEarly release's ramp from the launch's anchor to V_OH / V_OL is the ramp's
    own crossing time on its edge: one t_RF on a linear ramp, c·t_RF on an exponential one.
    On a contention leg c is at the data polarity with the larger value (the data's polarity
    is free), and changing only the releasing device's shape, every other term stays and the
    margin falls by (c − 1)·t_RF on a late edge: PM_contention and PP_contention with a
    releasing EDE Peripheral. At 0.20 / 0.80 c is 1 and nothing moves. On an EDE keeper leg the
    ramp is a credit, c at the polarity its keeper crossing is priced on (`_keeper_case`),
    on the edge the leg reports."""
    c_rise, c_fall = (_exp_to_valid(v_oh, v_ol, r) for r in (True, False))
    c = max(c_rise, c_fall)
    late = _edge(5.0)[0]
    drop = (c - 1.0) * late * 5.0
    from swi3s_studio.timing.calculator import _keeper_case, edge_factor
    ede = CalcInputs(V_OH_min_frac=v_oh, V_OL_max_frac=v_ol, Man_ede=True, Per_ede=True,
                     handover_UIs=0.0, Man_shape="exp", Per_shape="exp")
    for leg, side in (("keeper_Man", "Man"), ("keeper_Per", "Per")):
        b = compute(ede).breakdowns[leg]
        at = dc_replace(ede, fast_data_edge=b.fast_data_edge)
        lane = f"{side} DATA"
        rising = _keeper_case(at, lane, b.supplies, ede=True)[0]
        c_edge = c_rise if rising else c_fall
        ramp = _ramp_term(b.terms, side)
        fast = ",fast" if lane in b.fast_data_edge else ""
        assert ramp.symbol == _ramp_symbol(side, rising) + fast, leg
        assert ramp.value_ns == pytest.approx(
            c_edge * edge_factor(at, lane, "late") * 5.0, abs=1e-9), (leg, v_oh)
    for side, legs, kw in (("Per", ("PM_contention", "PP_contention"),
                            {"Per_ede": True, "handover_UIs": 0.0}),):
        lin = CalcInputs(V_OH_min_frac=v_oh, V_OL_max_frac=v_ol, **kw)
        exp = dc_replace(lin, **{f"{side}_shape": "exp"})
        for leg in legs:
            a, b = compute(lin).breakdowns[leg], compute(exp).breakdowns[leg]
            assert b.margin_ns - a.margin_ns == pytest.approx(-drop, abs=1e-9), \
                (leg, v_oh, v_ol)
            ramp = _ramp_term(b.terms, side)
            # The later edge to V_OH / V_OL, falling on a tie (`ede_release_rising`).
            assert ramp.symbol == _ramp_symbol(side, c_rise > c_fall + 1e-12), leg
            assert -ramp.value_ns == pytest.approx(c * late * 5.0, abs=1e-9), (leg, v_oh)
            assert ramp.factors == pytest.approx((c * late, 5.0), abs=1e-12), (leg, v_oh)
            lin_ramp = _ramp_term(a.terms, side)
            assert lin_ramp.symbol == f"{side}_t_RF,DATA,fall", leg
            assert -lin_ramp.value_ns == pytest.approx(late * 5.0, abs=1e-12), (leg, v_oh)
            others = [t.value_ns for t in b.terms if t is not ramp]
            assert others == pytest.approx(
                [t.value_ns for t in a.terms if t is not lin_ramp], abs=1e-12), (leg, v_oh)


@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.20), (0.90, 0.10), (0.80, 0.15), (0.85, 0.25)])
def test_a_manager_ede_release_charges_its_exponential_ramp_to_v_oh(v_oh, v_ol):
    """The Manager's EndDriveEarly release on MP_contention charges the ramp from its anchor
    to V_OH / V_OL as c·t_RF on a late edge, c the closed-form crossing time at the data
    polarity with the larger value, as the Peripheral's does. Read from the term itself: a
    Manager shape change also moves the clock lane's crossings, so the margin cannot isolate
    it. At 0.20 / 0.80 c is 1."""
    c_rise, c_fall = (_exp_to_valid(v_oh, v_ol, r) for r in (True, False))
    c = max(c_rise, c_fall)
    late = _edge(5.0)[0]
    inp = CalcInputs(Man_ede=True, handover_UIs=0.0, Man_shape="exp",
                     V_OH_min_frac=v_oh, V_OL_max_frac=v_ol)
    assert inp.tRF_Man_DATA_ns == 5.0
    b = compute(inp).breakdowns["MP_contention"]
    ramp = _ramp_term(b.terms, "Man")
    # An EndDriveEarly release takes the falling edge on a tie (`ede_release_rising`).
    assert ramp.symbol == _ramp_symbol("Man", c_rise > c_fall + 1e-12), ramp.symbol
    assert ramp.factors == pytest.approx((c * late, 5.0), abs=1e-12), (v_oh, v_ol)
    assert ramp.value_ns == pytest.approx(-c * late * 5.0, abs=1e-9), (v_oh, v_ol)
    assert (c == pytest.approx(1.0, abs=1e-12)) is ((v_oh, v_ol) == (0.80, 0.20))


def test_the_edge_tie_goes_rising_on_a_keeper_and_falling_on_an_ede_release():
    """Where the two polarities' crossings of the keeper's threshold are equal (the default
    window, symmetric about mid-rail, at one supply for every device), a keeper leg takes the
    rising edge (`_keeper_case`); a V_IH,max a few parts in a million off its tie takes the
    later crossing. Where the two polarities' ramps to V_OH / V_OL are equal (a linear ramp, or
    an exponential one at V_OH + V_OL = 1), an EndDriveEarly release takes the falling edge
    (`ede_release_rising`), and a pair whose ramps differ by a few parts in a million takes
    the longer ramp."""
    from swi3s_studio.timing.calculator import _keeper_case, ede_release_rising, to_voh_frac
    for shape in ("linear", "exp"):
        for v_ih, rising in ((0.65, True), (0.65 + 1e-6, True), (0.65 - 1e-6, False)):
            inp = CalcInputs(Man_shape=shape, Per_shape=shape, V_IH_max_frac=v_ih)
            for lane in ("Man DATA", "Per DATA"):
                for ede in (False, True):
                    got = _keeper_case(inp, lane, (1.0, 1.0, 1.0), ede=ede)[0]
                    assert got is rising, (shape, v_ih, lane, ede)
    for shape, v_ol in (("linear", 0.20), ("linear", 0.15), ("exp", 0.20)):
        inp = CalcInputs(Man_shape=shape, Per_shape=shape, V_OL_max_frac=v_ol)
        for side in ("Man", "Per"):
            assert ede_release_rising(inp, side) is False, (shape, v_ol, side)
    for eps, longer_rising in ((1e-6, True), (-1e-6, False)):
        inp = CalcInputs(Man_shape="exp", Per_shape="exp", V_OL_max_frac=0.20 + eps)
        for side, lane in (("Man", "Man DATA"), ("Per", "Per DATA")):
            diff = to_voh_frac(inp, lane, True) - to_voh_frac(inp, lane, False)
            assert 1e-7 < abs(diff) < 1e-5 and (diff > 0) is longer_rising, diff
            assert ede_release_rising(inp, side) is longer_rising, (eps, side)


@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.15), (0.85, 0.25), (0.90, 0.30), (0.90, 0.10)])
def test_one_ramp_symbol_has_one_value_on_every_leg_at_every_supply_assignment(v_oh, v_ol):
    """The ramp from a launch's anchor to V_OH / V_OL is priced on the edge each leg's release
    takes, and the term names that edge, `<dev>_t_RF,DATA,rise` or `,fall`, whatever the two
    polarities' crossing times; on an exponential ramp at V_OH + V_OL ≠ 1 they differ. One
    symbol has one value across all legs at each supply assignment
    (`CalcInputs.supply_corners`). Its coefficient is that edge's closed-form crossing time on
    a late edge (`_exp_to_valid`). Swept over SoundWire and SWI3S on either side, EndDriveEarly
    on either side, and zero or one handover UI."""
    from swi3s_studio.timing.calculator import _compute_at, edge_factor
    specs = ("SWI3S_PHY2", "SW13_1V2")
    edes = ((False, False), (True, False), (False, True), (True, True))
    qualified = set()
    checked = 0
    for (ms, ps), man, per, (me, pe), n in itertools.product(
            (("exp", "exp"), ("exp", "linear"), ("linear", "exp")), specs, specs, edes,
            (0.0, 1.0)):
        inp = CalcInputs(Man_shape=ms, Per_shape=ps, Man_spec=man, Per_spec=per, Man_ede=me,
                         Per_ede=pe, handover_UIs=n, V_OH_min_frac=v_oh, V_OL_max_frac=v_ol)
        for sup in inp.supply_corners():
            seen: dict[str, dict[str, float]] = {}
            for leg, b in _compute_at(inp, sup).breakdowns.items():
                for t in b.terms:
                    if " · " in t.symbol or "_t_RF," not in t.symbol:
                        continue
                    side = t.symbol[:3]
                    shape = ms if side == "Man" else ps
                    c = {r: (_exp_to_valid(v_oh, v_ol, r) if shape == "exp" else 1.0)
                         for r in (True, False)}
                    lane = f"{side} DATA"
                    assert t.symbol in (f"{side}_t_RF,DATA,rise", f"{side}_t_RF,DATA,fall"), (
                        leg, t.symbol)
                    rising = t.symbol.endswith(",rise")
                    qualified.add(t.symbol)
                    assert t.factors[0] == pytest.approx(
                        c[rising] * edge_factor(inp, lane, "late"), abs=1e-12), (leg, t.symbol)
                    seen.setdefault(t.symbol, {})[leg] = abs(t.value_ns)
                    checked += 1
            for sym, by_leg in seen.items():
                vals = list(by_leg.values())
                assert all(v == pytest.approx(vals[0], abs=1e-12) for v in vals), (
                    f"{sym} carries {by_leg} at supplies {sup}, {man}/{per}, {ms}/{ps}, EDE "
                    f"{me}/{pe}, N_HO {n}: one symbol, several values")
    assert checked, "the sweep found no ramp term to check"
    if not math.isclose(v_oh + v_ol, 1.0):
        assert {"Man_t_RF,DATA,rise", "Man_t_RF,DATA,fall", "Per_t_RF,DATA,rise",
                "Per_t_RF,DATA,fall"} <= qualified, qualified


@pytest.mark.parametrize("shape", ["linear", "exp"])
@pytest.mark.parametrize("man,per", [("SW13_1V2", "SWI3S_PHY2_EDE"), ("SW13_1V2", "SW13_1V2"),
                                     ("SWI3S_PHY2", "SW13_1V2")])
def test_a_ramp_priced_on_the_fast_edge_names_it(man, per, shape):
    """`compute` reports each leg at the slow or the fast data-edge set
    (`CalcInputs.fast_data_edge`), and the ramp to V_OH / V_OL is priced on its lane's edge
    in that set, so a ramp on the fast edge carries `,fast` (`to_voh_symbol`) and its note
    names the fast edge: across every leg of one result, one ramp symbol has one value."""
    inp = CalcInputs(Man_spec=man, Per_spec=per, Man_shape=shape, Per_shape=shape,
                     Man_ede=True, Per_ede=True, handover_UIs=0.0)
    seen: dict[str, dict[str, float]] = {}
    fast = 0
    for leg, b in compute(inp).breakdowns.items():
        for t in b.terms:
            if " · " in t.symbol or "_t_RF," not in t.symbol:
                continue
            on_fast = f"{t.symbol[:3]} DATA" in b.fast_data_edge
            assert t.symbol.endswith(",fast") is on_fast, (leg, t.symbol, b.fast_data_edge)
            assert ("on a fast edge" in t.note) is on_fast, (leg, t.note)
            fast += on_fast
            seen.setdefault(t.symbol, {})[leg] = abs(t.value_ns)
    assert seen, "no ramp term to check"
    for sym, by_leg in seen.items():
        vals = list(by_leg.values())
        assert all(v == pytest.approx(vals[0], abs=1e-12) for v in vals), (sym, by_leg)
    if man == "SW13_1V2" and per == "SWI3S_PHY2_EDE":
        assert fast, "the sweep must price a ramp on the fast edge"


@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.15), (0.85, 0.25), (0.90, 0.30), (0.80, 0.20)])
def test_one_anchor_symbol_has_one_value_on_every_leg_at_every_supply_assignment(v_oh, v_ol):
    """A SoundWire launch's anchor correction is placed on its edges, and the term names them
    (`anchor_symbol`) whatever their corrections, which differ at V_OH + V_OL ≠ 1: a
    Peripheral's data edge, `Per_t_<row>,anchor,rise` / `,fall`, and a Manager's clock then
    data edge, `Man_t_<row>,anchor,rr` / `,rf` / `,fr` / `,ff`. Its value is
    `sw_anchor_ns` on the edges it names, at the corner its row reads (max for a charge, min
    for a credit). A max-corner correction whose side's DATA lane is in the leg's fast
    data-edge set (`CalcInputs.fast_data_edge`) is priced on the fast edge and ends in `,fast`
    after those letters; no other does. One symbol has one value at a corner on every leg of
    both data-edge sets at each supply assignment, the legs `compute` reports together. Swept
    over SoundWire on either side, both shapes, EndDriveEarly, handover UIs and slews."""
    from swi3s_studio.timing.calculator import _compute_at, sw_anchor_ns
    qualified = set()
    fast_seen = set()
    checked = 0
    for (man, per), shape, (me, pe), n, trf in itertools.product(
            _SW_PAIRS, ("linear", "exp"), ((False, False), (True, True)), (0.0, 1.0),
            (5.0, 8.3)):
        inp = CalcInputs(Man_spec=man, Per_spec=per, Man_shape=shape, Per_shape=shape,
                         Man_ede=me, Per_ede=pe, handover_UIs=n, tRF_Man_CLK_ns=trf,
                         tRF_Man_DATA_ns=trf, tRF_Per_DATA_ns=trf, V_OH_min_frac=v_oh,
                         V_OL_max_frac=v_ol)
        lanes = inp.fast_edge_lanes()
        edge_sets = [frozenset(), frozenset(lanes)] if lanes else [frozenset()]
        for sup in inp.supply_corners():
            seen: dict[tuple, dict[str, float]] = {}
            runs = [(leg, b) for e in edge_sets
                    for leg, b in _compute_at(dc_replace(inp, fast_data_edge=e),
                                              sup).breakdowns.items()]
            for leg, b in runs:
                at = dc_replace(inp, fast_data_edge=b.fast_data_edge)
                leg = f"{leg} {sorted(b.fast_data_edge)}"
                for i, t in enumerate(b.terms):
                    if ",anchor" not in t.symbol:
                        continue
                    side = t.symbol[:3]
                    corner = "max" if b.terms[i - 1].op == "-" else "min"
                    on_fast = corner == "max" and f"{side} DATA" in b.fast_data_edge
                    assert t.symbol.endswith(",fast") is on_fast, (leg, t.symbol, corner)
                    if on_fast:
                        fast_seen.add(t.symbol)
                    corr = {(cn, r, c): sw_anchor_ns(at, side, cn, rising=r, clk_rising=c)
                            for cn in ("max", "min") for r in (True, False)
                            for c in (True, False)}
                    edge = t.symbol.partition(",anchor")[2].removesuffix(",fast")
                    if side == "Man":
                        assert edge in (",rr", ",rf", ",fr", ",ff"), (leg, t.symbol)
                        want = corr[corner, edge[2] == "r", edge[1] == "r"]
                    else:
                        assert edge in (",rise", ",fall"), (leg, t.symbol)
                        want = corr[corner, edge == ",rise", True]
                    qualified.add(t.symbol.removesuffix(",fast"))
                    assert abs(t.value_ns) == pytest.approx(abs(want), abs=1e-9), (
                        leg, t.symbol, t.value_ns, want)
                    seen.setdefault((t.symbol, corner), {})[leg] = abs(t.value_ns)
                    checked += 1
            for key, by_leg in seen.items():
                vals = list(by_leg.values())
                assert all(v == pytest.approx(vals[0], abs=1e-9) for v in vals), (
                    f"{key} carries {by_leg} at supplies {sup}, {man}/{per}, {shape}, EDE "
                    f"{me}/{pe}, N_HO {n}, t_RF {trf}: one symbol, several values")
    assert checked, "the sweep found no anchor term to check"
    # Positive control: a SoundWire Manager's keeper_Man launch is priced on the fast edge.
    assert any(s.startswith("Man_t_DD,anchor") for s in fast_seen), fast_seen
    assert {"Per_t_DD,anchor,rise", "Per_t_DD,anchor,fall"} <= qualified, qualified
    assert len({s for s in qualified if s.startswith("Man_t_DD,anchor,")}) >= 3, qualified


@pytest.mark.parametrize("man", ["SWI3S_PHY2_EDE", "SW13_1V2"])
def test_a_keepers_ede_leg_takes_its_crossing_and_ramp_on_one_data_edge(man):
    """An EndDriveEarly release charges the anchor to the keeper's threshold at the Manager
    (a SWI3S Manager's V_IH,max / V_IL,min, a SoundWire Manager's V_TH_Keeper,max /
    V_TL_Keeper,min) and credits the ramp to V_OH / V_OL on ONE data edge, the polarity whose
    crossing less ramp is the larger. On an exponential ramp at V_OH + V_OL ≠ 1 the ramp
    differs by polarity, log_4((1 − V_OL)/(1 − V_OH)) rising and log_4(V_OH/V_OL) falling."""
    from swi3s_studio.timing.calculator import _compute_at, edge_factor
    v_oh, v_ol = 0.80, 0.15
    inp = dc_replace(_mixed(man, "SWI3S_PHY2_EDE", t_keeper_ns=2.5, handover_UIs=0.0),
                     Per_ede=True, Per_shape="exp", V_OH_min_frac=v_oh, V_OL_max_frac=v_ol)
    fk = edge_factor(inp, "Per DATA", "late") * inp.tRF_Per_DATA_ns
    alpha = inp.V_noise_pp_frac
    for supplies in inp.supply_corners():
        man_s, tx = supplies[0], supplies[1]
        th, tl = _keeper_thresholds(inp, man_s)
        cross = {True: math.log(1.0 - v_ol, 4.0) - math.log(1.0 - (th + alpha) / tx, 4.0),
                 False: math.log(v_oh, 4.0) - math.log((tl - alpha) / tx, 4.0)}
        held = max(cross[r] - _exp_to_valid(v_oh, v_ol, r) for r in (True, False))
        want = inp.Per_t_EDE_ns - held * fk - 2.5
        b = _compute_at(inp, supplies).breakdowns["keeper_Per"]
        assert b.margin_ns == pytest.approx(want, abs=1e-9), supplies


def test_an_ede_release_shows_its_parts_in_time_order():
    """An EndDriveEarly release on a contention leg reads from the edge that opens the driven
    UI: the releaser's t_DD, its `,anchor` on a SoundWire side where the correction is not
    zero, the ramp to 80 %, the EDE Delay, then the UI back to the closing edge. The parts sit
    together right after the releaser's clock detection (first on M->P), and they sum to
    `−_ede_release_vs_edge_ns`, so the margin is the release referenced to the closing edge.
    Checked at N_HO 0, 1 and 2, with SoundWire on either side, on both ramp shapes."""
    from swi3s_studio.timing.calculator import _ede_release_vs_edge_ns, sw_anchor_ns

    specs = (("SWI3S_PHY2", "SWI3S_PHY2"), ("SW13_1V2", "SWI3S_PHY2"),
             ("SWI3S_PHY2", "SW13_1V2"), ("SW13_1V2", "SW13_1V2"))
    seen_anchor = False
    for n, (man, per), shape, ede in itertools.product(
            (0.0, 1.0, 2.0), specs, ("linear", "exp"), ("Man", "Per")):
        inp = CalcInputs(handover_UIs=n, Man_spec=man, Per_spec=per, Man_shape=shape,
                         Per_shape=shape, tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=8.3,
                         tRF_Per_DATA_ns=8.3, **{f"{ede}_ede": True})
        r = compute(inp)
        legs = ("MP_contention",) if ede == "Man" else ("PM_contention", "PP_contention")
        for leg in legs:
            b = r.breakdowns[leg]
            # Each leg is evaluated at its own fast-edge lanes and supplies.
            at = dc_replace(inp, fast_data_edge=b.fast_data_edge)
            anchor = abs(sw_anchor_ns(at, ede, "max")) > 1e-12
            seen_anchor |= anchor
            # The anchor and the ramp name the edges they are placed on.
            want = [f"{ede}_t_DD", *([f"{ede}_t_DD,anchor,"] if anchor else []),
                    f"{ede}_t_RF,DATA,", f"{ede}_t_EDE", "UI"]
            syms = [t.symbol for t in b.terms]
            start = 0 if ede == "Man" else 1
            got = syms[start:start + len(want)]
            assert len(got) == len(want) and all(
                g == w or (w.endswith(",") and g.startswith(w)) for g, w in zip(got, want)), (
                leg, n, man, per, shape, syms)
            parts = b.terms[start:start + len(want)]
            assert sum(t.value_ns for t in parts) == pytest.approx(
                -_ede_release_vs_edge_ns(at, ede), abs=1e-9), (leg, n, man, per, shape)
            assert b.margin_ns == pytest.approx(sum(t.value_ns for t in b.terms), abs=1e-9)
            assert not any("t_DZ" in s for s in syms), (leg, syms)
    assert seen_anchor, "no case put a SoundWire anchor correction on the release"


def test_a_launch_is_placed_at_its_anchor_at_every_slew():
    """t_DD and t_ZD hold at every t_RF of the ramp setting, so a launch sits at its row value
    whatever the slew, and its ramp starts the anchor fraction of t_RF earlier: 1/3 on a
    linear ramp, -ln(0.8)/ln(4) on an exponential one. A crossing is charged from the anchor:
    the envelope counts its coefficient from the 20 % point, so its term names the crossing
    alone, with no `f_A`."""
    from swi3s_studio.timing.calculator import _launch_anchor_ns, anchor_frac

    for trf in (3.0, 5.0, 8.3):
        inp = dc_replace(CalcInputs(), tRF_Per_DATA_ns=trf, tRF_Man_DATA_ns=trf)
        for side in ("Per", "Man"):
            assert _launch_anchor_ns(inp, side, "DD", "max") == getattr(
                inp, f"{side}_t_DD_max_ns"), (side, trf)
            assert _launch_anchor_ns(inp, side, "DD", "min") == getattr(
                inp, f"{side}_t_DD_min_ns"), (side, trf)
            assert _launch_anchor_ns(inp, side, "ZD", "min") == getattr(
                inp, f"{side}_t_ZD_ns"), (side, trf)
    assert anchor_frac(CalcInputs(), "Per") == pytest.approx(1.0 / 3.0, abs=1e-12)
    assert anchor_frac(CalcInputs(Per_shape="exp"), "Per") == pytest.approx(
        math.log(1 / 0.8) / math.log(4.0), abs=1e-12)
    # The data crossing of PP_setup reads from the anchor: the Peripheral-driven data lane's
    # late field of the reported sampling polarity, at the leg's supplies.
    from swi3s_studio.timing.calculator import tau_adj
    for inp in (CalcInputs(), CalcInputs(Per_shape="exp"),
                CalcInputs(Per_shape="exp", tRF_Per_DATA_ns=6.0, V_SEOS_tol_frac=0.08)):
        b = compute(inp).breakdowns["PP_setup"]
        env = inp.envelope(b.supplies)
        field = "mp_late_fall_per" if b.sampling_rising else "mp_late_rise_per"
        t = next(t for t in b.terms if t.symbol.endswith("· Per_t_RF,DATA"))
        assert t.symbol.startswith("X_PP,B,") and t.symbol.split(" · ")[0].endswith(",late")
        assert -t.value_ns == pytest.approx(
            getattr(env, field) * inp.tRF_Per_DATA_ns * tau_adj(inp, "Per DATA", "late"),
            abs=1e-12), (inp.Per_shape, field)


def test_a_clock_crossing_counts_from_the_manager_s_clock_reference():
    """Every Manager timing -- t_DD, t_ZD, t_DZ, t_IS, t_IH -- is measured from its clock
    output's V_OL / V_OH crossing, its clock reference, and every clock edge is placed by that
    20 % point (the Manager Output and Input Timing figures; the Manager sources the clock).
    A clock crossing therefore counts from it: each MP and PM leg's clock term is the crossing
    fraction from the ramp start less the anchor fraction, times the slew factor of the edge
    the crossing is on, times the clock's t_RF, at every clock slew of the setting. For setup
    that edge is MP's early sampling edge and PM's late launch edge; for hold it is the launch
    edge, late on MP and early on PM, each of the polarity of the case the leg reports. No
    term of any leg names `f_A`."""
    from swi3s_studio.timing.calculator import anchor_frac
    from swi3s_studio.timing.delta_tpd import compute_delta_tpd_envelope

    # The default bus's crossings counted from the ramp start, each with the envelope's
    # 1 ± 0.05 slew factor, at the leg's supplies (Manager, Peripheral, Peripheral B): the
    # clock and MP data from the Manager to the Peripheral, PM data the other way.
    def start(leg, tx, rx):
        sup = leg.supplies
        return compute_delta_tpd_envelope(ramp_shape="linear", tx_supply=sup[tx],
                                          rx_supply=sup[rx])

    f_a = 1 / 3
    assert anchor_frac(CalcInputs(), "Man") == pytest.approx(f_a, abs=1e-12)
    for k in (3.0, 5.0, 8.3):
        inp = CalcInputs(tRF_Man_CLK_ns=k, tRF_Man_DATA_ns=k)
        late_k, early_k = _edge(k)
        s = inp.tRF_Per_DATA_ns
        late_s, early_s = _edge(s)
        b = compute(inp).breakdowns
        clk = {leg: start(b[leg], 0, 1) for leg in ("MP_setup", "MP_hold", "PM_setup",
                                                    "PM_hold")}
        data = {leg: start(b[leg], 1, 0) for leg in ("PM_setup", "PM_hold")}
        # Σ_setup's cases on each launching edge, the crossings without their slew factors:
        # the data on the clock's polarity at δ = ±α, and on the other polarity at the δ that
        # makes both late. A tie keeps the rising launching edge.
        ce, de = clk["PM_setup"], data["PM_setup"]
        cases = [(True, ce.sigma_pm_setup_clk_dgnd_pos, de.sigma_pm_setup_data_dgnd_neg),
                 (True, ce.sigma_pm_setup_clk_dgnd_neg, de.sigma_pm_setup_data_dgnd_pos),
                 (True, ce.sigma_pm_setup_clk_dgnd_pos, de.sigma_pm_setup_fall_data_dgnd_neg),
                 (False, ce.sigma_pm_setup_fall_clk_dgnd_pos,
                  de.sigma_pm_setup_fall_data_dgnd_neg),
                 (False, ce.sigma_pm_setup_fall_clk_dgnd_neg,
                  de.sigma_pm_setup_fall_data_dgnd_pos),
                 (False, ce.sigma_pm_setup_fall_clk_dgnd_neg, de.sigma_pm_setup_data_dgnd_pos)]
        sums = [c / 1.05 * late_k * k + d / 1.05 * late_s * s for _, c, d in cases]
        idx = next(i for i, v in enumerate(sums) if v >= max(sums) - 1e-12)
        launch_rising, c0, d0 = cases[idx]
        # The third case of each launching edge has its data on the other polarity.
        data_rising = launch_rising != (idx % 3 == 2)
        pm_case = f"{'r' if launch_rising else 'f'}{'r' if data_rising else 'f'}"
        cc, cd = c0 / 1.05, d0 / 1.05
        # Each leg at the polarity of the case it reports: MP's sampling edge, PM's launching
        # edge (the sampling edge's on hold, the other on setup).
        assert b["MP_setup"].sampling_rising is False
        assert b["PM_setup"].sampling_rising is (not launch_rising), k
        assert b["MP_hold"].sampling_rising is True
        hold_rising = b["PM_hold"].sampling_rising
        hc = (clk["PM_hold"].sigma_pm_hold_rf_clk if hold_rising
              else clk["PM_hold"].mp_early_fall) / 0.95
        hd = (data["PM_hold"].sigma_pm_hold_rf_data if hold_rising
              else data["PM_hold"].sigma_pm_hold_rf_clk) / 0.95
        # (symbol, coefficient from the ramp start, edge factor, sign): an early crossing
        # credits the leg and a late one charges it.
        want = {
            "MP_setup": ("X_MP,fall,early", clk["MP_setup"].mp_early_fall / 0.95, early_k, +1),
            "PM_setup": (f"X_PM,{pm_case},late", cc, late_k, -1),
            "MP_hold": ("X_MP,rise,late", clk["MP_hold"].mp_late_rise / 1.05, late_k, -1),
            "PM_hold": ("X_MP,rise,early" if hold_rising else "X_MP,fall,early", hc, early_k,
                        +1),
        }
        for leg, (sym, x, factor, sign) in want.items():
            term = next(t for t in b[leg].terms if t.symbol.endswith("· Man_t_RF,CLK"))
            assert term.symbol == f"{sym} · Man_t_RF,CLK", (leg, k, term.symbol)
            assert term.value_ns == pytest.approx(sign * (x - f_a) * factor * k, abs=1e-9), \
                (leg, k)
        for leg, bd in b.items():
            assert not any("f_A" in t.symbol for t in bd.terms), (leg, k)
        # The PM margins, the peripheral's detection read from the Manager's clock reference
        # and its data from the launch's anchor.
        pm_setup = (inp.UI_ns() - 2 * inp.t_PD_ns() - (cc - f_a) * late_k * k
                    - inp.Per_t_DD_max_ns - (cd - f_a) * late_s * s - inp.Man_t_IS_max_ns)
        assert b["PM_setup"].margin_ns == pytest.approx(pm_setup, abs=1e-9), k
        pm_hold = (2 * inp.t_PD_ns() + (hc - f_a) * early_k * k + inp.Per_t_DD_min_ns
                   + (hd - f_a) * early_s * s - inp.Man_t_IH_max_ns)
        assert b["PM_hold"].margin_ns == pytest.approx(pm_hold, abs=1e-9), k


def test_an_edge_never_leaves_its_ramp_setting():
    """The per-edge slew tolerance moves one lane's two edges ±tau about the row, but no edge
    leaves its spec's range: at the slow end of the 5 ns setting the late edge stays at 8.3 ns
    and the early one is 10 % faster; at the fast end the early edge stays at 3.0 ns and the
    late one is 10 % slower. A SoundWire lane's range is t_Slew's, 2.0 to 5.0 ns small. A
    tolerance whose 2·tau window is wider than the range puts the two edges at its ends. The
    range bounds the Manager's two lanes as it does the Peripheral's: each side's ramp setting
    is its own output's."""
    from swi3s_studio.timing.calculator import edge_factor

    for spec, tau, cases in (
            ("SWI3S_PHY2", 0.05, ((3.0, 1.10, 1.00), (5.0, 1.05, 0.95),
                                  (8.0, 8.3 / 8.0, 8.3 / 8.0 - 0.1), (8.3, 1.00, 0.90))),
            ("SW13_1V2", 0.05, ((2.0, 1.10, 1.00), (3.5, 1.05, 0.95), (5.0, 1.00, 0.90))),
            ("SW13_1V2", 0.35, ((5.0, 1.00, 0.40), (4.5, 5.0 / 4.5, 2.0 / 4.5),
                                (2.0, 1.70, 1.00)))):
        lo, hi = (3.0, 8.3) if spec == "SWI3S_PHY2" else (2.0, 5.0)
        for trf, late, early in cases:
            for lane, field in (("Per DATA", "tRF_Per_DATA_ns"), ("Man DATA", "tRF_Man_DATA_ns"),
                                ("Man CLK", "tRF_Man_CLK_ns")):
                inp = CalcInputs(Man_spec=spec, Per_spec=spec, tRF_tolerance_frac=tau,
                                 **{field: trf})
                assert edge_factor(inp, lane, "late") == pytest.approx(late), (spec, lane, trf)
                assert edge_factor(inp, lane, "early") == pytest.approx(early), (spec, lane, trf)
                assert lo - 1e-9 <= early * trf <= late * trf <= hi + 1e-9, (spec, tau, trf)
                assert (late, early) == pytest.approx(_edge(trf, lo, hi, tau)), (spec, trf)


# ---------------------------------------------------------------------------
# 3. The SoundWire equivalents
# ---------------------------------------------------------------------------

# Table 17 (1.2 V): t_OV_Data small / large, t_Slew_Clock max small / large, and the values
# common to both envelopes.
_T_OV = {"small": 27.9, "large": 29.0}
_K_MAX = {"small": 5.0, "large": 6.0}
_K_MIN, _T_ZD, _T_DZ, _T_IS, _T_IH, _T_KEEP = 2.0, 8.1, 5.0, 0.0, 4.0, 2.5
_DEPART = 500.0 * 5e-12 * 1e9 * math.log(1 / 0.9)      # SWI3S test load, 10 % departure


def _exact_equivalents(side: str, size: str) -> dict:
    """The equivalents before rounding (`sw13_equivalents`' docstring), linear ramps,
    k = t_Slew_Clock, s = t_Slew_Data:

        Peripheral  t_DD,max = t_ZD,max = t_OV − s
                    t_DD,min = t_ZD,min = t_ZD + s_ref/3
                    t_DZ = t_DZ_Data + RC·ln(1/0.9)          t_IS = t_ISetup    t_IH = t_IHold
        Manager     t_DD,max = t_ZD,max = t_OV + 0.75·k − s
                    t_DD,min = t_ZD,min = t_ZD + (0.5/0.6)·k_min
                    t_DZ = t_DZ_Data + 0.75·k + RC·ln(1/0.9)
                    t_IS = t_ISetup − 0.5·k_min               t_IH = t_IHold + 0.75·k

    with k = s = t_Slew_Clock's max, k_min = 2.0 ns and s_ref the SWI3S reference slew.
    """
    k = s_ = _K_MAX[size]
    if side == "Per":
        return dict(dd_max=_T_OV[size] - s_, dd_min=_T_ZD + REF_TRF_NS / 3.0,
                    dz=_T_DZ + _DEPART, t_is=_T_IS, t_ih=_T_IH)
    return dict(dd_max=_T_OV[size] + 0.75 * k - s_, dd_min=_T_ZD + 0.5 / 0.6 * _K_MIN,
                dz=_T_DZ + 0.75 * k + _DEPART, t_is=_T_IS - 0.5 * _K_MIN,
                t_ih=_T_IH + 0.75 * k)


@pytest.mark.parametrize("side,size,want", [
    ("Per", "small", dict(dd=(9.7, 22.9), dz=5.3, t_is=0.0, t_ih=4.0, trf=5.0)),
    ("Man", "small", dict(dd=(9.7, 26.7), dz=9.1, t_is=-1.0, t_ih=7.8, trf=5.0)),
    ("Per", "large", dict(dd=(9.7, 23.0), dz=5.3, t_is=0.0, t_ih=4.0, trf=6.0)),
    ("Man", "large", dict(dd=(9.7, 27.5), dz=9.8, t_is=-1.0, t_ih=8.5, trf=6.0)),
])
def test_the_soundwire_equivalents_are_the_derivation_rounded_outward(side, size, want):
    """Each Table 17 value re-measured between the SWI3S anchors (`_exact_equivalents`),
    then rounded to 0.1 ns in the direction that keeps it a bound: a max and a receiver's
    demand up, a min down."""
    e = sw13_equivalents("SW13_1V2", side, size)
    assert (e["t_DD_min_ns"], e["t_DD_max_ns"]) == pytest.approx(want["dd"])
    assert (e["t_ZD_min_ns"], e["t_ZD_max_ns"]) == pytest.approx(want["dd"])
    assert e["t_DZ_max_ns"] == pytest.approx(want["dz"])
    assert e["t_IS_max_ns"] == pytest.approx(want["t_is"])
    assert e["t_IH_max_ns"] == pytest.approx(want["t_ih"])
    assert e["t_keeper_ns"] == pytest.approx(_T_KEEP)
    assert (e["t_RF_min_ns"], e["t_RF_max_ns"]) == pytest.approx((2.0, want["trf"]))

    x = _exact_equivalents(side, size)
    for got, exact, up in ((e["t_DD_max_ns"], x["dd_max"], True),
                           (e["t_DD_min_ns"], x["dd_min"], False),
                           (e["t_DZ_max_ns"], x["dz"], True),
                           (e["t_IS_max_ns"], x["t_is"], True),
                           (e["t_IH_max_ns"], x["t_ih"], True)):
        if up:
            assert exact - 1e-9 <= got < exact + 0.1, (got, exact)
        else:
            assert exact - 0.1 < got <= exact + 1e-9, (got, exact)


def test_the_presets_carry_the_equivalents_on_their_own_side():
    """`preset` for a SoundWire side is `sw13_equivalents` on that side, with t_DZ's row
    starting at 0 (SoundWire sets no lower limit, §5.1.4.1) and the keeper response a
    Manager value."""
    for side in ("Man", "Per"):
        for size in ("small", "large"):
            e = sw13_equivalents("SW13_1V2", side, size)
            p = preset("SW13_1V2", side, size)
            assert (p.t_DD_min_ns, p.t_DD_max_ns) == (e["t_DD_min_ns"], e["t_DD_max_ns"])
            assert (p.t_ZD_min_ns, p.t_ZD_ns) == (e["t_ZD_min_ns"], e["t_ZD_max_ns"])
            assert (p.t_DZ_min_ns, p.t_DZ_max_ns) == (0.0, e["t_DZ_max_ns"])
            assert (p.t_IS_max_ns, p.t_IH_max_ns) == (e["t_IS_max_ns"], e["t_IH_max_ns"])
            assert p.t_keeper_ns == 2.5 and p.tRF_ns == e["t_RF_max_ns"]
    assert preset("SWI3S_PHY2", "Man").t_keeper_ns == 3.0


@pytest.mark.parametrize("man,per", [("SW13_1V2", "SWI3S_PHY2"), ("SWI3S_PHY2", "SW13_1V2"),
                                     ("SW13_1V2", "SW13_1V2")])
@pytest.mark.usefixtures("no_toggle_ceiling")
def test_a_soundwire_side_is_evaluated_as_a_swi3s_side_with_the_same_rows(man, per):
    """Three SoundWire-specific things remain, the data-input window, the anchor correction
    (`sw13_anchor_correction`) and a SoundWire Manager's keeper threshold: relabelling a
    SoundWire side as SWI3S, rows unchanged, drops the `,anchor` terms and moves no other term
    on any leg except a data-lane crossing, the keeper legs' `X_K` among them
    (`test_a_soundwire_data_input_takes_its_own_threshold_window` and
    `test_a_keeper_counts_its_response_from_the_managers_threshold` pin those). On a leg with
    no data-lane crossing the margin moves by the anchor terms alone. Compared at 3.5 ns on
    every lane, inside both specs' slew ranges, so the per-edge slew tolerance is the same. On
    a leg with no data-lane crossing the anchor terms are the whole difference, and its F_max,
    0.0 or unbounded, is the same on both; every leg with a finite non-zero F_max here carries
    a data-lane crossing, the keeper legs their X_K. Each leg is compared at the supply
    assignment the relabelled side reports for it: the data window can move a leg's worst
    assignment. The two specs' slew ranges set different toggle ceilings, so the F_max are
    compared without one."""
    from swi3s_studio.timing.calculator import _compute_at
    inp = dc_replace(_mixed(man, per, F_CLK_target_MHz=12.0), tRF_Man_CLK_ns=3.5,
                     tRF_Man_DATA_ns=3.5, tRF_Per_DATA_ns=3.5)
    relabelled = dc_replace(inp, Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2")
    b = compute(relabelled)

    def data_crossing(t):
        return "X_" in t.symbol and "t_RF,DATA" in t.symbol

    def names(terms):
        # A gathered data-lane term lists its positive contribution first, and the window can
        # move a data crossing to before the anchor, so its order is not part of the name.
        import re
        out = []
        for t in terms:
            head, sep, lane = t.symbol.partition(" · ")
            if head.startswith("("):
                head = "(" + ",".join(sorted(re.split(r" [−+] ", head[1:-1]))) + ")"
            out.append(head + sep + lane)
        return out

    for leg in b.breakdowns:
        a = _compute_at(inp, b.breakdowns[leg].supplies)
        ta = [t for t in a.breakdowns[leg].terms if ",anchor" not in t.symbol]
        tb = b.breakdowns[leg].terms
        anchors = sum(t.value_ns for t in a.breakdowns[leg].terms if ",anchor" in t.symbol)
        assert not any(",anchor" in t.symbol for t in tb), leg
        assert names(ta) == names(tb), leg
        for x, y in zip(ta, tb):
            if not data_crossing(x):
                assert x.value_ns == pytest.approx(y.value_ns, abs=1e-12), (leg, x.symbol)
        fa, fb = a.breakdowns[leg].F_max_MHz, b.breakdowns[leg].F_max_MHz
        if 0.0 < fa < math.inf:
            assert any(data_crossing(t) for t in ta), (leg, fa)
        if not any(data_crossing(t) for t in ta):
            assert a.breakdowns[leg].margin_ns - b.breakdowns[leg].margin_ns == pytest.approx(
                anchors, abs=1e-12), leg
            assert fa == fb, (leg, fa, fb)
    assert compute(inp).caveats != b.caveats


def test_a_soundwire_peripheral_takes_no_clock_side_offset():
    """A SWI3S Peripheral's clock reference is its own V_IH crossing, and V_IH,max is
    SoundWire's 65 % V_TP_Clock,max, so a SoundWire Peripheral's launch is its equivalent at
    its anchor alone: PM setup carries its clock detection once, gathered with the Manager's
    clock reference, and no re-anchor of the clock's 0 % → 65 % on top. At the slews the
    equivalent is derived at, with no per-edge slew tolerance, the anchor correction is zero
    and is no term."""
    inp = _mixed("SWI3S_PHY2", "SW13_1V2", F_CLK_target_MHz=12.288, tRF_tolerance_frac=0.0)
    terms = compute(inp).breakdowns["PM_setup"].terms
    launch = next(t for t in terms if t.symbol == "Per_t_DD")
    assert -launch.value_ns == pytest.approx(22.9, abs=1e-9)
    assert not any(t.symbol.startswith("Per_t_DD,anchor") for t in terms)
    assert sum(1 for t in terms if t.symbol.endswith("· Man_t_RF,CLK")) == 1


@pytest.mark.parametrize("size", ["small", "large"])
def test_the_anchor_correction_is_zero_at_the_equivalents_own_slews(size):
    """`sw13_anchor_correction` is 0 at the slews `sw13_equivalents` derives each row at for
    the system size, `sw13_equivalent_slews`: t_Slew_Clock's max for k and s on the max side;
    on the min side t_Slew_Clock's min for k, and for s the reference slew on a Peripheral
    and t_Slew_Clock's min on a Manager. A SoundWire launch on a setup leg at the max-side
    slews, with no per-edge slew tolerance, therefore carries no `,anchor` term."""
    for side in ("Per", "Man"):
        for corner in ("max", "min"):
            k0, s0 = sw13_equivalent_slews("SW13_1V2", side, corner=corner, system_size=size)
            want = ((_K_MAX[size], _K_MAX[size]) if corner == "max"
                    else (_K_MIN, REF_TRF_NS if side == "Per" else _K_MIN))
            assert (k0, s0) == pytest.approx(want), (side, corner)
            assert sw13_anchor_correction("SW13_1V2", side, corner=corner, t_rf_clk_ns=k0,
                                          t_rf_data_ns=s0, system_size=size) == \
                pytest.approx(0.0, abs=1e-12), (side, corner)
    for man, per, leg, sym in (("SWI3S_PHY2", "SW13_1V2", "PM_setup", "Per_t_DD,anchor"),
                               ("SW13_1V2", "SWI3S_PHY2", "MP_setup", "Man_t_DD,anchor")):
        inp = _mixed(man, per, size, tRF_tolerance_frac=0.0)
        if is_soundwire(per):
            inp = dc_replace(inp, tRF_Per_DATA_ns=_K_MAX[size])
        else:
            inp = dc_replace(inp, tRF_Man_CLK_ns=_K_MAX[size], tRF_Man_DATA_ns=_K_MAX[size])
        terms = compute(inp).breakdowns[leg].terms
        assert not any(t.symbol.startswith(sym) for t in terms), (leg, [t.symbol for t in terms])


def test_a_soundwire_anchor_takes_its_sides_ramp_shape():
    """A SoundWire launch's anchor is placed with the side's ramp shape: on an exponential
    ramp the 20 % point is 0.161·s after low impedance (1/3·s linear) and the clock's 65 % and
    50 % crossings 0.596·k and 0.339·k after its 20 % point (0.75·k, 0.5·k linear). The rows
    are the linear equivalents, so the correction is the exponential anchor less the linear
    one at the equivalents' slews, and the calculator applies it per side."""
    from swi3s_studio.timing.calculator import sw_anchor_ns
    z = -math.log(0.8) / math.log(4.0)
    p = (-math.log(0.35) + math.log(0.8)) / math.log(4.0)
    q = (-math.log(0.5) + math.log(0.8)) / math.log(4.0)
    k, s = 5.0, 4.0
    for side, corner in (("Per", "max"), ("Per", "min"), ("Man", "max"), ("Man", "min")):
        k0, s0 = sw13_equivalent_slews("SW13_1V2", side, corner=corner)
        lin = {"max": (0.75 if side == "Man" else 0.0, -1.0),
               "min": (0.5 if side == "Man" else 0.0, 1.0 / 3.0)}[corner]
        exp = {"max": (p if side == "Man" else 0.0, -1.0),
               "min": (q if side == "Man" else 0.0, z)}[corner]
        want = exp[0] * k + exp[1] * s - lin[0] * k0 - lin[1] * s0
        got = sw13_anchor_correction("SW13_1V2", side, corner=corner, t_rf_clk_ns=k,
                                     t_rf_data_ns=s, shape="exp")
        assert got == pytest.approx(want, abs=1e-9), (side, corner)
    lin_inp = _mixed("SW13_1V2", "SW13_1V2")
    exp_inp = dc_replace(lin_inp, Man_shape="exp", Per_shape="exp")
    assert sw_anchor_ns(exp_inp, "Man", "min") < sw_anchor_ns(lin_inp, "Man", "min")
    assert sw_anchor_ns(exp_inp, "Per", "min") < sw_anchor_ns(lin_inp, "Per", "min")
    assert sw_anchor_ns(exp_inp, "Per", "max") == sw_anchor_ns(lin_inp, "Per", "max")


def test_a_soundwire_anchor_follows_the_v_oh_and_v_ol_the_calculator_reads():
    """A SoundWire launch's anchor is its ramp's V_OL point, the anchor every SWI3S launch and
    `anchor_frac` use, at the V_OH / V_OL in force. A t_OV ends at 80 % of the data ramp, so
    on a linear ramp from V_OL = 0.10 to V_OH = 0.90 the anchor is (0.80 − 0.10)/0.80 of the
    data slew before it, against one slew at the default 0.20 / 0.80; the rows hold the
    equivalent at the default. An exponential ramp's t_RF is its 20-80 % time whatever V_OL
    is, so from V_OL = 0.10 its 80 % point is (ln 0.9 − ln 0.2)/ln 4 of a slew after the
    anchor, not one slew, although V_OH is 0.80."""
    from swi3s_studio.timing.calculator import _launch_anchor_ns, anchor_frac

    p = preset("SW13_1V2", "Per")
    _, s0 = sw13_equivalent_slews("SW13_1V2", "Per", corner="max")
    s = 4.0
    for shape, v_oh, v_ol in (("linear", 0.80, 0.20), ("linear", 0.90, 0.10),
                              ("linear", 0.85, 0.25), ("linear", 0.80, 0.10),
                              ("exp", 0.80, 0.20), ("exp", 0.80, 0.10), ("exp", 0.90, 0.30)):
        inp = CalcInputs(Per_spec="SW13_1V2", Per_t_DD_max_ns=p.t_DD_max_ns,
                         tRF_Per_DATA_ns=s, tRF_tolerance_frac=0.0, Per_shape=shape,
                         V_OH_min_frac=v_oh, V_OL_max_frac=v_ol)
        if shape == "linear":
            to_80 = (0.80 - v_ol) / (v_oh - v_ol)
            assert anchor_frac(inp, "Per") == pytest.approx(v_ol / (v_oh - v_ol), abs=1e-12)
        else:
            to_80 = (math.log(1.0 - v_ol) - math.log(0.2)) / math.log(4.0)
        # The row less the equivalent's own placement is t_OV, wherever the anchor is.
        assert _launch_anchor_ns(inp, "Per", "DD", "max") + to_80 * s == pytest.approx(
            p.t_DD_max_ns + s0, abs=1e-12), (shape, v_oh, v_ol)


def test_an_eighty_percent_anchored_edge_is_evaluated_on_its_fast_factor_too():
    """A SoundWire launch's t_OV ends at 80 % of its ramp, and an EndDriveEarly keeper leg
    counts the keeper's response from its threshold crossing, before V_OH where the threshold
    is below it. On a leg where that edge's own anchor and a crossing of it both enter, the slew
    enters as (1 − c)·f·t_RF, so with the crossing before the 80 % point the fast edge leaves
    the less margin. Each such leg is reported at the worse of the two factors, and its
    crossing symbol names the fast edge. Checked against both factors at every supply
    assignment, against a SoundWire and a SWI3S Manager's keeper."""
    from swi3s_studio.timing.calculator import _compute_at
    cases = [
        (CalcInputs(Per_spec="SW13_1V2", F_CLK_target_MHz=12.0, tRF_Per_DATA_ns=5.0,
                    tRF_tolerance_frac=0.05, V_noise_pp_frac=0.0, V_SEOS_tol_frac=0.05),
         ("PM_setup", "PP_setup"), "Per DATA"),
        (CalcInputs(Man_spec="SW13_1V2", F_CLK_target_MHz=12.0, tRF_Man_CLK_ns=5.0,
                    tRF_Man_DATA_ns=5.0, tRF_tolerance_frac=0.05, V_noise_pp_frac=0.0,
                    V_SEOS_tol_frac=0.05), ("keeper_Man",), "Man DATA"),
        (CalcInputs(Man_spec="SW13_1V2", Per_spec="SWI3S_PHY2_EDE", Per_ede=True,
                    handover_UIs=0.0, F_CLK_target_MHz=6.0, tRF_Per_DATA_ns=8.3,
                    V_noise_pp_frac=0.0), ("keeper_Per",), "Per DATA"),
        (CalcInputs(Man_spec="SWI3S_PHY2_EDE", Per_spec="SWI3S_PHY2_EDE", Per_ede=True,
                    Man_ede=True, handover_UIs=0.0, F_CLK_target_MHz=12.0,
                    tRF_Per_DATA_ns=8.3, V_noise_pp_frac=0.0), ("keeper_Per",), "Per DATA"),
    ]
    for inp, legs, lane in cases:
        assert lane in inp.fast_edge_lanes()
        r = compute(inp).breakdowns
        slow = [_compute_at(inp, s) for s in inp.supply_corners()]
        fast = [_compute_at(dc_replace(inp, fast_data_edge=frozenset(inp.fast_edge_lanes())),
                            s) for s in inp.supply_corners()]
        for leg in legs:
            worst_slow = min(x.breakdowns[leg].margin_ns for x in slow)
            worst_fast = min(x.breakdowns[leg].margin_ns for x in fast)
            assert worst_fast < worst_slow, (leg, worst_fast, worst_slow)
            assert r[leg].margin_ns == worst_fast, leg
            assert lane in r[leg].fast_data_edge, leg
            assert any(",fast,late · " in t.symbol for t in r[leg].terms), leg
    # No leg reads the slow-edge role of two DATA lanes, so evaluating the lanes slow
    # together and fast together covers every leg (`compute`).
    both = CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2", Man_ede=True, Per_ede=True)
    for kw in ({}, {"Man_ede": False, "Per_ede": False}):
        inp = dc_replace(both, **kw)
        assert inp.fast_edge_lanes() == ("Man DATA", "Per DATA")
        for sup in inp.supply_corners():
            at = {e: _compute_at(dc_replace(inp, fast_data_edge=frozenset(e)), sup).breakdowns
                  for e in ((), ("Man DATA",), ("Per DATA",), ("Man DATA", "Per DATA"))}
            for leg in at[()]:
                m = {e: r[leg].margin_ns for e, r in at.items()}
                moved = [e for e in (("Man DATA",), ("Per DATA",)) if m[e] != m[()]]
                assert len(moved) <= 1, (leg, m)
                if moved:
                    assert m[("Man DATA", "Per DATA")] == pytest.approx(m[moved[0]]), leg
    # A SWI3S-only bus has no such edge and is evaluated once per supply assignment.
    assert CalcInputs().fast_edge_lanes() == ()
    assert CalcInputs(Per_spec="SW13_1V2", tRF_tolerance_frac=0.0).fast_edge_lanes() == ()


def test_soundwire_hold_bound_is_t_ZD_not_t_OH():
    """The hold-side parameter is t_ZD_Data,Min, not t_OH_Data.

    Table 15 measures t_OH_Data "from data first becoming valid ... to Data output becoming
    high impedance": a data-referenced drive-before-handover permission ({1209}), after
    which the BUS-KEEPER holds the level to the sampling edge (§5.1.11, Fig. 16). It is not a
    hold guarantee. t_ZD_Data,Min — "Time to enable Data output signal after positive or
    negative edge on Clock input signal" — is the earliest a driver may drive the NEXT
    value, and every SoundWire bit is a turn-on (Table 22).

    Its equivalent, rounded down, is the min of both t_DD and t_ZD on either side. At the
    operating slews the calculator puts the launch's ramp start no later than the SoundWire
    guarantee: 8.1 ns after the 50 % clock crossing (at its earliest, the fastest clock).
    """
    from swi3s_studio.timing.calculator import _launch_anchor_ns, anchor_frac

    for side in ("Per", "Man"):
        p = preset("SW13_1V2", side)
        assert p.t_DD_min_ns == p.t_ZD_min_ns == pytest.approx(9.7), side
        assert p.t_DD_min_ns > 6.7, "not t_OH_Data"
    for s_ in (2.0, 5.0):
        per = dc_replace(_mixed("SWI3S_PHY2", "SW13_1V2"), tRF_Per_DATA_ns=s_)
        start = (_launch_anchor_ns(per, "Per", "DD", "min")
                 - anchor_frac(per, "Per") * _edge(s_, 2.0, 5.0)[1] * s_)
        assert start <= 8.1 + 1e-9, (s_, start)
        man = dc_replace(_mixed("SW13_1V2", "SWI3S_PHY2"), tRF_Man_CLK_ns=2.0,
                         tRF_Man_DATA_ns=s_)
        # Measured from the Manager's clock reference, the 20 % point of its (late) clock
        # edge, which its 50 % crossing follows by the ramp from 20 % to 50 % of that edge.
        k_edge = 2.0 * _edge(2.0, 2.0, 5.0)[0]
        start = (_launch_anchor_ns(man, "Man", "DD", "min")
                 - anchor_frac(man, "Man") * _edge(s_, 2.0, 5.0)[1] * s_)
        assert start <= (0.5 / 0.6 - anchor_frac(man, "Man")) * k_edge + 8.1 + 1e-9, \
            (s_, start)


# ---------------------------------------------------------------------------
# 4. Advisories
# ---------------------------------------------------------------------------

def test_soundwire_caps_the_clock_below_the_swi3s_mandatory_rate():
    """No SoundWire configuration may legally clock at SWI3S PHY2's 13.2 MHz. The ceiling is
    the lower of the two sides' maxima, so a SWI3S PHY1 side, at 6.6 MHz, sets it against
    SoundWire."""
    assert fclk_ceiling_MHz("SWI3S_PHY2", "SWI3S_PHY2") == 13.2
    for size, expect in (("small", 12.3), ("large", 11.0)):
        assert fclk_ceiling_MHz("SWI3S_PHY2", "SW13_1V2", size) == expect
        assert fclk_ceiling_MHz("SW13_1V2", "SWI3S_PHY2", size) == expect
        assert fclk_ceiling_MHz("SWI3S_PHY1", "SW13_1V2", size) == 6.6
        assert fclk_ceiling_MHz("SW13_1V2", "SWI3S_PHY1", size) == 6.6

    r = compute(_mixed("SWI3S_PHY2", "SW13_1V2"))     # 13.2 MHz target
    assert not r.F_CLK_legal, "13.2 MHz is above every SoundWire ceiling"
    assert r.F_CLK_ceiling_MHz == 12.3
    ok = compute(_mixed("SWI3S_PHY2", "SW13_1V2", F_CLK_target_MHz=12.0))
    assert ok.F_CLK_legal


def test_a_soundwire_bus_states_which_threshold_window_its_data_inputs_use():
    """A SoundWire side's caveats name the data-input window and the t_Slew_Data stand-in."""
    r = compute(_mixed("SWI3S_PHY2", "SW13_1V2"))
    assert r.caveats == (THRESHOLD_NOTE, TRF_NOTE, HANDOVER_CROSS_SPEC_NOTE)
    assert "35-65 %" in THRESHOLD_NOTE


def test_a_soundwire_data_input_takes_its_own_threshold_window():
    """Each receiver's DATA input reads its own spec's window; every CLOCK input, and a
    SWI3S data input, reads the V_IH / V_IL rows.

    A SoundWire Data input guarantees a high above 65 % and a low below 35 %, with no
    hysteresis, so each edge's threshold lies anywhere in 35-65 %: the same late ends as
    SWI3S's rows (65 % rising, 35 % falling), early ends 10 % wider (35 % rising, 65 %
    falling). So a SoundWire receiver's late data crossings match a SWI3S one's and its early
    ones come sooner: its setup legs are unchanged and its hold legs pay.
    """
    from swi3s_studio.timing.delta_tpd import compute_delta_tpd_envelope
    from swi3s_studio.timing.spec_source import data_input_window

    sw = data_input_window("SW13_1V2")
    assert sw == (0.35, 0.65, 0.35, 0.65)
    assert all(data_input_window(spec) is None for spec in SPEC_SOURCES
               if not is_soundwire(spec))

    def window_env(window, shape):
        ih_min, ih_max, il_min, il_max = window
        return compute_delta_tpd_envelope(ramp_shape=shape, V_IH_min_frac=ih_min,
                                          V_IH_max_frac=ih_max, V_IL_min_frac=il_min,
                                          V_IL_max_frac=il_max, from_anchor=True)

    for man_shape, per_shape in (("linear", "linear"), ("linear", "exp"), ("exp", "linear")):
        base = CalcInputs(Man_shape=man_shape, Per_shape=per_shape)
        swi3s = base.envelope()
        bus_clk = window_env((0.45, 0.65, 0.35, 0.55), man_shape)
        # A SoundWire PERIPHERAL: MP and P->P data crossings at its data input move, the
        # clock crossings do not.
        env = dc_replace(base, Per_spec="SW13_1V2").envelope()
        mp = window_env(sw, man_shape)
        pp = window_env(sw, per_shape)
        assert env.mp_late_rise_data == pytest.approx(mp.mp_late_rise)
        assert env.mp_early_fall_data == pytest.approx(mp.mp_early_fall)
        assert env.mp_late_rise_per == pytest.approx(pp.mp_late_rise)
        assert env.mp_early_fall_per == pytest.approx(pp.mp_early_fall)
        assert env.mp_late_rise == pytest.approx(bus_clk.mp_late_rise)
        assert env.mp_early_fall == pytest.approx(bus_clk.mp_early_fall)
        assert env.sigma_pm_hold_rf_data == pytest.approx(swi3s.sigma_pm_hold_rf_data)
        # The late end matches, the early end comes sooner.
        assert env.mp_late_rise_data == pytest.approx(swi3s.mp_late_rise_data)
        assert env.mp_early_fall_data < swi3s.mp_early_fall_data
        # A SoundWire MANAGER: the PM data crossings at its data input move.
        env = dc_replace(base, Man_spec="SW13_1V2").envelope()
        pm = window_env(sw, per_shape)
        assert env.sigma_pm_hold_rf_data == pytest.approx(pm.sigma_pm_hold_rf_data)
        assert env.sigma_pm_setup_data_dgnd_pos == pytest.approx(pm.sigma_pm_setup_data_dgnd_pos)
        assert env.sigma_pm_hold_rf_data < swi3s.sigma_pm_hold_rf_data
        assert env.sigma_pm_setup_data_dgnd_pos == pytest.approx(
            swi3s.sigma_pm_setup_data_dgnd_pos)
        assert env.mp_early_fall_data == pytest.approx(swi3s.mp_early_fall_data)

    # On the margins: a SoundWire Peripheral receiving costs MP hold, not MP setup's late
    # data crossing; a SoundWire Manager receiving costs PM hold and leaves PM setup alone.
    # At 3.5 ns on every lane, inside both specs' slew ranges.
    lanes = dict(tRF_Man_CLK_ns=3.5, tRF_Man_DATA_ns=3.5, tRF_Per_DATA_ns=3.5)
    plain = compute(CalcInputs(**lanes)).breakdowns
    per_rx = compute(CalcInputs(Per_spec="SW13_1V2", **lanes)).breakdowns
    man_rx = compute(CalcInputs(Man_spec="SW13_1V2", **lanes)).breakdowns
    assert per_rx["MP_hold"].margin_ns < plain["MP_hold"].margin_ns
    assert per_rx["PP_hold"].margin_ns < plain["PP_hold"].margin_ns
    assert man_rx["PM_hold"].margin_ns < plain["PM_hold"].margin_ns
    assert man_rx["PM_setup"].margin_ns == pytest.approx(plain["PM_setup"].margin_ns)


def test_every_spec_source_builds_and_converts():
    """No preset is missing a field, and no combination crashes."""
    for man in SPEC_SOURCES:
        for per in SPEC_SOURCES:
            r = compute(_mixed(man, per))
            assert math.isfinite(r.breakdowns["MP_setup"].margin_ns)
            assert (THRESHOLD_NOTE in r.caveats) == (is_soundwire(man) or is_soundwire(per))


def test_converted_terms_carry_provenance():
    """A SoundWire launch's anchor correction explains itself where it is used, not only in
    the design document; a SWI3S launch has none."""
    r = compute(dc_replace(_mixed("SWI3S_PHY2", "SW13_1V2"), tRF_Per_DATA_ns=8.3))
    term = next(t for t in r.breakdowns["PM_setup"].terms
                if t.symbol.startswith("Per_t_DD,anchor,"))
    assert "t_OV ends at 80 % of the data ramp" in term.note, term.note
    assert "the equivalent is that at the slews it is derived at" in term.note, term.note
    for inp in (CalcInputs(), _mixed("SWI3S_PHY2", "SWI3S_PHY2")):
        assert not any(",anchor" in t.symbol
                       for b in compute(inp).breakdowns.values() for t in b.terms)


def test_the_keeper_term_names_the_managers_source():
    """The keeper is the Manager's, so its response time and its note come from the Manager's
    spec: SWI3S's Man_tKeeper_Response, or a SoundWire Manager's t_Keeper_Delay, each with
    what it is measured between."""
    for man, value, source, between in (
            ("SWI3S_PHY2", 3.0, '"PHY1 & PHY2 Bus Keeper Behavior" table',
             "output current changing sign"),
            ("SW13_1V2", 2.5, "SoundWire 1.3 t_Keeper_Delay (Tables 15 and 17)",
             "keeper output High / Low")):
        r = compute(_mixed(man, "SWI3S_PHY2", t_keeper_ns=preset(man, "Man").t_keeper_ns))
        for leg in ("keeper_Man", "keeper_Per"):
            (t,) = [t for t in r.breakdowns[leg].terms if t.symbol == "Man_t_Keeper_Response"]
            assert -t.value_ns == pytest.approx(value), (man, leg)
            assert source in t.note and between in t.note and f"{value:.2f} ns" in t.note, t.note


@pytest.mark.parametrize("man", ["SWI3S_PHY2", "SW13_1V2"])
def test_a_keeper_counts_its_response_from_the_managers_threshold(man):
    """The releasing data must stay driven for the keeper's response past its crossing of the
    keeper's threshold at the Manager: a SWI3S Manager's V_IH,max (rising) or V_IL,min
    (falling) at its supply, a SoundWire Manager's V_TH_Keeper,max (65 % of the larger of its
    Vdd and VddNom) or V_TL_Keeper,min (35 % of the smaller). The keeper leg charges the anchor
    to that crossing on a late edge, the later of the two edges, against the releaser's own
    rail, and for a releasing Peripheral the ground shift α. The note names the source, the
    threshold and, on keeper_Per, the ground shift."""
    from swi3s_studio.timing.calculator import _compute_at, edge_factor
    inp = _mixed(man, "SWI3S_PHY2", t_keeper_ns=preset(man, "Man").t_keeper_ns)
    swing = inp.V_OH_min_frac - inp.V_OL_max_frac
    source = ("Figures 18 and 19" if man == "SW13_1V2"
              else '"PHY1 & PHY2 Bus Keeper Behavior" table')
    for supplies in ((0.95, 0.95, 1.0), (1.05, 0.95, 1.0), (0.95, 1.05, 1.0)):
        m, p = supplies[:2]
        th, tl = _keeper_thresholds(inp, m)
        b = _compute_at(inp, supplies).breakdowns
        for leg, dev, lane, tx, alpha in (("keeper_Man", "Man", "Man DATA", m, 0.0),
                                          ("keeper_Per", "Per", "Per DATA", p,
                                           inp.V_noise_pp_frac)):
            rise = (th + alpha) / (swing * tx)
            fall = (tx - (tl - alpha)) / (swing * tx)
            want = (max(rise, fall) - 1.0 / 3.0) * edge_factor(inp, lane, "late")
            # The later crossing's edge, rising on a tie (`_keeper_case`).
            edge = "rise" if rise >= fall - 1e-12 else "fall"
            (t,) = [t for t in b[leg].terms if t.symbol == f"X_K,{edge},late · {dev}_t_RF,DATA"]
            assert t.factors[0] == pytest.approx(want, abs=1e-12), (leg, supplies)
            bound = (("V_TH_Keeper,max" if man == "SW13_1V2" else "V_IH,max") if edge == "rise"
                     else ("V_TL_Keeper,min" if man == "SW13_1V2" else "V_IL,min"))
            assert source in t.note and f"crossing {bound} at the Manager" in t.note, t.note
            assert ("by the ground shift α" in t.note) is (dev == "Per"), t.note
            assert not any(s.symbol.startswith(f"{dev}_t_RF,DATA") for s in b[leg].terms)


@pytest.mark.parametrize("man", ["SWI3S_PHY2", "SW13_1V2"])
@pytest.mark.parametrize("per", ["SWI3S_PHY2", "SW13_1V2"])
@pytest.mark.parametrize("shape", ["linear", "exp"])
@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.20), (0.85, 0.25), (0.80, 0.15)])
def test_a_keepers_crossing_is_its_closed_form_on_either_shape(shape, v_oh, v_ol, per, man):
    """A conventional release: the keeper leg's X_K coefficient is the crossing of the
    keeper's threshold at the Manager (a SWI3S Manager's V_IH,max / V_IL,min, a SoundWire
    Manager's V_TH_Keeper,max / V_TL_Keeper,min) counted from its own polarity's anchor, on a
    late edge, on the edge whose crossing is the later after the clock reference. A SoundWire
    launch's anchor is t_OV − r·s after it (`sw13_ramp_fracs`), r from the anchor to 80 % of
    the transition, so a SoundWire side's edge is the one whose crossing less r is the larger,
    and a SWI3S side's anchor is its row on either edge, so its edge is the later crossing.
    From
    the ramp's start, a crossing of level v on a ramp to rail V is v/(swing·V) rising and
    (V − v)/(swing·V) falling on a linear ramp, −log_4(1 − v/V) and −log_4(v/V) on an
    exponential one; the anchor is V_OL/swing and (1 − V_OH)/swing linear, −log_4(1 − V_OL)
    and −log_4(V_OH) exponential, and 80 % of the transition is the level 0.80 rising and
    0.20 falling, on a rail of 1."""
    from swi3s_studio.timing.calculator import _compute_at, edge_factor
    sw = dc_replace(_mixed(man, per, t_keeper_ns=2.5), Man_shape=shape,
                    Per_shape=shape, V_OH_min_frac=v_oh, V_OL_max_frac=v_ol)
    swing = v_oh - v_ol

    def cross(level, rail, rising):
        if shape == "linear":
            return (level if rising else rail - level) / (swing * rail)
        return -math.log(1.0 - level / rail if rising else level / rail, 4.0)

    if shape == "linear":
        anchor = {True: v_ol / swing, False: (1.0 - v_oh) / swing}
    else:
        anchor = {True: -math.log(1.0 - v_ol, 4.0), False: -math.log(v_oh, 4.0)}
    to_80 = {r: cross(0.80 if r else 0.20, 1.0, r) - anchor[r] for r in (True, False)}
    for supplies in ((0.95, 0.95, 1.0), (1.05, 0.95, 1.0), (0.95, 1.05, 1.0)):
        m, p = supplies[:2]
        b = _compute_at(sw, supplies).breakdowns
        for leg, dev, lane, tx, alpha in (("keeper_Man", "Man", "Man DATA", m, 0.0),
                                          ("keeper_Per", "Per", "Per DATA", p,
                                           sw.V_noise_pp_frac)):
            th, tl = _keeper_thresholds(sw, m)
            rise = cross(th + alpha, tx, True) - anchor[True]
            fall = cross(tl - alpha, tx, False) - anchor[False]
            sw_launch = is_soundwire(sw.Man_spec if dev == "Man" else sw.Per_spec)
            later = (rise - sw_launch * to_80[True]
                     >= fall - sw_launch * to_80[False] - 1e-12)
            want = (rise if later else fall) * edge_factor(sw, lane, "late")
            edge = "rise" if later else "fall"
            (t,) = [t for t in b[leg].terms if t.symbol == f"X_K,{edge},late · {dev}_t_RF,DATA"]
            assert t.factors[0] == pytest.approx(want, abs=1e-12), (leg, supplies)


@pytest.mark.parametrize("per", ["SWI3S_PHY2", "SW13_1V2"])
def test_keeper_man_opens_on_the_clock_edge_that_places_its_launch_later(per, monkeypatch):
    """At V_OH + V_OL ≠ 1 a SoundWire Manager's launch anchor depends on the polarity of the
    clock edge it launches from (`sw_anchor_ns`), and keeper_Man opens its last driven UI on
    the edge that places the launch the later (`keeper_open_rising`). At V_OL 0.30 the
    falling clock edge's anchor is 1.0 ns later than the rising one's: the leg opens on it,
    its anchor term names a falling clock edge (`,fr`), and it leaves the keeper 1.0 ns less
    than the rising edge would."""
    from swi3s_studio.timing import calculator

    inp = dc_replace(_mixed("SW13_1V2", per), V_OL_max_frac=0.30)
    rise, fall = (calculator.sw_anchor_ns(inp, "Man", "max", clk_rising=c)
                  for c in (True, False))
    assert fall - rise == pytest.approx(1.0, abs=1e-9), (rise, fall)
    assert calculator.keeper_open_rising(inp) is False
    b = compute(inp).breakdowns["keeper_Man"]
    (anchor,) = [t for t in b.terms if ",anchor" in t.symbol]
    assert ",fr" in anchor.symbol, anchor.symbol
    assert b.margin_ns == pytest.approx(8.783, abs=5e-4)
    monkeypatch.setattr(calculator, "keeper_open_rising", lambda i: True)
    rising = compute(inp).breakdowns["keeper_Man"]
    assert rising.margin_ns == pytest.approx(b.margin_ns + 1.0, abs=1e-9)


@pytest.mark.parametrize("man", ["SWI3S_PHY2_EDE", "SW13_1V2"])
@pytest.mark.parametrize("shape", ["linear", "exp"])
def test_a_keeper_counts_an_ede_release_from_its_threshold_crossing(shape, man):
    """An EndDriveEarly release holds its level for the EDE Delay past V_OH / V_OL. The
    keeper's response counts from the data's crossing of its threshold at the Manager
    (`_keeper_case`), so the EDE keeper leg charges the anchor to that crossing and credits
    the ramp to V_OH / V_OL, in time order, ahead of the EDE Delay; at every supply assignment
    its margin is the EDE Delay less the crossing's offset past V_OH and the keeper's
    response, and the EDE Delay's note names the threshold: a SWI3S Manager's V_IH,max /
    V_IL,min, a SoundWire Manager's V_TH_Keeper,max / V_TL_Keeper,min."""
    from swi3s_studio.timing.calculator import _compute_at, _keeper_case, edge_factor
    keep = preset(man, "Man").t_keeper_ns
    inp = dc_replace(_mixed(man, "SWI3S_PHY2_EDE", t_keeper_ns=keep, handover_UIs=0.0),
                     Per_ede=True, Per_shape=shape)
    names = ("V_TH_Keeper,max / V_TL_Keeper,min" if man == "SW13_1V2"
             else "V_IH,max / V_IL,min")
    ramp = edge_factor(inp, "Per DATA", "late") * inp.tRF_Per_DATA_ns
    worst = math.inf
    for supplies in inp.supply_corners():
        b = _compute_at(inp, supplies).breakdowns["keeper_Per"]
        rising, coef, _ = _keeper_case(inp, "Per DATA", supplies, ede=True)
        edge = "rise" if rising else "fall"
        assert [t.symbol for t in b.terms] == [f"X_K,{edge},late · Per_t_RF,DATA",
                                               f"Per_t_RF,DATA,{edge}", "Per_t_EDE",
                                               "Man_t_Keeper_Response"]
        want = inp.Per_t_EDE_ns - (coef * inp.tRF_Per_DATA_ns - ramp) - keep
        assert b.margin_ns == pytest.approx(want, abs=1e-12), supplies
        assert names in b.terms[2].note, b.terms[2].note
        worst = min(worst, want)
    r = compute(inp).breakdowns["keeper_Per"]
    assert r.margin_ns == pytest.approx(worst, abs=1e-12)
    # The threshold is crossed after V_OH on the worst assignment: a charge.
    assert r.margin_ns < inp.Per_t_EDE_ns - keep


@pytest.mark.parametrize("spec", SPEC_SOURCES)
def test_every_unratified_source_carries_the_proposal_note(spec):
    """A result computed from unratified values says so, whichever proposal it is.

    The EDE source is the proposed revision plus a clocked Manager release, so it is as
    unratified as `SWI3S_PHY2_PROP`, though the selector offers it and hides the other.
    """
    from swi3s_studio.timing.spec_source import (
        PROPOSAL_NOTE,
        is_ede,
        is_proposed,
        is_unratified,
    )
    unratified = spec in ("SWI3S_PHY2_PROP", "SWI3S_PHY2_EDE")
    assert is_unratified(spec) == unratified
    assert is_proposed(spec) == (spec == "SWI3S_PHY2_PROP")   # the selector's predicate
    assert is_unratified(spec) == (is_proposed(spec) or is_ede(spec))
    for side in ({"Man_spec": spec}, {"Per_spec": spec}):
        caveats = compute(CalcInputs(**side)).caveats
        assert (PROPOSAL_NOTE in caveats) == unratified, (spec, side, caveats)
        if unratified:
            assert caveats[0] == PROPOSAL_NOTE, caveats


# ---------------------------------------------------------------------------
# 5. UI
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", ["small", "large"])
def test_an_anchor_correction_note_reproduces_its_value(size):
    """Every SoundWire anchor-correction note states the slews the equivalent is derived at,
    the edge slews the correction is evaluated at and the move it applies; the note's own
    formula at those slews gives the move, and the term's value is that move with the
    launch's sign. Run at the slow end of every lane, where the corrections are not zero, for
    both system sizes, and checked against `sw13_anchor_correction`."""
    import re

    pat = re.compile(r"the equivalent is that at the slews it is derived at "
                     r"\((?:k = ([0-9.]+), )?s = ([0-9.]+) ns\), so at "
                     r"(?:k = ([0-9.]+), )?s = ([0-9.]+) ns \(.*\) the anchor moves by "
                     r"([+-][0-9]+\.[0-9]+) ns")
    formula = {("Per", "max"): lambda k0, s0, k, s_: s0 - s_,
               ("Per", "min"): lambda k0, s0, k, s_: (s_ - s0) / 3.0,
               ("Man", "max"): lambda k0, s0, k, s_: 0.75 * (k - k0) + (s0 - s_),
               ("Man", "min"): lambda k0, s0, k, s_: 0.5 * (k - k0) + (s_ - s0) / 3.0}
    slow = dict(tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=8.3, tRF_Per_DATA_ns=8.3)
    seen = 0
    for man, per in (("SW13_1V2", "SWI3S_PHY2"), ("SWI3S_PHY2", "SW13_1V2"),
                     ("SW13_1V2", "SW13_1V2")):
        inp = dc_replace(_mixed(man, per, size), **slow)
        for name, b in compute(inp).breakdowns.items():
            for t in b.terms:
                if ",anchor" not in t.symbol:
                    continue
                m = pat.search(t.note)
                assert m, (name, t.symbol, t.note)
                k0, s0, k, s_, move = (float(g) if g else 0.0 for g in m.groups())
                side = t.symbol[:3]
                # The note names the SoundWire parameter the row stands for: t_OV the max side.
                corner = "max" if t.note.startswith("SoundWire: t_OV") else "min"
                assert formula[(side, corner)](k0, s0, k, s_) == pytest.approx(move, abs=1e-2), \
                    (name, t.symbol, t.note)
                assert abs(t.value_ns) == pytest.approx(abs(move), abs=5e-3), \
                    (name, t.symbol, t.note)
                seen += 1
        # The Peripheral's max-side correction at the slow end: t_OV − s against t_OV − k_max.
        if is_soundwire(per):
            assert sw13_anchor_correction(per, "Per", corner="max", t_rf_clk_ns=8.3,
                                          t_rf_data_ns=8.3, system_size=size) == \
                pytest.approx(_K_MAX[size] - 8.3)
    assert seen


def _view():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    return TimingView()


def test_a_spec_change_reseeds_the_page_from_the_selected_pair():
    """Switching the Peripheral to SoundWire reseeds the page from the selected pair
    (`TimingView._on_spec_changed`): the Peripheral rows take its SWI3S equivalents, and the
    Manager rows and F_CLK, edited first, return to the pair's seeds — the Manager's SWI3S
    values and the pair's default rate. One row of each Manager group is edited: t_DD, t_ZD,
    t_IH and the Manager CLK slew."""
    tv = _view()
    man_rows = ("Man_t_DD", "Man_t_ZD", "Man_t_IH", "t_RF Man CLK")
    seeds = {(n, end): spins[("spec", n)].value() for n in man_rows
             for end, spins in (("min", tv._min), ("max", tv._max))}
    for n in man_rows:
        tv._max[("spec", n)].setValue(seeds[n, "max"] + 1.5)
        assert tv._max[("spec", n)].value() != pytest.approx(seeds[n, "max"]), n
    tv._fclk.setValue(9.0)
    # The title is fixed; the SUBTITLE names the selected bus.
    assert tv._title.text() == "SWI3S & SoundWire Timing Calculator"
    assert tv._subtitle.text() == "SWI3S PHY2 Manager and Peripheral"
    # The size envelope is SoundWire-only, and HIDDEN rather than greyed when
    # neither side is SoundWire.
    assert not tv._size_row.isVisibleTo(tv), "size envelope is SoundWire-only"

    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V2"))
    assert tv._size_row.isVisibleTo(tv)
    # Peripheral now carries the small-system equivalents.
    e = sw13_equivalents("SW13_1V2", "Per")
    for side in ("spec", "proposed"):
        assert tv._min[(side, "Per_t_DD")].value() == pytest.approx(e["t_DD_min_ns"])  # 9.7
        assert tv._max[(side, "Per_t_DD")].value() == pytest.approx(e["t_DD_max_ns"])  # 22.9
        assert tv._min[(side, "Per_t_ZD")].value() == pytest.approx(e["t_ZD_min_ns"])
        assert tv._max[(side, "Per_t_ZD")].value() == pytest.approx(e["t_ZD_max_ns"])
        assert (tv._min[(side, "Per_t_DZ")].value(),
                tv._max[(side, "Per_t_DZ")].value()) == pytest.approx((0.0, 5.3))
        assert tv._max[(side, "Per_t_IH")].value() == pytest.approx(4.0)
        assert tv._max[(side, "t_RF Per DATA")].value() == pytest.approx(5.0)
    # The Manager's edits and the rate are reseeded with the rest of the page.
    assert tv._min[("spec", "Man_t_DD")].value() == pytest.approx(7.0)
    assert tv._max[("spec", "Man_t_DD")].value() == pytest.approx(18.0)
    for (n, end), seed in seeds.items():
        spins = tv._min if end == "min" else tv._max
        assert spins[("spec", n)].value() == pytest.approx(seed), (n, end)
    assert tv._fclk.value() == pytest.approx(tv._default_fclk())
    assert tv._default_fclk() != pytest.approx(9.0)
    # The subtitle names both sides, Manager first — which side is which
    # changes the answer, so it must not be hidden.
    assert tv._subtitle.text() == ("SWI3S PHY2 Manager  ↔  "
                                   "SoundWire 1.3 (1.2 V) Peripheral")


def test_a_soundwire_manager_seeds_its_keeper_response_and_a_negative_setup():
    """A SoundWire Manager's keeper response is its t_Keeper_Delay, 2.5 ns, against SWI3S's
    3.0 ns, and its t_IS equivalent is −1.0 ns, which the box keeps."""
    tv = _view()
    assert tv.inputs("spec").t_keeper_ns == pytest.approx(3.0)
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V2"))
    for side in ("spec", "proposed"):
        assert tv.inputs(side).t_keeper_ns == pytest.approx(2.5), side
        assert tv._min[(side, "Man_t_IS")].value() == pytest.approx(-1.0), side
        assert tv._max[(side, "Man_t_IS")].value() == pytest.approx(-1.0), side
        assert tv.inputs(side).Man_t_IS_max_ns == pytest.approx(-1.0), side
        keeper = next(t for t in compute(tv.inputs(side)).breakdowns["keeper_Man"].terms
                      if t.symbol == "Man_t_Keeper_Response")
        assert keeper.value_ns == pytest.approx(-2.5), side
        assert (tv._min[(side, "Man_t_DD")].value(),
                tv._max[(side, "Man_t_DD")].value()) == pytest.approx((9.7, 26.7)), side
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SWI3S_PHY2"))
    assert tv.inputs("spec").t_keeper_ns == pytest.approx(3.0)


def test_system_size_switches_the_soundwire_envelope():
    tv = _view()
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V2"))
    tv._size_combo.setCurrentIndex(tv._size_combo.findData("large"))
    # 29.0 − 6.0: the large system's t_OV_Data less its t_Slew_Clock max.
    assert tv._max[("spec", "Per_t_DD")].value() == pytest.approx(23.0)
    assert tv._max[("spec", "t_RF Per DATA")].value() == pytest.approx(6.0)
    assert tv._fclk.value() == pytest.approx(11.0)


def test_fclk_seeds_to_the_legal_ceiling_not_the_swi3s_rate():
    """A mixed bus may not clock at 13.2 MHz, so reset must not seed it there."""
    tv = _view()
    # All-SWI3S opens on the 12.288 MHz audio design point (256 x 48 kHz), not
    # PHY2's 13.2 MHz mandatory maximum.
    assert tv._fclk.value() == pytest.approx(12.288)
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V2"))
    assert tv._fclk.value() == pytest.approx(12.288)     # below the 12.3 ceiling
    # The large envelope's 11.0 MHz ceiling DOES clamp the design point.
    tv._size_combo.setCurrentIndex(tv._size_combo.findData("large"))
    assert tv._fclk.value() == pytest.approx(11.0)


def test_advisory_warns_above_the_ceiling():
    tv = _view()
    assert tv._ceiling_label.text() == ""
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V2"))
    # Reset seeded a legal rate, so no warning shows; the caveats are the tooltip.
    assert tv._ceiling_label.text() == ""
    assert "35-65 %" in tv._ceiling_label.toolTip()
    # Push the target above the ceiling and the rate warning joins it.
    tv._fclk.setValue(13.2)
    tv.flush_recompute()
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
    """PHY1 is a spec source on each side.

    A legacy workspace carries `phy: 1` and no spec keys, meaning "both sides are
    PHY1", and restores that way.
    """
    # A realistic legacy dict: every row value present (to_dict always writes
    # them), spec keys absent, `phy` present. Row values come from the dict, so
    # this checks the SELECTOR mapping.
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
    tv2.set_from_dict({"phy": 1, "man_spec": "SW13_1V2"})
    assert tv2._man_spec_combo.currentData() == "SW13_1V2"
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
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V2"))
    tv2 = _view()
    assert not tv2._size_row.isVisibleTo(tv2)
    tv2.set_from_dict(tv.to_dict())
    assert tv2._size_row.isVisibleTo(tv2)


def test_phy1_example_column_follows_the_table_structure_not_the_numbers():
    """Which half of the proposal reaches a PHY1 side is decided by how the SPEC
    IS STRUCTURED, not by which numbers happen to coincide (v1.1r06).

    The "PHY1 & PHY2 Input Timing" table is genuinely shared — one row per parameter, no
    PHY1/PHY2 distinction — so revising Per_tIH 0-4.5 -> 0-1.5 revises it for both PHYs.

    The "PHY1 & PHY2 Output Timing" table prints EVERY parameter as a separate PHY1 and
    PHY2 row and never collapses them, so a revision to PHY2's row implies nothing about
    PHY1's. Man_tDD's, Man_tDZ's and Per_tDZ's two rows agree today, and revising them
    together is a decision the proposal makes. Per_tDD, Per_tZD and Man_tZD DIFFER per PHY,
    so they carry PHY1's own revision (Per_tDD 14-42, Per_tZD 17-42, Man_tZD 22-56) rather
    than PHY2's.
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
    for name in ("Man_t_DZ", "Per_t_DZ"):
        assert (tv._min[("spec", name)].value(),
                tv._max[("spec", name)].value()) == (0.0, 10.0), name
        assert (tv._min[("proposed", name)].value(),
                tv._max[("proposed", name)].value()) == (0.0, 9.0), name

    # PHY1's OWN revision on the rows that differ per PHY, NOT PHY2's proposed values.
    for name, spec_rng, prop_rng in (("Per_t_DD", (14.0, 50.0), (14.0, 42.0)),
                                     ("Per_t_ZD", (14.0, 50.0), (17.0, 42.0)),
                                     ("Man_t_ZD", (14.0, 30.0), (22.0, 56.0))):
        assert (tv._min[("spec", name)].value(),
                tv._max[("spec", name)].value()) == spec_rng, name
        assert (tv._min[("proposed", name)].value(),
                tv._max[("proposed", name)].value()) == prop_rng, name

    # The revisions move margins.
    spec = tv._per_inequality_worst("spec")
    prop = tv._per_inequality_worst("proposed")
    assert prop["MP_hold"].margin_ns > spec["MP_hold"].margin_ns, \
        "Man_tDD,min 7->9 and Per_tIH,max 4.5->1.5 fund MP hold"
    # Per_tDD,max 50->42 is worth exactly 8 ns of PM setup, and raises its F_max.
    assert prop["PM_setup"].margin_ns - spec["PM_setup"].margin_ns == pytest.approx(8.0)
    assert prop["PM_setup"].F_max_MHz > spec["PM_setup"].F_max_MHz


def test_the_shared_input_table_is_shared_on_both_phys():
    """The "PHY1 & PHY2 Input Timing" table has no PHY1/PHY2 split, so both PHYs must carry
    identical input
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
    """A PHY1 Manager with a PHY2 Peripheral.

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
    # The pair opens on the slower side's target: PHY1's, its 6.6 MHz mandatory maximum.
    assert tv._fclk.value() == pytest.approx(6.6)


# ---------------------------------------------------------------------------
# 6. The proposed SWI3S PHY2 revision
# ---------------------------------------------------------------------------

# The P->P DATA legs the spec does not promise: peripheral B READING a level peripheral A drove
# in order to be read. A claim of the form "the proposal closes every inequality" is a claim
# about the GUARANTEED directions, so these are filtered out; what they actually read is
# recorded by the tests named for them.
#
# IMPORTED FROM THE MODEL, NOT RESTATED: one definition, read by both.
_PP_DATA_LEGS = tuple(sorted(CalcResults._PP_DATA_LEGS))


def _guaranteed(breakdowns):
    """`breakdowns` less the P->P data legs — the directions the spec promises."""
    return {k: b for k, b in breakdowns.items() if k not in _PP_DATA_LEGS}


def test_proposal_closes_every_failing_inequality_at_the_reference_corner():
    """MP hold and PM setup both fail under the current spec and both close.

    Deltas are exact and attributable, so they are asserted rather than
    bounded -- each comes from one parameter change:
      MP hold  +5.00 = Man_t_DD,min +2.0 and Per_t_IH,max -3.0
      PM setup +11.00 = Per_t_DD,max 20 -> 9
      MP setup -5.00 = Man_t_DD,max 18 -> 23, the cost of funding the above
    Every edge is placed by its 20 % point, the Manager's clock by its clock reference, at
    these 5.0 ns slews.
    """
    cur = compute(_same("SWI3S_PHY2"))
    prop = compute(_same("SWI3S_PHY2_PROP"))
    for ineq, before, after in (("MP_hold", -2.47, +2.53),
                                ("PM_setup", -3.43, +7.57),
                                ("MP_setup", +7.12, +2.12)):
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
    """The proposal at the design point.

    Targets are 12.288 MHz (256 x 48 kHz) over a 30 cm bus, the design point, not PHY2's
    13.2 MHz mandatory ceiling. At that point the proposal closes
    everything except MP hold at the simultaneous worst case, and that residual
    is **0.25 ns**.

    That corner stacks every parameter at its individual extreme at once --
    slowest legal slew AND full 120 mV noise AND independent supplies at opposite
    tolerance ends. Any ONE of those relaxing is enough (see the companion test).
    """
    prop = compute(_same("SWI3S_PHY2_PROP", F_CLK_target_MHz=12.288))
    assert all(b.margin_ns >= 0 for b in _guaranteed(prop.breakdowns).values()), \
        "the proposal must close at the design point with nominal slew"
    assert prop.breakdowns["MP_hold"].margin_ns == pytest.approx(+2.53, abs=0.02)
    assert prop.breakdowns["MP_setup"].margin_ns == pytest.approx(+4.65, abs=0.02)
    assert prop.breakdowns["PM_setup"].margin_ns == pytest.approx(+10.10, abs=0.02)

    worst = compute(_same("SWI3S_PHY2_PROP", F_CLK_target_MHz=12.288,
                          tRF_Man_CLK_ns=8.3, tRF_Man_DATA_ns=8.3,
                          tRF_Per_DATA_ns=8.3))
    assert worst.breakdowns["MP_hold"].margin_ns == pytest.approx(-0.25, abs=0.02)
    # MP setup stays positive at the design rate; it goes negative only at the
    # 13.2 MHz corner, which is above the target.
    assert worst.breakdowns["MP_setup"].margin_ns > 0
    assert worst.breakdowns["PM_setup"].margin_ns > 0


def test_which_relaxations_close_the_all_extremes_corner():
    """The residual is covered by ANY one of three realistic assumptions.

    The three are alternatives, not a cumulative requirement. Slew is held at its
    8.3 ns worst throughout except where varied. MP hold is the only short leg.
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

    # THE MANAGER'S KEEPER LEG HOLDS even with t_DZ at the other end of its row -- a device
    # that goes high-Z the instant its UI closes, which the spec permits since it states
    # only a maximum. The launch is at its anchor, and the keeper's input crosses its own
    # V_IH,max 0.75·t_RF later (0.65 of its rail, from 0.20, over a 0.60 swing):
    #
    #     1*UI + Man_t_DZ - Man_t_DD,max - 0.75*t_RF - t_keeper
    #         = 36.62 + 0 - 23.00 - 6.225 - 3.00 = +4.40
    #
    # The crossing is the Manager's own data at its own input: a fixed fraction of its own
    # rail with no ground shift, so supply tolerance and noise cannot move it; it would need
    # an edge slower than 14.16 ns to fail.
    assert failing(Man_t_DZ_max_ns=0.0) == {"MP_hold"}
    keeper = compute(_same("SWI3S_PHY2_PROP", **W, Man_t_DZ_max_ns=0.0)).breakdowns["keeper_Man"]
    assert keeper.margin_ns == pytest.approx(36.62 - 23.0 - 0.75 * 8.3 - 3.0, abs=0.01)
    for kw in ({"V_SEOS_tol_frac": 0.0}, {"V_noise_pp_frac": 0.065}):
        assert failing(Man_t_DZ_max_ns=0.0, **kw) == set()
        moved = compute(_same("SWI3S_PHY2_PROP", **W, Man_t_DZ_max_ns=0.0, **kw))
        assert moved.breakdowns["keeper_Man"].margin_ns == pytest.approx(keeper.margin_ns), kw
    syms = [t.symbol for t in
            compute(_same("SWI3S_PHY2_PROP", **W)).breakdowns["keeper_Man"].terms]
    assert [s for s in syms if "X_" in s] == ["X_K,rise,late · Man_t_RF,DATA"], \
        f"the keeper leg's one crossing is its own data at its own input: {syms}"


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
    def limit(f_clk_MHz: float, mode: LaunchMode = LaunchMode.CLK2) -> float:
        t = launch_mode_tDD_ns(mode, f_clk_MHz)
        assert t is not None and t[0] == t[1]
        return t[0]

    assert limit(13.2) == pytest.approx(17.05, abs=0.01)
    assert limit(13.2) < 23.0            # tighter, not looser
    assert limit(13.2, LaunchMode.CLK4) == pytest.approx(8.52, abs=0.01)
    # They cross below 9.78 MHz, where the UI-relative form becomes the looser one.
    assert limit(9.78) == pytest.approx(23.0, abs=0.05)
    assert limit(6.6) > 23.0


@pytest.mark.parametrize("spec", ["SWI3S_PHY2_PROP", "SWI3S_PHY2_EDE"])
def test_a_proposed_source_presets_its_tabulated_t_ZD_range(spec):
    """The proposal's t_ZD ranges are the Manager's 9-23 ns and the Peripheral's 2-9 ns, the
    minimum following t_DD,min, so a preset-seeded row spans them instead of collapsing onto
    its maximum."""
    man, per = preset(spec, "Man"), preset(spec, "Per")
    assert (man.t_ZD_min_ns, man.t_ZD_ns) == (9.0, 23.0)
    assert (per.t_ZD_min_ns, per.t_ZD_ns) == (2.0, 9.0)


def test_a_ratified_source_presets_its_tabulated_t_ZD_range():
    """The "PHY1 & PHY2 Output Timing" table's t_ZD ranges, PHY2 2-18 ns on both sides, PHY1
    the Manager's 14-30 ns and the Peripheral's 14-50 ns: a preset's t_ZD_min_ns is the
    tabulated minimum, not its maximum. PHY1's minimum is at or above its t_DZ maximum, the
    relation that needs no handover UI (`default_handover_UIs`), and PHY2's is not."""
    want = {("SWI3S_PHY2", "Man"): (2.0, 18.0), ("SWI3S_PHY2", "Per"): (2.0, 18.0),
            ("SWI3S_PHY1", "Man"): (14.0, 30.0), ("SWI3S_PHY1", "Per"): (14.0, 50.0)}
    for (spec, side), rng in want.items():
        p = preset(spec, side)
        assert (p.t_ZD_min_ns, p.t_ZD_ns) == rng, (spec, side)
        assert (p.t_ZD_min_ns >= p.t_DZ_max_ns) is (default_handover_UIs(spec, spec) == 0.0)


# ---------------------------------------------------------------------------
# 7. Manager launch mode (analog vs clock-launched)
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
    # at 2x -- but they are inputs rather than something the mode imposes.
    UI = 1000 * 0.45 / 12.288
    base = dict(F_CLK_target_MHz=12.288)
    analog = compute(_same("SWI3S_PHY2_PROP", **base))

    def at(mode, frac):
        inp = _same("SWI3S_PHY2_PROP", launch_mode=mode, **base)
        return compute(dc_replace(inp, Man_t_DD_min_ns=frac * UI,
                                  Man_t_DD_max_ns=frac * UI))

    clk4 = at(LaunchMode.CLK4, 0.25)
    clk2 = at(LaunchMode.CLK2, 0.50)

    assert analog.breakdowns["MP_setup"].margin_ns == pytest.approx(+4.65, abs=0.02)
    assert analog.breakdowns["MP_hold"].margin_ns == pytest.approx(+2.53, abs=0.02)
    # 4x: much more setup room, and hold no worse.
    assert clk4.breakdowns["MP_setup"].margin_ns == pytest.approx(+18.50, abs=0.02)
    assert clk4.breakdowns["MP_hold"].margin_ns == pytest.approx(+2.69, abs=0.02)
    assert clk4.breakdowns["MP_setup"].margin_ns > analog.breakdowns["MP_setup"].margin_ns
    assert clk4.breakdowns["MP_hold"].margin_ns > analog.breakdowns["MP_hold"].margin_ns
    # 2x: trades setup for a large hold margin.
    assert clk2.breakdowns["MP_setup"].margin_ns == pytest.approx(+9.34, abs=0.02)
    assert clk2.breakdowns["MP_hold"].margin_ns == pytest.approx(+11.84, abs=0.02)


def test_the_two_clock_modes_are_bounded_from_opposite_ends():
    """The two clock modes' rate bounds, and which mode governs at which rate.

    4x is bounded by HOLD: its launch delay is 0.25*UI, which shrinks as the
    clock speeds up until it no longer covers the receiver's hold requirement.
    2x is bounded by SETUP: its 0.5*UI delay eats half the UI, so the remainder
    stops covering t_IS first.

    Because they fail from opposite ends, neither alone spans the rate range —
    4x is the better choice up to ~17 MHz (more setup room at adequate hold),
    and above that only 2x closes.
    """
    from swi3s_studio.timing.delta_tpd import compute_delta_tpd_envelope

    f4, bound4 = launch_mode_max_fclk_MHz(LaunchMode.CLK4)
    f2, bound2 = launch_mode_max_fclk_MHz(LaunchMode.CLK2)
    assert bound4 == "hold" and bound2 == "setup"
    # The default Δ_cross,MP coefficient is the default envelope's.
    coeff = compute_delta_tpd_envelope().correlated_diff
    for mode, f in ((LaunchMode.CLK4, f4), (LaunchMode.CLK2, f2)):
        assert launch_mode_max_fclk_MHz(mode, delta_cross_coeff=coeff)[0] == f, mode
    assert f4 == pytest.approx(17.4, abs=0.2)
    assert f2 == pytest.approx(25.1, abs=0.2)
    assert f2 > f4, "2x must reach higher, or the rate-dependent guidance inverts"
    # The figures `launch_mode_max_fclk_MHz`'s docstring quotes are these, to 0.1 MHz.
    import re
    doc = " ".join(launch_mode_max_fclk_MHz.__doc__.split())
    m = re.search(r"4x tops out at ~([0-9.]+) MHz \(hold\), 2x at ~([0-9.]+) MHz \(setup\)", doc)
    assert m, doc
    assert (float(m.group(1)), float(m.group(2))) == (round(f4, 1), round(f2, 1))

    # At `launch_mode_max_fclk_MHz`'s default corner the design point is inside 4x's range
    # (the Example column's worst corner bounds 4x lower, which the launch hint reports)...
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
    the leg improves. The page SEEDS these rows to the data-launch point on a mode
    change instead of leaving a tabulated value to be snapped.
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

    The PHY2 revision proposes Man_t_ZD as "9--23 ns or (0.5 UI 2x CLK, 0.25 UI
    4x CLK)" -- the SAME options as Man_t_DD, tracking it, so 0.00 UI is not on
    offer for this parameter, and its 9 ns analog minimum is the hold floor
    `MP_hold_ho` carries. EDE says nothing about t_ZD; an EndDriveEarly release takes
    the place of t_DZ in the legs (`calculator._ede_release_terms`), and the t_DZ row stays
    on the launch grid with the other two.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))
    UI = 1000 * 0.45 / tv._fclk.value()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.ANALOG.value))
    analog = {leg: b.margin_ns for leg, b in tv._per_inequality_worst("proposed").items()}

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
        # And the hold-on-acquire leg it exists to satisfy reaches at least PARITY with the
        # same column's analog launch, which is the bound and not zero: the revision's own
        # analog minimum leaves the leg short at its worst corner. PM_hold_ho reads no
        # Manager output timing, so the launch mode leaves it where it is.
        w = tv._per_inequality_worst("proposed")
        assert w["MP_hold_ho"].margin_ns >= analog["MP_hold_ho"] - 1e-9, \
            (mode, w["MP_hold_ho"].margin_ns, analog["MP_hold_ho"])
        assert w["PM_hold_ho"].margin_ns == pytest.approx(analog["PM_hold_ho"], abs=1e-9), \
            (mode, w["PM_hold_ho"].margin_ns, analog["PM_hold_ho"])


def test_the_hold_on_acquire_leg_is_what_the_handover_UI_was_paying_for():
    """`MP_hold_ho` / `PM_hold_ho`: an allocated handover UI covers them.

    What they bound: the bit sampled at the UI-opening edge is still inside its
    hold window when the ACQUIRING device turns on. Nothing else covers it ---
    non-contention bounds driver OVERLAP (once the releaser is off, the keeper
    holds the level and the acquirer may drive a different one immediately with no
    driver conflict), and the keeper legs bound the RELEASING side.

    Pinned three ways: each allocated handover UI adds exactly one UI to the leg, at two
    clock rates, it must FAIL on the current spec's Man_t_ZD,min of 2 ns, and it must PASS
    on the revision's 9 ns.
    """
    UI = 1000 * 0.45 / 12.288
    base = dict(F_CLK_target_MHz=12.288, bus_length_cm=30.0,
                Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP",
                Per_t_IH_max_ns=1.5, tRF_Man_DATA_ns=8.3)

    def leg(zd_min, n_ho, f_MHz=12.288):
        return compute(CalcInputs(Man_t_ZD_ns=zd_min, handover_UIs=n_ho,
                                  **{**base, "F_CLK_target_MHz": f_MHz})
                       ).breakdowns["MP_hold_ho"].margin_ns

    # The revision's before and after, at ZERO allocated UIs.
    assert leg(2.0, 0.0) < 0, \
        "2 ns must fail -- that is why the revision raised Man_t_ZD,min to 9"
    assert leg(9.0, 0.0) > 0, "9 ns must pass, or the revision does not fix it"
    # Each allocated UI pays one UI, at the rate in force.
    assert leg(2.0, 1.0) > 0, "an allocated handover UI must cover the leg"
    for z in (2.0, 9.0):
        assert leg(z, 1.0) - leg(z, 0.0) == pytest.approx(UI, abs=1e-9), z
        assert leg(z, 2.0) - leg(z, 0.0) == pytest.approx(2 * UI, abs=1e-9), z
        ui_6 = 1000 * 0.45 / 6.0
        assert leg(z, 1.0, 6.0) - leg(z, 0.0, 6.0) == pytest.approx(ui_6, abs=1e-9), z
    # And it is a hold leg, so it takes t_ZD's MIN corner: more is better.
    assert leg(9.0, 0.0) > leg(2.0, 0.0)


def test_launch_selector_preserves_the_users_clock_rate():
    """Switching launch mode must not reseed or reset F_CLK.

    It routes through its own handler rather than `_on_spec_changed`, because
    that one calls `_reset_to_defaults` and would discard the target rate.
    """
    tv = _view()
    tv._fclk.setValue(12.288)
    for mode in ("4x", "2x", "analog"):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode))
        assert tv._fclk.value() == pytest.approx(12.288), mode


def test_launch_selector_hint_tracks_the_rate_and_flags_the_ceiling():
    """The hint is the only place the rate-dependent bound is visible. It reads the bound from
    the Example column's own MP legs at their worst corner (`_launch_limit`), not from
    `launch_mode_max_fclk_MHz`'s default corner, so 4x's limit here is 12.2 MHz where
    `test_the_two_clock_modes_are_bounded_from_opposite_ends` reads 17.4."""
    tv = _view()
    tv._fclk.setValue(12.288)
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("analog"))
    assert "spread" in tv._launch_hint.text()          # analog: no fixed value
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("4x"))
    text = tv._launch_hint.text()
    assert "0.25 UI = 9.16 ns" in text
    assert "12.2 MHz" in text and "hold" in text
    assert "above this mode's limit" in text            # past 4x's worst-corner limit
    tv._fclk.setValue(10.0)
    assert "above this mode's limit" not in tv._launch_hint.text()
    tv._fclk.setValue(12.288)
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("2x"))
    text = tv._launch_hint.text()
    assert "0.5 UI = 18.31 ns" in text
    assert "19.2 MHz" in text and "setup" in text
    assert "above this mode's limit" not in text
    tv._fclk.setValue(20.0)
    assert "above this mode's limit" in tv._launch_hint.text()


def test_launch_selector_is_disabled_and_forced_analog_for_a_soundwire_manager():
    """A SoundWire Manager has no clock-launch option, so the control must not
    offer one — and must not leave a stale clock mode applied to it."""
    tv = _view()
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData("4x"))
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V2"))
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


def test_the_launch_hint_names_only_the_column_the_selector_drives():
    """On a bus with an EDE Manager and a clocked launch selected, the hint says the selector
    applies to the right-hand column, by its heading (Example / EDE Proposed), only, and that
    the left-hand one keeps its analog delays, and the two columns read their launch modes
    accordingly: the selected clocked mode on the right-hand column, ANALOG on the left-hand
    one."""
    from swi3s_studio.timing.spec_source import is_ede
    tv = _view()
    ede = next(i for i in range(tv._man_spec_combo.count())
               if is_ede(tv._man_spec_combo.itemData(i)))
    tv._man_spec_combo.setCurrentIndex(ede)
    left, right = tv._column_labels()
    assert right == "Example / EDE Proposed", right
    for mode in (LaunchMode.CLK2, LaunchMode.CLK4):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
        tv._update_launch_hint()
        hint = tv._launch_hint.text()
        assert f"applies to the {right} column only" in hint, (mode, hint)
        assert f"the {left} column keeps its analog delays" in hint, (mode, hint)
        assert tv._launch_mode_for("proposed") is mode
        assert tv._launch_mode_for("spec") is LaunchMode.ANALOG, mode


def test_a_soundwire_managers_margins_do_not_depend_on_the_launch_mode():
    """A clocked launch is a SWI3S Manager's option: a SoundWire Manager launches as tabulated,
    so every leg's margin, terms and F_max are the same under every launch mode."""
    inp = dc_replace(_mixed("SW13_1V2", "SWI3S_PHY2"), F_CLK_target_MHz=12.0)
    ref = compute(inp)
    for mode in (LaunchMode.CLK2, LaunchMode.CLK4):
        r = compute(dc_replace(inp, launch_mode=mode))
        for leg, b in ref.breakdowns.items():
            assert r.breakdowns[leg].margin_ns == b.margin_ns, (mode, leg)
            assert [t.symbol for t in r.breakdowns[leg].terms] == [t.symbol for t in b.terms]
            assert r.breakdowns[leg].F_max_MHz == b.F_max_MHz, (mode, leg)
    # A SWI3S Manager's margins do depend on it.
    swi3s = dc_replace(inp, Man_spec="SWI3S_PHY2")
    assert (compute(dc_replace(swi3s, launch_mode=LaunchMode.CLK4)).breakdowns["MP_setup"]
            .margin_ns != compute(swi3s).breakdowns["MP_setup"].margin_ns)


def test_the_launch_mode_never_governs_the_specification_column():
    """A clocked launch is the revision's option, so it cannot touch tabulated values.

    The wording that offers it -- "9-23 ns or (0.5 UI 2x CLK, 0.25 UI 4x CLK)" -- is in
    the PHY2 revision. Ratified PHY1 and PHY2 tabulate analog output delays and give a
    Manager no multiplied clock, so the Specification column is analog whatever the
    selector says.

    `snap_to_launch_grid(2.0, 2x)` is 0.00, so a mode applied to the spec column zeroes
    its Manager rows. This asserts the ROWS and not only the margins.
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
    nearest point is 0.00 UI. The floor makes it the lowest placeable point that still
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
    # The floor's tolerance is SNAP_TOL_NS in ns at every grid step: a floor that a grid
    # point misses by less keeps the point, and one it misses by more lifts to the next.
    step = 0.25 * UI
    for over, k in ((0.5 * SNAP_TOL_NS, 1), (2.0 * SNAP_TOL_NS, 2), (0.004, 2)):
        floor = step + over
        got = snap_to_launch_grid(floor, LaunchMode.CLK4, 12.288, 0.45, floor_ns=floor)
        assert got == pytest.approx(k * step), (over, got)
        assert got >= floor - SNAP_TOL_NS, (over, got)

    # The Example column seeds its Manager rows from the revision, whose minima are the floors.
    rev = preset("SWI3S_PHY2_PROP", "Man")
    floors = {"Man_t_DD": rev.t_DD_min_ns, "Man_t_ZD": rev.t_ZD_min_ns}
    assert floors == {"Man_t_DD": 9.0, "Man_t_ZD": 9.0}, floors
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2"))
    for mode in (LaunchMode.CLK2, LaunchMode.CLK4):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
        for name, floor in floors.items():
            got = tv._min[("proposed", name)].value()
            # The spin box carries one decimal, hence the 0.05.
            assert got >= floor - 0.05, f"{mode.value}: proposed {name} snapped to {got}"


# ---------------------------------------------------------------------------
# 8. The revision carried onto a mixed bus
# ---------------------------------------------------------------------------

def test_revision_reaches_only_one_of_two_hold_terms_on_a_mixed_bus():
    """The revision reaches one of MP hold's two gains on a mixed bus.

    The revision buys MP hold from two terms that sit on OPPOSITE sides of the
    link: Man_t_DD,min 7->9 (+2.0) needs a SWI3S Manager transmitting, and
    Per_t_IH,max 4.5->1.5 (+3.0) needs a SWI3S Peripheral receiving. A same-spec
    SWI3S bus collects both (+5.0, closes); a mixed bus collects exactly one,
    because the other term belongs to a SoundWire device that does not change.
    """
    F = dict(F_CLK_target_MHz=12.288)
    a_cur = compute(_mixed("SWI3S_PHY2", "SW13_1V2", **F))
    a_prop = compute(_mixed("SWI3S_PHY2_PROP", "SW13_1V2", **F))
    b_cur = compute(_mixed("SW13_1V2", "SWI3S_PHY2", **F))
    b_prop = compute(_mixed("SW13_1V2", "SWI3S_PHY2_PROP", **F))

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

    # A's residual: 0.875 ns of it is the SoundWire Peripheral's data-input window,
    # (0.454 − 0.279)·5.0.
    assert a_prop.breakdowns["MP_hold"].margin_ns == pytest.approx(-0.84, abs=0.02)
    window = compute(dc_replace(_mixed("SWI3S_PHY2_PROP", "SW13_1V2", **F),
                                Per_spec="SWI3S_PHY2_PROP")).breakdowns["MP_hold"].margin_ns
    assert window - a_prop.breakdowns["MP_hold"].margin_ns == pytest.approx(0.875, abs=0.005)
    # B closes: its hold bound is t_ZD_Data,Min's Manager equivalent, 9.7 ns, against
    # t_OH_Data's 6.7 ns, at its own anchor: t_ZD_Data from the clock's 50 % crossing on the
    # Manager's late clock edge (5.0 ns, the top of its range) plus the first 20 % of an early
    # data edge (4.5 ns), 2.33 ns later than the equivalent's fastest-clock derivation.
    assert b_prop.breakdowns["MP_hold"].margin_ns == pytest.approx(+5.79, abs=0.02)
    assert b_prop.breakdowns["MP_hold"].margin_ns >= 0


def test_scenario_A_pm_setup_is_not_swi3s_to_fix():
    """A's binding deficit after the revision belongs to SoundWire.

    On A/PM the SoundWire Peripheral transmits, so PM setup is governed by its
    t_DD equivalent and slew — no SWI3S revision can move it, and the value must be
    IDENTICAL before and after. B/PM is the SWI3S Peripheral's, and there the
    revision is worth +11 ns.

    Σ_cross,PM,setup does not assume the ground shift cancels across the two legs:
    cancellation needs equal per-leg t_RF, which the small/1.2 V presets give
    (5.00 ns on both legs).

    At 12.288 MHz the UI is 0.45·1000/12.288 = 36.62 ns. Both crossings count from their
    edges' 20 % points -- the clock's from the Manager's clock reference, the data's from its
    anchor -- and the leg is evaluated at one V_SEOS per device. A/PM setup is
    36.62 − 2·2.0 − 1.091·5.0 − Per_t_DD 22.9 − 0.5 − 0.868·5.0 − Man_t_IS 3.0 = −3.58 ns: a
    rising clock with falling data, Sigma_cross,PM,setup's cross case, the SoundWire data edge
    on its fast edge, 4.5 ns against the 5.0 ns top of its range: its t_OV ends at 80 % of the
    ramp, so the fast edge puts the 20 % point 0.5 ns later, more than it brings the crossing
    forward (`CalcInputs.fast_data_edge`). B/PM setup carries the SWI3S Peripheral's 20.0 ns
    and the SoundWire Manager's t_IS of −1.0 instead, +3.34 ns.
    """
    F = dict(F_CLK_target_MHz=12.288)
    a_cur = compute(_mixed("SWI3S_PHY2", "SW13_1V2", **F))
    a_prop = compute(_mixed("SWI3S_PHY2_PROP", "SW13_1V2", **F))
    assert a_cur.breakdowns["PM_setup"].margin_ns == pytest.approx(-3.58, abs=0.02)
    assert a_cur.breakdowns["PM_setup"].fast_data_edge == {"Per DATA"}
    assert a_prop.breakdowns["PM_setup"].margin_ns == pytest.approx(-3.58, abs=0.02)
    assert a_prop.breakdowns["PM_setup"].margin_ns == pytest.approx(
        a_cur.breakdowns["PM_setup"].margin_ns, abs=1e-9), \
        "a SWI3S revision must not move a leg the SoundWire side owns"

    b_cur = compute(_mixed("SW13_1V2", "SWI3S_PHY2", **F))
    b_prop = compute(_mixed("SW13_1V2", "SWI3S_PHY2_PROP", **F))
    assert b_cur.breakdowns["PM_setup"].margin_ns == pytest.approx(+3.34, abs=0.02)
    assert b_prop.breakdowns["PM_setup"].margin_ns == pytest.approx(+14.34, abs=0.02)
    # The revision's worth on B is a clean +11 ns.
    assert (b_prop.breakdowns["PM_setup"].margin_ns
            - b_cur.breakdowns["PM_setup"].margin_ns) == pytest.approx(11.0, abs=5e-3)
    # Scenario B's one deficit under the revision is PM hold. The SoundWire Manager's t_IHold
    # runs from its clock's 65 % crossing, its equivalent (7.8 ns) from the 20 % reference
    # 5.0/3 ns after the clock starts, and the Peripheral's t_DD,min of 2.0 ns does not cover
    # it: 2.0 + 2·2.0 + (0.168 − 0.038)·5.0 − 7.8 = −1.15 ns at these slews, the two crossings
    # counted from their edges' 20 % points, the Manager's clock reference and the data's
    # anchor.
    short = {k: b.margin_ns for k, b in _guaranteed(b_prop.breakdowns).items()
             if b.margin_ns < 0}
    assert set(short) == {"PM_hold"}, short
    assert short["PM_hold"] == pytest.approx(-1.15, abs=0.02)


def test_preset_trf_means_different_things_on_the_two_sides():
    """SWI3S's preset t_RF is a NOMINAL; SoundWire's is a MAXIMUM.

    SWI3S tabulates 3.0-8.3 ns with 5 ns as its reference test condition.
    SoundWire tabulates t_Slew_Clock 2.0-5.0 ns (small/1.2 V, Table 17) and
    publishes no nominal at all. So a table built from the presets alone compares
    a SWI3S nominal against a SoundWire worst case; put each side at its own maximum.

    The asymmetry is invisible in the numbers (both presets read 5.0 ns) and
    changes every cross-spec margin that scales with slew.
    """
    from swi3s_studio.timing.spec_source import trf_range_ns

    assert preset("SWI3S_PHY2", "Man").tRF_ns == 5.0      # reference condition
    assert preset("SW13_1V2", "Per").tRF_ns == 5.0        # tabulated MAXIMUM
    # SWI3S's own maximum is well above its preset, which lies inside its range;
    # SoundWire's IS its preset, at either system size.
    for spec in ("SWI3S_PHY1", "SWI3S_PHY2", "SWI3S_PHY2_PROP", "SWI3S_PHY2_EDE"):
        lo, hi = trf_range_ns(spec)
        assert (lo, hi) == (3.0, 8.3), spec
        for side in ("Man", "Per"):
            assert lo < preset(spec, side).tRF_ns < hi, (spec, side)
    for size, hi in (("small", 5.0), ("large", 6.0)):
        assert trf_range_ns("SW13_1V2", size) == (2.0, hi), size
        for side in ("Man", "Per"):
            p = preset("SW13_1V2", side, size)
            assert p.tRF_ns == hi == sw13_equivalents("SW13_1V2", side, size)["t_RF_max_ns"]


def test_mp_hold_at_each_sides_own_slew_maximum():
    """MP hold on a mixed bus at each side's own slew maximum.

    Quoted at the each-side-own-maximum corner (SWI3S 8.3, SoundWire 5.0),
    because that is the only worst case defined for both specs. The same-spec
    figure is ~-5.2 ns, not the ~-2.5 ns of the 5 ns reference condition — the two
    differ by 2.78 ns and must never be quoted interchangeably.
    """
    F = dict(F_CLK_target_MHz=12.288)
    SWI3S_MAX, SW_MAX = 8.3, 5.0

    def mp_hold(inp, t_man, t_per):
        return compute(dc_replace(inp, tRF_Man_CLK_ns=t_man, tRF_Man_DATA_ns=t_man,
                                tRF_Per_DATA_ns=t_per)).breakdowns["MP_hold"].margin_ns

    # Same-spec SWI3S: both sides at 8.3.
    s_cur = mp_hold(_same("SWI3S_PHY2", **F), SWI3S_MAX, SWI3S_MAX)
    s_prop = mp_hold(_same("SWI3S_PHY2_PROP", **F), SWI3S_MAX, SWI3S_MAX)
    assert s_cur == pytest.approx(-5.25, abs=0.02)
    assert s_prop == pytest.approx(-0.25, abs=0.02)

    # Mixed: SWI3S side at 8.3, SoundWire side at 5.0. On A the SoundWire Peripheral's data
    # input falls at up to 65 % instead of 55 %: its early data crossing term on the 8.3 ns
    # lane's early edge is −0.29 ns rather than a SWI3S Peripheral's +1.08 ns, 1.38 ns of
    # hold. On B the SoundWire Manager
    # drives both MP lanes, so B is its preset corner: Man_t_DD,min 9.7 plus its anchor
    # correction, 2.33 (`test_revision_reaches_only_one_of_two_hold_terms_on_a_mixed_bus`),
    # against Per_t_IH 4.5 current / 1.5 proposed: +2.79 / +5.79 ns.
    a_cur = mp_hold(_mixed("SWI3S_PHY2", "SW13_1V2", **F), SWI3S_MAX, SW_MAX)
    a_prop = mp_hold(_mixed("SWI3S_PHY2_PROP", "SW13_1V2", **F), SWI3S_MAX, SW_MAX)
    b_cur = mp_hold(_mixed("SW13_1V2", "SWI3S_PHY2", **F), SW_MAX, SWI3S_MAX)
    b_prop = mp_hold(_mixed("SW13_1V2", "SWI3S_PHY2_PROP", **F), SW_MAX, SWI3S_MAX)
    assert a_cur == pytest.approx(-6.12, abs=0.02)
    assert a_prop == pytest.approx(-4.12, abs=0.02)
    a_window = mp_hold(dc_replace(_mixed("SWI3S_PHY2", "SW13_1V2", **F), Per_spec="SWI3S_PHY2"),
                       SWI3S_MAX, SW_MAX) - a_cur
    assert a_window == pytest.approx(1.38, abs=0.02)
    assert b_cur == pytest.approx(+2.79, abs=0.02)
    assert b_prop == pytest.approx(+5.79, abs=0.02)   # both close at the worst corner

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
    single-valued, but the SWI3S proposal revises them as ranges.
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

    # Nothing is labelled "proposed".
    assert "PROPOSED" not in tv._title.text()
    assert "PROPOSED" not in tv._ceiling_label.text()
    # ...and the selector offers ratified specs only.
    data = [tv._man_spec_combo.itemData(i) for i in range(tv._man_spec_combo.count())]
    assert not any(d.endswith("_PROP") for d in data), data


def test_proposal_delta_is_visible_in_the_two_columns():
    """The Spec-vs-Example delta must BE the revision's effect.

    +5.00 on MP hold, +11.00 on PM setup, -5.00 on MP setup: the revision's per-term
    gains (+2.0 Man_t_DD,min and +3.0 Per_t_IH,max on MP hold).
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
# 9. Handover non-contention
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
    model reads the note as a PEER-TO-PEER statement.

    "EndDriveEarly is typically not used with PHY1 because this FBCSE operating
    mode already allows for handover between outputs without needing extra UIs
    due to the inequality satisfied by the values of the output timing
    parameters (t_ZD_Min_Phy1 >= t_DZ_Max_Phy1)."

    The note's bare difference, 14 - 10 = 4 ns, is the device half of every leg. P->P adds
    the two peripherals' detections of the one clock edge they share at zero UIs, at that
    edge's one slew factor (`_one_edge`), and is rate-independent, which is the note's actual
    content.

    THE MIXED LEGS DO NOT GIVE 4 ns.
    A peripheral's output parameters are measured from ITS DETECTION of the clock
    edge (the "PHY1 & PHY2 Output Timing" table, "after edge on clock input"; the Peripheral
    Output Timing figure marks it at V_IH / V_IL of the input), while a Manager's are
    measured from its clock reference, the clock output's V_OL / V_OH crossing (Manager
    Output Timing figure). So on a mixed handover the two ends do not share a zero: the
    detection, less the Manager's reference, enters with a sign that depends on which class
    is releasing:

        MP  the peripheral ACQUIRES, so its lateness keeps it clear   -> +(detect - ref)
        PM  the peripheral RELEASES, so its lateness holds the bus    -> -(detect - ref)

    Checked on a ZERO-LENGTH bus, so none of this is propagation: it is the
    reference-class difference alone, and it does not go away on a short bus.
    """
    assert default_handover_UIs("SWI3S_PHY1", "SWI3S_PHY1") == 0.0
    for fclk in (0.032, 6.4, 6.6):
        inp = CalcInputs(
            Man_spec="SWI3S_PHY1", Per_spec="SWI3S_PHY1", handover_UIs=0.0,
            F_CLK_target_MHz=fclk, bus_length_cm=0.0,
            Man_t_DZ_max_ns=10.0, Per_t_DZ_max_ns=10.0,
            Man_t_ZD_ns=14.0, Per_t_ZD_ns=14.0,
        )
        r = compute(inp)
        # The two clock-crossing corners, taken from the envelope at each leg's supplies
        # rather than written out, so this test cannot drift from the coefficients it is
        # checking. The envelope counts each from its edge's 20 % point, the Manager's clock
        # reference, so each is a detection less the reference on the edge the peripheral
        # detects. The closing edge rises or falls, and each leg takes the worse: the smaller
        # early credit, the larger late charge.
        def env(leg):
            return inp.envelope(r.breakdowns[leg].supplies)

        e = env("MP_contention")
        early = min(e.sigma_pm_hold_rf_clk, e.mp_early_fall) * 5.0
        e = env("PM_contention")
        late = max(e.mp_late_rise, e.mp_late_fall) * 5.0
        # P->P has TWO peripherals, and they read the SAME clock edge: the Manager drives it
        # from one pin, so one slew serves both detections and both take the edge's one slew
        # factor (`_one_edge`). Each detects it at its own supply: the releaser A late, the
        # acquirer B early (`_b`), with one Manager supply for both.
        e = env("PP_contention")
        pp = min(_one_edge(inp, e.sigma_pm_hold_rf_clk_b * 5.0, e.mp_late_rise * 5.0),
                 _one_edge(inp, e.mp_early_fall_b * 5.0, e.mp_late_fall * 5.0))
        # NO LEG EQUALS THE NOTE'S BARE DIFFERENCE. Every one of the three has at
        # least one peripheral on it, and a peripheral's t_DZ / t_ZD are referenced to
        # ITS OWN detection of the clock -- so each leg carries the crossing at
        # whichever corner its roles demand:
        #
        #   M->P  the peripheral only ACQUIRES        -> + early
        #   P->M  the peripheral only RELEASES        -> - late
        #   P->P  one of each, both on the same edge   -> + early - late
        #
        # The 4 ns is still in there, as the DEVICE half; what the note omits is
        # where those parameters' own zero sits on the Manager's timeline.
        assert r.breakdowns["MP_contention"].margin_ns == pytest.approx(4.0 + early), fclk
        assert r.breakdowns["PM_contention"].margin_ns == pytest.approx(4.0 - late), fclk
        assert r.breakdowns["PP_contention"].margin_ns == pytest.approx(4.0 + pp), fclk
        # Rate-independence is the note's content: no UI appears in any of these
        # legs, so no clock rate changes them. A PASSING zero-UI leg reports inf; a
        # FAILING one reports 0.0 rather than inf, which says "no legal rate" instead
        # of "any rate". Both are statements that no clock rate reaches the leg.
        assert r.breakdowns["MP_contention"].F_max_MHz == math.inf, fclk
        for leg in ("PM_contention", "PP_contention"):
            assert r.breakdowns[leg].F_max_MHz == 0.0, (leg, fclk)

    # Two of the three are NEGATIVE at zero bus length, so it is not a propagation
    # effect -- it is the crossing alone. PHY1's zero-UI handover does not close on
    # either leg with a releasing peripheral with the two reference classes on one
    # timeline. Whether the Sec. 11.1.5 note is a device-parameter statement or a bus
    # one is a question this model does not answer.
    assert r.breakdowns["PM_contention"].margin_ns < 0
    assert r.breakdowns["PP_contention"].margin_ns < 0
    assert r.breakdowns["MP_contention"].margin_ns > 0, \
        "M->P must still close: its peripheral only acquires, so it gains the crossing"


def test_the_level_is_owned_by_the_hold_ho_legs_not_by_contention():
    """The `*_hold_ho` legs own the level during a contention-leg overlap.

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
    # P2P has BOTH: a contention leg (two drivers can damage each other whether or not
    # either reads the other) and its own data legs. The leg that funds the level here is
    # the acquirer's own turn-on.
    assert "PP_contention" in r.breakdowns
    for leg in ("PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho"):
        assert leg in r.breakdowns, f"{leg} is a P->P data leg and must be evaluated"
    assert any(t.symbol.startswith("Per_t_ZD") for t in r.breakdowns["PP_hold_ho"].terms), \
        "the P->P level leg is funded by the acquiring peripheral's turn-on, like MP/PM"


def test_the_out_of_scope_split_matches_the_legs_own_terms():
    """Which P->P legs bound the bus, checked against WHOSE LEVEL each one protects.

    An out-of-scope declaration is a claim about an inequality's TERMS; this checks it
    against them.

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

    # AT THE WORST CORNER, not at the typicals: the leg passes at nominal values and fails
    # once the search has it.
    rows = default_swi3s_rows()

    def worst(base, leg, rs=None):
        rs = rows if rs is None else rs
        return compute(apply_row_picks(base, rs, find_worst_corner_rows(leg, rs, base)))

    base = _same("SWI3S_PHY2_PROP", handover_UIs=0.0)
    assert compute(base).breakdowns["PP_hold_ho"].margin_ns > 0
    assert worst(base, "PP_hold_ho").breakdowns["PP_hold_ho"].margin_ns < 0, \
        "the premise of the rest of this test: PP_hold_ho fails at zero handover UIs"
    # MEMBERSHIP OF THE BOUNDING SET IS THE MECHANISM, so check it there rather than only
    # in the frozenset above -- `_bounding` is what F_max and the results tree read.
    b = worst(base, "PP_hold_ho")._bounding()
    assert "PP_hold_ho" in b and "PP_hold" not in b, sorted(b)

    # THE BEHAVIOURAL HALF, and it needs a configuration where PP_hold_ho is the ONLY
    # failing bounding leg. Otherwise MP_hold's own negative margin pins the rate to zero
    # and F_max says nothing about this leg at all. A launch at 20 ns clears both MP hold
    # legs and leaves PP_hold_ho untouched, since no Manager parameter appears in it.
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
    assert worst(base_ho, "PP_hold_ho", lift).breakdowns["PP_hold_ho"].margin_ns > 0, \
        "one allocated handover UI clears PP_hold_ho at its own worst corner"
    assert r1.breakdowns["PP_hold"].margin_ns < 0, \
        ("PP_hold has no handover-UI term and stays negative -- so if it were in the "
         "bounding set no allocation could ever clear the bus, which is the reason the "
         "exclusion exists for it and not for its handover variant")
    assert r1.binding_inequality != "PP_hold" and r1.F_max_binding_MHz > 0.0, \
        f"PP_hold must not bound the bus, got {r1.binding_inequality}"


def test_contention_is_overlap_bounded_by_one_flight_not_a_gap():
    """What counts as contention, and what the bus therefore costs each leg.

    The model's criterion: two drivers may be on together for up to the flight time
    between them without contending. The acquirer turns on into a
    Z0 load and sources V/Z0 -- an ordinary switching current -- and the releaser does
    not learn it has company until that wave arrives one traversal later. So the
    criterion bounds OVERLAP at +t_PD, and the terms are absolute driver-enable times:

        overlap = T_off(releaser) - T_on(acquirer) <= t_PD

    A bare difference of two parameter values is a MIXED frame; both events are put in
    absolute time, not moved to one pin.

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
    # M->P's two traversals are two `t_PD` terms, the clock's flight to the acquiring
    # peripheral and its drive's flight back, in time order around its detection and
    # turn-on. A zero net count states the cancellation instead of showing a +t_PD/-t_PD
    # pair at the bus row's typ value, which is not a corner.
    for leg, n in (("MP_contention", 2), ("PM_contention", 0), ("PP_contention", 0)):
        pd = [t for t in far[leg].terms if t.symbol.endswith("t_PD")]
        if not n:
            # DROPPED, not shown as zero: the count is zero for every input, so the
            # term could never be anything else. `0·UI` is dropped too, because nothing
            # is allocated -- see test_no_handover_ui_means_no_ui_term. The zero that
            # STAYS is `Man_t_IH`, a parameter whose value happens to be 0 on this spec.
            assert pd == [], f"{leg}: a structurally-zero bus term must be dropped: {pd}"
            continue
        assert [t.symbol for t in pd] == ["t_PD"] * n, (leg, [t.symbol for t in pd])
        assert all(t.value_ns == pytest.approx(2.0) for t in pd), leg
        syms = [t.symbol for t in far[leg].terms]
        first, second = (i for i, sym in enumerate(syms) if sym == "t_PD")
        detect = next(i for i, sym in enumerate(syms) if sym.endswith("· Man_t_RF,CLK"))
        assert first < detect < syms.index("Per_t_ZD") < second, syms


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

    Asserted as the identity, not as a number: the magnitudes follow this model's own
    envelope inputs (V_IH/V_IL, supply, noise), and the vendored reference fixture's P->P
    values are not compared.
    """
    from swi3s_studio.timing.calculator import (
        _per_clk_cross_parts,
        _role_on_edge,
        pp_clock_edges,
    )

    inp = CalcInputs()
    r = compute(inp)

    def crossings(leg):
        # Match `"X_" in` the symbol: a GATHERED pair's symbol opens with a bracket —
        # `(X_MP,A,rise,late − X_MP,B,fall,early) · Man_t_RF,CLK` — because two crossings of
        # one pin net into one product.
        return sum(t.value_ns for t in r.breakdowns[leg].terms if "X_" in t.symbol)

    # Each leg at its own supplies (Manager, A, B): clk@A at A's, clk@B and data@B at B's,
    # B's pair differenced as Delta_cross,MP is, the data lane on `Per DATA` driven by A.
    set_env = inp.envelope(r.breakdowns["PP_setup"].supplies)
    hold_env = inp.envelope(r.breakdowns["PP_hold"].supplies)
    k, s = inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns
    late = _per_clk_cross_parts(inp, set_env, corner="late")[0]
    d_set = set_env.mp_late_rise_per * s - set_env.mp_early_fall_b * k

    # Setup wants A's clock LATE; hold wants it EARLY, and there it is the one credit. Every
    # crossing counts from its edge's 20 % point: B's data crossing from A's launch anchor,
    # each clock crossing from the Manager's clock reference. On hold A launches on the edge B
    # samples, so A's and B's clock detections take that edge's one slew factor (`_one_edge`);
    # on setup they are of two edges, each at its corner's own.
    assert crossings("PP_setup") == pytest.approx(-(late + d_set))
    assert crossings("PP_hold") == pytest.approx(
        _one_edge(inp, hold_env.sigma_pm_hold_rf_clk * k, hold_env.mp_late_rise_b * k)
        + hold_env.mp_early_fall_per * s)
    # A's clock crossing and B's are the SAME PIN's, so they gather into a single product;
    # B's DATA crossing is a different row and stays its own term. Each P->P leg therefore
    # shows TWO crossing terms, not three: the netted clock and the peripheral's data.
    # A crossing on an edge whose slew factor is not its corner's own names that edge in its
    # symbol (`_role_on_edge`). Each detection names its device and edge, `X_MP,A` and
    # `X_MP,B`, and B's detection of A's data is `X_PP,B`: B samples on a falling edge on setup
    # and a rising one on hold.
    a_edge, b_edge = pp_clock_edges(inp, hold_env, "PP_hold")
    assert a_edge == b_edge, "PP_hold's two clock detections are of one edge"
    for leg in ("PP_setup", "PP_hold"):
        syms = sorted(t.symbol for t in r.breakdowns[leg].terms if "X_" in t.symbol)
        data = "X_PP,B,rise,late" if "setup" in leg else "X_PP,B,fall,early"
        gathered = ("(X_MP,A,rise,late − X_MP,B,fall,early)" if "setup" in leg
                    else f"(X_MP,B,rise,{_role_on_edge('late', b_edge)} − "
                         f"X_MP,A,rise,{_role_on_edge('early', a_edge)})")
        assert syms == sorted([f"{gathered} · Man_t_RF,CLK",
                               f"{data} · Per_t_RF,DATA"]), syms
        # B's DATA lane is the peripheral's: the data reaching B was driven by A, not by the
        # Manager.
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

    A SELECTOR, not a corner ROW: as a row its value would multiply the bus-length row, and
    the bus-length row would move the margin in a direction that depends on it, which
    `find_worst_corner_rows` forbids (calculator checklist rule 4). The brute-force check
    below is the guard.
    """
    base = CalcInputs()
    t_pd = base.t_PD_ns()

    def pd_terms(placement, leg):
        b = compute(dc_replace(base, pp_placement=placement)).breakdowns[leg]
        return [(t.symbol, round(t.value_ns, 6)) for t in b.terms if t.symbol == "t_PD"]

    # TWO TERMS, NOT ONE `2·t_PD`: the legs read in time order and the count is two
    # different FLIGHTS -- the clock reaching A at the far end, then A's data running back to
    # B -- so they bracket A's launch instead of sitting together, and each carries a note
    # saying which it is. The contention legs' bus term stays NETTED
    # (test_contention_is_overlap_bounded_by_one_flight_not_a_gap): there the pair cancels.
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
    # `PP_PLACEMENT_LABELS` without a cost decided would otherwise go untested.
    from swi3s_studio.timing.calculator import PP_PLACEMENT_LABELS

    free = set(PP_PLACEMENT_LABELS) - {"unknown", "driver_far"}
    assert free == {"both_near", "driver_near", "both_far"}, (
        f"a placement was added or removed: {sorted(free)} — decide its bracket in "
        f"`pp_net_pd` and say so here")
    for placement in sorted(free):
        assert pd_terms(placement, "PP_setup") == [], placement
        assert pd_terms(placement, "PP_hold") == [], placement

    # And the search is exact: no combination of the geometric/peripheral rows beats
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
        assert got <= brute + 1e-9, f"{leg}: search found {got:+.3f}, brute force {brute:+.3f}"


def test_pp_completes_the_launch_method_2x2():
    """Setup and hold, each on both launch methods -- the same structural grid MP and PM
    carry. A receiver cannot tell whether an edge came from t_DD or out of high-Z, so both
    must satisfy both.

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
    the two differences compound: the leg is short, and the shortfall IS the price of
    guaranteeing P->P.
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

    Display only: with no handover UI allocated, no term shows one, and every margin is the
    sum of the terms it shows. One allocated UI shows as a `1·UI` term, moves the margin by
    that UI plus the shift of the crossings it moves, and leaves every other term as it is.

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
        # The crossings move with the allocation: one UI puts a peripheral's launch or
        # turn-on on a falling edge (`handover_flips_edge`), and PP_contention's two clock
        # detections are of one edge at zero UIs and of two at one. Every other term stays.
        xs = {n: sum(t.value_ns for t in b.terms if "X_" in t.symbol)
              for n, b in ((0, none), (1, one))}
        if leg in ("MP_hold_ho", "PM_contention"):
            assert xs[0] == pytest.approx(xs[1]), leg
        assert one.margin_ns - none.margin_ns == pytest.approx(
            CalcInputs().UI_ns() + xs[1] - xs[0])

        def rest(b):
            return sorted((t.symbol, round(t.value_ns, 9)) for t in b.terms
                          if "UI" not in t.symbol and "X_" not in t.symbol)
        assert rest(one) == rest(none), leg
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
    spec. The leg carries no bus term: the releasing peripheral's clock lateness and the
    overlap allowance are the same flight with opposite signs, so the leg does not depend
    on the bus length (test_contention_is_overlap_bounded_by_one_flight_not_a_gap). At one
    handover UI the acquirer turns on after a falling edge, detected at V_IL.
    """
    inp = CalcInputs(
        Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2", handover_UIs=1.0,
        F_CLK_target_MHz=12.288, bus_length_cm=30.0,
        Per_t_DZ_max_ns=10.0, Per_t_ZD_ns=2.0, Man_t_ZD_ns=2.0,
        Man_t_DZ_max_ns=10.0,
    )
    r = compute(inp).breakdowns
    pp = r["PP_contention"]
    # Both device terms are the PERIPHERAL's, unlike either mixed leg.
    syms = [t.symbol for t in pp.terms]
    assert any("Per_t_ZD" in s for s in syms) and any("Per_t_DZ" in s for s in syms)
    # NO MANAGER *DRIVER*, which is not the same as no Manager symbol at all: the
    # Manager drives the CLOCK on a P2P data handover, so `Man_t_RF` belongs here. What
    # must be absent is a Manager output-timing or input-timing parameter -- a Manager
    # DRIVER or RECEIVER, not the clock it sources.
    assert not any(s.startswith(("Man_t_DD", "Man_t_ZD", "Man_t_DZ",
                                 "Man_t_IS", "Man_t_IH")) for s in syms), \
        f"a P2P handover involves no Manager driver or receiver: {syms}"
    # PP does not equal PM: the difference is the acquiring peripheral's own crossing
    # credit, which the envelope counts from the clock reference PM's acquiring Manager turns
    # on from, and the releaser's late detection at each leg's supplies. That is what makes
    # PP a separate leg rather than a relabelling of PM. On PP the acquirer is B (`_b`).
    _e = inp.envelope(pp.supplies)
    _pm = inp.envelope(r["PM_contention"].supplies)
    delta = (_e.mp_early_fall_b - _e.mp_late_rise + _pm.mp_late_rise) * 5.0
    assert pp.margin_ns - r["PM_contention"].margin_ns == pytest.approx(delta)
    # ...and PP carries BOTH crossing corners — but GATHERED into one product, because both
    # ends read the SAME clock edge. The Manager sources the clock from one pin, so one slew
    # serves both detections. The netted term keeps both contributions visible in its
    # note rather than in two symbols. The releaser's late detection, at A, is
    # `X_MP,A,rise,late`; the acquirer's early one, of the falling edge one UI gives, at B's
    # supply, is `X_MP,B,fall,early`.
    xs = {t.symbol: t.value_ns for t in pp.terms if "X_MP,B," in t.symbol}
    assert {s.split(" · ")[0] for s in xs} == {"(X_MP,A,rise,late − X_MP,B,fall,early)"}, xs
    assert all(s.endswith("Man_t_RF,CLK") for s in xs), xs
    net = next(t for t in pp.terms if "X_MP,B," in t.symbol)
    # The note decomposes it, and the inner signs are relative to the operator hoisted out:
    # "- (late - early)", never "- (late + early)", which would state a different number.
    assert "late" in net.note and "early" in net.note, net.note
    assert " − " in net.note, net.note
    assert abs(net.value_ns) > 0.1, net.value_ns
    # The gathered value IS the difference of the two crossings at one t_RF.
    assert net.value_ns == pytest.approx(
        _e.mp_early_fall_b * 5.0 - _e.mp_late_rise * 5.0)
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

    Read on P->P, where the device half is the 8 ns and the leg adds the spread of the two
    peripherals' detections of the clock: of one Manager edge at zero UIs, at that edge's one
    slew factor (`_one_edge`), and of two edges with the UI, each at its corner's own.
    """
    kw = dict(Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2", bus_length_cm=0.0,
              Per_t_DZ_max_ns=10.0, Per_t_ZD_ns=2.0)
    forced = compute(CalcInputs(handover_UIs=0.0, **kw)).breakdowns["PP_contention"]
    # The DEVICE half of the deficit is the 8 ns the note is about: t_DZ,max 10 against
    # t_ZD,min 2. On top of it sits the crossing spread — the acquirer B's early detection
    # against the releaser A's late one, both of ONE clock edge from one Manager pin, so at
    # its one slew factor (`_one_edge`), each at its own supply on the leg's assignment.
    _e = CalcInputs(**kw).envelope(forced.supplies)
    _trf = CalcInputs(**kw).tRF_Man_CLK_ns
    spread = _one_edge(CalcInputs(**kw), _e.sigma_pm_hold_rf_clk_b * _trf,
                       _e.sigma_pm_setup_clk_dgnd_pos * _trf)
    assert forced.margin_ns == pytest.approx(-8.0 + spread)
    assert forced.F_max_MHz == 0.0, "no clock rate fixes a zero-UI deficit"

    # With the scheduled UI it passes, and now the rate DOES matter: the UI has to
    # cover the whole deficit, device half and crossing together. The UI puts the acquirer's
    # turn-on on the falling edge after the releaser's, detected at V_IL, so each detection is
    # at its corner's own slew factor, which the envelope's coefficients carry.
    ok = compute(CalcInputs(handover_UIs=1.0, F_CLK_target_MHz=12.288,
                            **kw)).breakdowns["PP_contention"]
    _e = CalcInputs(**kw).envelope(ok.supplies)
    spread = (_e.mp_early_fall_b - _e.sigma_pm_setup_clk_dgnd_pos) * _trf
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


def test_every_spec_source_has_a_handover_allocation():
    """Each source in `SPEC_SOURCES` has its own default: PHY1 and the EDE source 0 UIs,
    PHY2, the proposed revision and SoundWire 1. A source with no entry raises KeyError
    rather than defaulting to one UI."""
    want = {"SWI3S_PHY1": 0.0, "SWI3S_PHY2": 1.0, "SWI3S_PHY2_PROP": 1.0,
            "SWI3S_PHY2_EDE": 0.0, "SW13_1V2": 1.0}
    assert set(want) == set(SPEC_SOURCES)
    for spec, n in want.items():
        assert default_handover_UIs(spec, spec) == n, spec
    with pytest.raises(KeyError):
        default_handover_UIs("SWI3S_PHY3", "SWI3S_PHY2")  # type: ignore[arg-type]


def test_a_soundwire_side_flags_the_handover_assumption():
    """SoundWire does not allocate SWI3S handover UIs, so a contention margin on a bus with a
    SoundWire side rests on an assumption that must be stated, not buried: the SWI3S side's
    allocation on a mixed bus (`HANDOVER_CROSS_SPEC_NOTE`), the count as given on a SoundWire
    pair (`HANDOVER_SW_PAIR_NOTE`)."""
    r = compute(_mixed("SWI3S_PHY2", "SW13_1V2"))
    assert any("handover-UI count" in c for c in r.caveats), r.caveats
    # An all-SWI3S bus makes no such assumption.
    assert not any("handover-UI count" in c
                   for c in compute(CalcInputs()).caveats)
    # A SoundWire pair has no SWI3S side whose allocation could apply.
    r = compute(_mixed("SW13_1V2", "SW13_1V2"))
    assert r.caveats == (THRESHOLD_NOTE, TRF_NOTE, HANDOVER_SW_PAIR_NOTE), r.caveats
    assert "SWI3S side" not in HANDOVER_SW_PAIR_NOTE


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
    # close.
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

    Like the launch selector, it does not route through `_on_spec_changed`,
    which calls `_reset_to_defaults` and would discard the target rate.
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
    # selected PHYs imply.
    d.pop("handover_ui")
    tv3 = _view()
    tv3.set_from_dict(d)
    assert tv3._ho_ui_check.isChecked(), "legacy PHY2 workspace implied one UI"


# ---------------------------------------------------------------------------
# 10. Crossing symbols
# ---------------------------------------------------------------------------


def test_a_crossing_symbol_carries_one_coefficient_wherever_it_appears():
    """One name on two legs at one supply assignment asserts one quantity, so it must be one
    number.

    A crossing's coefficient is a function of its devices' supplies, so the rule holds among
    the legs evaluated at one assignment, and it is checked at EVERY assignment
    (`CalcInputs.supply_corners`), not only at those `compute` reports, so a name that stands
    for two devices' quantities fails wherever their supplies differ.

    `X_PM,rr,late · Man_t_RF,CLK` on PM_setup is the clock addend of Sigma_cross,PM,setup,
    whose ground-shift polarity is chosen jointly with the data lane and flips once the Per
    DATA lane is the slower one or the two devices' ramp shapes differ. A releasing
    peripheral's lone clock detection on a contention leg is always cornered at the raised
    threshold, so it carries a different coefficient there and a different name,
    `X_MP,rise,late`, whose value it equals.

    Swept where the selections flip: unequal per-lane t_RF in both directions, both ramp
    shapes on each device independently, noise, supply and slew tolerance, and the EDE and
    zero-UI configurations that change which terms appear.
    """
    from swi3s_studio.timing.calculator import _compute_at
    lanes = ((5.0, 5.0, 5.0), (3.0, 5.0, 8.3), (8.3, 6.0, 3.0))
    shapes = ("linear", "exp")
    extras = ({}, {"Man_ede": True, "Per_ede": True}, {"handover_UIs": 0.0},
              {"pp_placement": "driver_far"})
    for (clk, mdata, pdata), ms, ps, a, d, t, kw in itertools.product(
            lanes, shapes, shapes, (0.0, 0.10), (0.0, 0.05), (0.0, 0.05), extras):
        inp = CalcInputs(tRF_Man_CLK_ns=clk, tRF_Man_DATA_ns=mdata, tRF_Per_DATA_ns=pdata,
                         Man_shape=ms, Per_shape=ps, V_noise_pp_frac=a, V_SEOS_tol_frac=d,
                         tRF_tolerance_frac=t, **kw)
        for sup in inp.supply_corners():
            for (sym, lane), by_leg in _crossing_coefficients(
                    _compute_at(inp, sup).breakdowns).items():
                vals = list(by_leg.values())
                assert all(v == pytest.approx(vals[0], rel=1e-9) for v in vals), (
                    f"{sym} · {lane} carries {by_leg} at supplies {sup}, t_RF "
                    f"{clk}/{mdata}/{pdata}, shapes {ms}/{ps}, alpha {a}, delta {d}, tau {t}, "
                    f"{kw}: one symbol, several values — the symbols must split")


def test_every_lone_clock_detection_is_an_x_mp_crossing():
    """A single crossing is named for its direction, driver then receiver: the Manager drives
    the clock and a Peripheral receives it, so a Peripheral's lone detection of it is `X_MP`,
    `X_MP,A` / `X_MP,B` at Peripheral A / B on a P->P leg (`clock_detection_symbol`). `X_PM`
    on `Man_t_RF,CLK` is only a Sigma_cross,PM sum's clock addend, shown beside its data
    addend on `Per_t_RF,DATA` with the sum's case letters: PM_setup's, and
    Sigma_cross,PM,hold's same-polarity cases, `rr` / `ff`, on both lanes. Every other
    clock-lane crossing at a Peripheral is a lone detection: the contention legs', keeper_Per's,
    the P->P data legs', and the clock lane of Sigma_cross,PM,hold's rf and fr cases.

    A lone detection takes the M->P symbol whose coefficient it equals, so each `X_MP` clock
    symbol has one coefficient across the legs at every supply assignment
    (`CalcInputs.supply_corners`). Swept over both spec
    families on either side, EndDriveEarly on either side, zero, one and two handover UIs,
    linear and exponential ramps and a pinned P->P placement."""
    from types import SimpleNamespace

    from swi3s_studio.timing.calculator import _compute_at

    specs = ("SWI3S_PHY2", "SWI3S_PHY2_EDE", "SW13_1V2")
    ramps = ({}, {"Man_shape": "exp", "Per_shape": "exp", "V_OH_min_frac": 0.85,
                  "V_OL_max_frac": 0.25})
    pm_legs = ("PM_setup", "PM_setup_ho", "PM_hold", "PM_hold_ho")
    lone_on: dict[str, set[str]] = {}
    addends: set[str] = set()

    def _check(breakdowns, where):
        for leg, b in breakdowns.items():
            data_heads = {t.symbol.partition(" · ")[0] for t in b.terms
                          if t.symbol.endswith(" · Per_t_RF,DATA")}
            for t in b.terms:
                if not t.symbol.endswith(" · Man_t_RF,CLK") or "X_" not in t.symbol:
                    continue
                head = t.symbol.partition(" · ")[0]
                gathered = head.startswith("(")
                for sym, _lane in _crossing_coefficients({leg: SimpleNamespace(terms=[t])}):
                    if not sym.startswith("X_PM"):
                        if sym.startswith("X_MP"):
                            lone_on.setdefault(sym, set()).add(leg)
                        continue
                    qual = sym.split(",")[1:-1]
                    case = [q for q in qual if q in ("rr", "ff")]
                    addend = (not gathered and leg in pm_legs and (
                        (leg.startswith("PM_setup") and any(d.startswith("X_PM,")
                                                            for d in data_heads))
                        or (case and f"X_PM,{case[0]},early" in data_heads)))
                    assert addend, (
                        f"{where} {leg}: {t.symbol} is a lone clock detection named X_PM; "
                        "a Peripheral's detection of the Manager's clock is X_MP")
                    addends.add(sym)

    configs = [dict(Man_spec=man, Per_spec=per, Man_ede=me, Per_ede=pe, handover_UIs=n, **ramp)
               for man, per, (me, pe), n, ramp in itertools.product(
                   specs, specs, ((False, False), (True, False), (False, True), (True, True)),
                   (0.0, 1.0, 2.0), ramps)]
    configs += [{"pp_placement": "driver_far"}, {"pp_placement": "both_near", "handover_UIs": 0.0},
                # Sigma_cross,PM,hold's same-polarity cases, rr and ff, at zero noise.
                {"V_noise_pp_frac": 0.0, "V_SEOS_tol_frac": 0.0, "tRF_tolerance_frac": 0.0,
                 "tRF_Man_CLK_ns": 3.0, "tRF_Man_DATA_ns": 3.0, "tRF_Per_DATA_ns": 8.3}]
    for kw in configs:
        inp = CalcInputs(**kw)
        where = str(kw)
        _check(compute(inp).breakdowns, where)
        for sup in inp.supply_corners():
            breakdowns = _compute_at(inp, sup).breakdowns
            _check(breakdowns, f"{where} supplies {sup}")
            for (sym, lane), by_leg in _crossing_coefficients(breakdowns).items():
                if lane != "Man_t_RF,CLK" or not sym.startswith("X_MP"):
                    continue
                vals = list(by_leg.values())
                assert all(v == pytest.approx(vals[0], rel=1e-9) for v in vals), (
                    f"{sym} · {lane} carries {by_leg} at {where} supplies {sup}: "
                    "one symbol, several values")

    # The sweep reaches every lone detection's name on every leg kind that carries one, and
    # each Sigma_cross,PM clock addend.
    want = {"X_MP,rise,late", "X_MP,fall,late", "X_MP,fall,early", "X_MP,rise,early",
            "X_MP,A,rise,late", "X_MP,A,fall,late", "X_MP,A,rise,early", "X_MP,A,fall,early",
            "X_MP,A,rise,slow,early", "X_MP,A,fall,slow,early",
            "X_MP,B,rise,late", "X_MP,B,fall,late",
            "X_MP,B,fall,early", "X_MP,B,rise,early", "X_MP,B,fall,slow,early",
            "X_MP,B,rise,slow,early"}
    assert want <= lone_on.keys(), sorted(want - lone_on.keys())
    for sym in ("X_MP,fall,early", "X_MP,rise,early"):
        assert {"MP_setup", "MP_contention", "PM_hold", "PM_hold_ho",
                "keeper_Per"} <= lone_on[sym], (sym, lone_on[sym])
    # B's early clock detection on PP_setup is the P->P acquirer's, one symbol on both legs.
    for sym in ("X_MP,B,fall,early", "X_MP,B,rise,early"):
        assert {"PP_setup", "PP_contention"} <= lone_on[sym], (sym, lone_on[sym])
    assert {"X_PM,rr,late", "X_PM,rf,late", "X_PM,fr,late", "X_PM,rr,early",
            "X_PM,ff,early"} <= addends, addends


def _sw_anchor_closed_form(side, corner, rising, clk_rising, shape, v_oh, v_ol, k, s, k0, s0):
    """A SoundWire launch's anchor correction, from first principles: the anchor (the data
    ramp's V_OL crossing rising, V_OH falling) at the operating slews k, s, less the
    equivalent's placement on linear ramps at 0.20 / 0.80 at its slews k0, s0. Max corner:
    t_OV from the clock's 65 % point (35 % falling) to 80 % of the data transition (the level
    0.80 rising, 0.20 falling); min corner: t_ZD_Data from the clock's 50 % point to the data
    ramp's start. On a rail of 1, a level v is reached v/swing (rising) or (1 − v)/swing
    (falling) into a linear ramp, −log_4(1 − v) or −log_4(v) into an exponential one."""
    swing = v_oh - v_ol

    def at(level, r):
        if shape == "linear":
            return (level if r else 1.0 - level) / swing
        return -math.log(1.0 - level if r else level, 4.0)

    def from_anchor(v, r):
        return at(v if r else 1.0 - v, r) - at(v_ol if r else v_oh, r)

    man = side == "Man"
    if corner == "max":
        return (from_anchor(0.65, clk_rising) * k * man - from_anchor(0.80, rising) * s
                - (0.75 * k0 * man - s0))
    return (from_anchor(0.50, clk_rising) * k * man + at(v_ol if rising else v_oh, rising) * s
            - (0.5 * k0 * man + s0 / 3.0))


@pytest.mark.parametrize("v_oh, v_ol", [(0.80, 0.20), (0.80, 0.15), (0.85, 0.25), (0.90, 0.30)])
def test_every_soundwire_anchor_is_its_launchs_placement_at_the_edges_it_is_drawn_on(v_oh,
                                                                                      v_ol):
    """A SoundWire launch's `,anchor` term is the anchor of the data edge it launches, on that
    edge's polarity, from the Manager clock edge it is launched from, on that edge's
    (`sw13_anchor_correction`): a falling launch's anchor is placed on the falling ramp. Read
    against the timeline: the data ramp the leg's anchor event lies on gives the data polarity,
    and the clock ramp its launch span is drawn from the clock's. Checked on every leg with a
    `,anchor` term against `_sw_anchor_closed_form`, at V_OH + V_OL = 1 and away from it, on
    both shapes, with EndDriveEarly and handover UIs."""
    from swi3s_studio.timing.calculator import _sw_anchor_slews, sw_anchor_ns
    from swi3s_studio.timing.timeline import leg_timeline
    seen = set()
    for (man, per), shape, (me, pe), n in itertools.product(
            _SW_PAIRS, ("linear", "exp"), ((False, False), (True, True)), (0.0, 1.0, 2.0)):
        inp = dc_replace(_mixed(man, per), Man_shape=shape, Per_shape=shape, Man_ede=me,
                         Per_ede=pe, handover_UIs=n, V_OH_min_frac=v_oh, V_OL_max_frac=v_ol)
        res = compute(inp).breakdowns
        for leg, b in res.items():
            if not any(",anchor" in t.symbol for t in b.terms):
                continue
            tl = leg_timeline(inp, leg)
            at = dc_replace(inp, fast_data_edge=b.fast_data_edge)
            for sp in tl.spans:
                if sp.term is None or ",anchor" not in sp.label:
                    continue
                term = b.terms[sp.term]
                row_sym, _, edge = term.symbol.partition(",anchor")
                side = row_sym[:3]
                (row_sp,) = [r for r in tl.spans if r.label == row_sym and r.end == sp.start]
                row = b.terms[row_sp.term]
                corner = "max" if row.op == "-" else "min"
                on_fast = corner == "max" and f"{side} DATA" in b.fast_data_edge
                assert edge.endswith(",fast") is on_fast, (leg, term.symbol)
                edge = edge.removesuffix(",fast")
                rising = tl.ramp(tl.event(sp.end).ramp).rising
                clk = tl.ramp(row_sp.view[0])
                assert clk.signal == "clock", (leg, row_sp.view)
                letters = f",{'r' if clk.rising else 'f'}{'r' if rising else 'f'}"
                assert edge in ("", letters if side == "Man"
                                else ",rise" if rising else ",fall"), (leg, term.symbol)
                spec = inp.Man_spec if side == "Man" else inp.Per_spec
                k0, s0 = sw13_equivalent_slews(spec, side, corner=corner)
                k, s = _sw_anchor_slews(at, side, corner)
                want = _sw_anchor_closed_form(side, corner, rising, clk.rising, shape, v_oh,
                                              v_ol, k, s, k0, s0)
                sign = -1.0 if corner == "max" else 1.0
                assert term.value_ns == pytest.approx(sign * want, abs=1e-9), (
                    leg, term.symbol, man, per, shape, me, n, rising, clk.rising)
                assert tl.duration(sp) == pytest.approx(want, abs=1e-9), leg
                seen.add((side, corner, rising))
            # The correction itself, at every pair of polarities.
            for side, corner, r, c in itertools.product(("Man", "Per"), ("max", "min"),
                                                        (True, False), (True, False)):
                spec = inp.Man_spec if side == "Man" else inp.Per_spec
                if not is_soundwire(spec):
                    continue
                k0, s0 = sw13_equivalent_slews(spec, side, corner=corner)
                k, s = _sw_anchor_slews(at, side, corner)
                assert sw_anchor_ns(at, side, corner, rising=r, clk_rising=c) == pytest.approx(
                    _sw_anchor_closed_form(side, corner, r, c, shape, v_oh, v_ol, k, s, k0, s0),
                    abs=1e-9), (side, corner, r, c, shape)
    # Every corner of each side's anchor is drawn on a rising and a falling data edge.
    want_seen = {(side, corner, r) for side in ("Man", "Per") for corner in ("max", "min")
                 for r in (True, False)}
    assert want_seen <= seen, sorted(want_seen - seen)


def test_the_lone_clock_detections_are_named_for_the_coefficient_they_carry():
    """The contention legs' clock detections, against the legs whose names they borrow.

    `X_MP,rise,late · Man_t_RF,CLK` is MP_hold's late clock crossing on a rising sampling edge
    and `X_MP,fall,late` on a falling one; a releasing peripheral's detection of a rising or
    falling closing edge carries the same names, `X_MP,A,…` on P->P contention, whose releaser
    is Peripheral A. `X_MP,rise,early · Man_t_RF,CLK` is MP_setup's early clock crossing on a
    rising sampling edge and `X_MP,fall,early` on a falling one; an acquiring peripheral's
    detection carries the same names, and so does PM_hold's clock lane in Sigma_cross,PM,hold's
    rf case (a rising launching edge) and fr case (a falling one). On P->P contention the
    acquirer is Peripheral B, at its own supply, so its early detection is `X_MP,B,rise,early`
    or `X_MP,B,fall,early`, names it shares only with B's detections on the P->P data legs.
    The acquirer's turn-on follows the closing edge by its handover count, so at an odd count
    its polarity is the other one. Checked where PM_setup's own clock addend has flipped
    polarity, so a shared name would show two values.
    """
    def fall(rising: bool | None) -> str:
        """The polarity qualifier of an edge a breakdown reports; every leg read reports one."""
        assert rising is not None, "the leg reports no polarity"
        return "rise," if rising else "fall,"

    for kw in ({"tRF_Man_CLK_ns": 3.0, "tRF_Per_DATA_ns": 8.3},
               {"Per_shape": "exp"}, {"Man_shape": "exp"}):
        for n_ho in (1.0, 2.0):
            res = compute(CalcInputs(handover_UIs=n_ho, **kw)).breakdowns
            coeffs = _crossing_coefficients(res)
            for leg, who in (("PM_contention", "X_MP"), ("PP_contention", "X_MP,A")):
                rel = f"{who},{fall(res[leg].closing_rising)}late"
                assert leg in coeffs[(rel, "Man_t_RF,CLK")], (kw, n_ho, leg)
            mp_hold = f"X_MP,{fall(res['MP_hold'].sampling_rising)}late"
            assert "MP_hold" in coeffs[(mp_hold, "Man_t_RF,CLK")], (kw, n_ho)
            for leg, who in (("MP_contention", "X_MP"), ("PP_contention", "X_MP,B")):
                acq_rising = res[leg].closing_rising != (n_ho == 1.0)
                assert leg in coeffs[(f"{who},{fall(acq_rising)}early", "Man_t_RF,CLK")], (
                    kw, n_ho, leg)
            for leg in ("PM_hold", "PM_hold_ho"):
                launch_rising = res[leg].sampling_rising != (leg == "PM_hold_ho" and n_ho == 1.0)
                sym = "X_MP,rise,early" if launch_rising else "X_MP,fall,early"
                assert leg in coeffs[(sym, "Man_t_RF,CLK")], (kw, n_ho, leg)
            pp_legs = {"PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho", "PP_contention"}
            for dev in ("A", "B"):
                syms = {sym for sym, _lane in coeffs if sym.startswith(f"X_MP,{dev},")}
                assert syms and all(set(coeffs[(sym, "Man_t_RF,CLK")]) <= pp_legs
                                    for sym in syms), (kw, dev)
            # PM_setup's clock addend carries its case letters, the launching edge's first.
            launch = "r" if not res["PM_setup"].sampling_rising else "f"
            (pm_setup,) = [coeffs[key] for key in coeffs if key[1] == "Man_t_RF,CLK"
                           and key[0].startswith(f"X_PM,{launch}")
                           and "PM_setup" in coeffs[key]]
            assert set(pm_setup) <= {"PM_setup", "PM_setup_ho"}, (kw, pm_setup)


def test_sigma_pm_holds_same_polarity_cases_have_names_of_their_own():
    """Sigma_cross,PM,hold's both-rising case and its both-falling mirror carry clock and data
    coefficients that are not the rf and fr cases', so where one binds its terms are
    `X_PM,rr,early` or `X_PM,ff,early`, and `X_MP,rise,early · Man_t_RF,CLK` and
    `X_MP,fall,early · Man_t_RF,CLK` keep the one value the lone early detections have. rr
    is checked without noise at an assignment where it binds, on a SWI3S and on a SoundWire
    Manager; ff without noise in a window whose V_IL,max sits above mid-rail, where the
    falling launching edge binds."""
    from swi3s_studio.timing.calculator import _compute_at
    quiet = dict(V_noise_pp_frac=0.0, V_SEOS_tol_frac=0.05, tRF_tolerance_frac=0.0,
                 handover_UIs=2.0)
    cases = [(CalcInputs(tRF_Man_CLK_ns=3.0, tRF_Man_DATA_ns=3.0, tRF_Per_DATA_ns=3.0,
                         **quiet), (0.95, 0.95, 0.95), "rr"),
             (dc_replace(_mixed("SW13_1V2", "SWI3S_PHY2"), tRF_Man_CLK_ns=3.0,
                         tRF_Per_DATA_ns=5.65, **quiet), (0.95, 0.95, 0.95), "rr"),
             (CalcInputs(V_IL_max_frac=0.60, **quiet), (1.0, 1.0, 1.0), "ff")]
    for inp, sup, case in cases:
        r = _compute_at(inp, sup).breakdowns
        syms = {t.symbol for t in r["PM_hold"].terms}
        assert {f"X_PM,{case},early · Man_t_RF,CLK",
                f"X_PM,{case},early · Per_t_RF,DATA"} <= syms, (case, syms)
        assert r["PM_hold"].sampling_rising is (case == "rr"), case
        coeffs = _crossing_coefficients(r)
        for (sym, _lane), by_leg in coeffs.items():
            vals = list(by_leg.values())
            assert all(v == pytest.approx(vals[0], rel=1e-9) for v in vals), (sym, by_leg)


def test_crossing_terms_are_dimensionless_coefficients_times_a_named_lane_trf():
    """X is one lane's crossing coefficient, not a nanosecond constant.

    Sigma and Delta name the COMBINING -- a sum over legs, a difference between them --
    so on a single lane they denote nothing, and an addend carrying the sum's name is
    wrong. The composites keep them: Delta_cross,MP = X_MP,rise,late − X_MP,fall,early, and
    Sigma_cross,PM,setup = X_PM,rr,late (clock) + X_PM,rr,late (data).

    Each is a sum over legs of (dimensionless coefficient) × (that leg's t_RF), and the
    three lanes -- Man CLK, Man DATA, Per DATA -- are independent rows. The per-lane
    terms show that the quantity scales with slew, that it scales with a DIFFERENT slew
    per lane, and that on Δ_cross,MP which lane carries the late crossing SWAPS between
    setup and hold.

    IT CANNOT BE ONE PRODUCT. `Σ_cross,PM,hold × t_RF` is only equal to the envelope
    when the two lanes happen to share a t_RF, and both envelopes select their
    coefficient pair by a max (setup) or min (hold) over branches, so the coefficients
    are not even fixed. So this checks the per-lane split, and checks it where it would
    break: three unequal lanes, both ramp shapes.

    The exactness of the split is asserted inside `compute` itself; here we check the
    presentation is what it claims -- every crossing term names a lane that exists, its
    factors are its coefficients and that lane's t_RF, it scales with the slews, and the
    products still sum to the envelope the results object reports.
    """
    LANES = {"Man CLK": "tRF_Man_CLK_ns", "Man DATA": "tRF_Man_DATA_ns",
             "Per DATA": "tRF_Per_DATA_ns"}
    # Every lane is qualified, and every crossing reads its own row.
    _LANE_OF = {"Man_t_RF,CLK": "Man CLK", "Man_t_RF,DATA": "Man DATA",
                "Per_t_RF,DATA": "Per DATA"}
    # Every lane scaled by one factor, with every edge still inside the ramp setting's range:
    # each case's crossings scale alike, so each leg reports the same case.
    k = 1.1
    for shape in ("linear", "exp"):
        inp = CalcInputs(F_CLK_target_MHz=12.288, Man_shape=shape, Per_shape=shape,
                         tRF_Man_CLK_ns=7.0, tRF_Man_DATA_ns=5.0, tRF_Per_DATA_ns=4.0)
        scaled = dc_replace(inp, **{f: getattr(inp, f) * k for f in LANES.values()})
        r, r_k = compute(inp), compute(scaled)
        seen = 0
        for leg, b in r.breakdowns.items():
            # Match `"X_" in` the symbol: a gathered pair opens with a bracket, and the legs
            # that read one lane twice are among those checked.
            terms = [t for t in b.terms if "X_" in t.symbol]
            terms_k = [t for t in r_k.breakdowns[leg].terms if "X_" in t.symbol]
            assert [t.symbol for t in terms_k] == [t.symbol for t in terms], leg
            for t, t_k in zip(terms, terms_k):
                seen += 1
                assert " · " in t.symbol, f"{leg}: {t.symbol} hides its t_RF"
                lane = _LANE_OF.get(t.symbol.split(" · ")[1])
                assert lane is not None, f"{leg}: {t.symbol} names lane {lane!r}"
                # The factors are the coefficients and the named lane's t_RF, and their
                # product is the value: a coefficient times a slew, not a nanosecond constant.
                assert t.factors is not None, f"{leg}: {t.symbol} has no factors"
                *coeffs, trf = t.factors
                assert trf == getattr(inp, LANES[lane]), f"{leg}: {t.symbol} reads {trf}"
                assert abs(t.value_ns) == pytest.approx(abs(sum(coeffs) * trf), abs=1e-9), \
                    f"{leg}: {t.symbol} {t.value_ns} vs {t.factors}"
                assert t_k.value_ns == pytest.approx(k * t.value_ns, abs=1e-9), \
                    f"{leg}: {t.symbol} does not scale with its t_RF"
        assert seen >= 12, f"{shape}: only {seen} crossing terms found"

    # Δ_cross,MP's late leg swaps lane between setup and hold; that is the whole reason
    # the lane is in the symbol, so pin it rather than leaving it to the docstring.
    r = compute(CalcInputs(F_CLK_target_MHz=12.288, tRF_Man_CLK_ns=8.3,
                           tRF_Man_DATA_ns=6.0))
    late = {leg: [t.symbol for t in r.breakdowns[leg].terms
                  if "X_MP,rise,late" in t.symbol][0]
            for leg in ("MP_setup", "MP_hold")}
    assert late["MP_setup"].endswith("Man_t_RF,DATA"), late
    assert late["MP_hold"].endswith("Man_t_RF,CLK"), late

    # And the sums equal what the results object reports in ns. Both count each crossing
    # from its edge's 20 % point -- the Manager's clock reference on Man CLK, the launch's
    # anchor on a data lane -- so no reference term is gathered with them.
    for leg, attr in (("MP_setup", "delta_cross_MP_setup_ns"),
                      ("MP_hold", "delta_cross_MP_hold_ns"),
                      ("PM_setup", "sigma_pm_setup_ns"),
                      ("PM_hold", "sigma_pm_hold_ns")):
        parts = [t.value_ns for t in r.breakdowns[leg].terms
                 if "X_" in t.symbol]
        want = getattr(r, attr)
        assert sum(parts) == pytest.approx(want if leg == "PM_hold" else -want), \
            (leg, parts, want)
        assert not any("f_A" in t.symbol for t in r.breakdowns[leg].terms), leg


def test_no_term_symbol_names_a_corner_the_picker_owns():
    """A displayed inequality names PARAMETERS; the corner search names corners.

    `Man_t_DD,max` in a symbol asserts which end of the row is in force, and that is
    not the symbol's to say: `find_worst_corner_rows` chooses per inequality, and it
    chooses OPPOSITE ends of the same field for different legs -- t_DZ goes to its low
    end for the keeper and its high end for contention. So a baked-in suffix can
    contradict the number printed beside it.

    `apply_row_picks` sets every field a row binds to the same picked value, so
    `Man_t_DD,min` and `Man_t_DD,max` would display the identical number. And
    `Man_t_DZ,min` would name a corner the spec does not define at all -- t_DZ is given
    only as a maximum.

    Suffixes that are not corners are fine and are not caught here: `,anchor` is a
    SoundWire anchor correction, and a corner on a field NO row binds is a fact rather than a
    claim, because the picker never touches it. An `,anchor` term follows
    its parameter's corner, as the parameter does: the max corner's correction on a setup or
    keeper leg, the min corner's on a hold leg or a turn-on, so one symbol shows two values
    across legs at the same inputs.

    Checked over every pairing of the Manager's and the Peripheral's spec, every launch mode,
    both ramp shapes and EndDriveEarly on and off, since the symbols are built conditionally
    and a suffix can hide in a branch.
    """
    rows = {r.name for r in default_swi3s_rows()}
    assert "Man_t_DD" in rows and "Man_t_DZ" in rows, rows   # the check has teeth
    offenders = []
    for man, per, ede, mode, shape in itertools.product(
            SPEC_SOURCES, SPEC_SOURCES, (False, True), LaunchMode, ("linear", "exp")):
        r = compute(CalcInputs(
            Man_spec=man, Per_spec=per, launch_mode=mode, Man_shape=shape, Per_shape=shape,
            Man_ede=ede, Per_ede=ede, handover_UIs=0.0 if ede else 1.0))
        for leg, b in r.breakdowns.items():
            for t in b.terms:
                for row in rows:
                    if (t.symbol.startswith(f"{row},")
                            and t.symbol.split(",")[1] in ("min", "max")):
                        offenders.append(f"{man}/{per}/{mode.value}/{shape}/{leg}: {t.symbol}")
    assert not offenders, (
        "these symbols name a corner that the picker owns:\n  "
        + "\n  ".join(sorted(set(offenders))))
    from swi3s_studio.timing.calculator import sw_anchor_ns
    inp = CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")
    seen = set()
    for leg, b in compute(inp).breakdowns.items():
        at = dc_replace(inp, fast_data_edge=b.fast_data_edge)
        corner = "max" if b.is_setup or leg.startswith("keeper") else "min"
        for t in b.terms:
            if ",anchor" in t.symbol:
                seen.add(corner)
                assert abs(t.value_ns) == pytest.approx(
                    abs(sw_anchor_ns(at, t.symbol[:3], corner)), abs=1e-12), (leg, t.symbol)
    assert seen == {"max", "min"}, seen


# ---------------------------------------------------------------------------
# 11. Shared edges and polarities
# ---------------------------------------------------------------------------


def test_a_shared_clock_edge_takes_its_worse_factor_and_two_edges_their_own():
    """Where a P->P leg's two clock detections are of ONE Manager edge, both take that edge's
    one slew factor and the leg takes the factor that leaves it the less; each detection keeps
    its own crossing coefficient. Detections of two edges take their corners' own factors.

    One edge: PP_hold always; PP_hold_ho and PP_contention at zero handover UIs, and
    PP_contention only with a conventional release. At an odd handover count the acquirer's
    detection is of the other polarity (`handover_flips_edge`): A's on PP_hold_ho to B's
    sampling edge, B's on PP_contention to the closing edge, which is of either polarity. A
    hold leg's B may sample on a falling edge, at V_IL, with A on the edge its count gives
    after it (`pp_hold_edges`). Checked at every
    supply assignment, so the leg's worst over supplies and factors together is the search's.
    """
    import re

    from swi3s_studio.timing.calculator import (
        _compute_at,
        edge_factor,
        handover_flips_edge,
        pp_shares_clock_edge,
    )

    cases = [CalcInputs(), CalcInputs(handover_UIs=2.0), CalcInputs(handover_UIs=0.0),
             CalcInputs(handover_UIs=0.0, Per_ede=True),
             CalcInputs(handover_UIs=0.0, tRF_Man_CLK_ns=8.3),
             CalcInputs(handover_UIs=0.0, tRF_Man_CLK_ns=3.0),
             CalcInputs(handover_UIs=0.0, V_noise_pp_frac=0.0, V_SEOS_tol_frac=0.0),
             # A collapsed V_IH window without noise makes the net coefficient positive at
             # some supply assignments, so the worse factor is the early one there.
             CalcInputs(handover_UIs=0.0, V_noise_pp_frac=0.0, V_IH_min_frac=0.55,
                        V_IH_max_frac=0.55)]
    seen = set()
    early_worse = False
    for inp in cases:
        tau, k = inp.tRF_tolerance_frac, inp.tRF_Man_CLK_ns
        # The two edge factors, inside the setting's range (`edge_factor`).
        late, early = (edge_factor(inp, "Man CLK", r) for r in ("late", "early"))
        zero = inp.handover_UIs <= 0.0
        for sup in inp.supply_corners():
            env = inp.envelope(sup)
            early_worse |= (env.sigma_pm_hold_rf_clk / (1.0 - tau)
                            - env.mp_late_rise_b / (1.0 + tau)) > 0.0
        assert pp_shares_clock_edge(inp, "PP_hold")
        assert pp_shares_clock_edge(inp, "PP_hold_ho") == zero
        assert pp_shares_clock_edge(inp, "PP_contention") == (zero and not inp.Per_ede)
        assert not pp_shares_clock_edge(inp, "PP_setup")
        assert not pp_shares_clock_edge(inp, "PP_setup_ho")
        for sup in inp.supply_corners():
            r, env = _compute_at(inp, sup), inp.envelope(sup)
            for leg in ("PP_hold", "PP_hold_ho", "PP_contention"):
                # The credit is the early detection, the charge the late one, each without
                # the (1 ± τ) its envelope coefficient carries.
                flip = handover_flips_edge(inp)
                # B's late detection of a falling sampling edge.
                b_fall = any(re.search(r"X_MP,B,fall,(?:fast,)?late", t.symbol)
                             for t in r.breakdowns[leg].terms)
                assert not b_fall or leg != "PP_contention", leg
                assert r.breakdowns[leg].sampling_rising in (
                    (None,) if leg == "PP_contention" else (not b_fall,)), leg
                if leg == "PP_contention":
                    # The releaser on the closing edge (the one before it under EDE), the
                    # acquirer on the edge its count gives after it.
                    closing = r.breakdowns[leg].closing_rising
                    acq_rising = closing != flip
                    credit = env.sigma_pm_hold_rf_clk_b if acq_rising else env.mp_early_fall_b
                    charge = env.mp_late_rise if closing != inp.Per_ede else env.mp_late_fall
                else:
                    # A's edge is B's, flipped at an odd count on PP_hold_ho.
                    a_rising = (not b_fall) != (flip and leg == "PP_hold_ho")
                    credit = env.sigma_pm_hold_rf_clk if a_rising else env.mp_early_fall
                    charge = env.mp_late_fall_b if b_fall else env.mp_late_rise_b
                credit, charge = credit / (1.0 - tau), charge / (1.0 + tau)
                clk = [t for t in r.breakdowns[leg].terms
                       if "X_" in t.symbol and "Man_t_RF,CLK" in t.symbol]
                assert len(clk) == 1, (leg, [t.symbol for t in r.breakdowns[leg].terms])
                shared = pp_shares_clock_edge(inp, leg)
                seen.add((leg, shared))
                want = (min((credit - charge) * late, (credit - charge) * early) if shared
                        else credit * early - charge * late) * k
                assert clk[0].value_ns == pytest.approx(want, abs=1e-9), (leg, inp, sup)
    assert seen == {(leg, s) for leg in ("PP_hold_ho", "PP_contention") for s in (True, False)
                    } | {("PP_hold", True)}, seen
    assert early_worse, "no case reaches a shared edge whose worse factor is the early one"


def test_a_fractional_handover_count_is_one_whole_count_for_every_edge_rule():
    """A fractional handover count takes the nearest whole one, a tie going up, and the polarity
    and shared-edge rules read that one count."""
    from swi3s_studio.timing.calculator import (
        handover_flips_edge,
        handover_whole_UIs,
        pp_shares_clock_edge,
    )

    for n, whole in [(0.0, 0), (0.4, 0), (0.5, 1), (1.4, 1), (1.5, 2), (2.5, 3), (3.5, 4)]:
        inp = CalcInputs(handover_UIs=n)
        assert handover_whole_UIs(inp) == whole, n
        assert handover_flips_edge(inp) == (whole % 2 == 1), n
        assert pp_shares_clock_edge(inp, "PP_hold_ho") == (whole == 0), n


def test_pp_hold_ho_launches_on_the_edge_its_handover_count_gives():
    """PP_hold_ho by hand, at one supply assignment: Manager 0.95, A 1.05, B 1.05 of V_nom.

    B samples on a rising edge, detected late. A launches N_HO edges later: on a rising edge
    at an even count, detected early at V_IH (`X_MP,A,rise,early`), and on a falling one at an
    odd count, detected early at V_IL (`X_MP,A,fall,early`). Linear ramps at the defaults:
    swing 0.6, alpha 0.1, tau 0.05, every crossing counted from its ramp's 20 % point, a
    third of t_RF, and the Manager's clock swinging to 0.95.

        A, rising, early   ((0.45·1.05 − 0.1) / (0.6·0.95) − 1/3) · 0.95 = 0.30417
        A, falling, early  ((0.95 − (0.55·1.05 + 0.1)) / (0.6·0.95) − 1/3) · 0.95 = 0.13750
        B, rising, late    ((0.65·1.05 + 0.1) / (0.6·0.95) − 1/3) · 1.05 = 1.09145
        B's data, early    ((1.05 − (0.55·1.05 + 0.1)) / (0.6·1.05) − 1/3) · 0.95 = 0.24504

        margin = N_HO·UI + (c_A − c_B)·t_RF,CLK + Per_t_ZD + c_data·t_RF,DATA − Per_t_IH
    """
    from swi3s_studio.timing.calculator import _compute_at

    sup = (0.95, 1.05, 1.05)
    third = 1.0 / 3.0
    c_a_rise = ((0.45 * 1.05 - 0.1) / (0.6 * 0.95) - third) * 0.95
    c_a_fall = ((0.95 - (0.55 * 1.05 + 0.1)) / (0.6 * 0.95) - third) * 0.95
    c_b = ((0.65 * 1.05 + 0.1) / (0.6 * 0.95) - third) * 1.05
    c_data = ((1.05 - (0.55 * 1.05 + 0.1)) / (0.6 * 1.05) - third) * 0.95
    assert (round(c_a_rise, 5), round(c_a_fall, 5), round(c_b, 5), round(c_data, 5)) == (
        0.30417, 0.1375, 1.09145, 0.24504)
    for n in (1.0, 2.0, 3.0):
        inp = CalcInputs(handover_UIs=n)
        k, s, ui = inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns, inp.UI_ns()
        odd = n % 2 == 1
        c_a, a_sym = ((c_a_fall, "X_MP,A,fall,early") if odd
                      else (c_a_rise, "X_MP,A,rise,early"))
        b = _compute_at(inp, sup).breakdowns["PP_hold_ho"]
        clk = [t for t in b.terms if t.symbol.endswith("· Man_t_RF,CLK")]
        assert [t.symbol for t in clk] == [f"(X_MP,B,rise,late − {a_sym}) · Man_t_RF,CLK"], n
        assert clk[0].value_ns == pytest.approx((c_a - c_b) * k, abs=1e-9), n
        want = n * ui + (c_a - c_b) * k + inp.Per_t_ZD_ns + c_data * s - inp.Per_t_IH_max_ns
        assert b.margin_ns == pytest.approx(want, abs=1e-9), n
    # PP_hold is one edge whatever the handover count: A's detection is of B's polarity.
    for n in (1.0, 2.0):
        b = _compute_at(CalcInputs(handover_UIs=n), sup).breakdowns["PP_hold"]
        clk = next(t.symbol for t in b.terms if t.symbol.endswith("· Man_t_RF,CLK"))
        assert ("X_MP,A,fall," in clk) == ("X_MP,B,fall," in clk) == (
            b.sampling_rising is False), (n, clk)


def test_pp_hold_ho_samples_on_either_polarity_and_reports_the_worse():
    """PP_hold_ho by hand at two supply assignments (Manager, A, B at 0.95, 1.05, 1.05 and at
    1.05, 0.95, 0.95), on a window that is not symmetric about mid-rail, V_IH 0.40..0.65 and
    V_IL 0.35..0.50.

    B samples on a rising edge, detected late at V_IH,max + α, against A's falling data at
    V_IL,max + α; or on a falling edge, detected late at V_IL,min − α, against A's rising data
    at V_IH,min − α. A launches on B's polarity at an even count and the other at an odd one,
    detected early: rising at V_IH,min − α, falling at V_IL,max + α. Linear ramps, swing 0.6,
    α 0.1, τ 0.05, every crossing counted from its ramp's 20 % point. The leg is the worse
    case, and each case's symbols name its polarities.
    """
    from swi3s_studio.timing.calculator import _compute_at

    alpha, tau, third = 0.1, 0.05, 1.0 / 3.0

    def rise(v, tx, sign):
        return (v / (0.6 * tx) - third) * (1 + sign * tau)

    def fall(v, tx, sign):
        return ((tx - v) / (0.6 * tx) - third) * (1 + sign * tau)

    seen = set()
    for (m, pa, pb), n in itertools.product(((0.95, 1.05, 1.05), (1.05, 0.95, 0.95)),
                                            (1.0, 2.0, 3.0)):
        a_rise, a_fall = rise(0.40 * pa - alpha, m, -1), fall(0.50 * pa + alpha, m, -1)
        b_rise, b_fall = rise(0.65 * pb + alpha, m, +1), fall(0.35 * pb - alpha, m, +1)
        d_fall, d_rise = fall(0.50 * pb + alpha, pa, -1), rise(0.40 * pb - alpha, pa, -1)
        inp = CalcInputs(handover_UIs=n, V_IH_min_frac=0.40, V_IL_max_frac=0.50)
        k, s, ui = inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns, inp.UI_ns()
        odd = n % 2 == 1
        rest = n * ui + inp.Per_t_ZD_ns - inp.Per_t_IH_max_ns
        b_rising = rest + ((a_fall if odd else a_rise) - b_rise) * k + d_fall * s
        b_falling = rest + ((a_rise if odd else a_fall) - b_fall) * k + d_rise * s
        b = _compute_at(inp, (m, pa, pb)).breakdowns["PP_hold_ho"]
        assert b.margin_ns == pytest.approx(min(b_rising, b_falling), abs=1e-9), n
        syms = [t.symbol for t in b.terms if "X_" in t.symbol]
        if b_falling < b_rising:
            a_sym = "X_MP,A,rise,early" if odd else "X_MP,A,fall,early"
            assert syms == [f"(X_MP,B,fall,late − {a_sym}) · Man_t_RF,CLK",
                            "X_PP,B,rise,early · Per_t_RF,DATA"], (n, syms)
        else:
            a_sym = "X_MP,A,fall,early" if odd else "X_MP,A,rise,early"
            assert syms == [f"(X_MP,B,rise,late − {a_sym}) · Man_t_RF,CLK",
                            "X_PP,B,fall,early · Per_t_RF,DATA"], (n, syms)
        seen.add(b_falling < b_rising)
    assert seen == {True, False}, "both polarities bind somewhere in the sweep"


def _polarity_cases(inp, sup, n_ho):
    """Every setup and hold leg's crossing terms by hand, signed as they enter the margin, at
    each polarity of its sampling edge: {leg: {sampling_rising: ns}}.

    Linear ramps, swing 0.6, every crossing counted from its ramp's 20 % point, a third of
    t_RF, α 0.1, at supplies `sup` (Manager, Peripheral or A, B). A rising crossing is at
    V_IH and a falling one at V_IL; a late crossing pairs with an early one of the other
    polarity under one ground shift, +α with the late crossing rising and −α with it falling.
    `inp`'s t_RF rows sit inside their ranges, so each edge takes its corner's own (1 ± τ).

        M->P   the sampling edge s; the Manager's launch enters no crossing.
        P->M   the launching edge: the other polarity to s on setup, s on hold, and on
               PM_hold_ho the one an odd count flips. On setup the data crosses on either
               polarity (Sigma_cross,PM,setup's same-polarity and cross cases).
        P->P   B samples on s; A launches on the edge before, of the other polarity, on setup,
               on s on PP_hold, and on PP_hold_ho on the one its count gives.
    """
    ih_min, ih_max = inp.V_IH_min_frac, inp.V_IH_max_frac
    il_min, il_max = inp.V_IL_min_frac, inp.V_IL_max_frac
    alpha, tau, third = 0.1, inp.tRF_tolerance_frac, 1.0 / 3.0
    m, p, pb = sup
    k, d, s = inp.tRF_Man_CLK_ns, inp.tRF_Man_DATA_ns, inp.tRF_Per_DATA_ns

    def rise(v, tx, sign):
        return (v / (0.6 * tx) - third) * (1 + sign * tau)

    def fall(v, tx, sign):
        return ((tx - v) / (0.6 * tx) - third) * (1 + sign * tau)

    # A receiver's late and early crossing of each polarity: (rx supply, tx supply).
    def late(rising, rx, tx):
        return rise(ih_max * rx + alpha, tx, +1) if rising else fall(il_min * rx - alpha, tx, +1)

    def early(rising, rx, tx):
        return rise(ih_min * rx - alpha, tx, -1) if rising else fall(il_max * rx + alpha, tx, -1)

    def pm_setup(launch_rising):
        # The data on the clock's polarity at δ = ±α, and on the other polarity at the δ that
        # makes both crossings late.
        lv = ih_max if launch_rising else il_min
        x = rise if launch_rising else fall
        same = [x(lv * p + g, m, +1) * k + x(lv * m - g, p, +1) * s for g in (alpha, -alpha)]
        cross = late(launch_rising, p, m) * k + late(not launch_rising, m, p) * s
        return -max(*same, cross)

    def pm_hold(launch_rising):
        if launch_rising:
            same = rise(ih_min * p, m, -1) * k + rise(ih_min * m, p, -1) * s
        else:
            same = fall(il_max * p, m, -1) * k + fall(il_max * m, p, -1) * s
        return min(same, early(launch_rising, p, m) * k + early(not launch_rising, m, p) * s)

    def pp_hold(b_rising, a_rising, one_edge):
        a, b_ = early(a_rising, p, m) / (1 - tau), late(b_rising, pb, m) / (1 + tau)
        if one_edge:       # one edge, one factor: the worse of (1 + τ) and (1 − τ)
            clk = min((a - b_) * (1 + tau), (a - b_) * (1 - tau)) * k
        else:
            clk = (a * (1 - tau) - b_ * (1 + tau)) * k
        return clk + early(not b_rising, pb, p) * s

    odd = round(n_ho) % 2 == 1
    out = {}
    for r in (True, False):
        mp_setup = -(late(not r, p, m) * d - early(r, p, m) * k)
        mp_hold = -(late(r, p, m) * k - early(not r, p, m) * d)
        pp_setup = (-late(not r, p, m) * k + early(r, pb, m) * k
                    - late(not r, pb, p) * s)
        cases = {"MP_setup": mp_setup, "MP_setup_ho": mp_setup,
                 "MP_hold": mp_hold, "MP_hold_ho": mp_hold,
                 "PM_setup": pm_setup(not r), "PM_setup_ho": pm_setup(not r),
                 "PM_hold": pm_hold(r), "PM_hold_ho": pm_hold(r != odd),
                 "PP_setup": pp_setup, "PP_setup_ho": pp_setup,
                 "PP_hold": pp_hold(r, r, True),
                 "PP_hold_ho": pp_hold(r, r != odd, round(n_ho) == 0)}
        for leg, v in cases.items():
            out.setdefault(leg, {})[r] = v
    return out


_POLARITY_LEGS = ("MP_setup", "MP_setup_ho", "MP_hold", "MP_hold_ho", "PM_setup",
                  "PM_setup_ho", "PM_hold", "PM_hold_ho", "PP_setup", "PP_setup_ho",
                  "PP_hold", "PP_hold_ho")


def _check_polarity_cases(inp, sup, n_ho):
    """Each setup and hold leg's margin is its non-crossing terms plus the worse of its two
    hand-derived cases, and it reports that case; returns {leg: (rising case, falling case)}."""
    from swi3s_studio.timing.calculator import _compute_at

    r = _compute_at(inp, sup).breakdowns
    hand = _polarity_cases(inp, sup, n_ho)
    out = {}
    for leg in _POLARITY_LEGS:
        b = r[leg]
        rest = sum(t.value_ns for t in b.terms if "X_" not in t.symbol)
        rising, falling = hand[leg][True], hand[leg][False]
        assert b.margin_ns == pytest.approx(rest + min(rising, falling), abs=1e-9), \
            (leg, b.margin_ns, rest, rising, falling)
        first = not b.is_setup                 # the case the leg lists first
        want = first if abs(rising - falling) < 1e-9 else rising < falling
        assert b.sampling_rising is want, (leg, rising, falling)
        out[leg] = (rising, falling)
    return out


def test_every_setup_and_hold_leg_evaluates_both_sampling_polarities():
    """Every setup and hold leg by hand at one supply assignment, in a window that is not
    symmetric about mid-rail, V_IH 0.40..0.60 and V_IL 0.30..0.50, with the Peripheral's data
    slower than the clock: the falling sampling edge binds every hold leg and the rising one
    every setup leg but PM's, the case each leg does not list first. PM_setup's
    falling sampling edge binds, its rising launching edge with falling data, the cross case
    of Sigma_cross,PM,setup. PM_hold_ho's launching edge is the one its count gives after the
    sampling edge, so at an odd count its rising sampling edge binds. Each case's symbols
    name its polarities."""
    inp0 = CalcInputs(V_IH_min_frac=0.40, V_IH_max_frac=0.60, V_IL_min_frac=0.30,
                      V_IL_max_frac=0.50, tRF_Per_DATA_ns=6.0)
    from swi3s_studio.timing.calculator import _compute_at

    for n in (1.0, 2.0):
        inp = dc_replace(inp0, handover_UIs=n)
        cases = _check_polarity_cases(inp, (1.0, 1.0, 1.0), n)
        for leg, (rising, falling) in cases.items():
            binding_rising = (("setup" in leg and not leg.startswith("PM_setup"))
                              or (leg == "PM_hold_ho" and n == 1.0))
            assert (rising < falling) is binding_rising, (leg, n, rising, falling)
        r = _compute_at(inp, (1.0, 1.0, 1.0)).breakdowns
        syms = {leg: [t.symbol for t in r[leg].terms if "X_" in t.symbol] for leg in r}
        assert syms["MP_setup"] == ["X_MP,rise,early · Man_t_RF,CLK",
                                    "X_MP,fall,late · Man_t_RF,DATA"], syms["MP_setup"]
        assert syms["MP_hold"] == ["X_MP,fall,late · Man_t_RF,CLK",
                                   "X_MP,rise,early · Man_t_RF,DATA"], syms["MP_hold"]
        assert syms["PM_setup"] == ["X_PM,rf,late · Man_t_RF,CLK",
                                    "X_PM,rf,late · Per_t_RF,DATA"], syms["PM_setup"]
        assert syms["PM_hold"] == ["X_MP,fall,early · Man_t_RF,CLK",
                                   "X_PM,rise,early · Per_t_RF,DATA"], syms["PM_hold"]
        assert syms["PM_hold_ho"] == syms["PM_hold"], syms["PM_hold_ho"]
        assert syms["PP_setup"] == ["(X_MP,A,fall,late − X_MP,B,rise,early) · Man_t_RF,CLK",
                                    "X_PP,B,fall,late · Per_t_RF,DATA"], syms["PP_setup"]
        assert syms["PP_hold"] == ["(X_MP,B,fall,late − X_MP,A,fall,slow,early) · "
                                   "Man_t_RF,CLK",
                                   "X_PP,B,rise,early · Per_t_RF,DATA"], syms["PP_hold"]
    # The results report the Delta and Sigma of the case in force.
    res = compute(inp0)
    for leg, attr in (("MP_setup", "delta_cross_MP_setup_ns"),
                      ("MP_hold", "delta_cross_MP_hold_ns"),
                      ("PM_setup", "sigma_pm_setup_ns"), ("PM_hold", "sigma_pm_hold_ns")):
        parts = sum(t.value_ns for t in res.breakdowns[leg].terms if "X_" in t.symbol)
        want = getattr(res, attr)
        assert parts == pytest.approx(want if leg == "PM_hold" else -want, abs=1e-9), leg


def test_pm_setup_takes_data_of_either_polarity():
    """Sigma_cross,PM,setup by hand at one supply (1.0 for every device), linear ramps, swing
    0.6, crossings from the 20 % point, (1 + τ) = 1.05 on the late edges, α 0.1.

    Below mid-rail, V_IH 0.40..0.60 and V_IL 0.30..0.50, Man_t_RF,CLK 5 and Per_t_RF,DATA 6:
    a rising clock at 0.60 + α is (0.70/0.6 − 1/3)·1.05 = 0.875 and falling data at 0.30 − α
    is (0.80/0.6 − 1/3)·1.05 = 1.05, so the cross case is 0.875·5 + 1.05·6 = 10.675 ns,
    against 9.80 for the worst same-polarity case (both falling, 0.70·5 + 1.05·6).

    In the default window, V_IH 0.45..0.65 and V_IL 0.35..0.55, the cross case is
    2·0.9625·5 = 9.625 ns against 7.875 for both rising, its ground shift not cancelling.
    Without noise the two tie at 2·(0.65/0.6 − 1/3)·1.05·5 = 7.875 ns, and the leg reports
    the same-polarity case, both crossings `X_PM,rr,late`."""
    from swi3s_studio.timing.calculator import _compute_at

    sup = (1.0, 1.0, 1.0)
    third = 1.0 / 3.0

    def late_rise(v):
        return (v / 0.6 - third) * 1.05

    def late_fall(v):
        return ((1.0 - v) / 0.6 - third) * 1.05

    shifted = CalcInputs(V_IH_min_frac=0.40, V_IH_max_frac=0.60, V_IL_min_frac=0.30,
                         V_IL_max_frac=0.50, tRF_Per_DATA_ns=6.0)
    quiet = CalcInputs(V_noise_pp_frac=0.0)
    for inp, want, same, syms in (
            (shifted, late_rise(0.70) * 5 + late_fall(0.20) * 6,
             late_fall(0.40) * 5 + late_fall(0.20) * 6,
             ["X_PM,rf,late · Man_t_RF,CLK", "X_PM,rf,late · Per_t_RF,DATA"]),
            (CalcInputs(), late_rise(0.75) * 5 + late_fall(0.25) * 5,
             late_rise(0.75) * 5 + late_rise(0.55) * 5,
             ["X_PM,rf,late · Man_t_RF,CLK", "X_PM,rf,late · Per_t_RF,DATA"]),
            (quiet, 2 * late_rise(0.65) * 5, 2 * late_rise(0.65) * 5,
             ["X_PM,rr,late · Man_t_RF,CLK", "X_PM,rr,late · Per_t_RF,DATA"])):
        r = _compute_at(inp, sup)
        for leg in ("PM_setup", "PM_setup_ho"):
            b = r.breakdowns[leg]
            crossing = -sum(t.value_ns for t in b.terms if "X_" in t.symbol)
            assert crossing == pytest.approx(want, abs=1e-9), (leg, crossing, want)
            assert [t.symbol for t in b.terms if "X_" in t.symbol] == syms, leg
            assert b.sampling_rising is False, leg
        assert r.sigma_pm_setup_ns == pytest.approx(want, abs=1e-9)
        assert want >= same - 1e-12
    assert late_rise(0.70) * 5 + late_fall(0.20) * 6 == pytest.approx(10.675)
    assert late_fall(0.40) * 5 + late_fall(0.20) * 6 == pytest.approx(9.80)
    assert late_rise(0.75) * 10 == pytest.approx(9.625)


def test_the_two_sampling_polarities_tie_in_a_symmetric_window_at_equal_supplies():
    """In the default window, symmetric about mid-rail, at one supply for every device, each
    setup and hold leg's two sampling polarities give one margin, and the leg reports the
    case it lists first: a rising sampling edge on hold, a falling one on setup."""
    for n in (0.0, 1.0, 2.0):
        for sup in ((1.0, 1.0, 1.0), (0.95, 0.95, 0.95)):
            cases = _check_polarity_cases(CalcInputs(handover_UIs=n), sup, n)
            for leg, (rising, falling) in cases.items():
                assert rising == pytest.approx(falling, abs=1e-9), (leg, n, sup)


def test_pm_hold_ho_detects_the_falling_edge_an_odd_handover_count_gives():
    """PM_hold_ho by hand at one supply assignment (Manager 0.95, Peripheral 1.05).

    The Peripheral launches on the edge its count gives after the Manager's sampling edge, so
    at an odd count a rising sampling edge has a falling launching edge, detected early at
    V_IL,max + α from the Manager's 0.95 rail on a fast edge,

        ((0.95 − (0.55·1.05 + 0.1)) / (0.6·0.95) − 1/3) · 0.95 = 0.13750

    against the Manager's detection of rising data at V_IH,min − α. At an even count the leg
    reads PM_hold's case.
    """
    from swi3s_studio.timing.calculator import _compute_at

    sup = (0.95, 1.05, 1.05)
    c_fall = ((0.95 - (0.55 * 1.05 + 0.1)) / (0.6 * 0.95) - 1.0 / 3.0) * 0.95
    assert round(c_fall, 5) == 0.1375
    for n in (1.0, 2.0, 3.0):
        inp = CalcInputs(handover_UIs=n)
        env = inp.envelope(sup)
        r = _compute_at(inp, sup).breakdowns
        ho, hold = r["PM_hold_ho"], r["PM_hold"]
        clk = [t for t in ho.terms if t.symbol.endswith("· Man_t_RF,CLK")]
        data = [t for t in ho.terms if t.symbol.endswith("· Per_t_RF,DATA")]
        if n % 2 == 1:
            assert ho.sampling_rising is True, n
            assert [t.symbol for t in clk] == ["X_MP,fall,early · Man_t_RF,CLK"], n
            assert clk[0].factors[0] == pytest.approx(c_fall, abs=1e-12), n
            assert data[0].factors[0] == pytest.approx(env.sigma_pm_hold_fr_data, abs=1e-12)
        else:
            same = [t.symbol for t in hold.terms if "X_" in t.symbol]
            assert [t.symbol for t in clk + data] == same, n
        rest = sum(t.value_ns for t in ho.terms if "X_" not in t.symbol)
        assert ho.margin_ns == pytest.approx(rest + sum(t.value_ns for t in clk + data)), n
    _check_polarity_cases(CalcInputs(handover_UIs=1.0), sup, 1.0)


def test_the_contention_acquirer_detects_the_edge_its_handover_count_gives():
    """MP_contention's and PP_contention's acquirer turns on N_HO UIs after the closing edge,
    on the polarity that count gives: the closing edge's at an even count and the other at an
    odd one, detected early at V_IL when falling (`mp_early_fall`, B's `mp_early_fall_b`) and
    at V_IH when rising (`sigma_pm_hold_rf_clk`, `_b`). Checked at one supply assignment
    against the envelope, at the closing edge each leg reports."""
    from swi3s_studio.timing.calculator import _compute_at, _per_clk_cross_parts

    sup = (0.95, 1.05, 1.0)
    for n in (1.0, 2.0, 3.0):
        inp = CalcInputs(handover_UIs=n)
        env = inp.envelope(sup)
        odd = n % 2 == 1
        r = _compute_at(inp, sup).breakdowns
        acq_rising = r["MP_contention"].closing_rising != odd
        mp = next(t for t in r["MP_contention"].terms if t.symbol.startswith("X_MP"))
        assert mp.symbol == ("X_MP,rise,early" if acq_rising else "X_MP,fall,early") + \
            " · Man_t_RF,CLK"
        assert mp.factors[0] == pytest.approx(
            (env.sigma_pm_hold_rf_clk if acq_rising else env.mp_early_fall), abs=1e-12), n
        pp_b = r["PP_contention"]
        acq_rising = pp_b.closing_rising != odd
        pp = next(t for t in pp_b.terms if "X_MP,B" in t.symbol)
        rel = "X_MP,A,rise,late" if pp_b.closing_rising else "X_MP,A,fall,late"
        acq = "X_MP,B,rise,early" if acq_rising else "X_MP,B,fall,early"
        assert pp.symbol == f"({rel} − {acq}) · Man_t_RF,CLK", n
        want = _per_clk_cross_parts(inp, env, corner="early", device="B", rising=acq_rising)
        assert want[1] == pytest.approx(
            env.sigma_pm_hold_rf_clk_b if acq_rising else env.mp_early_fall_b, abs=1e-12)
        assert abs(pp.factors[1]) == pytest.approx(want[1], abs=1e-12), n


def _contention_cases(inp, sup, n_ho):
    """The contention legs' clock detections by hand, signed as they enter the margin, at
    each polarity of the closing edge: {leg: {closing_rising: ns}}. The `_polarity_cases`
    construction: linear ramps, swing 0.6, crossings from the 20 % point, α 0.1.

    The releasing peripheral detects the closing edge late (with an EndDriveEarly release the
    edge before it, of the other polarity), at V_IH,max + α rising or V_IL,min − α falling; the
    acquiring one detects the edge N_HO UIs later early, at V_IH,min − α rising or
    V_IL,max + α falling, the closing edge's polarity at an even count. At zero UIs with a
    conventional release P->P's two detections are of one edge and take its one factor, the
    worse of (1 + τ) and (1 − τ).

    keeper_Per with a conventional release: its Peripheral detects the edge opening the UI,
    of the other polarity to the closing edge, late, and the closing edge early, at its own
    supply; the spacing enters as the early detection less the late one.
    """
    ih_min, ih_max = inp.V_IH_min_frac, inp.V_IH_max_frac
    il_min, il_max = inp.V_IL_min_frac, inp.V_IL_max_frac
    alpha, tau, third = 0.1, inp.tRF_tolerance_frac, 1.0 / 3.0
    m, p, pb = sup
    k = inp.tRF_Man_CLK_ns

    def raw_late(rising, rx):
        if rising:
            return (ih_max * rx + alpha) / (0.6 * m) - third
        return (m - (il_min * rx - alpha)) / (0.6 * m) - third

    def raw_early(rising, rx):
        if rising:
            return (ih_min * rx - alpha) / (0.6 * m) - third
        return (m - (il_max * rx + alpha)) / (0.6 * m) - third

    odd = round(n_ho) % 2 == 1
    out: dict = {}
    for c in (True, False):
        rel, acq = c != inp.Per_ede, c != odd
        one_edge = round(n_ho) == 0 and not inp.Per_ede
        if one_edge:
            net = raw_early(acq, pb) - raw_late(rel, p)
            pp = min(net * (1 + tau), net * (1 - tau)) * k
        else:
            pp = (raw_early(acq, pb) * (1 - tau) - raw_late(rel, p) * (1 + tau)) * k
        for leg, v in (("MP_contention", raw_early(acq, p) * (1 - tau) * k),
                       ("PM_contention", -raw_late(rel, p) * (1 + tau) * k),
                       ("PP_contention", pp)):
            out.setdefault(leg, {})[c] = v
        if not inp.Per_ede:
            out.setdefault("keeper_Per", {})[c] = (
                raw_early(c, p) * (1 - tau) - raw_late(not c, p) * (1 + tau)) * k
    return out


def _check_contention_cases(inp, sup, n_ho):
    """Each contention leg's margin, and keeper_Per's with a conventional release, is its
    other terms plus the worse of its two hand-derived closing-edge cases, and it reports that
    case, the rising one on a tie; returns
    {leg: (rising case, falling case)}."""
    from swi3s_studio.timing.calculator import _compute_at

    r = _compute_at(inp, sup).breakdowns
    hand = _contention_cases(inp, sup, n_ho)
    out = {}
    for leg, cases in hand.items():
        b = r[leg]
        rest = sum(t.value_ns for t in b.terms
                   if "X_" not in t.symbol or t.symbol.startswith("X_K"))
        rising, falling = cases[True], cases[False]
        assert b.margin_ns == pytest.approx(rest + min(rising, falling), abs=1e-9), \
            (leg, b.margin_ns, rest, rising, falling)
        want = True if abs(rising - falling) < 1e-9 else rising < falling
        assert b.closing_rising is want, (leg, rising, falling)
        assert b.sampling_rising is None, leg
        out[leg] = (rising, falling)
    return out


def test_every_contention_leg_evaluates_both_closing_edge_polarities():
    """The contention legs by hand in the window V_IH 0.40..0.60, V_IL 0.30..0.50, not
    symmetric about mid-rail. A falling crossing at V_IL,min − α = 0.20 is later than a rising
    one at V_IH,max + α = 0.70 (1.05 against 0.875 of t_RF from the 20 % point at one supply),
    so a releasing peripheral's falling detection binds PM_contention. A rising early
    detection at 0.30 (0.158) is a smaller credit than a falling one at 0.60 (0.317), so
    MP_contention binds where its acquirer's edge rises: the rising closing edge at an even
    count and the falling one at an odd count. keeper_Per binds at a rising closing edge: the
    opening edge falls, detected late at 0.20 (1.05), and the closing edge rises, detected
    early at 0.30 (0.158), against 0.875 and 0.317 the other way. Each case's symbols name its
    polarities."""
    from swi3s_studio.timing.calculator import _compute_at

    inp0 = CalcInputs(V_IH_min_frac=0.40, V_IH_max_frac=0.60, V_IL_min_frac=0.30,
                      V_IL_max_frac=0.50, tRF_Per_DATA_ns=6.0)
    third = 1.0 / 3.0
    assert (0.70 / 0.6 - third) * 1.05 == pytest.approx(0.875)
    assert ((1 - 0.20) / 0.6 - third) * 1.05 == pytest.approx(1.05)
    assert (0.30 / 0.6 - third) * 0.95 == pytest.approx(0.95 / 6)
    assert ((1 - 0.60) / 0.6 - third) * 0.95 == pytest.approx(0.95 / 3)
    for n in (0.0, 1.0, 2.0):
        inp = dc_replace(inp0, handover_UIs=n)
        for sup in ((1.0, 1.0, 1.0), (0.95, 1.05, 1.0)):
            cases = _check_contention_cases(inp, sup, n)
            rising, falling = cases["PM_contention"]
            assert falling < rising, (n, sup)
            rising, falling = cases["MP_contention"]
            assert (falling < rising) is (n == 1.0), (n, sup)
            rising, falling = cases["keeper_Per"]
            assert rising < falling, (n, sup)
        r = _compute_at(inp, (1.0, 1.0, 1.0)).breakdowns
        syms = {leg: [t.symbol for t in r[leg].terms if "X_" in t.symbol]
                for leg in ("MP_contention", "PM_contention", "keeper_Per")}
        assert syms["PM_contention"] == ["X_MP,fall,late · Man_t_RF,CLK"], syms
        assert syms["MP_contention"] == ["X_MP,rise,early · Man_t_RF,CLK"], syms
        assert syms["keeper_Per"] == ["X_K,fall,late · Per_t_RF,DATA",
                                      "(X_MP,fall,late − X_MP,rise,early) · Man_t_RF,CLK"], syms
    # At one supply the falling detection's late corner charges 1.05·5 = 5.25 ns.
    b = _compute_at(inp0, (1.0, 1.0, 1.0)).breakdowns["PM_contention"]
    clk = next(t for t in b.terms if t.symbol.startswith("X_MP"))
    assert clk.value_ns == pytest.approx(-5.25, abs=1e-9)
    # Under an EndDriveEarly release the releaser detects the edge opening its UI, of the
    # other polarity to the closing edge.
    _check_contention_cases(dc_replace(inp0, Per_ede=True, handover_UIs=0.0),
                            (1.0, 1.0, 1.0), 0.0)


def test_the_two_closing_edge_polarities_tie_in_a_symmetric_window_at_equal_supplies():
    """In the default window, symmetric about mid-rail, at one supply for every device, each
    contention leg's two closing-edge polarities give one margin, and so do keeper_Per's with
    a conventional release, and the leg reports the rising one, the case it lists first."""
    for n in (0.0, 1.0, 2.0):
        for sup in ((1.0, 1.0, 1.0), (0.95, 0.95, 0.95)):
            for kw in ({}, {"Per_ede": True}):
                inp = CalcInputs(handover_UIs=n, **kw)
                cases = _check_contention_cases(inp, sup, n)
                for leg, (rising, falling) in cases.items():
                    assert rising == pytest.approx(falling, abs=1e-9), (leg, n, sup, kw)


# ---------------------------------------------------------------------------
# 12. EndDriveEarly
# ---------------------------------------------------------------------------


def test_ede_replaces_the_release_with_one_before_the_closing_edge():
    """EDE's content, as one assertion: the releasing side's t_DZ is replaced by its EDE
    release, which sits BEFORE the closing edge, so in an end-of-UI-referenced inequality it
    is a negative effective t_DZ and no new inequality is needed.
    """
    from swi3s_studio.timing.calculator import _ede_release_vs_edge_ns
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=0.0, bus_length_cm=0.0,
              Man_t_DZ_max_ns=10.0, Per_t_ZD_ns=0.0, Man_t_EDE_ns=5.0)
    off = compute(CalcInputs(Man_ede=False, **kw)).breakdowns["MP_contention"]
    inp = CalcInputs(Man_ede=True, **kw)
    on = compute(inp).breakdowns["MP_contention"]
    release = _ede_release_vs_edge_ns(inp, "Man")
    assert release < 0, "an early release is before the closing edge"
    assert on.margin_ns - off.margin_ns == pytest.approx(10.0 - release, abs=1e-9)
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
    # Right column = EDE on the revision's own values.
    assert (tv._min[("proposed", "Man_t_DD")].value(),
            tv._max[("proposed", "Man_t_DD")].value()) == (9.0, 23.0)
    for name in ("Man_t_EDE", "Per_t_EDE"):
        assert (tv._min[("proposed", name)].value(),
                tv._max[("proposed", name)].value()) == (3.0, 7.0), name

    # BOTH sides on the EDE source release by their EDE Delay, on the EDE column only.
    assert tv.inputs("proposed").Per_ede and tv.inputs("proposed").Man_ede
    assert not tv.inputs("spec").Man_ede and not tv.inputs("spec").Per_ede
    # And no handover UI is allocated, which is the point of the feature.
    assert tv.inputs("proposed").handover_UIs == 0.0
    # One side on EDE: only that side releases early.
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SWI3S_PHY2"))
    assert tv.inputs("proposed").Per_ede and not tv.inputs("proposed").Man_ede


def test_ede_helps_every_contention_leg_it_owns():
    """What EDE buys. The MECHANISM is asserted; where the margins land is recorded.

    * EDE MUST IMPROVE EVERY CONTENTION LEG WHOSE RELEASER HAS IT. That is a statement
      about the mechanism, it holds regardless of where the margins land, and it is what
      catches a sign error or a swapped reference. The keeper legs read the EDE Delay,min
      and the ramp to V_OH less the crossing of the keeper's threshold and the keeper demand
      instead (`test_the_ede_column_releases_both_sides_by_their_ede_delay`).
    * Which legs reach positive is a RESULT, and is recorded as one rather than
      demanded.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))
    ede = tv._per_inequality_worst("proposed")
    base = tv._per_inequality_worst("spec")

    for leg in ("MP_contention", "PM_contention", "PP_contention"):
        assert ede[leg].margin_ns > base[leg].margin_ns, \
            f"{leg}: the releasing device's change must improve the leg it owns"
        assert base[leg].margin_ns < 0, f"{leg}: the baseline should fail at zero UIs"

    # The result, recorded: all three contention legs close at zero UIs. M->P is the
    # thinnest on the analog launch, because the Manager's 23 ns launch puts its release
    # past the closing edge at the slow corner; the acquiring peripheral's own lateness
    # still covers it. P->P closing is a statement about driver current only: `PP_hold_ho`
    # still fails wherever a peripheral acquires, so the P->P handover still owes its UI.
    assert ede["MP_contention"].margin_ns == pytest.approx(1.40, abs=0.005)
    assert ede["PM_contention"].margin_ns == pytest.approx(12.69, abs=0.005)
    assert ede["PP_contention"].margin_ns == pytest.approx(6.77, abs=0.005)
    # The setup and hold legs are not owned by the releaser: EDE leaves them positive and
    # no worse than the baseline.
    for leg in ("MP_setup", "PM_setup", "PM_hold"):
        assert ede[leg].margin_ns > 0, leg
        assert ede[leg].margin_ns >= base[leg].margin_ns - 1e-9, leg


def test_the_threshold_window_is_never_collapsed_by_a_corner_pick():
    """Two receivers may sit at opposite ends of one compliance window, so the picker
    must not close it.

    A DEVICE'S THRESHOLD IS A DEVICE CORNER -- a given part's rising threshold is
    somewhere in [V_IH,min, V_IH,max] -- and that is exactly why one bus-wide value
    cannot express two of them. Binding one picked value to BOTH ends would give every
    receiver on the bus the same threshold, and a leg that CHARGES one receiver's crossing
    while CREDITING another's would see them partially cancel.

    Within one receiver what matters is the V_IH-to-V_IL separation; ACROSS two receivers it
    is the window's WIDTH, which a collapse destroys.

    Not the same question as the clock's slew: one Manager pin drives one edge, so both
    peripherals detect the same slew and collapsing THAT is correct. Independence has to
    be decided per quantity.
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
    # cancel; a collapsed window puts both at one threshold, where they do.
    e = inp.envelope()
    late, early = e.sigma_pm_setup_clk_dgnd_pos, e.sigma_pm_hold_rf_clk
    collapsed = dc_replace(inp, V_IH_min_frac=0.65, V_IH_max_frac=0.65).envelope()
    assert late == pytest.approx(collapsed.sigma_pm_setup_clk_dgnd_pos), \
        "the releaser's late crossing is already at the top of the window"
    assert early < collapsed.sigma_pm_hold_rf_clk, \
        "and the acquirer's early crossing must NOT rise with it"


def test_no_ede_row_moves_with_the_rate():
    """Every EDE column value is absolute (`_ede_ranges`), so every row, edits included, holds
    its value across a rate change."""
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))
    tv._max[("proposed", "Per_t_ZD")].setValue(7.5)      # absolute row, edited

    seen: dict[str, list[float]] = {}
    for f in (12.288, 9.0, 6.0):
        tv._fclk.setValue(f)
        for name in ("Man_t_DD", "Man_t_ZD", "Man_t_DZ", "Per_t_DD", "Per_t_ZD", "Per_t_IH"):
            seen.setdefault(name, []).append(tv._max[("proposed", name)].value())
    for name, vals in seen.items():
        assert max(vals) - min(vals) < 0.05, f"{name} moved with the rate ({vals})"
    assert tv._max[("proposed", "Per_t_ZD")].value() == pytest.approx(7.5), \
        "a rate change must not discard an edit to an absolute row"


def test_one_man_t_dz_is_read_by_both_the_keeper_and_the_contention_legs():
    """Two legs, one release time, and it has to be the same number in both.

    Under a clocked mode both families read `inp.Man_t_DZ_max_ns` snapped to the launch
    mode's grid (`man_tdz_parts`; `_man_tzd_ns` does the same for t_ZD), so one output
    stage has one release time.

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


def test_an_ede_release_follows_its_launch_so_a_slow_launch_costs_contention_not_the_keeper():
    """The release is measured from the launch, so the launch moves it.

    A slow analog launch pushes the release toward, and past, the closing edge: M->P
    contention pays it ns for ns. The keeper leg does not: what it reads is the ramp from the
    anchor to the keeper's crossing and to V_OH, and the EDE Delay past the ramp's 80 % point,
    which no launch changes. Its own V_IH,max on its own rail is 0.75·t_RF past the anchor,
    so the ramp leaves 0.25·t_RF of the EDE Delay's 7 ns before the keeper's 3 ns response,
    on the fast 4.75 ns edge.
    """
    UI = 1000 * 0.45 / 12.288
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=0.0, Man_ede=True, Man_t_EDE_ns=7.0,
              Per_t_DD_max_ns=9.0)
    clocked = compute(CalcInputs(Man_t_DD_min_ns=0.25 * UI,
                                 Man_t_DD_max_ns=0.25 * UI, **kw)).breakdowns
    analog = compute(CalcInputs(Man_t_DD_min_ns=9.0, Man_t_DD_max_ns=23.0, **kw)).breakdowns
    assert clocked["MP_contention"].margin_ns - analog["MP_contention"].margin_ns == \
        pytest.approx(23.0 - 0.25 * UI, abs=1e-9)
    assert analog["keeper_Man"].margin_ns == pytest.approx(clocked["keeper_Man"].margin_ns)
    assert analog["keeper_Man"].margin_ns == pytest.approx(7.0 - 3.0 + 0.25 * 0.95 * 5.0)


def test_the_keeper_leg_is_meaningful_without_ede_too():
    """Expressed in launch-referenced terms so it is not an EDE special case.

    A conventional handover drives its whole UI and goes on driving into the next one
    until t_DZ, so a non-EDE bus has a large genuine margin rather than a vacuous one.
    If this ever went negative on a plain PHY2 bus it would mean the release had
    overtaken data-valid, which is a real defect worth seeing.

    THE TERM STRUCTURE IS WHAT THIS PINS. `1*UI + t_DZ` is the release point: the UI
    carries the reference change (t_DD is measured from the edge that opens the UI, t_DZ
    from the one that closes it), and t_DZ carries the driving that continues past that
    edge. Under EDE the leg is the ramp to V_OH and the EDE Delay less the crossing of the
    keeper's threshold and the keeper's demand, with no UI.
    """
    r = compute(CalcInputs(F_CLK_target_MHz=12.288, handover_UIs=1.0))
    assert r.breakdowns["keeper_Man"].margin_ns > 0
    ede_in = CalcInputs(F_CLK_target_MHz=12.288, handover_UIs=0.0,
                        Man_ede=True, Per_ede=True)
    ede = compute(ede_in).breakdowns
    for leg, side in (("keeper_Man", "Man"), ("keeper_Per", "Per")):
        b = ede[leg]
        x_k, ramp = b.terms[0].value_ns, b.terms[1].value_ns
        assert b.margin_ns == pytest.approx(
            getattr(ede_in, f"{side}_t_EDE_ns") - ede_in.t_keeper_ns + ramp + x_k)
        fast = ",fast" if f"{side} DATA" in b.fast_data_edge else ""
        # The crossing and the ramp name one data edge (`_keeper_case`).
        edge = b.terms[0].symbol.split(",")[1]
        assert edge in ("rise", "fall"), b.terms[0].symbol
        assert [t.symbol for t in b.terms] == [
            f"X_K,{edge},{fast[1:] + ',' if fast else ''}late · {side}_t_RF,DATA",
            f"{side}_t_RF,DATA,{edge}{fast}", f"{side}_t_EDE", "Man_t_Keeper_Response"]
        assert not any("UI" in t.symbol for t in b.terms), leg
    # The terms, named: a UI and a t_DZ on the conventional legs, and no invented `,min`
    # corner on a parameter the spec gives only a maximum for.
    for leg, dz in (("keeper_Man", "Man_t_DZ"), ("keeper_Per", "Per_t_DZ")):
        syms = [t.symbol for t in r.breakdowns[leg].terms]
        assert "1·UI" in syms and dz in syms, (leg, syms)
        assert not any(s.endswith(",min") for s in syms), (leg, syms)


def test_the_ede_column_releases_both_sides_by_their_ede_delay():
    """What the EDE column IS: the revision's values, both sides releasing their EDE Delay
    after their ramp's 80 % point, and no handover UI.

    * EDE closes every CONTENTION leg at zero UIs.
    * Both keeper legs read `EDE Delay,min + (ramp to V_OH / V_OL) − (crossing of the keeper's
      threshold) − Man_tKeeper_Response`, both counted from the anchor: keeper_Man +0.75,
      its own V_IH,max 0.75·t_RF past the anchor on a fast 3.0 ns edge, and keeper_Per −0.33,
      the Manager's V_IH,max + α at a high Manager supply crossed on a low Peripheral rail
      1.039·t_RF past the anchor, on an 8.3 ns edge.
    * keeper_Per is short by that crossing's offset past V_OH; the other legs that stay short
      are not EDE's to close: MP_hold / MP_hold_ho, the ordinary legs on the revision's 9 ns
      Man_t_DD,min, and PP_hold_ho, whose level belongs to whoever RELEASED, so when the
      Manager released it is Manager-to-peripheral data and the leg binds (see
      `test_the_out_of_scope_split_...`).
    * The release follows the launch, so a clocked launch moves M->P contention by the
      launch it saves.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))

    # The launch: the revision's own analog range, and RANGED rather than collapsed.
    for name in ("Man_t_DD", "Man_t_ZD"):
        lo = tv._min[("proposed", name)].value()
        hi = tv._max[("proposed", name)].value()
        assert (lo, hi) == (9.0, 23.0), (name, lo, hi)
    assert tv.inputs("proposed").Man_ede and tv.inputs("proposed").Per_ede

    w = tv._per_inequality_worst("proposed")
    short = [k for k, b in w.items() if b.margin_ns < -0.005]
    # ASSERTED BY EQUALITY, not as a subset: a subset cannot see a leg LEAVING the set.
    # `_PP_DATA_LEGS` is IMPORTED from the model rather than restated here.
    assert set(short) - set(_PP_DATA_LEGS) == {"MP_hold", "MP_hold_ho", "PP_hold_ho",
                                                "keeper_Per"}, \
        f"the set of short legs moved: {short}"
    assert w["PP_hold_ho"].margin_ns == pytest.approx(-5.69, abs=0.02), \
        ("the level a releasing MANAGER drove, against an acquiring peripheral's turn-on: "
         "in scope, unclosed, and what still owes a handover UI wherever a peripheral "
         "acquires")
    for leg in ("MP_contention", "PM_contention", "PP_contention"):
        assert w[leg].margin_ns > 0, (leg, w[leg].margin_ns)
    for leg, margin in (("keeper_Man", 0.25 * 3.0), ("keeper_Per", -(1.0394737 - 1.0) * 8.3)):
        assert w[leg].margin_ns == pytest.approx(margin, abs=1e-4), (leg, w[leg].margin_ns)
    assert tv._handover_UIs() == 0.0, "this whole column is the zero-UI hypothetical"
    base_w = tv._per_inequality_worst("spec")
    assert len([k for k, b in base_w.items() if b.margin_ns < 0]) >= 3, \
        "the baseline should fail the handover legs, or the comparison shows nothing"

    # The launch mode moves the Manager's release with its launch: from the analog
    # 23 ns max to a clocked 0.25 UI point, M->P contention gains the difference.
    analog_mp = w["MP_contention"].margin_ns
    tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(LaunchMode.CLK4.value))
    w4 = tv._per_inequality_worst("proposed")
    ui = tv.inputs("proposed").UI_ns()
    assert w4["MP_contention"].margin_ns - analog_mp == pytest.approx(23.0 - 0.25 * ui,
                                                                       abs=0.02)
    # The keeper legs do not read the launch: the same margins under a clocked launch.
    for leg in ("keeper_Man", "keeper_Per"):
        assert w4[leg].margin_ns == pytest.approx(w[leg].margin_ns), leg


def test_setup_and_contention_agree_about_man_t_zd():
    """One definition of Man_t_ZD, read by both families.

    The setup handover variants and the contention legs both read `_man_tzd_ns`,
    so on a clock-launched bus they model one turn-on time for the device.

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
        # (a SWI3S Manager's t_ZD takes no anchor correction).
        if mode is not LaunchMode.ANALOG:
            used = -next(t.value_ns for t in r["MP_setup_ho"].terms
                         if "Man_t_ZD" in t.symbol)
            assert used == pytest.approx(want), mode


def test_ede_column_spreads_are_not_tighter_than_2_to_1():
    """A delay's max/min over PVT and mismatch is floored at 2:1.

    The rule bites ASYMMETRICALLY, which is why this asserts a floor and not a band: a range
    WIDER than 2:1 is merely conservative, but a range TIGHTER than 2:1 asks for better than
    PVT delivers -- an unbuildable part, not a tight one. It applies to every value that
    carries a PVT spread: the peripheral's output timings, the Manager's analog launch, and
    each side's EDE Delay, which is timed (3-7 ns, 2.33:1). A clocked Manager value is
    deterministic and carries none. t_DZ is tabulated as a maximum only, so its row's
    minimum is 0 and it has no ratio to floor.
    """
    tv = _view()
    for combo in (tv._man_spec_combo, tv._per_spec_combo):
        combo.setCurrentIndex(combo.findData("SWI3S_PHY2_EDE"))

    def ratio(name):
        lo = tv._min[("proposed", name)].value()
        hi = tv._max[("proposed", name)].value()
        return hi / lo if lo else math.inf

    for side in ("Man", "Per"):
        assert tv._min[("proposed", f"{side}_t_DZ")].value() == 0.0, side
    for name in ("Per_t_DD", "Per_t_ZD", "Man_t_DD", "Man_t_ZD", "Man_t_EDE", "Per_t_EDE"):
        assert ratio(name) >= 2.0 - 0.02, \
            f"{name} is {ratio(name):.2f}:1 — tighter than PVT allows"
    assert ratio("Man_t_EDE") == pytest.approx(7.0 / 3.0)
    assert ratio("Man_t_ZD") == pytest.approx(ratio("Man_t_DD")), \
        "t_DD and t_ZD are the two launch methods and the revision states them identically"


# ---------------------------------------------------------------------------
# 13. The value on screen is the value in force
# ---------------------------------------------------------------------------


def test_the_manager_output_timings_are_all_clocked_or_all_analog():
    """CLOCKED OR ANALOG, NOT A MIX — the invariant, stated on all three at once.

    One Manager has one output stage. If its data edge is launched from a
    multiplied clock then it HAS that clock, and its output enable and disable are
    launched from it too. A t_DD on a clock grid point beside an analog t_ZD and
    t_DZ is not a conservative model of anything; it is two different devices.
    `test_clocking_the_manager_release_costs_mp_contention` pins what the clocked release
    costs.

    What the mode does NOT dictate is which grid point each one sits on — see
    `test_a_four_x_clock_places_the_three_timings_independently`. This asserts only
    that each is on SOME point of the mode's grid, or that all are tabulated.

    Asserted on the three margins that read the three parameters, so it cannot be
    satisfied by a row that displays the right number while the model reads
    another, t_DZ included.
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

    The coherence rule above is about clocked-vs-analog, not about sharing an edge.
    Forcing every clocked Manager output onto the data edge's own point would restrict
    the design space rather than simplify it conservatively — e.g. t_DD 0.50 UI,
    t_ZD 0.00 UI, t_DZ 0.50 UI is a valid design.

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
    """Clocking the Manager's release costs MP contention.

    A clocked t_DZ at 2× is the 10 ns row on the 0.5 UI grid, LATER than the tabulated
    max, so the Manager holds the bus longer: MP contention loses exactly the difference,
    and the keeper gains.
    """
    kw = dict(F_CLK_target_MHz=12.288, handover_UIs=1.0,
              Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP",
              Man_t_DD_min_ns=9.0, Man_t_DD_max_ns=23.0,
              Man_t_ZD_ns=23.0, Man_t_DZ_max_ns=10.0)
    mp = {m: compute(CalcInputs(launch_mode=m, **kw)).breakdowns["MP_contention"].margin_ns
          for m in (LaunchMode.ANALOG, LaunchMode.CLK2)}
    clocked_dz = snap_to_launch_grid(10.0, LaunchMode.CLK2, 12.288)
    assert clocked_dz > 10.0
    assert mp[LaunchMode.ANALOG] - mp[LaunchMode.CLK2] == pytest.approx(clocked_dz - 10.0,
                                                                         abs=1e-9), mp
    # And the keeper gains, which is the same coupling read the other way: the
    # release moves later relative to a fixed data-valid point.
    kp = {m: compute(CalcInputs(launch_mode=m, **kw)).breakdowns["keeper_Man"].margin_ns
          for m in (LaunchMode.ANALOG, LaunchMode.CLK2)}
    assert kp[LaunchMode.CLK2] > kp[LaunchMode.ANALOG], kp


def test_every_governed_row_agrees_with_the_term_it_feeds():
    """When the picker moves the margins, the parameters they are computed from
    move with them on screen, so the value in force is on the page.

    Asserted as an IDENTITY between each row and the term it feeds, for all three
    Manager outputs, in both clocked modes. That is the invariant regardless of
    which placement a design picks. The selector supplies the GRID; the rows supply
    the placements.
    """
    tv = _view()
    for mode in (LaunchMode.CLK2, LaunchMode.CLK4):
        tv._launch_combo.setCurrentIndex(tv._launch_combo.findData(mode.value))
        w = tv._per_inequality_worst("proposed")
        # t_DD via the keeper, t_ZD via the handover setup variant, t_DZ via MP
        # contention -- one leg each, chosen because each reads that row directly, and on
        # each the row subtracts from the margin.
        for name, leg in (("Man_t_DD", "keeper_Man"), ("Man_t_ZD", "MP_setup_ho"),
                          ("Man_t_DZ", "MP_contention")):
            row = tv._max[("proposed", name)].value()
            term = next(t for t in w[leg].terms if t.symbol == name)
            used = -term.value_ns
            assert row == pytest.approx(used, abs=0.06), (mode, name, row, used)
        # And every one is editable: none is the mode's output.
        for name in ("Man_t_DD", "Man_t_ZD", "Man_t_DZ"):
            assert not tv._min[("proposed", name)].isReadOnly(), \
                f"{mode}: {name} is a placement the design chooses, not a fixed value"


def test_the_keeper_legs_are_split_so_the_corner_search_stays_exact():
    """`find_worst_corner_rows` requires every row to move each margin in one direction
    (calculator checklist rule 4), and a min over two legs does not: a row only one leg reads
    stops moving the min wherever the other leg binds.

    Reported as the worse of its two legs, the keeper saturates: when one leg sits at
    a fixed value, no single move of the other leg's rows lowers the min, and reaching
    the peripheral's worst needs Per_t_DZ,min and Per_t_DD,max TOGETHER, a
    two-coordinate move. So each keeper leg is reported on its own.

    Checked against brute force over the rows the keeper reads, at 13.2 MHz (PHY2's
    mandatory maximum), with and without EndDriveEarly.
    """
    from swi3s_studio.ui.timing_view import _ede_ranges

    f = 13.2
    ede = _ede_ranges()
    rows = [dc_replace(r, range=ede[r.name]) if r.name in ede else r
            for r in default_swi3s_rows()]
    plain = CalcInputs(F_CLK_target_MHz=f, duty_min=0.45, bus_length_cm=30.0,
                       Man_spec="SWI3S_PHY2_EDE", Per_spec="SWI3S_PHY2_EDE",
                       handover_UIs=0.0, launch_mode=LaunchMode.CLK2)
    base = dc_replace(plain, Man_ede=True, Per_ede=True)
    names = ["Man_t_DZ", "Man_t_DD", "Per_t_DZ", "Per_t_DD", "Man_t_EDE", "Per_t_EDE"]
    for inp, leg in itertools.product((plain, base), ("keeper_Man", "keeper_Per")):
        picks = find_worst_corner_rows(leg, rows, inp)
        got = compute(apply_row_picks(inp, rows, picks)).breakdowns[leg].margin_ns
        brute = min(
            compute(apply_row_picks(inp, rows, {**picks, **dict(zip(names, c))}))
            .breakdowns[leg].margin_ns
            for c in itertools.product(("min", "typ", "max"), repeat=len(names)))
        assert got == pytest.approx(brute, abs=1e-9), \
            f"{leg}: search found {got:+.2f}, true worst {brute:+.2f}"
    # Under EDE the peripheral's keeper margin is its EDE Delay,min and its ramp to V_OH less
    # its crossing of the keeper's threshold and the keeper's demand, the EDE Delay and the
    # demand both 3 ns.
    per = find_worst_corner_rows("keeper_Per", rows, base)
    leg = compute(apply_row_picks(base, rows, per)).breakdowns["keeper_Per"]
    x_k, ramp = (next(t.value_ns for t in leg.terms if p(t.symbol))
                 for p in (lambda s: s.startswith("X_K"), lambda s: s.startswith("Per_t_RF")))
    assert leg.margin_ns == pytest.approx(3.0 - 3.0 + ramp + x_k), \
        "under EDE the peripheral's keeper margin is its EDE Delay and ramp less its crossing"
    syms = {t.symbol for t in leg.terms}
    assert syms == {"X_K,rise,late · Per_t_RF,DATA", "Per_t_RF,DATA,rise", "Per_t_EDE",
                    "Man_t_Keeper_Response"}, syms
    assert not any("UI" in t.symbol for t in leg.terms), \
        "the peripheral's release is not UI-scaled"


# ---------------------------------------------------------------------------
# 14. The corner search
# ---------------------------------------------------------------------------


def test_the_keeper_per_corner_search_is_exact_over_the_rows_its_detections_read():
    """keeper_Per's spacing of its Peripheral's two clock detections reads the clock's slew, τ,
    α and δ, and its crossing of the keeper's threshold the Per DATA slew, τ, α and δ. Over
    those rows the search's corner leaves no more margin than the worst of their whole
    min/typ/max grid, the other rows at the search's picks, on either ramp shape and against
    either Manager's keeper."""
    rows = default_swi3s_rows()
    names = ["t_RF Man CLK", "t_RF Per DATA", "τ (per-edge slew)", "α (V_noise / V_SEOS)",
             "δ (V_SEOS tol)"]
    for inp in (CalcInputs(), CalcInputs(Man_shape="exp", Per_shape="exp"),
                CalcInputs(Man_spec="SW13_1V2")):
        picks = find_worst_corner_rows("keeper_Per", rows, inp)
        got = compute(apply_row_picks(inp, rows, picks)).breakdowns["keeper_Per"]
        assert any(t.symbol.endswith("· Man_t_RF,CLK") for t in got.terms), got.terms
        brute = min(
            compute(apply_row_picks(inp, rows, {**picks, **dict(zip(names, c))}))
            .breakdowns["keeper_Per"].margin_ns
            for c in itertools.product(("min", "typ", "max"), repeat=len(names)))
        assert got.margin_ns <= brute + 1e-9, (inp.Man_spec, inp.Man_shape, got.margin_ns,
                                               brute)


_KEEPER_LEGS = ("keeper_Man", "keeper_Per")


@pytest.mark.parametrize("window", [None, ((0.30, 0.40, 0.50), (0.35, 0.45, 0.60))],
                         ids=["default-window", "low-asymmetric-window"])
@pytest.mark.parametrize("shape", ["linear", "exp"])
@pytest.mark.parametrize("ede", [False, True], ids=["conventional", "ede"])
@pytest.mark.parametrize("man", ["SWI3S_PHY2", "SW13_1V2"])
def test_the_corner_search_is_exact_on_both_keeper_legs(man, ede, shape, window):
    """A keeper leg's crossing of the keeper's threshold at the Manager reads the supply
    assignment, α, τ, the slew rows and the keeper's threshold: a SWI3S Manager's V_IH,max /
    V_IL,min rows, a SoundWire Manager's own levels. On both keeper legs, with a conventional
    and an EndDriveEarly release, on either ramp shape and at the default and an asymmetric low
    input window, the search's corner leaves no more margin than any min/typ/max combination of
    the slews, τ, α and δ, the other rows at the search's picks, nor than any point of a slew
    row's range. The release rows move a keeper margin on their own
    (`test_the_keeper_legs_are_split_so_the_corner_search_stays_exact`)."""
    from swi3s_studio.timing import RowPoint, find_worst_corner_rows_all

    kw = {"Man_ede": True, "Per_ede": True, "handover_UIs": 0.0} if ede else {}
    per = "SWI3S_PHY2_EDE" if ede else "SWI3S_PHY2"
    base = dc_replace(_mixed(man, per, t_keeper_ns=preset(man, "Man").t_keeper_ns),
                      Man_shape=shape, Per_shape=shape, **kw)
    rows = default_swi3s_rows()
    if window is not None:
        ranges = {"V_IH": ParamRange(*window[0]), "V_IL": ParamRange(*window[1])}
        rows = [dc_replace(r, range=ranges[r.name]) if r.name in ranges else r for r in rows]
        base = apply_row_picks(base, rows, {})
    coupled = [r.name for r in rows if r.name in _SW_COUPLED_ROWS and r.linked_to is None]
    picks = find_worst_corner_rows_all(_KEEPER_LEGS, rows, base)
    for leg in _KEEPER_LEGS:
        def margin(p, leg=leg):
            return compute(apply_row_picks(base, rows, p)).breakdowns[leg].margin_ns

        got = margin(picks[leg])
        brute = min(margin({**picks[leg], **dict(zip(coupled, c))})
                    for c in itertools.product(("min", "typ", "max"), repeat=len(coupled)))
        assert got <= brute + 1e-9, (leg, got, brute)
        for name in (n for n in coupled if n.startswith("t_RF")):
            for k in range(13):
                at = margin({**picks[leg], name: RowPoint(k / 12, 0.0)})
                assert got <= at + 1e-9, (leg, name, k, got, at)


# The rows a SoundWire launch's anchor correction couples: the slews and τ the search moves
# jointly, and α and δ.
_SW_COUPLED_ROWS = ("t_RF Man CLK", "t_RF Man DATA", "t_RF Per DATA", "τ (per-edge slew)",
                    "α (V_noise / V_SEOS)", "δ (V_SEOS tol)")
# The rows the exactness checks search over: the coupled rows and the bus length.
_SW_SEARCHED_ROWS = (*_SW_COUPLED_ROWS, "bus length (cm)")


def _assert_search_is_exact(base, rows, masters):
    """The joint corner search over `rows` leaves each leg no more margin than the worst
    min/typ/max combination of `masters` does."""
    from swi3s_studio.timing import INEQUALITIES, find_worst_corner_rows_all

    picks = find_worst_corner_rows_all(INEQUALITIES, rows, base)
    got = {leg: compute(apply_row_picks(base, rows, picks[leg])).breakdowns[leg].margin_ns
           for leg in INEQUALITIES}
    brute = {leg: math.inf for leg in INEQUALITIES}
    for combo in itertools.product(("min", "typ", "max"), repeat=len(masters)):
        r = compute(apply_row_picks(base, rows, dict(zip(masters, combo))))
        for leg in INEQUALITIES:
            brute[leg] = min(brute[leg], r.breakdowns[leg].margin_ns)
    missed = {leg: (round(got[leg], 4), round(brute[leg], 4)) for leg in INEQUALITIES
              if got[leg] > brute[leg] + 1e-9}
    assert not missed, f"search found a better margin than a reachable corner: {missed}"


@pytest.mark.parametrize("man,per,size,locked", [
    *((m, p, "small", True) for m, p in _SW_PAIRS),
    ("SW13_1V2", "SW13_1V2", "large", True),
    ("SW13_1V2", "SW13_1V2", "small", False),
])
def test_the_corner_search_is_exact_over_the_rows_a_soundwire_anchor_couples(
        man, per, size, locked):
    """With a SoundWire side the direction a slew row moves a margin depends on α, δ and τ,
    and τ's on α and δ: the anchor correction credits a slew the crossings charge. Over the
    rows that coupling reaches, and the bus length, the search's corner is the worst corner
    of the whole min/typ/max grid, on every leg. With the Manager's lanes unlinked, Man DATA
    is a row of its own."""
    base = _mixed(man, per, size)
    rows = [r for r in default_swi3s_rows(lock_mgr_lanes=locked)
            if r.is_window or r.name in _SW_SEARCHED_ROWS]
    masters = [r.name for r in rows if r.linked_to is None and not r.is_window]
    if not locked:
        masters.remove("bus length (cm)")       # keeps the grid at 3**6
        rows = [r for r in rows if r.name != "bus length (cm)"]
    assert len(masters) == 6, masters
    _assert_search_is_exact(base, rows, masters)


@pytest.mark.parametrize("edit", [
    {"t_RF Man CLK": ParamRange(5.0, 5.5, 6.0)},
    {"t_RF Man DATA": ParamRange(5.0, 5.5, 6.0)},
    {"α (V_noise / V_SEOS)": ParamRange(0.0, 0.1, 0.2)},
    {"α (V_noise / V_SEOS)": ParamRange(0.0, 0.1, 0.2), "t_RF Man CLK": ParamRange(5.0, 5.5, 6.0)},
], ids=["man-clk-range", "man-data-range", "alpha-0.2", "both"])
def test_the_corner_search_is_exact_on_a_swi3s_bus_whatever_the_slew_and_noise_ranges(edit):
    """On a SWI3S-only bus the slews and τ are searched jointly too: the direction a slew or τ
    row moves a margin depends on α, δ and the other slews where a linked Man DATA row's range
    is not its master's, or where α is large enough to put a crossing before its edge's 20 %
    point. Over those rows and the bus length the search's corner is the worst corner of the
    whole min/typ/max grid, on every leg."""
    base = CalcInputs(Man_spec="SWI3S_PHY2", Per_spec="SWI3S_PHY2")
    rows = [dc_replace(r, range=edit[r.name]) if r.name in edit else r
            for r in default_swi3s_rows()
            if r.is_window or r.name in _SW_SEARCHED_ROWS]
    masters = [r.name for r in rows if r.linked_to is None and not r.is_window]
    assert len(masters) == 6, masters
    _assert_search_is_exact(base, rows, masters)


@pytest.mark.parametrize("man,v_oh,v_ol", [("SWI3S_PHY2", 0.90, 0.10), ("SW13_1V2", 0.80, 0.15)])
def test_the_corner_search_is_exact_where_the_ramp_to_v_oh_is_not_one_t_rf(man, v_oh, v_ol):
    """On exponential ramps at a V_OH / V_OL pair other than 0.20 / 0.80 an EndDriveEarly
    release's ramp to V_OH / V_OL is not one t_RF, and its keeper leg's data edge depends on
    α. Over the coupled rows and the bus length the search's
    corner is still the worst corner of the whole min/typ/max grid, on every leg."""
    base = dc_replace(_mixed(man, "SWI3S_PHY2_EDE"), Man_shape="exp", Per_shape="exp",
                      Man_ede=True, Per_ede=True, V_OH_min_frac=v_oh, V_OL_max_frac=v_ol)
    rows = [r for r in default_swi3s_rows() if r.is_window or r.name in _SW_SEARCHED_ROWS]
    masters = [r.name for r in rows if r.linked_to is None and not r.is_window]
    _assert_search_is_exact(base, rows, masters)


# V_OH / V_OL pairs with V_OH + V_OL ≠ 1, where a SoundWire launch's anchor and the ramp to
# V_OH / V_OL differ by polarity; 0.85 / 0.25 is the timeline tests' `_ASYMMETRIC`.
_ASYMMETRIC_LEVELS = ((0.80, 0.15), (0.85, 0.25), (0.90, 0.30))


@pytest.mark.parametrize("v_oh, v_ol", _ASYMMETRIC_LEVELS)
@pytest.mark.parametrize("man,per,kw", [
    ("SW13_1V2", "SW13_1V2", {}),
    ("SW13_1V2", "SWI3S_PHY2", {"Man_shape": "exp", "Per_shape": "exp", "handover_UIs": 2.0}),
    ("SWI3S_PHY2", "SW13_1V2", {"Man_shape": "exp", "Per_shape": "exp", "Man_ede": True,
                                "Per_ede": True, "handover_UIs": 0.0}),
], ids=["sw-sw-linear", "sw-swi3s-exp-2ui", "swi3s-sw-exp-ede"])
def test_the_corner_search_is_exact_at_asymmetric_soundwire_levels(man, per, kw, v_oh, v_ol):
    """At V_OH + V_OL ≠ 1 a SoundWire launch's anchor depends on its data and clock edges'
    polarities, and each leg places it on the edges its case gives: the P->M envelopes select
    their case with it, a contention leg's turn-on and an EndDriveEarly or keeper release take
    the edge that leaves the less margin. Over the coupled rows and the bus length the search's
    corner is still the worst corner of the whole min/typ/max grid, on every leg."""
    base = dc_replace(_mixed(man, per), V_OH_min_frac=v_oh, V_OL_max_frac=v_ol, **kw)
    rows = [r for r in default_swi3s_rows() if r.is_window or r.name in _SW_SEARCHED_ROWS]
    masters = [r.name for r in rows if r.linked_to is None and not r.is_window]
    assert len(masters) == 6, masters
    _assert_search_is_exact(base, rows, masters)


@pytest.mark.parametrize("man,per,kw,v_ih,v_il", [
    ("SWI3S_PHY2", "SWI3S_PHY2", {}, (0.35, 0.40, 0.45), (0.25, 0.35, 0.45)),
    ("SWI3S_PHY1", "SWI3S_PHY2_EDE", {"launch_mode": LaunchMode.CLK4},
     (0.30, 0.35, 0.40), (0.35, 0.50, 0.65)),
    ("SWI3S_PHY1", "SWI3S_PHY2_EDE", {"launch_mode": LaunchMode.CLK2},
     (0.25, 0.30, 0.35), (0.35, 0.40, 0.45)),
    ("SWI3S_PHY2", "SWI3S_PHY2", {"V_OH_min_frac": 0.80, "V_OL_max_frac": 0.30},
     (0.35, 0.40, 0.45), (0.25, 0.35, 0.45)),
    ("SWI3S_PHY2", "SWI3S_PHY2", {"Man_shape": "exp", "Per_shape": "exp"},
     (0.35, 0.40, 0.45), (0.25, 0.35, 0.45)),
], ids=["phy2-linear", "phy1-ede-4x", "phy1-ede-2x", "phy2-levels-0.80-0.30", "phy2-exp"])
def test_the_corner_search_is_exact_with_the_input_windows_set_low(man, per, kw, v_ih, v_il):
    """With the V_IH and V_IL windows set low, the direction α and δ move a P->M margin depends
    on the slews: at the typ slews one of them moves it the other way or not at all. The search
    takes α and δ with the slews and τ jointly, so over the coupled rows and the bus length its
    corner is the worst corner of the whole min/typ/max grid, on every leg."""
    windows = {"V_IH": ParamRange(*v_ih), "V_IL": ParamRange(*v_il)}
    base = _mixed(man, per, **kw)
    rows = [dc_replace(r, range=windows[r.name]) if r.name in windows else r
            for r in default_swi3s_rows() if r.is_window or r.name in _SW_SEARCHED_ROWS]
    masters = [r.name for r in rows if r.linked_to is None and not r.is_window]
    assert len(masters) == 6, masters
    _assert_search_is_exact(base, rows, masters)


def test_the_corner_search_is_exact_where_delta_binds_at_typ():
    """δ can leave a margin smaller at typ than at either end: on the Timing view's
    Specification column, a SWI3S PHY2 Manager on an exponential ramp and a SoundWire
    Peripheral, V_IH 0.30-0.60 and V_IL 0.30-0.40, no handover UI, P->M hold is least at δ's
    typ. The search takes every pick of α and δ with the slews and τ, so over the coupled rows
    and the bus length its corner is the worst corner of the whole min/typ/max grid, on every
    leg, and its PM_hold pick holds δ inside its range, with a margin no larger than at typ
    and below both ends."""
    from swi3s_studio.timing import INEQUALITIES, find_worst_corner_rows_all

    tv = _view()
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SWI3S_PHY2"))
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V2"))
    tv._set_shape(tv._man_shape, "exp")
    base, rows = tv._typ_baseline("spec")
    windows = {"V_IH": ParamRange(0.30, 0.45, 0.60), "V_IL": ParamRange(0.30, 0.35, 0.40)}
    rows = [dc_replace(r, range=windows[r.name]) if r.name in windows else r
            for r in rows if r.is_window or r.name in _SW_SEARCHED_ROWS]
    base = apply_row_picks(dc_replace(base, handover_UIs=0.0), rows, {})
    masters = [r.name for r in rows if r.linked_to is None and not r.is_window]
    assert len(masters) == 6, masters
    _assert_search_is_exact(base, rows, masters)
    delta = "δ (V_SEOS tol)"
    picks = find_worst_corner_rows_all(INEQUALITIES, rows, base)["PM_hold"]
    at = {p: compute(apply_row_picks(base, rows, {**picks, delta: p}))
          .breakdowns["PM_hold"].margin_ns for p in ("min", "typ", "max", picks[delta])}
    assert picks[delta] not in ("min", "max"), picks
    assert at[picks[delta]] <= at["typ"] < min(at["min"], at["max"]) - 1e-6, at


def test_the_corner_search_finds_an_interior_delta_minimum_past_a_supply_case_boundary(
        monkeypatch):
    """A margin is the least of its supply assignments' cases, so along δ it can fall to a
    minimum inside the range and then rise to another case's plateau: SWI3S PHY1 on both
    sides, linear ramps, no handover UI, 6.6 MHz, V_IH 0.332-0.398, δ, α and τ each from 0
    (to 0.10, 0.05, 0.05). PM_setup's least margin along δ is near 3.5 %, below its value at
    every one of min, typ and max. The search samples δ across its range
    (`_refine_fraction_row`): its pick is a point inside the range, its margin is below the
    min/typ/max grid's, and no point of a fine sweep of δ beats it by more than 0.2 ps."""
    from swi3s_studio.timing import calculator, find_worst_corner_rows_all
    from swi3s_studio.timing.calculator import RowPoint, default_swi3s_rows
    ranges = {"V_IH": (0.33174528354441485, 0.36508340608666306, 0.3984215286289112),
              "δ (V_SEOS tol)": (0.0, 0.05, 0.10), "α (V_noise / V_SEOS)": (0.0, 0.025, 0.05),
              "τ (per-edge slew)": (0.0, 0.025, 0.05)}
    rows = [dc_replace(r, range=ParamRange(*ranges[r.name])) if r.name in ranges else r
            for r in default_swi3s_rows()]
    base = CalcInputs(Man_spec="SWI3S_PHY1", Per_spec="SWI3S_PHY1", handover_UIs=0.0,
                      F_CLK_target_MHz=6.6)
    delta, leg = "δ (V_SEOS tol)", "PM_setup"

    def margin(picks):
        return compute(apply_row_picks(base, rows, picks)).breakdowns[leg].margin_ns

    picks = find_worst_corner_rows_all((leg,), rows, base)[leg]
    assert isinstance(picks[delta], RowPoint) and 0.2 < picks[delta].at < 0.5, picks
    found = margin(picks)
    grid = min(margin({**picks, delta: p}) for p in ("min", "typ", "max"))
    assert found < grid - 1e-3, (found, grid)
    sweep = min(margin({**picks, delta: RowPoint(k / 200, 0.0)}) for k in range(1, 200))
    assert found <= sweep + 2e-4, (found, sweep)
    monkeypatch.setattr(calculator, "_REFINE_SAMPLES", 0)
    coarse = find_worst_corner_rows_all((leg,), rows, base)[leg]
    assert margin(coarse) > found + 1e-3


_FRACTION_ROWS = ("τ (per-edge slew)", "α (V_noise / V_SEOS)", "δ (V_SEOS tol)")


@pytest.mark.parametrize("name", _FRACTION_ROWS)
def test_the_corner_search_refines_each_fraction_row_and_reports_its_inside_point(
        name, monkeypatch):
    """Every leg's least combination is refined along each of τ, α and δ, and a point the
    refinement finds is reported for that row and no other: with the refinement of `name`
    replaced by one that moves it to position 0.37 and lowers the margin, every leg reports a
    `RowPoint` at 0.37 for `name` and none for the other two."""
    from swi3s_studio.timing import calculator, find_worst_corner_rows_all
    from swi3s_studio.timing.calculator import RowPoint, default_swi3s_rows
    rows = default_swi3s_rows()
    base = CalcInputs()
    real = calculator._refine_fraction_row
    refined: dict[str, int] = {}

    def spy(row, i, best_m, best_c, margin_of):
        refined[row.name] = refined.get(row.name, 0) + 1
        if row.name != name:
            return real(row, i, best_m, best_c, margin_of)
        point = RowPoint(0.37, row.range.at_position(0.37), "at 0.37")
        return best_m - 1.0, (*best_c[:i], point, *best_c[i + 1:])

    for leg in ("MP_setup", "PM_hold"):
        refined.clear()
        monkeypatch.setattr(calculator, "_refine_fraction_row", spy)
        picks = find_worst_corner_rows_all((leg,), rows, base)[leg]
        monkeypatch.setattr(calculator, "_refine_fraction_row", real)
        assert refined == {r: 1 for r in _FRACTION_ROWS}, refined
        assert isinstance(picks[name], RowPoint) and picks[name].at == 0.37, picks
        assert not any(isinstance(picks[r], RowPoint) for r in _FRACTION_ROWS if r != name)


@pytest.mark.parametrize("name", _FRACTION_ROWS)
def test_the_refinement_keeps_an_inside_point_only_where_its_margin_is_smaller(name):
    """`_refine_fraction_row` on each of τ, α and δ: a margin with its least point inside the
    range, at position 0.3, is reported there as a `RowPoint` whose margin is that minimum; a
    margin least at an end of the range leaves the combination and its margin as they were."""
    from swi3s_studio.timing.calculator import (
        RowPoint,
        _refine_fraction_row,
        default_swi3s_rows,
    )
    (row,) = [r for r in default_swi3s_rows() if r.name == name]
    picks = ("typ", "min", "max")

    def pos(p):
        return p.at if isinstance(p, RowPoint) else {"min": 0.0, "typ": 0.5, "max": 1.0}[p]

    def inside(combo):
        return (pos(combo[1]) - 0.3) ** 2

    m, c = _refine_fraction_row(row, 1, inside(picks), picks, inside)
    assert isinstance(c[1], RowPoint) and c[1].at == pytest.approx(0.3, abs=1e-6), c
    assert (c[0], c[2]) == ("typ", "max")
    assert m == pytest.approx(0.0, abs=1e-12)
    assert c[1] == f"at {100.0 * row.range.at_position(c[1].at):.2f} %"

    def at_an_end(combo):
        return pos(combo[1])

    held = ("typ", "min", "max")
    assert _refine_fraction_row(row, 1, 0.0, held, at_an_end) == (0.0, held)


@pytest.mark.parametrize("man,per", _SW_PAIRS)
def test_a_joint_row_a_leg_does_not_read_is_reported_at_typ(man, per):
    """Ties in the joint search go to the combination with the most rows at typ, so a joint row
    a leg does not read is not reported at a corner: MP_setup does not read the Peripheral's
    DATA slew."""
    from swi3s_studio.timing import find_worst_corner_rows_all

    base = _mixed(man, per)
    rows = [r for r in default_swi3s_rows() if r.is_window or r.name in _SW_SEARCHED_ROWS]
    picks = find_worst_corner_rows_all(("MP_setup",), rows, base)["MP_setup"]
    assert picks["t_RF Per DATA"] == "typ", picks


@pytest.mark.parametrize("spec,size", [("SWI3S_PHY2", "small"), ("SW13_1V2", "small"),
                                       ("SW13_1V2", "large")])
def test_each_edge_is_affine_in_its_row_between_two_kinks(spec, size):
    """Each edge's t_RF, the row value times its `edge_factor`, is affine in the row value
    between two consecutive kinks of `edge_kinks_ns` and past the outermost, on every lane and
    at every τ, and turns at each kink; at τ = 0 there is none. The corner search evaluates a
    slew row at its range's ends and at these kinks, so they are every place a slew term
    turns."""
    from swi3s_studio.timing.calculator import _LANE_FIELDS, edge_factor, edge_kinks_ns

    for tau, lane in itertools.product((0.0, 0.025, 0.05, 0.3, 0.5, 0.6, 0.9), _LANE_FIELDS):
        base = CalcInputs(tRF_tolerance_frac=tau, Man_spec=spec, Per_spec=spec,
                          system_size=size)
        kinks = edge_kinks_ns(base, lane)
        assert bool(kinks) == (tau > 0.0), (lane, tau, kinks)

        def edges(x):
            inp = dc_replace(base, **{_LANE_FIELDS[lane]: x})
            return [x * edge_factor(inp, lane, role) for role in ("late", "early")]

        stops = [0.0, *kinks, 2.0 * max(kinks, default=5.0) + 10.0]
        assert stops == sorted(set(stops)), stops
        slopes = []
        for a, b in zip(stops, stops[1:]):
            ea, eb = edges(a), edges(b)
            for t in (0.25, 0.5, 0.8):
                want = [u + t * (v - u) for u, v in zip(ea, eb)]
                assert edges(a + t * (b - a)) == pytest.approx(want, abs=1e-9), \
                    (lane, tau, a, b, t)
            slopes.append([(v - u) / (b - a) for u, v in zip(ea, eb)])
        for k, before, after in zip(kinks, slopes, slopes[1:]):
            assert before != pytest.approx(after, abs=1e-9), \
                f"{lane} at τ {tau}: no edge turns at the kink {k:.4f} ns"


def _assert_no_slew_point_beats_the_search(base, rows, steps=24):
    """No point of a slew row's range, every other row at a leg's picks, leaves the leg less
    margin than the search's corner, at `steps` + 1 evenly spaced positions (`RowPoint.at`)
    of each slew row, a linked row at the same position in its own range. Returns the legs
    whose corner holds a slew row inside its range."""
    from swi3s_studio.timing import INEQUALITIES, RowPoint, find_worst_corner_rows_all

    picks = find_worst_corner_rows_all(INEQUALITIES, rows, base)
    slews = [r.name for r in rows if r.linked_to is None and r.binds[0].startswith("tRF_")
             and r.binds[0] != "tRF_tolerance_frac"]
    assert slews, [r.name for r in rows]
    evaluated = {}

    def margin(p, leg):
        inp = apply_row_picks(base, rows, p)
        if inp not in evaluated:
            evaluated[inp] = compute(inp)
        return evaluated[inp].breakdowns[leg].margin_ns

    inside = set()
    for leg in INEQUALITIES:
        got = margin(picks[leg], leg)
        for name in slews:
            for k in range(steps + 1):
                at = margin({**picks[leg], name: RowPoint(k / steps, 0.0)}, leg)
                assert got <= at + 1e-9, \
                    f"{leg}: search found {got:+.4f}, {name} at position {k}/{steps} {at:+.4f}"
        if any(isinstance(p, RowPoint) for p in picks[leg].values()):
            inside.add(leg)
    return picks, inside


_CLK_WIDE = {"t_RF Man CLK": ParamRange(1.0, 3.0, 6.0), "t_RF Per DATA": ParamRange(1.5, 4.0, 7.0)}


@pytest.mark.parametrize("base,locked,edit,leg", [
    (CalcInputs(), True, {"t_RF Man CLK": ParamRange(1.0, 3.5, 6.0)}, "MP_setup"),
    (_mixed("SW13_1V2", "SWI3S_PHY2"), True, _CLK_WIDE, "MP_hold"),
    (dc_replace(_mixed("SWI3S_PHY2", "SW13_1V2"), Man_shape="exp", Per_shape="exp"), True,
     _CLK_WIDE, "MP_setup"),
    (dc_replace(CalcInputs(), Man_shape="exp", Per_shape="exp"), False,
     {"t_RF Man DATA": ParamRange(2.0, 4.0, 9.0), "t_RF Per DATA": ParamRange(2.5, 6.0, 8.0),
      "τ (per-edge slew)": ParamRange(0.0, 0.05, 0.10)}, None),
], ids=["linked-data-clips-mid-range", "soundwire-manager", "soundwire-peripheral-exp",
        "unlinked-exp-wide-tau"])
def test_the_corner_search_is_exact_over_each_slew_rows_whole_range(base, locked, edit, leg):
    """An edge stops at an end of its spec's slew range while the other edge's t_RF turns back
    (`edge_factor`), so a margin can be least inside a slew row's range, at a kink
    (`edge_kinks_ns`), and the search evaluates the row there. At each configuration no point
    of a slew row's range leaves any leg less margin than the search's corner, over the rows
    the slews couple (`_SW_COUPLED_ROWS`). With the Man
    DATA row linked to a Man CLK row of another range, the DATA lane reaches its kink with
    the CLK row mid-range, and there `leg`'s corner is inside the range, with less margin
    than at any of the row's min, typ and max."""
    rows = [dc_replace(r, range=edit[r.name]) if r.name in edit else r
            for r in default_swi3s_rows(lock_mgr_lanes=locked)
            if r.is_window or r.name in _SW_COUPLED_ROWS]
    picks, inside = _assert_no_slew_point_beats_the_search(base, rows)
    if leg is None:
        return
    assert leg in inside, (leg, sorted(inside))
    clk = "t_RF Man CLK"
    got = compute(apply_row_picks(base, rows, picks[leg])).breakdowns[leg].margin_ns
    ends = {p: compute(apply_row_picks(base, rows, {**picks[leg], clk: p})).breakdowns[leg]
            .margin_ns for p in ("min", "typ", "max")}
    assert got < min(ends.values()) - 0.01, (got, ends)


@pytest.mark.parametrize("man,per,levels", [
    *((m, p, {}) for m, p in (*_SW_PAIRS, ("SWI3S_PHY2", "SWI3S_PHY2"))),
    *(("SW13_1V2", "SW13_1V2", {"V_OH_min_frac": v_oh, "V_OL_max_frac": v_ol,
                                "Man_shape": "exp", "Per_shape": "exp"})
      for v_oh, v_ol in _ASYMMETRIC_LEVELS),
])
def test_the_other_rows_move_a_margin_independently_of_the_coupled_rows(man, per, levels):
    """The search holds every row outside the coupled set (`_SW_COUPLED_ROWS`; the bus length
    is outside it) at typ while it searches the coupled set jointly, which is exact because
    each such row changes every margin by an amount the coupled rows do not change: its min
    and max move each leg by the same amount at every corner of the slews, τ, α and δ, with a
    SoundWire side or without, and at V_OH / V_OL pairs where a SoundWire launch's anchor
    depends on its edges' polarities."""
    from swi3s_studio.timing import INEQUALITIES

    base = dc_replace(_mixed(man, per), **levels)
    rows = default_swi3s_rows()
    masters = [r.name for r in rows if r.linked_to is None and not r.is_window]
    coupled = [n for n in masters if n in _SW_COUPLED_ROWS]
    others = [n for n in masters if n not in coupled]
    typ = {n: "typ" for n in masters}

    def margins(picks):
        r = compute(apply_row_picks(base, rows, picks))
        return {leg: r.breakdowns[leg].margin_ns for leg in INEQUALITIES}

    def moves(at):
        m0 = margins(at)
        return {(n, p): {leg: v - m0[leg] for leg, v in margins({**at, n: p}).items()}
                for n in others for p in ("min", "max")}

    ref = moves(typ)
    for combo in itertools.product(("min", "max"), repeat=len(coupled)):
        at = moves({**typ, **dict(zip(coupled, combo))})
        for key, by_leg in at.items():
            for leg, d in by_leg.items():
                assert d == pytest.approx(ref[key][leg], abs=1e-9), \
                    (key, leg, dict(zip(coupled, combo)), d, ref[key][leg])


def test_the_view_shows_the_reachable_worst_with_soundwire_on_both_sides():
    """Specification column, both sides SoundWire 1.3 at 12.288 MHz: MP_hold,
    PM_setup and keeper_Per, as the view shows them, are no better than any corner of the
    coupled rows reachable from their own picks."""
    from swi3s_studio.timing import INEQUALITIES

    tv = _view()
    tv._man_spec_combo.setCurrentIndex(tv._man_spec_combo.findData("SW13_1V2"))
    tv._per_spec_combo.setCurrentIndex(tv._per_spec_combo.findData("SW13_1V2"))
    tv._fclk.setValue(12.288)
    shown, picked = tv._worst_results("spec")
    base, rows = tv._typ_baseline("spec")
    names = [r.name for r in rows if r.name in _SW_SEARCHED_ROWS and r.linked_to is None]
    for leg in ("MP_hold", "PM_setup", "keeper_Per"):
        assert leg in INEQUALITIES
        got = shown[leg].breakdowns[leg].margin_ns
        brute = min(compute(apply_row_picks(base, rows, {**picked[leg], **dict(zip(names, c))}))
                    .breakdowns[leg].margin_ns
                    for c in itertools.product(("min", "typ", "max"), repeat=len(names)))
        assert got <= brute + 1e-9, (leg, got, brute)
