"""Per-leg timelines (`swi3s_studio/timing/timeline.py`): the events a leg's terms connect,
placed on their ramps at the leg's corner, with one span per term.

Pinned here:
- every term of a leg's breakdown has exactly one span, as long as the term's value, and the
  margin span is the margin, over specs, shapes, slews, handover UIs, placements and launch
  modes. The timeline places its events from the inputs, so this checks the calculator and
  the timeline against each other;
- every crossing and detection lies on its ramp at its threshold, and every anchor and clock
  reference at its ramp's 20 % point; on an edge of zero t_RF, an ideal step, every one of
  them lies at the step;
- past its settling time a linear ramp holds the far rail;
- each threshold is the receiver's own level plus the ground offset, and a P→M setup leg's
  two offsets are one physical offset seen from each side;
- the crossing corners reproduce the envelope's coefficients, counted from the 20 % point;
- each term names the ramps it is drawn with, an end of it that lies on a ramp lies on one
  of them, and a launch is drawn at the launcher's own pin, from its clock reference to its data's
  anchor;
- a flight mismatch is drawn with no ramps, as the clock's and the data's flights from the
  launch, and a setup or hold window with the clock and the data at the receiver, beside the
  margin;
- one device has one supply in a leg: the rail of every ramp it drives and the supply of
  every threshold it receives at, at the leg's supply corner;
- a clock edge two peripherals both detect has one t_RF at A and at B, the calculator's edge
  factor, and detections of two edges take their corners' own;
- a handover's launching edge is of the polarity its count gives, on M→P and on P→P, where
  A's threshold is that polarity's early corner;
- every leg has a timeline, the contention and keeper legs too, over EDE on either side,
  handover UIs 0/1/2, SoundWire on either side and both ramp shapes; a leg that does not
  apply (a forced keeper's keeper_Man) is refused with a ValueError naming the reason;
- a release is where its driver goes high-Z, a conventional contention releaser's last bit
  being held at the other level to the acquirer's first edge, and an EDE Delay starts at its
  ramp's 80 % point;
- a turn-on leaves high-Z at the launcher's pin and a t_DD launch does not;
- a contention leg is drawn at its releaser's pin (M→P) or with both devices at the Manager
  (P→M, P→P), at the closing-edge polarity its breakdown reports, the releaser's clock on
  the edge its timing counts from and the acquirer's on the edge its handover count gives;
- a keeper's response starts at the data's crossing of the keeper's threshold at the Manager
  (V_IH,max / V_IL,min on a SWI3S Manager, V_TH_Keeper,max / V_TL_Keeper,min on a SoundWire
  one), offset by the ground shift for a Peripheral's data;
- `LegTimeline.event` and `.ramp` raise a KeyError naming the leg and the key for a key the
  timeline does not hold.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import replace

import pytest

from swi3s_studio.timing import CalcInputs, compute
from swi3s_studio.timing.calculator import INEQUALITIES
from swi3s_studio.timing.delta_tpd import _corner_coefficient, per_tdd_offset_frac
from swi3s_studio.timing.spec_source import (
    LaunchMode,
    data_input_window,
    sw13_keeper_thresholds,
)
from swi3s_studio.timing.timeline import CO_LOCATED_NOTE, SUPPORTED_LEGS, leg_timeline

_OTHER = ("MP_contention", "PM_contention", "PP_contention", "keeper_Man", "keeper_Per")
SETUP_HOLD = tuple(leg for leg in INEQUALITIES if leg not in _OTHER)


def _cases():
    specs = ("SWI3S_PHY2", "SWI3S_PHY2_PROP", "SW13_1V2")
    slews = ((5.0, 5.0, 5.0), (8.3, 8.3, 8.3), (3.0, 3.0, 3.0), (8.3, 6.0, 3.0), (3.0, 5.0, 8.3))
    for man, per in itertools.product(specs, specs):
        for k, s_man, s_per in slews:
            yield CalcInputs(Man_spec=man, Per_spec=per, tRF_Man_CLK_ns=k,
                             tRF_Man_DATA_ns=s_man, tRF_Per_DATA_ns=s_per)
    base = CalcInputs()
    for kw in ({"Man_shape": "exp"}, {"Per_shape": "exp"},
               {"Man_shape": "exp", "Per_shape": "exp", "tRF_Per_DATA_ns": 8.3},
               {"handover_UIs": 0.0}, {"handover_UIs": 2.0},
               {"pp_placement": "driver_far"}, {"pp_placement": "both_far"},
               {"launch_mode": LaunchMode.CLK4, "F_CLK_target_MHz": 12.288},
               {"bus_length_cm": 0.0}, {"tRF_tolerance_frac": 0.0},
               {"V_noise_pp_frac": 0.0, "V_SEOS_tol_frac": 0.0},
               {"V_IH_min_frac": 0.40, "V_IL_max_frac": 0.50},
               {"V_IH_min_frac": 0.40, "V_IL_max_frac": 0.50, "handover_UIs": 0.0},
               {"V_IH_min_frac": 0.40, "V_IL_max_frac": 0.50, "handover_UIs": 2.0},
               _SHIFTED, {**_SHIFTED, "handover_UIs": 2.0},
               {"V_IL_max_frac": 0.60, "V_noise_pp_frac": 0.0},
               _ASYMMETRIC, {**_ASYMMETRIC, "Man_shape": "exp", "Per_shape": "exp"},
               {**_ASYMMETRIC, "handover_UIs": 2.0, "Man_shape": "exp"},
               {**_ASYMMETRIC, "Man_ede": True, "Per_ede": True, "Per_shape": "exp"},
               {**_ASYMMETRIC, "Man_spec": "SW13_1V2", "Per_spec": "SW13_1V2"},
               {**_WIDE, "Man_ede": True, "Per_ede": True, "Man_shape": "exp",
                "Per_shape": "exp"},
               {**_LOW_V_OL, "Man_ede": True, "Per_ede": True, "Man_shape": "exp",
                "Per_shape": "exp"},
               {**_LOW_V_OL, "Man_shape": "exp", "Per_shape": "exp"},
               {"tRF_Per_DATA_ns": 0.0},
               {"tRF_Man_CLK_ns": 0.0, "tRF_Man_DATA_ns": 0.0, "Per_shape": "exp"}):
        yield replace(base, **kw)
    # The releases and keepers: EDE on either side or both, handover UIs 0/1/2, SoundWire
    # on either side, the shapes alternating, and no keeper requirement.
    for i, ((m_ede, p_ede), n, (man, per)) in enumerate(itertools.product(
            ((True, False), (False, True), (True, True)), (0.0, 1.0, 2.0),
            (("SWI3S_PHY2", "SWI3S_PHY2"), ("SW13_1V2", "SWI3S_PHY2"),
             ("SWI3S_PHY2", "SW13_1V2")))):
        shape = "exp" if i % 2 else "linear"
        yield replace(base, Man_ede=m_ede, Per_ede=p_ede, handover_UIs=n, Man_spec=man,
                      Per_spec=per, Man_shape=shape, Per_shape=shape)
    for kw in ({"t_keeper_ns": 0.0}, {"t_keeper_ns": 0.0, "Man_ede": True, "Per_ede": True},
               {"Per_t_DZ_max_ns": 0.0}, {"Man_spec": "SW13_1V2", "Per_spec": "SW13_1V2"},
               {"Man_spec": "SW13_1V2", "Per_spec": "SW13_1V2", "Man_ede": True,
                "Per_ede": True, "Man_shape": "exp", "Per_shape": "exp"},
               {"launch_mode": LaunchMode.CLK4, "F_CLK_target_MHz": 12.288, "Man_ede": True}):
        yield replace(base, **kw)


# A window below mid-rail, where every setup and hold leg's case of the other polarity binds.
_SHIFTED = {"V_IH_min_frac": 0.40, "V_IH_max_frac": 0.60, "V_IL_min_frac": 0.30,
            "V_IL_max_frac": 0.50, "tRF_Per_DATA_ns": 6.0}


# A V_OH / V_OL pair with V_OH + V_OL ≠ 1, where a rising edge's 20 % point (V_OL) and a
# falling edge's (V_OH) sit at different fractions of their ramps.
_ASYMMETRIC = {"V_OH_min_frac": 0.85, "V_OL_max_frac": 0.25}

# V_OH / V_OL pairs where an exponential ramp's anchor-to-valid time is not one t_RF: wider than
# its 20-80 % t_RF at either polarity, and with V_OH + V_OL ≠ 1 longer falling than rising.
_WIDE = {"V_OH_min_frac": 0.90, "V_OL_max_frac": 0.10}
_LOW_V_OL = {"V_OH_min_frac": 0.80, "V_OL_max_frac": 0.15}


CASES = list(_cases())


def test_every_leg_has_a_timeline():
    assert set(SUPPORTED_LEGS) == set(INEQUALITIES)
    for leg in INEQUALITIES:
        assert leg_timeline(CalcInputs(), leg).leg == leg
    with pytest.raises(ValueError):
        leg_timeline(CalcInputs(), "MP_setup_typo")


def test_a_leg_that_does_not_apply_has_no_timeline():
    """A forced keeper leaves keeper_Man not applicable; `leg_timeline` refuses it with a
    ValueError that names the leg and the breakdown's reason, and still draws keeper_Per."""
    inp = CalcInputs(Man_keeper_forced=True)
    reason = compute(inp).breakdowns["keeper_Man"].not_applicable
    assert reason
    with pytest.raises(ValueError, match="keeper_Man: does not apply") as err:
        leg_timeline(inp, "keeper_Man")
    assert reason in str(err.value)
    assert leg_timeline(inp, "keeper_Per").leg == "keeper_Per"


def test_every_term_has_one_span_as_long_as_its_value():
    """The spans are the terms: each term once, as long as its value, and the margin span the
    margin, which the terms sum to. A setup leg's UI is a span too, from the origin to the
    sampling reference."""
    for inp in CASES:
        breakdowns = compute(inp).breakdowns
        for leg in SUPPORTED_LEGS:
            tl = leg_timeline(inp, leg)
            terms = breakdowns[leg].terms
            covered = sorted(sp.term for sp in tl.spans if sp.term is not None)
            assert covered == list(range(len(terms))), (leg, covered, len(terms))
            margins = [sp for sp in tl.spans if sp.term is None]
            assert len(margins) == 1, leg
            assert tl.duration(margins[0]) == pytest.approx(breakdowns[leg].margin_ns,
                                                            abs=1e-9), (leg, inp)
            signed = sum(abs(tl.duration(sp)) * (1 if terms[sp.term].value_ns >= 0 else -1)
                         for sp in tl.spans if sp.term is not None)
            assert signed == pytest.approx(tl.duration(margins[0]), abs=1e-9), (leg, inp)
            for sp in tl.spans:
                if sp.term is None:
                    continue
                assert abs(tl.duration(sp)) == pytest.approx(abs(terms[sp.term].value_ns),
                                                             abs=1e-9), (leg, sp.label, inp)


def test_every_event_lies_on_its_ramp():
    """A crossing or a detection is where its ramp reaches its threshold; an anchor or a clock
    reference is where its ramp is 20 % of the way across (V_OL rising, V_OH falling); a
    ramp's 80 % point where it reaches V_OH rising and V_OL falling, on either shape; a release
    where the ramp's driver goes high-Z. On an edge of zero t_RF, an ideal step, each of the
    others lies at the step: the ramp is at its first rail before it and its far rail from it."""
    seen = set()
    for inp in CASES:
        for leg in SUPPORTED_LEGS:
            tl = leg_timeline(inp, leg)
            for e in tl.events:
                if e.ramp is None:
                    continue
                r = tl.ramp(e.ramp)
                level = r.level(e.t_ns)
                if (r.edge and r.t_rf_ns <= 0.0
                        and e.kind in ("crossing", "detection", "anchor", "reference", "valid")):
                    seen.add("step")
                    first, far = (0.0, r.vdd_tx) if r.rising else (r.vdd_tx, 0.0)
                    assert e.t_ns == pytest.approx(r.start_ns, abs=1e-9), (leg, e.key, inp)
                    assert (r.level(r.start_ns - 1e-6), r.level(r.start_ns)) == (first, far), \
                        (leg, e.key, inp)
                    continue
                if e.kind in ("crossing", "detection"):
                    th = next(t for t in tl.thresholds if t.ramp == e.ramp)
                    want = th.corner.threshold
                elif e.kind in ("anchor", "reference"):
                    want = r.vdd_tx * (inp.V_OL_max_frac if r.rising else inp.V_OH_min_frac)
                elif e.kind == "valid":
                    want = r.vdd_tx * (inp.V_OH_min_frac if r.rising else inp.V_OL_max_frac)
                elif e.kind == "release":
                    seen.add(e.kind)
                    assert r.off_ns == pytest.approx(e.t_ns, abs=1e-12), (leg, e.key)
                    continue
                else:
                    continue
                seen.add(e.kind)
                assert level == pytest.approx(want, abs=1e-9), (leg, e.key, inp)
    assert seen == {"crossing", "detection", "anchor", "reference", "valid", "release",
                    "step"}, seen


def test_each_threshold_is_the_receivers_level_plus_the_ground_offset():
    """On its own ground the receiver's threshold is its V_IH or V_IL bound at its supply
    corner, from its own window: a SoundWire Data input's (`data_input_window`), else the
    bus's V_IH / V_IL rows (a SWI3S keeper's V_IH,max / V_IL,min among them), and a SoundWire
    keeper's threshold. Against the transmitter's
    ground it is moved by the ground offset, 0 or α. On P→M setup the clock crosses at the
    Peripheral and the data at the Manager, so the one offset between the two grounds raises
    one threshold and lowers the other."""
    for inp in (CalcInputs(), CalcInputs(Man_spec="SW13_1V2"),
                CalcInputs(Per_spec="SW13_1V2"),
                CalcInputs(Man_spec="SW13_1V2", Man_ede=True, Per_ede=True)):
        bus = (inp.V_IH_min_frac, inp.V_IH_max_frac, inp.V_IL_min_frac, inp.V_IL_max_frac)
        for leg in SUPPORTED_LEGS:
            tl = leg_timeline(inp, leg)
            signal = {r.key: r.signal for r in tl.ramps}
            for th in tl.thresholds:
                c = th.corner
                assert c.threshold == pytest.approx(c.rx_threshold + c.ground_shift)
                if c.bound.startswith(("V_TH_Keeper", "V_TL_Keeper")):
                    keeper = sw13_keeper_thresholds(c.rx_supply)
                    assert c.rx_threshold == pytest.approx(keeper[0 if c.rising else 1])
                    continue
                spec = inp.Man_spec if th.receiver == "Manager" else inp.Per_spec
                window = ((data_input_window(spec) if signal[th.ramp] == "data" else None)
                          or bus)
                bound = dict(zip(("V_IH,min", "V_IH,max", "V_IL,min", "V_IL,max"),
                                 window))[c.bound]
                assert c.rx_threshold == pytest.approx(bound * c.rx_supply), (leg, th)
                assert abs(c.ground_shift) in (0.0, pytest.approx(inp.V_noise_pp_frac)), \
                    (leg, th)
    inp = CalcInputs()
    tl = leg_timeline(inp, "PM_setup")
    shifts = {th.receiver: th.corner.ground_shift for th in tl.thresholds}
    assert shifts["Peripheral"] == pytest.approx(-shifts["Manager"])
    assert shifts["Peripheral"] != 0.0


def test_the_crossing_corners_reproduce_the_envelope():
    """Every per-device corner gives the coefficient the envelope holds under the same name,
    counted from its ramp's 20 % point at the corner's own polarity."""
    for kw in ({}, {"Man_shape": "exp"}, {"Per_shape": "exp"}, {"Per_spec": "SW13_1V2"},
               {"Man_spec": "SW13_1V2"},
               {"V_noise_pp_frac": 0.02, "V_SEOS_tol_frac": 0.03},
               _ASYMMETRIC, {**_ASYMMETRIC, "Man_shape": "exp"}):
        inp = replace(CalcInputs(), **kw)
        env, corners = inp.envelope(), inp.crossing_corners()
        swing = inp.V_OH_min_frac - inp.V_OL_max_frac
        for name, c in corners.items():
            # A peripheral drives the P->P data lane and the P->M data lane; the Manager drives
            # the clock and the M->P data lane.
            per_driven = name.endswith("_per") or (name.startswith("sigma_pm") and "data" in name)
            shape = inp.Per_shape if per_driven else inp.Man_shape
            want = getattr(env, name)
            got = _corner_coefficient(c, shape, swing_frac=swing,
                                      tRF_tolerance=inp.tRF_tolerance_frac,
                                      anchor_frac=per_tdd_offset_frac(
                                          shape, V_OH_frac=inp.V_OH_min_frac,
                                          V_OL_frac=inp.V_OL_max_frac, rising=c.rising))
            assert got == pytest.approx(want, abs=1e-12), (name, kw)


def test_each_term_is_drawn_with_the_ramps_it_connects():
    """Every span's view names ramps the timeline holds and is not empty but a flight
    mismatch's, and an end on a ramp is on one the span is drawn with."""
    for inp in CASES:
        for leg in SUPPORTED_LEGS:
            tl = leg_timeline(inp, leg)
            keys = {r.key for r in tl.ramps}
            for sp in tl.spans:
                if sp.label == "t_PD,mis":
                    assert sp.view == (), leg
                    continue
                assert sp.view, (leg, sp.label)
                assert set(sp.view) <= keys, (leg, sp.label)
                ends = [sp.start, sp.end] + [k for g in sp.guides for k in g[:2]]
                for end in map(tl.event, ends):
                    if end.ramp is not None and end.kind != "origin":
                        assert end.ramp in sp.view, (leg, sp.label, end.key)


def test_a_launch_is_drawn_at_the_launchers_pin():
    """Man_t_DD runs from the Manager's clock reference to its data's 20 % point, both at the
    Manager's pin; Per_t_DD from the peripheral's detection to its data's 20 % point at its
    own pin. With handover UIs the Manager's launching edge is that many UIs after the
    closing one, of the polarity the count gives."""
    for inp in (CalcInputs(), CalcInputs(Man_spec="SW13_1V2", Per_spec="SW13_1V2")):
        for leg, sym, pin in (("MP_setup", "Man_t_DD", "Manager"),
                              ("MP_hold_ho", "Man_t_ZD", "Manager"),
                              ("PM_setup", "Per_t_DD", "Peripheral"),
                              ("PP_hold", "Per_t_DD", "Peripheral A")):
            tl = leg_timeline(inp, leg)
            terms = compute(inp).breakdowns[leg].terms
            spans = [sp for sp in tl.spans if sp.term is not None
                     and terms[sp.term].symbol.split(",")[0] == sym]
            assert spans, (leg, sym)
            for sp in spans:
                ramps = [tl.ramp(k) for k in sp.view]
                assert {(r.pin, r.signal) for r in ramps} == {(pin, "clock"), (pin, "data")}
            first, last = tl.event(spans[0].start), tl.event(spans[-1].end)
            assert tl.ramp(first.ramp).signal == "clock", (leg, first)
            assert last.kind == "anchor" and tl.ramp(last.ramp).signal == "data", (leg, last)
    for n, rising in ((1.0, False), (2.0, True)):
        tl = leg_timeline(CalcInputs(handover_UIs=n), "MP_hold_ho")
        closing, launching = tl.ramp("clk@man"), tl.ramp("clk_l@man")
        assert launching.rising is rising and closing.rising
        assert launching.start_ns - closing.start_ns == pytest.approx(n * CalcInputs().UI_ns())


def test_a_pp_handover_launch_is_on_the_polarity_its_count_gives():
    """On PP_hold_ho B samples on a rising edge and A launches N_HO edges later: A's clock ramp
    rises at an even count and falls at an odd one, and A's threshold is that polarity's early
    corner, V_IH,min rising and V_IL,max falling. PP_hold's A launches on B's edge."""
    for n in (0.0, 1.0, 2.0, 3.0):
        inp = CalcInputs(handover_UIs=n)
        tl = leg_timeline(inp, "PP_hold_ho")
        clk_a, clk_b = tl.ramp("clk@a"), tl.ramp("clk@b")
        rising = n % 2 == 0
        assert clk_b.rising and clk_a.rising is rising, n
        corner = next(t.corner for t in tl.thresholds if t.ramp == "clk@a")
        assert corner.rising is rising, n
        assert corner.bound == ("V_IH,min" if rising else "V_IL,max"), n
        assert tl.event("det_a").ramp == "clk@a", n
        assert leg_timeline(inp, "PP_hold").ramp("clk@a").rising, n


def test_a_falling_detection_is_drawn_on_a_falling_clock_edge():
    """PM_hold_ho at an odd handover count: the Peripheral detects the falling launching edge,
    so its clock ramp falls and its threshold is V_IL,max. PP_hold_ho where B's falling
    sampling edge binds: B's clock ramp falls with its threshold at V_IL,min, A's data rises,
    and A's edge is B's flipped at an odd count."""
    for n in (1.0, 2.0, 3.0):
        odd = n % 2 == 1
        tl = leg_timeline(CalcInputs(handover_UIs=n), "PM_hold_ho")
        clk_p = tl.ramp("clk@per")
        assert clk_p.rising is not odd, n
        assert tl.ramp("clk_l@man").rising is clk_p.rising, n
        corner = next(t.corner for t in tl.thresholds if t.ramp == "clk@per")
        assert corner.bound == ("V_IL,max" if odd else "V_IH,min"), n
    seen = set()
    for n in (0.0, 1.0, 2.0):
        inp = CalcInputs(handover_UIs=n, V_IH_min_frac=0.40, V_IL_max_frac=0.50)
        b_fall = any("X_MP,B,fall," in t.symbol
                     for t in compute(inp).breakdowns["PP_hold_ho"].terms)
        seen.add(b_fall)
        tl = leg_timeline(inp, "PP_hold_ho")
        clk_b, data_b = tl.ramp("clk@b"), tl.ramp("data@b")
        assert clk_b.rising is not b_fall and data_b.rising is b_fall, n
        assert tl.ramp("clk@a").rising is (clk_b.rising != (n % 2 == 1)), n
        corner = next(t.corner for t in tl.thresholds if t.ramp == "clk@b")
        assert corner.bound == ("V_IL,min" if b_fall else "V_IH,max"), n
    assert True in seen, "B's falling sampling edge binds somewhere in the sweep"


def test_each_leg_is_drawn_at_the_polarities_of_the_case_it_reports():
    """Every setup and hold leg at the sampling polarity its breakdown reports, in the default
    window and in one where the other polarity binds: the sampling edge's ramp at the sampler
    of that polarity, detected at its V_IH or V_IL corner, the launching edge of the polarity
    that follows from it, and the data's edge of the other polarity to the sampler's
    detection, or on a P->M leg the polarity its data crossing's symbol names: on PM_setup
    its Sigma_cross,PM,setup case's data letter (`rr` / `fr` rising, `rf` / `ff` falling), on
    PM_hold and PM_hold_ho its Sigma_cross,PM,hold case (`rise` or `rr` rising, `fall` or `ff`
    falling)."""
    seen = set()
    for kw in ({}, _SHIFTED, {**_SHIFTED, "handover_UIs": 2.0},
               {**_SHIFTED, "tRF_Per_DATA_ns": 3.0}):
        inp = replace(CalcInputs(), **kw)
        res = compute(inp).breakdowns
        flip = round(inp.handover_UIs) % 2 == 1
        for leg in SETUP_HOLD:
            tl = leg_timeline(inp, leg)
            s_rising = res[leg].sampling_rising
            seen.add((leg, s_rising))
            setup = "setup" in leg
            sampler, data = {"MP": (("clk_s@per" if setup else "clk@per"), "data@per"),
                             "PM": (("clk_s@man" if setup else "clk@man"), "data@man"),
                             "PP": (("clk_s@b" if setup else "clk@b"), "data@b")}[leg[:2]]
            assert tl.ramp(sampler).rising is s_rising, (kw, leg)
            if leg[:2] != "PM":
                corner = next(t.corner for t in tl.thresholds if t.ramp == sampler)
                assert corner.rising is s_rising, (kw, leg)
                assert tl.ramp(data).rising is not s_rising, (kw, leg)
            if leg[:2] == "PM":
                launch = s_rising != (leg == "PM_hold_ho" and flip) if not setup \
                    else not s_rising
                assert tl.ramp("clk@per").rising is launch, (kw, leg)
                data_sym = next(t.symbol for t in res[leg].terms
                                if t.symbol.endswith("· Per_t_RF,DATA"))
                quals = set(data_sym.split(" · ")[0].split(",")[1:-1])
                data_rising = bool(quals & ({"rr", "fr"} if setup else {"rise", "rr"}))
                assert tl.ramp(data).rising is data_rising, (kw, leg, data_sym)
            if leg[:2] == "PP":
                a_launch = (not s_rising if setup
                            else s_rising != (leg == "PP_hold_ho" and flip))
                assert tl.ramp("clk@a").rising is a_launch, (kw, leg)
    assert {(leg, r) for leg in SETUP_HOLD for r in (True, False)} <= seen, \
        sorted(set((leg, r) for leg in SETUP_HOLD for r in (True, False)) - seen)


def test_a_mismatch_is_the_two_flights_and_a_window_both_signals_at_the_receiver():
    """t_PD,mis is the data's flight against the clock's: drawn with no ramps, beside the
    clock's flight and the data's from one start, the data's 20 % point at the Manager, and
    ending at their two ends. The longer flight is t_PD, the shorter t_PD less the mismatch:
    the data's is the longer on setup, the clock's on hold. A setup or hold window is drawn
    as the clock and the data at the receiver, beside the margin from the data's crossing."""
    windows = {"MP": ("Per_t_IS", "Per_t_IH", "Peripheral"),
               "PM": ("Man_t_IS", "Man_t_IH", "Manager"),
               "PP": ("Per_t_IS", "Per_t_IH", "Peripheral B")}
    mis_seen: set[str] = set()
    window_seen: set[str] = set()
    for inp in (CalcInputs(), CalcInputs(handover_UIs=0.0), CalcInputs(handover_UIs=2.0)):
        terms_of = compute(inp).breakdowns
        for leg in SETUP_HOLD:
            tl = leg_timeline(inp, leg)
            terms = terms_of[leg].terms
            for sp in tl.spans:
                if sp.term is None:
                    continue
                sym = terms[sp.term].symbol
                pins = {(tl.ramp(k).pin, tl.ramp(k).signal) for k in sp.view}
                if sym == "t_PD,mis":
                    mis_seen.add(leg)
                    assert pins == set(), leg
                    (c0, c1, c_label), (d0, d1, d_label) = sp.guides
                    assert (c_label, d_label) == ("Clock propagation", "Data propagation")
                    assert c0 == d0 and tl.event(c0).kind == "anchor"
                    assert tl.ramp(tl.event(c0).ramp).key == "data@man"
                    clock = tl.event(c1).t_ns - tl.event(c0).t_ns
                    data = tl.event(d1).t_ns - tl.event(d0).t_ns
                    longer, shorter = (data, clock) if "setup" in leg else (clock, data)
                    assert longer == pytest.approx(inp.t_PD_ns(), abs=1e-12)
                    assert shorter == pytest.approx(inp.t_PD_ns() - inp.t_PD_mis_ns(),
                                                    abs=1e-12)
                    assert {sp.start, sp.end} == {c1, d1}
                if sym in windows[leg[:2]][:2]:
                    window_seen.add(leg)
                    pin = windows[leg[:2]][2]
                    assert pins == {(pin, "clock"), (pin, "data")}, (leg, sym)
                    (start, end, label), = sp.guides
                    assert label == "margin"
                    assert "crossing" in (start, end)
    assert mis_seen == {leg for leg in SETUP_HOLD if leg.startswith("MP_")}, mis_seen
    assert window_seen == set(SETUP_HOLD), window_seen


_DEVICE = {"Manager": 0, "Peripheral": 1, "Peripheral A": 1, "Peripheral B": 2}


def test_one_device_has_one_supply_in_a_leg():
    """Every ramp a device drives swings to its supply in the leg's supply corner, and every
    threshold it receives at is its level at that supply."""
    for inp in CASES:
        breakdowns = compute(inp).breakdowns
        for leg in SUPPORTED_LEGS:
            tl = leg_timeline(inp, leg)
            supplies = breakdowns[leg].supplies
            ramps = {r.key: r for r in tl.ramps}
            for r in tl.ramps:
                # A Peripheral's data on a P→P leg is A's, but where the ramp names B.
                dev = 2 if r.device == "Per B" else 0 if r.driver == "Man" else 1
                assert r.vdd_tx == pytest.approx(supplies[dev], abs=1e-12), (leg, r.key)
            for th in tl.thresholds:
                assert th.corner.rx_supply == pytest.approx(
                    supplies[_DEVICE[th.receiver]], abs=1e-12), (leg, th.receiver)
                assert th.corner.vdd_tx == pytest.approx(ramps[th.ramp].vdd_tx, abs=1e-12)


def test_a_clock_edge_two_peripherals_detect_has_one_t_rf_at_both():
    """On a P->P leg whose two clock detections are of one Manager edge, A's clock ramp and B's
    take that edge's one slew factor, so they have one t_RF; on two edges each takes its
    corner's own. The factors are the calculator's (`calculator.pp_clock_edges`)."""
    from swi3s_studio.timing.calculator import edge_factor, pp_clock_edges, pp_shares_clock_edge

    shared_seen = two_seen = False
    for inp in CASES:
        for leg in ("PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho"):
            tl = leg_timeline(inp, leg)
            supplies = compute(inp).breakdowns[leg].supplies
            a_edge, b_edge = pp_clock_edges(inp, inp.envelope(supplies), leg)
            clk_a = tl.ramp("clk@a")
            clk_b = tl.ramp("clk_s@b" if leg.startswith("PP_setup") else "clk@b")
            assert (clk_a.role, clk_b.role) == (a_edge, b_edge), leg
            for r, edge in ((clk_a, a_edge), (clk_b, b_edge)):
                assert r.t_rf_ns == pytest.approx(
                    inp.tRF_Man_CLK_ns * edge_factor(inp, "Man CLK", edge), abs=1e-12), leg
            if pp_shares_clock_edge(inp, leg):
                shared_seen = True
                assert a_edge == b_edge, leg
                assert clk_a.t_rf_ns == pytest.approx(clk_b.t_rf_ns, abs=1e-12), leg
            else:
                two_seen = True
                own = ("late", "early") if leg.startswith("PP_setup") else ("early", "late")
                assert (a_edge, b_edge) == own, leg
    assert shared_seen and two_seen


def test_the_pp_clock_ramps_are_drawn_at_the_slew_factors_the_symbols_name():
    """A P->P data leg's two clock ramps are drawn at the slew factors its gathered clock term
    names: each detection's role, `late` (1 + τ) or `early` (1 − τ), or the other factor where
    a `slow` / `fast` qualifier says its edge is the other corner's. So the diagram's clock
    ramps are the ones that set the margin, on setup and hold alike."""
    import re

    def factor(sym: str, dev: str) -> str:
        m = re.search(rf"X_MP,{dev},([a-z,]*?)(late|early)\b", sym)
        assert m, (sym, dev)
        quals = m.group(1).split(",")
        return "late" if "slow" in quals else "early" if "fast" in quals else m.group(2)

    for inp in CASES:
        breakdowns = compute(inp).breakdowns
        for leg in ("PP_setup", "PP_setup_ho", "PP_hold", "PP_hold_ho"):
            (sym,) = [t.symbol for t in breakdowns[leg].terms
                      if t.symbol.endswith("· Man_t_RF,CLK") and "X_MP,A," in t.symbol]
            tl = leg_timeline(inp, leg)
            clk_a = tl.ramp("clk@a")
            clk_b = tl.ramp("clk_s@b" if leg.startswith("PP_setup") else "clk@b")
            assert (clk_a.role, clk_b.role) == (factor(sym, "A"), factor(sym, "B")), (leg, sym)


def test_a_linear_ramp_stops_at_its_rail():
    """Past its settling time a linear ramp holds the far rail: V_DD rising, 0 falling."""
    from swi3s_studio.timing.delta_tpd import ramp_level

    for rising, rail in ((True, 1.05), (False, 0.0)):
        for t in (1.0 / 0.6, 2.0 / 0.6, 1e6):
            assert ramp_level("linear", rising, t, V_DD=1.05, swing_frac=0.6) == pytest.approx(rail)


_CONTENTION = ("MP_contention", "PM_contention", "PP_contention")
_RELEASE_CASES = [replace(CalcInputs(), **kw) for kw in (
    {}, {"handover_UIs": 0.0}, {"handover_UIs": 2.0}, {"Man_ede": True, "Per_ede": True},
    {"Man_ede": True, "Per_ede": True, "handover_UIs": 0.0}, _SHIFTED,
    {**_SHIFTED, "Per_ede": True}, {"Man_spec": "SW13_1V2"}, {"Per_spec": "SW13_1V2"},
    {"Man_spec": "SW13_1V2", "Man_ede": True, "Per_ede": True},
    {"Man_spec": "SW13_1V2", "Per_spec": "SW13_1V2", "Man_shape": "exp", "Per_shape": "exp"},
    {**_ASYMMETRIC, "Man_spec": "SW13_1V2", "Per_spec": "SW13_1V2"})]


def _span_of(tl, terms, pred):
    return [sp for sp in tl.spans if sp.term is not None and pred(terms[sp.term].symbol)]


def test_a_release_is_where_the_driver_goes_high_z():
    """On every contention and keeper leg the release is one event, at its ramp's `off_ns`. An
    EDE releaser's last bit is an edge with its 80 % point between its anchor and the release;
    a conventional contention release holds a level with no edge, the other level to the
    acquirer's first edge (high where that edge falls, as it does on a SoundWire side whose
    V_OH + V_OL is not 1), and a keeper's releaser drives its edge and holds it, the keeper's
    threshold crossing (a conventional release) or the 80 % point (EndDriveEarly) between its
    anchor and the release at one pin."""
    held = set()
    for inp in _RELEASE_CASES:
        for leg in (*_CONTENTION, "keeper_Man", "keeper_Per"):
            tl = leg_timeline(inp, leg)
            rel = tl.event("release")
            r = tl.ramp(rel.ramp)
            assert r.off_ns == pytest.approx(rel.t_ns, abs=1e-12), leg
            side = ("Man" if leg in ("MP_contention", "keeper_Man") else "Per")
            ede = inp.Man_ede if side == "Man" else inp.Per_ede
            if ede or leg.startswith("keeper"):
                assert r.edge, leg
                # A conventional keeper leg reads the keeper's crossing, not V_OH.
                key = "kcross" if leg.startswith("keeper") and not ede else "valid"
                point = tl.event(key)
                anchor = tl.event("anchor_man" if point.ramp != r.key else "rel_anchor")
                release = tl.event("release_man" if point.ramp != r.key else "release")
                assert anchor.t_ns < point.t_ns <= release.t_ns + 1e-12, (leg, key)
            else:
                (margin,) = (sp for sp in tl.spans if sp.term is None)
                acq = tl.ramp(tl.event(margin.end).ramp)
                assert not r.edge and r.level(rel.t_ns) == (0.0 if acq.rising else r.vdd_tx), \
                    (leg, inp)
                held.add(r.rising)
            if leg in _CONTENTION and ede:
                assert r.rising is (_to_valid(inp, r.shape, True)
                                    > _to_valid(inp, r.shape, False) + 1e-12), \
                    "an EDE releaser's last bit is the edge slower to V_OH / V_OL, else falling"
    assert held == {False, True}, held


def _to_valid(inp, shape, rising):
    """A ramp's time from its anchor to V_OH (rising) / V_OL (falling), in units of its t_RF:
    1 on a linear ramp, and on an exponential one (1 − 4^(−t) rising, 4^(−t) falling) the
    closed form."""
    v_oh, v_ol = inp.V_OH_min_frac, inp.V_OL_max_frac
    if shape == "linear":
        return 1.0
    if rising:
        return math.log(1.0 - v_ol, 4.0) - math.log(1.0 - v_oh, 4.0)
    return math.log(v_oh, 4.0) - math.log(v_ol, 4.0)


@pytest.mark.parametrize("levels", [_WIDE, _LOW_V_OL, _ASYMMETRIC, {}])
def test_the_ramp_to_v_oh_is_drawn_as_long_as_its_term(levels):
    """The `<dev>_t_RF,DATA` span of an EndDriveEarly release, on a contention leg and on a
    keeper leg, runs from the launch's anchor to where the drawn ramp reaches V_OH / V_OL, on
    the edge of the polarity the term is priced at: (c · the edge's t_RF) with c the
    closed-form crossing, so the span, the term and the ramp agree on an exponential ramp at
    any level pair. A contention leg's edge is the longer of the two; a keeper leg's is the one
    its keeper crossing gives (`calculator._keeper_case`). The symbol names that edge, `,rise`
    or `,fall`, and it is the drawn ramp's."""
    from swi3s_studio.timing.calculator import edge_factor
    inp = replace(CalcInputs(), Man_shape="exp", Per_shape="exp", Man_ede=True, Per_ede=True,
                  handover_UIs=0.0, **levels)
    res = compute(inp).breakdowns
    c_rise, c_fall = (_to_valid(inp, "exp", r) for r in (True, False))
    for leg, side in (("MP_contention", "Man"), ("PM_contention", "Per"),
                      ("PP_contention", "Per"), ("keeper_Man", "Man"), ("keeper_Per", "Per")):
        tl = leg_timeline(inp, leg)
        (sp,) = _span_of(tl, res[leg].terms,
                         lambda s, d=side: " · " not in s and s.startswith(f"{d}_t_RF,DATA"))
        start, end = tl.event(sp.start), tl.event(sp.end)
        r = tl.ramp(end.ramp)
        c = c_rise if r.rising else c_fall
        if leg.endswith("_contention"):
            assert c == pytest.approx(max(c_rise, c_fall), abs=1e-12), leg
        lane = f"{side} DATA"
        at = replace(inp, fast_data_edge=res[leg].fast_data_edge)
        want = c * edge_factor(at, lane, "late") * getattr(
            inp, {"Man": "tRF_Man_DATA_ns", "Per": "tRF_Per_DATA_ns"}[side])
        assert tl.duration(sp) == pytest.approx(want, abs=1e-9), (leg, levels)
        assert start.kind == "anchor" and end.kind == "valid", leg
        edge = ",rise" if r.rising else ",fall"
        fast = ",fast" if lane in res[leg].fast_data_edge else ""
        assert sp.label == f"{side}_t_RF,DATA{edge}{fast}", (leg, sp.label, levels)
        level = inp.V_OH_min_frac if r.rising else inp.V_OL_max_frac
        assert r.level(end.t_ns) == pytest.approx(r.vdd_tx * level, abs=1e-9), leg


def test_an_ede_delay_starts_at_the_ramps_80_percent_point():
    """Every EDE Delay span runs from its releaser's 80 % point (V_OL falling, V_OH rising) to
    the release, on either shape."""
    for inp in _RELEASE_CASES:
        if not (inp.Man_ede or inp.Per_ede):
            continue
        res = compute(inp).breakdowns
        for leg in (*_CONTENTION, "keeper_Man", "keeper_Per"):
            tl = leg_timeline(inp, leg)
            spans = _span_of(tl, res[leg].terms, lambda s: s.endswith("_t_EDE"))
            for sp in spans:
                start, end = tl.event(sp.start), tl.event(sp.end)
                assert (start.kind, end.kind) == ("valid", "release"), leg
                r = tl.ramp(start.ramp)
                want = inp.V_OH_min_frac if r.rising else inp.V_OL_max_frac
                assert r.level(start.t_ns) == pytest.approx(r.vdd_tx * want, abs=1e-9), leg
            side = "Man" if leg in ("MP_contention", "keeper_Man") else "Per"
            assert bool(spans) is (inp.Man_ede if side == "Man" else inp.Per_ede), leg


def test_a_turn_on_leaves_high_z_and_a_t_dd_launch_does_not():
    """Every t_ZD launch ends at the 20 % point of a ramp that leaves high-Z at the launcher's
    own pin; every t_DD launch at one that does not."""
    seen = set()
    for inp in _RELEASE_CASES:
        res = compute(inp).breakdowns
        for leg in SUPPORTED_LEGS:
            tl = leg_timeline(inp, leg)
            for which in ("ZD", "DD"):
                spans = _span_of(tl, res[leg].terms,
                                 lambda s, w=which: s.split(",")[0].endswith(f"_t_{w}"))
                if not spans:
                    continue
                end = tl.event(spans[-1].end)
                r = tl.ramp(end.ramp)
                assert end.kind == "anchor" and r.from_hiz is (which == "ZD"), (leg, which)
                seen.add(which)
                launcher = spans[0].start
                clk = tl.ramp(tl.event(launcher).ramp)
                assert clk.pin == r.pin, (leg, which)
    assert seen == {"ZD", "DD"}


def test_a_contention_leg_is_drawn_at_the_releasers_pin_or_co_located():
    """MP_contention: the release and the acquirer's 20 % point the margin runs to are both at
    the Manager, the clock's flight out the first t_PD and the acquirer's drive flying back,
    from its 20 % point at its pin to that point at the Manager, the second. PM and
    PP_contention: the releaser's and the acquirer's data at their own pins, with no flight,
    and the note says both devices are drawn at the Manager."""
    for inp in _RELEASE_CASES:
        terms_of = compute(inp).breakdowns
        for leg in _CONTENTION:
            tl = leg_timeline(inp, leg)
            terms = terms_of[leg].terms
            flights = [sp for sp in tl.spans
                       if sp.term is not None and terms[sp.term].symbol == "t_PD"]
            (margin,) = [sp for sp in tl.spans if sp.term is None]
            start, end = tl.event(margin.start), tl.event(margin.end)
            assert start.kind == "release" and end.kind == "anchor", leg
            pins = (tl.ramp(start.ramp).pin, tl.ramp(end.ramp).pin)
            if leg == "MP_contention":
                assert pins == ("Manager", "Manager") and tl.note == "", leg
                assert tl.ramp(end.ramp).from_hiz is False
                assert tl.ramp("data_acq@per").from_hiz
                assert len(flights) == 2, (leg, [sp.label for sp in flights])
                back = [sp for sp in flights if sp.end == margin.end]
                assert len(back) == 1, leg
                assert tl.ramp(tl.event(back[0].start).ramp).pin == "Peripheral", leg
                assert tl.event(back[0].start).kind == "anchor", leg
            else:
                assert flights == [], (leg, [sp.label for sp in flights])
                assert pins == {"PM_contention": ("Peripheral", "Manager"),
                                "PP_contention": ("Peripheral A", "Peripheral B")}[leg]
                assert tl.note == CO_LOCATED_NOTE, leg
                assert tl.ramp(end.ramp).from_hiz, leg


def test_contention_is_drawn_at_the_closing_polarity_it_reports():
    """The releaser's clock is the edge its timing counts from: the closing edge, or under EDE
    the opening edge of the other polarity; the acquirer's is the edge its handover count gives
    after the closing one. Each detection's threshold is its edge's polarity. keeper_Per is
    drawn at its closing polarity too, its opening edge the other. P->P's two clocks take the
    calculator's edge factors."""
    from swi3s_studio.timing.calculator import handover_flips_edge, pp_clock_edges
    seen = set()
    for inp in _RELEASE_CASES:
        res = compute(inp).breakdowns
        for leg in _CONTENTION:
            tl = leg_timeline(inp, leg)
            closing = res[leg].closing_rising
            assert closing is not None
            seen.add((leg, closing))
            ede = inp.Man_ede if leg == "MP_contention" else inp.Per_ede
            rel, acq = {"MP_contention": ("clk_o@man" if ede else "clk@man", "clk@per"),
                        "PM_contention": ("clk@per", "clk_l@man" if inp.handover_UIs
                                          else "clk@man"),
                        "PP_contention": ("clk@a", "clk@b")}[leg]
            assert tl.ramp(rel).rising is (closing != ede), (leg, inp)
            assert tl.ramp(acq).rising is (closing != handover_flips_edge(inp)), (leg, inp)
            for th in tl.thresholds:
                assert th.corner.rising is tl.ramp(th.ramp).rising, (leg, th.ramp)
            if leg == "PP_contention":
                roles = pp_clock_edges(inp, inp.envelope(res[leg].supplies), leg,
                                       closing_rising=closing)
                assert (tl.ramp("clk@a").role, tl.ramp("clk@b").role) == roles
        if not inp.Per_ede:
            tl = leg_timeline(inp, "keeper_Per")
            closing = res["keeper_Per"].closing_rising
            assert tl.ramp("clk_c@per").rising is closing
            assert tl.ramp("clk_o@per").rising is not closing
    assert {(leg, r) for leg in _CONTENTION for r in (True, False)} <= seen, seen


def test_a_keeper_response_starts_at_the_keeper_crossing():
    """The response starts at the data's crossing of the keeper's threshold, on the data at the
    Manager: a SWI3S Manager's V_IH,max / V_IL,min, a SoundWire Manager's V_TH_Keeper,max /
    V_TL_Keeper,min, at the Manager's supply and offset by the ground shift for a Peripheral's
    data; the flight to it is a guide."""
    for inp in _RELEASE_CASES:
        res = compute(inp).breakdowns
        sw = inp.Man_spec == "SW13_1V2"
        for leg in ("keeper_Man", "keeper_Per"):
            tl = leg_timeline(inp, leg)
            (sp,) = _span_of(tl, res[leg].terms, lambda s: s == "Man_t_Keeper_Response")
            start, end = tl.event(sp.start), tl.event(sp.end)
            assert end.kind == "keeper"
            r = tl.ramp(start.ramp)
            assert start.kind == "crossing" and r.pin == "Manager", leg
            (th,) = [t for t in tl.thresholds if t.ramp == r.key]
            assert th.receiver == "Manager"
            assert th.corner.bound == (("V_TH_Keeper,max" if sw else "V_IH,max")
                                       if th.corner.rising else
                                       ("V_TL_Keeper,min" if sw else "V_IL,min")), th.corner
            assert th.corner.rx_supply == pytest.approx(res[leg].supplies[0])
            alpha = 0.0 if leg == "keeper_Man" else inp.V_noise_pp_frac
            assert abs(th.corner.ground_shift) == pytest.approx(alpha)
            (xk,) = _span_of(tl, res[leg].terms, lambda s: s.startswith("X_K"))
            assert bool(xk.guides) is (leg == "keeper_Per")


def test_a_missing_event_or_ramp_raises_a_key_error_naming_it():
    """`LegTimeline.event` and `.ramp` raise KeyError naming the leg and the key for a key the
    timeline does not hold."""
    tl = leg_timeline(CalcInputs(), "MP_setup")
    assert tl.event(tl.events[0].key) is tl.events[0]
    assert tl.ramp(tl.ramps[0].key) is tl.ramps[0]
    with pytest.raises(KeyError, match="MP_setup: no event 'nowhere'"):
        tl.event("nowhere")
    with pytest.raises(KeyError, match="MP_setup: no ramp 'nowhere'"):
        tl.ramp("nowhere")
