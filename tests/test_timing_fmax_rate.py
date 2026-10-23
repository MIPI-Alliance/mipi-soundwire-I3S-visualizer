"""F_max on legs whose terms scale with the UI, and the Manager notes under a clocked launch.

- A clocked Manager t_DD / t_ZD sits at a fixed fraction of the UI, so a setup leg's F_max is
  solved with that fraction held: re-evaluated at the reported rate, the margin is zero.
- An EDE release is referenced to the closing edge (one UI earlier), so a contention leg on an
  EDE release is rate-dependent even at N_HO = 0.
- The MP_setup / MP_hold conversion notes are given the value in force after the launch-mode
  snap, and carry the snap note before it.
- Hold, handover-hold and keeper legs whose margin carries a UI-scaled part (a clocked Manager
  t_DD / t_ZD / t_DZ, N_HO·UI, a conventional release's closing UI) report a finite F_max, and
  `F_max_binding_MHz` / `binding_inequality` read every bounding leg's F_max: just below the
  binding rate every bounding leg passes, just above it the binding leg fails.

Run: PYTHONPATH=. python3 -m pytest tests/test_timing_fmax_rate.py
"""
import math
import re
from dataclasses import replace

import pytest

from swi3s_studio.timing.calculator import CalcInputs, compute
from swi3s_studio.timing.spec_source import (
    LaunchMode,
    launch_grid_UI,
    launch_mode_max_fclk_MHz,
    pure_output_delay,
    snap_to_launch_grid,
)

_PROP = dict(Man_spec="SWI3S_PHY2_PROP", Per_spec="SWI3S_PHY2_PROP")


def _clocked(mode: LaunchMode, f_MHz: float = 12.288, **kw) -> CalcInputs:
    """A clocked-launch column with every governed Manager row on the grid at `f_MHz`."""
    base = CalcInputs(**_PROP, launch_mode=mode, F_CLK_target_MHz=f_MHz,
                      Per_t_IH_max_ns=1.5, **kw)
    dd = snap_to_launch_grid(9.0, mode, f_MHz, base.duty_min, floor_ns=9.0)
    zd = snap_to_launch_grid(base.Man_t_ZD_ns, mode, f_MHz, base.duty_min)
    dz = snap_to_launch_grid(base.Man_t_DZ_max_ns, mode, f_MHz, base.duty_min)
    return replace(base, Man_t_DD_max_ns=dd, Man_t_DD_min_ns=dd,
                   Man_t_ZD_ns=zd, Man_t_DZ_max_ns=dz)


def _at_rate_holding_placement(inp: CalcInputs, f_MHz: float) -> CalcInputs:
    """`inp` at another rate, each clocked Manager row kept at its fraction of the UI."""
    ui0 = inp.UI_ns()
    out = replace(inp, F_CLK_target_MHz=f_MHz)
    k = out.UI_ns() / ui0
    return replace(out, Man_t_DD_max_ns=inp.Man_t_DD_max_ns * k,
                   Man_t_DD_min_ns=inp.Man_t_DD_min_ns * k,
                   Man_t_ZD_ns=inp.Man_t_ZD_ns * k,
                   Man_t_DZ_max_ns=inp.Man_t_DZ_max_ns * k)


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
@pytest.mark.parametrize("leg", ["MP_setup", "MP_setup_ho"])
def test_a_clocked_setup_fmax_closes_the_leg_when_evaluated_at_that_rate(mode, leg):
    inp = _clocked(mode)
    b = compute(inp).breakdowns[leg]
    assert b.margin_ns > 0, (leg, b.margin_ns)
    assert math.isfinite(b.F_max_MHz) and b.F_max_MHz > inp.F_CLK_target_MHz
    at = compute(_at_rate_holding_placement(inp, b.F_max_MHz)).breakdowns[leg]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), (leg, mode, b.F_max_MHz)
    # Just above it the leg fails, just below it passes.
    assert compute(_at_rate_holding_placement(inp, b.F_max_MHz * 1.01)
                   ).breakdowns[leg].margin_ns < 0
    assert compute(_at_rate_holding_placement(inp, b.F_max_MHz * 0.99)
                   ).breakdowns[leg].margin_ns > 0


def test_the_2x_setup_fmax_agrees_with_the_launch_hint():
    """The 2x setup leg and `launch_mode_max_fclk_MHz` solve the same inequality."""
    b = compute(_clocked(LaunchMode.CLK2)).breakdowns["MP_setup"]
    f_hint, bound = launch_mode_max_fclk_MHz(LaunchMode.CLK2)
    assert bound == "setup"
    assert b.F_max_MHz == pytest.approx(f_hint, rel=1e-3)


def test_an_analog_setup_fmax_is_the_closed_form():
    inp = CalcInputs()
    b = compute(inp).breakdowns["MP_setup"]
    assert b.F_max_MHz == pytest.approx(1000.0 * inp.duty_min / b.required_ns, rel=1e-12)


_EDE_CASES = [
    # (leg, overrides)
    ("PM_contention", dict(Per_ede=True, Per_ede_hold_max_ns=20.0, handover_UIs=0.0)),
    ("PM_contention", dict(Per_ede=True, Per_ede_hold_max_ns=20.0, handover_UIs=1.0,
                           F_CLK_target_MHz=13.2)),
    ("PP_contention", dict(Per_ede=True, Per_ede_hold_max_ns=20.0, handover_UIs=0.0,
                           F_CLK_target_MHz=9.6)),
    # Inside the EndDriveEarly ceiling at the target, pulled back to it at F_max.
    ("MP_contention", dict(Man_ede=True, Man_t_DZ_max_ns=27.0, Per_t_ZD_ns=0.0,
                           handover_UIs=0.0, bus_length_cm=0.0)),
    # Past the ceiling at the target, so 0.60·UI + 10 ns throughout.
    ("MP_contention", dict(Man_ede=True, Man_t_DZ_max_ns=60.0, Per_t_ZD_ns=0.0,
                           handover_UIs=0.0, bus_length_cm=0.0)),
]


@pytest.mark.parametrize("leg,kw", _EDE_CASES)
def test_an_ede_contention_fmax_is_where_the_margin_reaches_zero(leg, kw):
    inp = replace(CalcInputs(), **{"F_CLK_target_MHz": 12.288, **kw})
    b = compute(inp).breakdowns[leg]
    assert 0.0 < b.F_max_MHz < math.inf, (leg, kw, b.F_max_MHz, b.margin_ns)
    at = compute(replace(inp, F_CLK_target_MHz=b.F_max_MHz)).breakdowns[leg]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), (leg, kw)
    # Pass at the target exactly when the target is at or below F_max.
    assert (b.margin_ns >= 0) == (inp.F_CLK_target_MHz <= b.F_max_MHz), (leg, kw)


def test_a_zero_ui_ede_deficit_is_closed_by_a_slower_clock():
    """At N_HO = 0 a peripheral EDE release still moves with the UI, so F_max is not 0.0."""
    inp = CalcInputs(Per_ede=True, Per_ede_hold_max_ns=20.0, handover_UIs=0.0,
                     F_CLK_target_MHz=12.288)
    b = compute(inp).breakdowns["PM_contention"]
    assert b.margin_ns < 0
    assert 9.6 < b.F_max_MHz < 12.288
    assert compute(replace(inp, F_CLK_target_MHz=9.6)
                   ).breakdowns["PM_contention"].margin_ns > 0


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
def test_a_clocked_manager_turn_on_moves_the_pm_contention_fmax(mode):
    """P->M: the Manager acquires with a clocked t_ZD, a fraction of the UI."""
    inp = _clocked(mode, handover_UIs=1.0)
    b = compute(inp).breakdowns["PM_contention"]
    assert 0.0 < b.F_max_MHz < math.inf, (mode, b.F_max_MHz)
    at = compute(_at_rate_holding_placement(inp, b.F_max_MHz)).breakdowns["PM_contention"]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), mode


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
def test_a_clocked_manager_release_that_never_contends_reports_no_ceiling(mode):
    """M->P: the clocked release is less than the allocated UI, and the rest is positive.

    The margin is then `(1 − p_DZ)·UI + B` with B > 0, non-negative at every rate.
    """
    inp = _clocked(mode, handover_UIs=1.0)
    b = compute(inp).breakdowns["MP_contention"]
    assert b.F_max_MHz == math.inf
    for f in (20.0, 50.0, 200.0):
        assert compute(_at_rate_holding_placement(inp, f)
                       ).breakdowns["MP_contention"].margin_ns > 0, (mode, f)


def _at_rate(inp: CalcInputs, f_MHz: float) -> CalcInputs:
    """`inp` at another rate: clocked Manager rows held at their UI fraction, analog ones as
    they are."""
    if launch_grid_UI(inp.launch_mode) is None:
        return replace(inp, F_CLK_target_MHz=f_MHz)
    return _at_rate_holding_placement(inp, f_MHz)


def test_the_4x_hold_fmax_agrees_with_the_launch_hint():
    """4x is bounded by hold: MP_hold's F_max is `launch_mode_max_fclk_MHz`'s 4x answer."""
    b = compute(_clocked(LaunchMode.CLK4)).breakdowns["MP_hold"]
    f_hint, bound = launch_mode_max_fclk_MHz(LaunchMode.CLK4)
    assert bound == "hold"
    assert b.F_max_MHz == pytest.approx(f_hint, rel=1e-3)


_HOLD_CASES = [
    # (leg, inputs) -- each leg's margin carries a UI-scaled part and closes at a finite rate.
    ("MP_hold", _clocked(LaunchMode.CLK4)),
    ("MP_hold", _clocked(LaunchMode.CLK2)),
    ("MP_hold_ho", _clocked(LaunchMode.CLK4, handover_UIs=0.0)),
    ("keeper_Man", CalcInputs(Man_t_DZ_max_ns=0.0, F_CLK_target_MHz=12.288)),
    ("keeper_Man", _clocked(LaunchMode.CLK4)),
    ("keeper_Per", CalcInputs(Per_t_DZ_max_ns=0.0, F_CLK_target_MHz=12.288)),
    ("PM_hold_ho", CalcInputs(Per_t_ZD_ns=0.0, Per_spec="SW13_1V8", Man_spec="SW13_1V8",
                              Man_t_IH_max_ns=40.0, F_CLK_target_MHz=6.0)),
]


@pytest.mark.parametrize("leg,inp", _HOLD_CASES)
def test_a_ui_scaled_non_setup_leg_reports_the_rate_where_it_closes(leg, inp):
    b = compute(inp).breakdowns[leg]
    assert not b.is_setup
    assert 0.0 < b.F_max_MHz < math.inf, (leg, b.F_max_MHz, b.margin_ns)
    at = compute(_at_rate(inp, b.F_max_MHz)).breakdowns[leg]
    assert at.margin_ns == pytest.approx(0.0, abs=1e-6), (leg, b.F_max_MHz)
    assert compute(_at_rate(inp, b.F_max_MHz * 1.01)).breakdowns[leg].margin_ns < 0
    assert compute(_at_rate(inp, b.F_max_MHz * 0.99)).breakdowns[leg].margin_ns > 0


def test_a_rate_independent_hold_leg_still_reads_inf_or_zero():
    """No UI-scaled part: infinity when it passes, 0.0 when it fails."""
    r = compute(CalcInputs())
    assert r.breakdowns["PM_hold"].margin_ns > 0 and r.breakdowns["PM_hold"].F_max_MHz == math.inf
    assert r.breakdowns["PP_hold"].margin_ns < 0 and r.breakdowns["PP_hold"].F_max_MHz == 0.0


_BINDING_CASES = [
    # (binding leg, inputs)
    ("PM_contention", CalcInputs(Per_ede=True, Per_ede_hold_max_ns=20.0, handover_UIs=0.0,
                                 F_CLK_target_MHz=9.6, Man_t_DD_min_ns=12.0,
                                 Per_t_DD_min_ns=8.0)),
    ("MP_hold", _clocked(LaunchMode.CLK4, Per_t_DD_max_ns=2.0, Per_t_ZD_ns=2.0)),
    ("keeper_Man", CalcInputs(Man_t_DZ_max_ns=0.0, Man_t_DD_min_ns=12.0, Per_t_DD_max_ns=6.0,
                              F_CLK_target_MHz=12.288)),
]


@pytest.mark.parametrize("leg,inp", _BINDING_CASES)
def test_every_bounding_leg_closes_just_below_the_binding_rate(leg, inp):
    """F_max_binding is the highest rate at which the whole bounding set passes.

    Each case is bound by a NON-setup leg, which a setup-only minimum misses: it reports a
    higher rate at which that leg already fails.
    """
    r = compute(inp)
    f = r.F_max_binding_MHz
    assert 0.0 < f < math.inf, (leg, f)
    assert r.binding_inequality == leg, (r.binding_inequality, f)
    below = compute(_at_rate(inp, f * 0.999))._bounding()
    assert all(b.margin_ns >= 0 for b in below.values()), \
        {k: b.margin_ns for k, b in below.items() if b.margin_ns < 0}
    assert compute(_at_rate(inp, f * 1.001)).breakdowns[leg].margin_ns < 0


def test_a_leg_failing_where_its_margin_grows_with_the_rate_binds_at_zero():
    """A failing leg whose F_max is not below the target offers no ceiling, so it pins
    F_max_binding to 0.0 rather than hiding behind an infinity."""
    # A clocked Manager release at 0.75 UI with no handover UI: MP_contention's UI-scaled
    # part is negative, so the leg fails at the target and passes at higher rates.
    inp = _clocked(LaunchMode.CLK4, handover_UIs=0.0)
    inp = replace(inp, Man_t_DZ_max_ns=0.75 * inp.UI_ns(), Per_t_ZD_ns=0.0,
                  bus_length_cm=0.0)
    b = compute(inp).breakdowns["MP_contention"]
    assert b.margin_ns < 0 and b.F_max_MHz >= inp.F_CLK_target_MHz, (b.margin_ns, b.F_max_MHz)
    r = compute(inp)
    assert r.F_max_binding_MHz == 0.0
    assert r.breakdowns[r.binding_inequality].margin_ns < 0


_CONV = re.compile(r"tabulated ([0-9]+\.[0-9]+) ns.*?→ pure ([0-9]+\.[0-9]+) ns")


@pytest.mark.parametrize("mode", [LaunchMode.CLK2, LaunchMode.CLK4])
def test_the_mp_notes_convert_the_value_in_force_under_a_clocked_launch(mode):
    """An off-grid Man_t_DD is snapped first, and the note says so before any conversion.

    The conversion half names the snapped value, so it reproduces the pure value it shows.
    A SWI3S Manager needs no anchor conversion, so no V_OL back-out appears.
    """
    inp = CalcInputs(launch_mode=mode, F_CLK_target_MHz=12.288,
                     Man_t_DD_max_ns=18.0, Man_t_DD_min_ns=7.0)
    r = compute(inp)
    for leg in ("MP_setup", "MP_hold"):
        t = next(t for t in r.breakdowns[leg].terms if t.symbol == "Man_t_DD,pure")
        assert "snapped to" in t.note or "clocked:" in t.note, (leg, t.note)
        assert "V_OL" not in t.note, (leg, t.note)
        assert "already a pure delay" in t.note, (leg, t.note)
        tab = re.search(r"tabulated ([0-9]+\.[0-9]+) ns", t.note)
        assert tab and float(tab.group(1)) == pytest.approx(abs(t.value_ns), abs=5e-3), \
            (leg, t.note)


def test_a_soundwire_mp_note_still_reproduces_its_conversion():
    """The analog path is unchanged: the note's tabulated value converts to the pure one."""
    inp = CalcInputs(Man_spec="SW13_1V8", Per_spec="SWI3S_PHY2", F_CLK_target_MHz=10.0)
    r = compute(inp)
    for leg, field, corner in (("MP_setup", "Man_t_DD_max_ns", "max"),
                               ("MP_hold", "Man_t_DD_min_ns", "min")):
        t = next(t for t in r.breakdowns[leg].terms if t.symbol == "Man_t_DD,pure")
        m = _CONV.search(t.note)
        assert m, t.note
        tab, pure = float(m.group(1)), float(m.group(2))
        assert tab == pytest.approx(getattr(inp, field), abs=5e-3)
        assert pure == pytest.approx(pure_output_delay(
            getattr(inp, field), spec="SW13_1V8", side="Man", shape=inp.Man_shape,
            corner=corner), abs=5e-3)


def test_the_soundwire_min_corner_takes_the_clock_re_anchor_only():
    """The t_DD,min slot is t_ZD_Data,Min: clock-referenced, ending before the slew."""
    from swi3s_studio.timing.spec_source import clock_anchor_frac, preset
    p = preset("SW13_1V8", "Per")
    assert p.t_DD_min_ns == p.t_ZD_ns
    got = pure_output_delay(p.t_DD_min_ns, spec="SW13_1V8", side="Per",
                            shape="linear", corner="min")
    assert got == pytest.approx(
        p.t_DD_min_ns + clock_anchor_frac("SW13_1V8", "linear") * p.tRF_ns)
    assert got != pytest.approx(p.t_DD_min_ns)
