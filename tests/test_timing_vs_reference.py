"""Cross-check: this calculator against the standalone reference analysis.

The same timing model exists twice: here in `swi3s_studio/timing/`, and in a reference
analysis whose module `swtiming.emit_ede` evaluates each of its legs.

WHAT THIS PINS. Every reference leg except those `_UNCOMPARED_LEGS` declares is evaluated
against its counterpart here under ONE matched configuration, each delta declared. A delta
that moves fails.

THE REFERENCE SIDE IS A FIXTURE. Every value a test reads from the reference (each constant,
each leg's value at the arguments the test passes) is recorded in
`tests/fixtures/timing_reference.json`, whose `reference` block names the module and the
revision it was taken from. The tests read the fixture always, so they run wherever this tree
does. When the reference checkout is present, `test_the_fixture_matches_the_live_reference`
evaluates every recorded read against it and fails, naming each value that differs, when the
fixture is stale. The fixture is rewritten only by hand:

    PYTHONPATH=. python3 tests/test_timing_vs_reference.py --regenerate

which collects every `E.<name>` read in this file (`E` is the reference), evaluates each against
the live module and writes the fixture. It refuses a checkout whose `swtiming` directory has
uncommitted changes, since the revision it records would not reproduce the values;
`--allow-dirty` overrides that and records the changed files; a recorded file must be one the
module does not load (`_UNLOADED_DIRTY`). A changed value is a decision: read the diff of the
fixture before keeping it. The checkout is taken from `SWI3S_TIMING_ANALYSIS`, else the
directory named `timing-analysis` beside this tree; point the variable at a missing directory
to run without it. A checkout whose `swtiming` directory is present but does not import fails
the staleness check rather than skipping it.

TWO RULES for comparing the models:

1. THE TWO MODELS NAME LEGS BY OPPOSITE CONVENTIONS. The reference names a leg for the
   HANDOVER direction, this project for the DATA direction, and they are inverted --
   `emit_ede.setup_mp` says in its own docstring "= Timing Analysis PM setup". Comparing
   like-named legs reports an error that does not exist.

2. EACH REFERENCE LEG CARRIES ITS OWN CORNER IN ITS DEFAULT ARGUMENTS, not a global one.
   `hold_mp` defaults to T_RF_FAST and t_pd=0.0 because it is CREDITED 2·t_PD and Σ_hold, so
   its worst case minimises them. `nc_pm` bakes in a 0.25 UI clocked turn-on. Evaluate each
   leg at its own defaults, not at one blanket slow-slew corner.
"""
from __future__ import annotations

import ast
import functools
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from swi3s_studio.timing import CalcInputs, compute
from swi3s_studio.timing.spec_source import LaunchMode

# ---------------------------------------------------------------------------
# The reference: the recorded fixture, and the live module when it is present
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURE = os.path.join(_HERE, "fixtures", "timing_reference.json")
_SIBLING = os.path.normpath(os.path.join(_HERE, "..", "..", "timing-analysis"))
_REF_DIR = os.environ.get("SWI3S_TIMING_ANALYSIS", _SIBLING)
_REGENERATE = "PYTHONPATH=. python3 tests/test_timing_vs_reference.py --regenerate"


def _key(name, args=(), kwargs=None):
    """The fixture's key for a read: `NAME` for a constant, `name(a, k=v)` for a call, each
    argument by its `repr`."""
    parts = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in (kwargs or {}).items()]
    return f"{name}({', '.join(parts)})"


class _Recorded:
    """The reference as the fixture holds it: `E.NAME` and `E.name(args)`."""

    def __init__(self, data):
        self._values = data["values"]
        self._parameters = data["parameters"]

    def parameters(self, name):
        """The parameter names of the reference function `name`."""
        return self._parameters[name]

    def __getattr__(self, name):
        """A recorded constant's value, or a function replaying recorded calls. The reference
        names its constants in upper case, so an upper-case name the fixture does not hold
        raises KeyError naming the regeneration rather than reading as a function."""
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._values:
            return self._values[name]
        if name.isupper():
            raise KeyError(f"{name} is not in tests/fixtures/timing_reference.json; "
                           f"regenerate it: {_REGENERATE}")

        def call(*args, **kwargs):
            key = _key(name, args, kwargs)
            if key not in self._values:
                raise KeyError(f"{key} is not in tests/fixtures/timing_reference.json; "
                               f"regenerate it: {_REGENERATE}")
            return self._values[key]
        return call


class _Recorder:
    """The live reference module, recording every read into `values`."""

    def __init__(self, module, values):
        self._module, self._values = module, values

    def __getattr__(self, name):
        member = getattr(self._module, name)
        if not callable(member):
            self._values[name] = member
            return member

        def call(*args, **kwargs):
            value = member(*args, **kwargs)
            self._values[_key(name, args, kwargs)] = value
            return value
        return call


def _live_module():
    """`swtiming.emit_ede` from the reference checkout, or None when the checkout has no
    `swtiming` directory. Raises ImportError, naming the checkout, when the directory is there
    and the module does not import."""
    if not os.path.isdir(os.path.join(_REF_DIR, "swtiming")):
        return None
    # The checkout is on the path for the import only: its top-level directories (tests,
    # scripts, ...) must not shadow this tree's for the rest of the process.
    added = _REF_DIR not in sys.path
    if added:
        sys.path.insert(0, _REF_DIR)
    try:
        from swtiming import emit_ede
    except Exception as e:                         # noqa: BLE001 - any import failure
        raise ImportError(f"the reference checkout at {_REF_DIR} has a swtiming directory, "
                          f"but swtiming.emit_ede does not import: {e!r}") from e
    finally:
        if added:
            sys.path.remove(_REF_DIR)
    return emit_ede


# The files a fixture may record as uncommitted in the reference checkout: modules that
# `swtiming.emit_ede` does not load, so a change in them cannot reach a recorded value.
# `test_the_fixture_matches_the_live_reference` confirms against the live checkout that none of
# them is loaded.
_UNLOADED_DIRTY = frozenset({"swtiming/emit_inequalities.py"})


def _reads():
    """Every read of the reference this file makes: the source of each `E.<name>` constant and
    `E.<name>(...)` call, outermost first. A read's arguments are literals or further reads."""
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found, inner = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_reference(node.func):
            found.append(node)
            inner.add(id(node.func))
        elif _is_reference(node):
            found.append(node)
    return [ast.Expression(n) for n in found
            if id(n) not in inner and _read_name(n) != "parameters"]


def _is_reference(node):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "E")


def _read_name(node):
    return (node.func if isinstance(node, ast.Call) else node).attr


def _collect(module):
    """(values, parameters) read from the live module by every `E.<name>` in this file."""
    values = {}
    recorder = _Recorder(module, values)
    for expr in _reads():
        ast.fix_missing_locations(expr)
        eval(compile(expr, __file__, "eval"), {"E": recorder})    # noqa: S307 - own source
    import inspect
    parameters = {n: list(inspect.signature(f).parameters) for n, f in vars(module).items()
                  if callable(f) and getattr(f, "__module__", None) == module.__name__
                  and not n.startswith("_") and inspect.isfunction(f)}
    return values, parameters


def _header(module):
    def git(*args):
        return subprocess.run(["git", "-C", _REF_DIR, *args], capture_output=True, text=True,
                              check=True).stdout.rstrip("\n")
    return {
        "module": module.__name__,
        "revision": git("rev-parse", "--short", "HEAD").strip(),
        "uncommitted_files": sorted(line[3:] for line in git(
            "status", "--porcelain", "--", "swtiming").splitlines()),
        "regenerate": _REGENERATE,
    }


def regenerate(*, allow_dirty: bool = False):
    """Rewrite the fixture from the live reference. Run by hand, never by a test. Refuses a
    checkout with uncommitted changes under `swtiming` unless `allow_dirty`."""
    module = _live_module()
    if module is None:
        raise SystemExit(f"no reference checkout at {_REF_DIR}")
    header = _header(module)
    if header["uncommitted_files"] and not allow_dirty:
        raise SystemExit(
            f"the reference checkout has uncommitted changes under swtiming "
            f"({', '.join(header['uncommitted_files'])}), so revision {header['revision']} "
            f"would not reproduce the fixture; commit them, or pass --allow-dirty")
    values, parameters = _collect(module)
    data = {"reference": header, "values": dict(sorted(values.items())),
            "parameters": dict(sorted(parameters.items()))}
    with open(_FIXTURE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    print(f"wrote {len(values)} values from {data['reference']['module']} at "
          f"{data['reference']['revision']} to {_FIXTURE}")


@functools.lru_cache(maxsize=1)
def _reference():
    """The fixture as `_Recorded`, read once per process."""
    with open(_FIXTURE, encoding="utf-8") as f:
        return _Recorded(json.load(f))


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
        # 0-10 ns.
        Man_t_DZ_max_ns=E.MAN_DZ_MAX_T129,
        Per_t_DD_min_ns=E.PER_DD_MIN, Per_t_DD_max_ns=E.PER_DD_MAX,
        Per_t_ZD_ns=E.PER_ZD_MAX, Per_t_DZ_max_ns=E.PER_DZ_MAX,
        Per_t_IS_max_ns=E.PER_IS_MAX, Man_t_IS_max_ns=E.MAN_IS_MAX,
        Per_t_IH_max_ns=E.PER_IH_MAX, Man_t_IH_max_ns=E.MAN_IH_MAX,
        t_keeper_ns=E.T_KEEPER, handover_UIs=0.0, launch_mode=LaunchMode.ANALOG,
        pp_placement="unknown",
    )


# The anchors, deliberate divergences that show up as numbers (docs/anchors.md). This model places
# a launch at the 20 % point its t_DD / t_ZD is measured to, and a Manager's timing at its clock
# output's 20 % point; the reference places both at the start of the ramp. So at a leg's slews:
#
#   a launch's data anchor  its ramp starts t_RF/3 before the tabulated value here, at it there
#   a Manager reference     t_RF,CLK/3 after the clock starts here, at the start there
#
# A peripheral launch carries the anchor. A Manager LAUNCH carries both, and they cancel while
# its two lanes share a slew and both sit on edges of one slew factor.
#
# Each sits on an edge, and the per-edge slew tolerance scales it: (1 + TAU) on a late edge,
# (1 − TAU) on an early one.
def _anchor(trf, role):
    """A launch's data anchor: its ramp's first third on an edge of `role`."""
    return trf * _slew(role) / 3.0


def _man_ref(trf, role):
    """A Manager's clock reference: the same first third, on its clock edge of `role`."""
    return _anchor(trf, role)


def _slew(role):
    E = _reference()
    tau = E.TAU
    return 1.0 + tau if role == "late" else 1.0 - tau


# Declared divergence "the clock's edge spacing" (docs/anchors.md): where the clock's edges are
# spaced. This model places every edge by its 20 % point, so the UI runs between two consecutive
# clock references and one edge's references at two pins are aligned; the reference spaces ramp
# starts. Where a leg's two clock events take opposite slew factors -- a setup leg's launch and
# sampling edges -- the later one is f_A·(late − early)·t_RF,CLK earlier there, in this model's
# favour. Two detections of one edge take one factor here, so their ramp starts coincide and they
# carry `_shared_edge` instead.
def _ref_spacing(trf):
    return _man_ref(trf, "late") - _man_ref(trf, "early")


@pytest.fixture(autouse=True)
def _unbounded_edges(monkeypatch):
    """The reference moves each edge ±TAU about its lane's t_RF with no limit; this model keeps
    every edge inside its spec's range (`calculator.edge_factor`, pinned by
    `test_timing_cross_spec.py::test_an_edge_never_leaves_its_ramp_setting`). At the matched
    8.3 ns that bound binds, so this file compares with it lifted."""
    from swi3s_studio.timing import calculator

    def unbounded(inp, lane, role):
        tau = inp.tRF_tolerance_frac
        return 1.0 + tau if role == "late" else 1.0 - tau

    monkeypatch.setattr(calculator, "edge_factor", unbounded)

# Declared divergence "the P→P slew split" (docs/anchors.md): a disagreement rather than a
# conversion.
#
# The reference corners a P→P leg's two peripherals at OPPOSITE ends of the clock's slew band —
# `nc_pp(trf=T_RF_SLOW, trf_acq=T_RF_FAST)`. This model does not: the clock is driven by the
# MANAGER FROM ONE PIN, so at any instant that edge has one slew and both peripherals detect the
# same edge. Their thresholds (the V_IH/V_IL spread inside the envelope) and internal delays
# (their own parameters) differ; the slew does not. So both crossings take one t_RF and the pair
# gathers into a single product.
#
# Worth 2.617 ns in this model's favour. It narrows P→P contention's deficit and does not flip
# its sign: at the matched configuration the leg reads −18.48 in the reference, and here −14.96
# with the slew-range limit lifted (`_unbounded_edges`) and −14.63 with the limit in force, all
# three pinned by test_pp_contention_differs_by_the_slew_split_this_model_declines_to_make.
# The gap carries the shared edge's factor and the supply cornering on top of this split
# (`_shared_edge`, `_supply_split`).
_PP_SLEW_SPLIT = 2.6174


# Declared divergence "the keeper's threshold criterion" (docs/anchors.md): what a keeper leg
# counts the keeper's response from. The reference counts it from the ramp reaching the rail,
# 5/3 · t_RF after the tabulated launch; this model from the data crossing the keeper's input
# threshold at the Manager ("PHY1 & PHY2 Bus Keeper Behavior" table: measured from the input
# crossing V_IH / V_IL), the Manager's V_IH,max rising and V_IL,min falling at its supply,
# c_K · (1 + TAU) · t_RF after the launch's anchor on a late edge, c_K the crossing from the
# anchor in units of t_RF on a ramp to the releaser's supply, a Peripheral's offset by the ground
# shift. The anchor and this together are (5/3 − c_K · (1 + TAU)) · t_RF at the reference's slow
# slew, in this model's favour on both keeper legs.
#
# c_K is pinned per leg at the matched configuration; `_keeper_crossing` asserts that the model
# still computes it. keeper_Man's is the Manager's own data at its own input, (V_IH,max − V_OL) /
# (V_OH − V_OL) at any supply; keeper_Per's is at the leg's supplies.
_KEEPER_CROSSING = {"keeper_Man": 0.75, "keeper_Per": 0.9643}


def _keeper_crossing(inp, leg, **overrides):
    """The pinned c_K of `leg` (`_KEEPER_CROSSING`), after checking that the model computes it
    from the thresholds, supplies and ground shift its breakdown reports."""
    inp = replace(inp, **overrides)
    b = compute(inp).breakdowns[leg]
    m, p = b.supplies[:2]
    tx, alpha = (m, 0.0) if leg == "keeper_Man" else (p, inp.V_noise_pp_frac)
    v_oh, v_ol = inp.V_OH_min_frac, inp.V_OL_max_frac
    swing = v_oh - v_ol
    rise = (inp.V_IH_max_frac * m + alpha) / (swing * tx) - v_ol / swing
    fall = (tx - (inp.V_IL_min_frac * m - alpha)) / (swing * tx) - (1.0 - v_oh) / swing
    got = max(rise, fall)
    (x_k,) = [t for t in b.terms if t.symbol.startswith("X_K")]
    assert x_k.factors[0] == pytest.approx(got * (1.0 + inp.tRF_tolerance_frac), abs=1e-12)
    assert got == pytest.approx(_KEEPER_CROSSING[leg], abs=5e-5), (
        f"the {leg} keeper crossing moved: pinned {_KEEPER_CROSSING[leg]:.4f}, "
        f"computed {got:.4f} at supplies {b.supplies}")
    return _KEEPER_CROSSING[leg]


def _keeper_delta(E, c_k):
    return (5.0 / 3.0 - c_k * (1.0 + E.TAU)) * E.T_RF_SLOW


# Declared divergence "the supply split" (docs/anchors.md): how the supplies are cornered. The
# reference puts each crossing at its own worst V_SEOS pair; this model gives each device ONE
# supply on a leg, its output rail and its input thresholds together (`calculator.compute`), so a
# device that a leg's crossings want at opposite ends sits at one. Where they do, this model is the
# more generous by the leg's crossings at its own assignment (`InequalityBreakdown.supplies`) less
# the same crossings each at its own worst pair (`CalcInputs.envelope()` without supplies). The
# crossings, as the margin carries them, of each leg it moves:
#
#   PM setup        − Σ_cross,PM,setup
#   P→P setup       − (A's late clock detection + B's Δ_cross,setup)
#   P→P hold        + A's early clock detection − B's Δ_cross,hold
#   P→P contention  + B's early clock detection − A's late one
#
# The Manager-launched legs, P→M hold and the mixed contention legs have no device pulled
# both ways, so they carry none, except where the case reported is another polarity
# (`_polarity_case`): its supplies are that case's worst, not the reference crossing's, as on
# M→P contention.
#
# Pinned as NUMBERS, in ns, at the matched configuration with the slew-range limit lifted (P→P
# hold and contention at Per_t_ZD = PER_ZD_MIN, as their tests evaluate them). The margin
# carries the crossings at the leg's own assignment, so a split computed from the model alone
# would move with it; `_supply_split` returns the pinned number and asserts that the model
# still computes it.
#
# The split is of the REFERENCE'S crossings (`_leg_crossings_ns`), at the supplies this model
# reports the leg at, so a leg whose reported case is another polarity also carries
# `_polarity_case`. P->P contention with a releasing EDE peripheral is keyed on its own.
_SUPPLY_SPLIT = {
    "PM_setup_ho": 1.7474,
    "PP_setup_ho": 0.9938,
    "PP_hold_ho": 0.4315,
    "PP_contention": 0.4315,
    "PP_contention,EDE": 1.0540,
    "MP_contention": 1.0540,
}


def _leg_crossings_ns(env, inp, leg):
    """A leg's crossings as the reference constructs them, signed as they enter the margin:
    every clock edge rising, and Sigma_cross,PM,setup's data on the clock's polarity, at the
    worse ground-shift polarity."""
    k, s = inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns
    sigma_rr = max(env.sigma_pm_setup_clk_dgnd_pos * k + env.sigma_pm_setup_data_dgnd_neg * s,
                   env.sigma_pm_setup_clk_dgnd_neg * k + env.sigma_pm_setup_data_dgnd_pos * s)
    return {
        "PM_setup_ho": -sigma_rr,
        "PP_setup_ho": -(env.mp_late_rise * k
                         + env.delta_cross_PP_ns(k, s, direction="setup")),
        "PP_hold_ho": (env.sigma_pm_hold_rf_clk * k
                       - env.delta_cross_PP_ns(k, s, direction="hold")),
        "PP_contention": (env.sigma_pm_hold_rf_clk_b - env.mp_late_rise) * k,
        "MP_contention": env.sigma_pm_hold_rf_clk * k,
    }[leg]


def _supply_split(inp, leg, key=None, **overrides):
    """The pinned divergence on `leg` (`_SUPPLY_SPLIT[key or leg]`), after checking that the
    model computes it: the leg's crossings at its one-supply-per-device assignment less the
    same crossings each at its own worst pair."""
    key = key or leg
    inp = replace(inp, **overrides)
    supplies = compute(inp).breakdowns[leg].supplies
    split = (_leg_crossings_ns(inp.envelope(supplies), inp, leg)
             - _leg_crossings_ns(inp.envelope(), inp, leg))
    assert split == pytest.approx(_SUPPLY_SPLIT[key], abs=5e-4), (
        f"the supply split on {key} moved: pinned {_SUPPLY_SPLIT[key]:.4f}, "
        f"computed {split:.4f} at supplies {supplies}")
    return _SUPPLY_SPLIT[key]


# Declared divergence "the polarity case" (docs/anchors.md): the polarities a leg is evaluated at.
# The reference puts every clock crossing on a rising edge and Sigma_cross,PM,setup's data on the
# clock's polarity. This model evaluates every setup and hold leg at both polarities of its
# sampling edge, every contention leg at both polarities of the edge closing the releaser's UI, and
# Sigma_cross,PM,setup with its data on either polarity, and reports the worse. Where another case
# binds, the leg differs by its reported crossings less the reference's construction of them
# (`_leg_crossings_ns`), both at the leg's supplies:
#
#   P->M setup       a rising clock with falling data, the cross case, whose ground shift
#                    delays both crossings
#   M->P contention  the acquirer detecting a falling closing edge early, at V_IL,max + α
#   P->P contention  under an EDE release, the releaser detecting the rising edge opening its
#                    UI and the acquirer the falling closing edge
#
# Pinned as NUMBERS, in ns, as `_SUPPLY_SPLIT` is; `_polarity_case` asserts that the model still
# computes them.
_POLARITY_CASE = {
    "PM_setup_ho": -4.1500,
    "MP_contention": -1.3833,
    "PP_contention,EDE": -1.3833,
}


def _polarity_case(inp, leg, key=None, **overrides):
    """The pinned divergence on `leg` (`_POLARITY_CASE[key or leg]`), after checking that the
    model computes it from the crossings its breakdown reports."""
    key = key or leg
    inp = replace(inp, **overrides)
    b = compute(inp).breakdowns[leg]
    got = (sum(t.value_ns for t in b.terms if "X_" in t.symbol)
           - _leg_crossings_ns(inp.envelope(b.supplies), inp, leg))
    assert got == pytest.approx(_POLARITY_CASE[key], abs=5e-4), (
        f"the polarity divergence on {key} moved: pinned {_POLARITY_CASE[key]:.4f}, "
        f"computed {got:.4f}")
    return _POLARITY_CASE[key]


# Declared divergence "a shared edge's slew factor" (docs/anchors.md): the slew factor of an edge
# two peripherals share. On a P→P leg whose two clock detections are of ONE edge
# (`calculator.pp_shares_clock_edge`), this model takes both at that edge's one factor, whichever
# of (1 + TAU) and (1 − TAU) leaves the leg the less (`calculator.pp_clock_edges`); the reference
# takes each at its own corner's factor, from one ramp start. Both detections' ramps start together
# here too, so the leg carries no edge spacing (`_ref_spacing`). The difference, at the leg's
# supply assignment, is
#
#   [f · (c_credit − c_charge) − ((1 − TAU) · c_credit − (1 + TAU) · c_charge)] · t_RF,CLK
#
# with each c counted from the ramp's start (its coefficient without its (1 ± TAU), plus the
# 1/3 of the ramp before the clock reference) and f the edge's factor.
#
# Pinned as NUMBERS, in ns, as `_SUPPLY_SPLIT` is; `_shared_edge` asserts that the model still
# computes them.
_SHARED_EDGE = {
    "PP_hold_ho": 0.4769,
    "PP_contention": 0.4769,
}


def _shared_edge(inp, leg, **overrides):
    """The pinned divergence on `leg` (`_SHARED_EDGE`), after checking that the model computes
    it from the leg's supplies and the edge factor it takes."""
    from swi3s_studio.timing.calculator import pp_clock_edges

    inp = replace(inp, **overrides)
    env = inp.envelope(compute(inp).breakdowns[leg].supplies)
    a_edge, b_edge = pp_clock_edges(inp, env, leg)
    assert a_edge == b_edge, f"{leg}: the two detections should be of one edge"
    tau, k = inp.tRF_tolerance_frac, inp.tRF_Man_CLK_ns
    if leg == "PP_hold_ho":
        credit, charge = env.sigma_pm_hold_rf_clk, env.mp_late_rise_b
    else:
        credit, charge = env.sigma_pm_hold_rf_clk_b, env.mp_late_rise
    credit = credit / (1.0 - tau) + 1.0 / 3.0
    charge = charge / (1.0 + tau) + 1.0 / 3.0
    got = (_slew(a_edge) * (credit - charge)
           - ((1.0 - tau) * credit - (1.0 + tau) * charge)) * k
    assert got == pytest.approx(_SHARED_EDGE[leg], abs=5e-4), (
        f"the shared-edge divergence on {leg} moved: pinned {_SHARED_EDGE[leg]:.4f}, "
        f"computed {got:.4f}")
    return _SHARED_EDGE[leg]


# Declared divergence "the keeper_Per detection spacing" (docs/anchors.md): the Peripheral's
# detection spacing on keeper_Per. This model counts the Peripheral's t_DD from its detection of
# the edge opening the UI and its t_DZ from its detection of the closing edge, the opening one late
# and the closing one, of the other polarity, early, at the leg's supplies; the reference takes the
# two one UI apart. The difference is the gathered clock term the leg shows:
#
#   −(c_late,open − c_early,close) · t_RF,CLK
#
# Pinned as a NUMBER, in ns, at the matched configuration; `_keeper_per_spacing` asserts that
# the model still computes it.
_KEEPER_PER_SPACING = -7.9177


def _keeper_per_spacing(inp, **overrides):
    """The pinned divergence on keeper_Per (`_KEEPER_PER_SPACING`), after checking that the
    model computes it from the polarities and supplies its breakdown reports."""
    inp = replace(inp, **overrides)
    b = compute(inp).breakdowns["keeper_Per"]
    env = inp.envelope(b.supplies)
    late = env.mp_late_fall if b.closing_rising else env.mp_late_rise
    early = env.sigma_pm_hold_rf_clk if b.closing_rising else env.mp_early_fall
    got = -(late - early) * inp.tRF_Man_CLK_ns
    shown = [t.value_ns for t in b.terms if t.symbol.endswith("· Man_t_RF,CLK")]
    assert shown == [pytest.approx(got, abs=1e-9)], (shown, got)
    assert got == pytest.approx(_KEEPER_PER_SPACING, abs=5e-4), (
        f"the keeper_Per detection spacing moved: pinned {_KEEPER_PER_SPACING:.4f}, "
        f"computed {got:.4f} at supplies {b.supplies}")
    return _KEEPER_PER_SPACING


def _margin(inp, leg, **overrides):
    return compute(replace(inp, **overrides)).breakdowns[leg].margin_ns


# ---------------------------------------------------------------------------
# The Manager legs, each by a declared placement delta
# ---------------------------------------------------------------------------

def test_manager_launched_and_contention_legs_differ_by_the_declared_anchors():
    """Manager-launched setup and hold each differ by 2τ·t_RF,CLK/3, the mixed contention legs
    by the Manager's clock reference, and the Manager keeper leg by its data anchor and the
    keeper's threshold criterion.

    Manager-launched setup (`MP_setup_ho`): the Manager's reference and its data anchor cancel
    while its two lanes share a slew and both sit on late edges, leaving the clock's edge
    spacing (`_ref_spacing`). Manager-launched hold (`MP_hold_ho`): the reference is on a late
    edge and the anchor on an early one, which at a shared slew is the same 2τ·t_RF/3. The
    mixed contention legs with a conventional release (`MP_contention`, `PM_contention`) read
    t_ZD/t_DZ as tabulated on both sides, and differ by the Manager's clock reference: its
    release is later here (MP), and so is its turn-on (PM). `MP_contention` reports a falling
    closing edge, so it also carries `_polarity_case` and the supply split of that case, and
    at the Manager's boundary release it differs the same way. `keeper_Man` at the boundary
    release has no clock reference term, and differs by `_keeper_delta`. If any of these
    moves, one model has changed its physics.

    P→P contention is NOT here: it is the one leg where the two models disagree about the
    clock's slew rather than about a conversion, so it has its own test and its own declared
    number (`_PP_SLEW_SPLIT`). The EDE contention rows are not here either: a peripheral's EDE
    release contains its launch, so they carry the peripheral's anchor (see
    `test_the_peripheral_ede_release_differs_by_the_ede_release_delta`).
    """
    E = _reference()
    inp = _matched_inputs(E)
    ui = inp.UI_ns()

    assert ui == pytest.approx(E.UI, abs=1e-9), "the two disagree about the UI itself"

    # THE REFERENCE'S DEFAULTS ARE THE PROPOSAL, so each leg is evaluated at the placement the
    # proposal states and this side is configured to match it. The Manager's launch is the
    # revision's ANALOG 9-23 ns (`_matched_inputs` already sets that) and its release is a timed
    # move to high-Z AT the boundary -- Man_t_DZ,max = MAN_DZ_PROP, which is 0 and therefore
    # reads as an end-of-UI-referenced release with no EDE flag anywhere. The keeper check below
    # compares the same boundary release (`keeper_man(forced=False)`).
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
    # MP setup's reference and anchor are both on late edges and cancel, leaving the edge
    # spacing; MP hold's reference is on a late edge and its anchor on an early one. A
    # contention leg's Manager reference is on the edge the peripheral detects there: early on
    # MP, late on PM.
    t = E.T_RF_SLOW
    delta = {"MP_setup_ho": _ref_spacing(t),
             "MP_hold_ho": _man_ref(t, "late") - _anchor(t, "early"),
             "MP_contention": -_man_ref(t, "early"), "PM_contention": +_man_ref(t, "late")}
    mp_contention = next(over for _r, _v, leg, over in exact if leg == "MP_contention")
    delta["MP_contention"] += (_supply_split(inp, "MP_contention", **mp_contention)
                               + _polarity_case(inp, "MP_contention", **mp_contention))
    for ref_name, ref_val, leg, overrides in exact:
        got = _margin(inp, leg, **overrides)
        assert got - ref_val == pytest.approx(delta[leg], abs=5e-4), (
            f"{ref_name} vs {leg}: reference {ref_val:+.4f}, here {got:+.4f}")

    # THE MANAGER'S RELEASE AT THE BOUNDARY, which is the proposal rather than a variant, so it
    # gets its own line: Man_t_DZ,max = 0 with the conventional end-of-UI reference. The
    # release moves no crossing, so the leg carries MP_contention's declared delta.
    assert _margin(inp, "MP_contention", Per_t_ZD_ns=E.PER_ZD_MIN,
                   Man_t_DZ_max_ns=E.MAN_DZ_PROP) - E.nc_mp() == pytest.approx(
        delta["MP_contention"], abs=5e-4)

    # The Manager keeper leg at the boundary release. The reference's mid-UI grid placement
    # of an EndDriveEarly release has no counterpart here: this model's EndDriveEarly release
    # is the launch plus the ramp plus the EDE Delay (`_ede_release_vs_edge_ns`).
    analog_edge = dict(Man_t_DZ_max_ns=E.MAN_DZ_PROP)
    c_k = _keeper_crossing(inp, "keeper_Man", **analog_edge)
    assert _margin(inp, "keeper_Man", **analog_edge) - E.keeper_man(forced=False) == \
        pytest.approx(_keeper_delta(E, c_k), abs=5e-4)


# ---------------------------------------------------------------------------
# The legs that differ, each by a declared amount
# ---------------------------------------------------------------------------

def test_peripheral_launched_legs_differ_by_their_declared_placement_and_supply_deltas():
    """Setup better and hold worse by the peripheral's anchor, and on P→M by the Manager's
    reference too, plus the declared edge-spacing and supply-cornering deltas, and nothing
    else.

    Every leg with a peripheral OUTPUT value in it differs by the Peripheral Output Timing
    figure's anchor: this model starts the ramp t_RF/3 before the tabulated value, the reference
    at it. A setup leg subtracts the launch, so its margin is that much better; a hold leg adds
    it, so hold goes the other way. A P→M leg's Manager samples from its clock reference here,
    t_RF,CLK/3 after the clock starts: setup later (better), hold later (worse). Each setup leg
    also carries the clock's edge spacing (`_ref_spacing`) and the supply cornering
    (`_supply_split`), and P→M setup the cross case of Sigma_cross,PM,setup
    (`_polarity_case`).

    Pinned per-leg, so a second divergence on one of these cannot hide inside a band wide
    enough to admit the first.
    """
    E = _reference()
    inp = _matched_inputs(E)

    slow = E.T_RF_SLOW
    for ref_val, leg, want in (
        (E.setup_mp(), "PM_setup_ho",
         _anchor(slow, "late") + _man_ref(slow, "early") + _ref_spacing(slow)
         + _supply_split(inp, "PM_setup_ho") + _polarity_case(inp, "PM_setup_ho")),
        (E.setup_pp(), "PP_setup_ho",
         _anchor(slow, "late") + _ref_spacing(slow) + _supply_split(inp, "PP_setup_ho")),
    ):
        got = _margin(inp, leg)
        assert got - ref_val == pytest.approx(want, abs=5e-4), leg

    # hold: the reference's hold_mp carries its OWN corner in its defaults — fast slew and a
    # co-packaged peripheral, because the leg is credited 2·t_PD and Σ_hold and its worst case
    # minimises both.
    fast = E.T_RF_FAST
    got = _margin(inp, "PM_hold_ho", tRF_Man_CLK_ns=fast, tRF_Man_DATA_ns=fast,
                  tRF_Per_DATA_ns=fast, bus_length_cm=0.0, Per_t_ZD_ns=E.PER_ZD_MIN)
    assert got - E.hold_mp() == pytest.approx(
        -_anchor(fast, "early") - _man_ref(fast, "early"), abs=5e-4)


def test_pp_hold_differs_by_the_anchor_the_shared_edge_and_the_supply_split():
    """P→P hold agrees with the reference except for the peripheral's anchor, the shared edge's
    slew factor and the supply cornering.

    Both models evaluate the leg's two peripherals at a single clock slew — one Manager pin,
    one edge, one slew — so `hold_pp` and `PP_hold_ho` differ by the Peripheral Output Timing
    figure's anchor, by the two detections taking that edge's one factor here and each its
    corner's own there (`_shared_edge`), and by one supply per device here and per crossing
    there (`_supply_split`).
    """
    E = _reference()
    inp = _matched_inputs(E)
    got = _margin(inp, "PP_hold_ho", Per_t_ZD_ns=E.PER_ZD_MIN)
    assert got - E.hold_pp() == pytest.approx(
        -_anchor(E.T_RF_SLOW, "early") + _shared_edge(inp, "PP_hold_ho", Per_t_ZD_ns=E.PER_ZD_MIN)
        + _supply_split(inp, "PP_hold_ho", Per_t_ZD_ns=E.PER_ZD_MIN), abs=5e-4)


def test_pp_contention_differs_by_the_slew_split_this_model_declines_to_make(monkeypatch):
    """The one leg where the reference splits the clock's slew and this model does not.

    `nc_pp` takes the releaser's charge at T_RF_SLOW and the acquirer's credit at T_RF_FAST;
    this model takes both at the one t_RF the corner search picked, because the clock is driven
    by a single Manager pin. Asserted as a NUMBER so a change on either side fails here, and
    as a SIGN: declining the split makes this model more generous. The two detections also
    carry the shared edge's factor (`_shared_edge`) and the supply cornering (`_supply_split`).
    The three margins `_PP_SLEW_SPLIT`'s note quotes are pinned: the reference's, this model's
    with the slew-range limit lifted, and this model's with it in force.

    See `_PP_SLEW_SPLIT`.
    """
    E = _reference()
    inp = _matched_inputs(E)
    got = _margin(inp, "PP_contention", Per_t_ZD_ns=E.PER_ZD_MIN,
                  Per_t_DZ_max_ns=E.PER_DZ_MAX_T129)
    ref = E.nc_pp(ede=False)
    split = _supply_split(inp, "PP_contention", Per_t_ZD_ns=E.PER_ZD_MIN,
                          Per_t_DZ_max_ns=E.PER_DZ_MAX_T129)
    shared = _shared_edge(inp, "PP_contention", Per_t_ZD_ns=E.PER_ZD_MIN,
                          Per_t_DZ_max_ns=E.PER_DZ_MAX_T129)
    assert got - ref == pytest.approx(_PP_SLEW_SPLIT + shared + split, abs=5e-4), (
        f"the P→P slew-split divergence moved: reference {ref:+.4f}, here {got:+.4f}")
    assert got > ref, "declining the split is the GENEROUS direction — never silently"
    assert (ref, got) == pytest.approx((-18.48, -14.96), abs=5e-3)
    monkeypatch.undo()                      # the slew-range limit back in force
    bounded = _margin(inp, "PP_contention", Per_t_ZD_ns=E.PER_ZD_MIN,
                      Per_t_DZ_max_ns=E.PER_DZ_MAX_T129)
    assert bounded == pytest.approx(-14.63, abs=5e-3), bounded


def test_the_peripheral_ede_release_differs_by_the_ede_release_delta():
    """P→M and P→P contention with a releasing EDE peripheral, against `nc_pm(ede=True)` and
    `nc_pp(ede=True)`.

    The reference places the peripheral's EDE release at its tabulated launch plus the full
    rail-to-rail swing plus a hold interval. This model places it at the launch's anchor plus
    a late edge's t_RF (to 80 %) plus the EDE Delay. With the EDE Delay set to the reference's
    hold plus the ramp's last 20 % (t_RF/3 on a linear ramp), the release here is
    (4/3 − (1 + TAU)) · t_RF earlier, and a contention leg charges the release, so its margin is
    that much better. P→M also carries the acquiring Manager's clock reference, and P→P carries
    `_PP_SLEW_SPLIT`, the edge spacing, the supply cornering and the falling closing edge it
    reports (`_polarity_case`) on top: an EndDriveEarly release is referenced to the edge that
    opens the UI, so the two detections are of two edges and each takes its corner's own
    factor.
    """
    E = _reference()
    inp = replace(_matched_inputs(E), Per_t_EDE_ns=E.PER_HOLD_MAX + E.T_RF_SLOW / 3.0)

    ede_release = (4.0 / 3.0 - _slew("late")) * E.T_RF_SLOW
    pm = _margin(inp, "PM_contention", Per_ede=True, Man_t_ZD_ns=E.MAN_ANALOG_MIN)
    ref_pm = E.nc_pm(ede=True)
    assert pm - ref_pm == pytest.approx(ede_release + _man_ref(E.T_RF_SLOW, "late"),
                                        abs=5e-4), (
        f"nc_pm(ede=True) vs PM_contention: reference {ref_pm:+.4f}, here {pm:+.4f}")

    pp = _margin(inp, "PP_contention", Per_ede=True, Per_t_ZD_ns=E.PER_ZD_MIN)
    ref_pp = E.nc_pp(ede=True)
    split = (_supply_split(inp, "PP_contention", "PP_contention,EDE", Per_ede=True,
                           Per_t_ZD_ns=E.PER_ZD_MIN)
             + _polarity_case(inp, "PP_contention", "PP_contention,EDE", Per_ede=True,
                              Per_t_ZD_ns=E.PER_ZD_MIN))
    assert pp - ref_pp == pytest.approx(
        ede_release + _PP_SLEW_SPLIT + _ref_spacing(E.T_RF_SLOW) + split, abs=5e-4), (
        f"nc_pp(ede=True) vs PP_contention: reference {ref_pp:+.4f}, here {pp:+.4f}")
    # With the reference's slew split removed, the EDE release delta, the edge spacing, the
    # supply cornering and the polarity case are all that is left.
    ref_pp_one_slew = E.nc_pp(ede=True, trf_acq=E.T_RF_SLOW)
    assert pp - ref_pp_one_slew == pytest.approx(
        ede_release + _ref_spacing(E.T_RF_SLOW) + split, abs=5e-4)


def test_the_keeper_per_legs_agree_once_both_are_at_the_earliest_release():
    """They differ by t_DZ only because they read it at different corners, not by an omission.

    The reference's `keeper_per_conventional` is `UI − Per_t_DD − swing − keeper`, with no
    t_DZ term; this project's leg carries `1·UI + Per_t_DZ` explicitly.

    THE KEEPER LEG WANTS THE EARLIEST RELEASE. t_DZ is specified only as a MAXIMUM, so a
    device may go high-Z the instant its UI closes; t_DZ = 0 is legal and is the corner that
    starves the keeper. The reference HARDCODES that corner by omitting the term. This
    project makes it a corner the search finds. Two spellings of one worst case.

    Both are then evaluated the same way, and what is left is the peripheral's anchor, the
    keeper's threshold criterion (`_keeper_delta`) and the spacing of the Peripheral's two
    detections (`_keeper_per_spacing`).
    Asserted at BOTH ends of the row: at t_DZ,max this project is 9 ns more generous, since a
    device holding the bus longer feeds the keeper better.
    """
    E = _reference()
    inp = _matched_inputs(E)
    ref = E.keeper_per_conventional(forced=False)

    at_earliest = _margin(inp, "keeper_Per", Per_t_DZ_max_ns=0.0)
    spacing = _keeper_per_spacing(inp, Per_t_DZ_max_ns=0.0)
    c_k = _keeper_crossing(inp, "keeper_Per", Per_t_DZ_max_ns=0.0)
    assert at_earliest - ref == pytest.approx(_keeper_delta(E, c_k) + spacing, abs=5e-4), (
        "at the earliest release the two keeper legs should differ by the peripheral's anchor, "
        "the keeper's threshold criterion and the detection spacing, and nothing else")

    at_tabulated = _margin(inp, "keeper_Per", Per_t_DZ_max_ns=E.PER_DZ_MAX)
    assert at_tabulated - at_earliest == pytest.approx(E.PER_DZ_MAX, abs=5e-4), (
        "driving t_DZ longer must feed the keeper exactly that much more")

    # ...and the corner search finds t_DZ = 0 on its own.
    from swi3s_studio.timing import apply_row_picks, default_swi3s_rows, find_worst_corner_rows

    rows = default_swi3s_rows()
    picks = find_worst_corner_rows("keeper_Per", rows, inp)
    assert picks["Per_t_DZ"] == "min", picks["Per_t_DZ"]
    worst = compute(apply_row_picks(inp, rows, picks)).breakdowns["keeper_Per"]
    # The zero release term STAYS: `0·t_PD` is dropped (structurally zero), `0·UI` is dropped
    # (nothing allocated), but a PARAMETER sitting at zero is shown — like Man_t_IH on PHY2 —
    # and `_release_note` spells it out.
    release = [t for t in worst.terms if t.symbol.startswith("Per_t_DZ")]
    assert len(release) == 1 and release[0].value_ns == 0.0, release
    assert "may go high-Z the instant its UI closes" in release[0].note


# The reference's legs this file does not compare, each with the reason. Every other
# `setup_*` / `hold_*` / `nc_*` / `keeper_*` function the reference defines is read here
# (`test_every_reference_leg_is_compared_or_declared`).
_UNCOMPARED_LEGS = {
    "keeper_per_ede": "a constant in the reference, its hold minimum less the keeper's response; "
                      "this model's EDE keeper_Per is the EDE Delay and the ramp to V_OH less "
                      "the crossing of the keeper's threshold and the keeper's response, the "
                      "EDE Delay being an input",
    "keeper_response_man": "the Bus Keeper Behavior table's form of keeper_man, from the "
                           "keeper's input crossing; keeper_man is compared",
    "keeper_man_retired_launch": "keeper_man at a 0.50 UI launch, a figure of the reference's "
                                 "own audit; this model has no such placement",
    "keeper_window_opens": "a part of keeper_response_man's arithmetic, not a leg",
}
_REFERENCE_LEG_PREFIXES = ("setup_", "hold_", "nc_", "keeper_")


def test_every_reference_leg_is_compared_or_declared():
    """Each leg function the reference defines (a `setup_*`, `hold_*`, `nc_*` or `keeper_*`
    name in the fixture's parameter list) is either read by this file or declared in
    `_UNCOMPARED_LEGS`, never both, and every declared name is one the reference defines. A
    leg the reference gains fails here until it is compared or declared. Helpers with those
    prefixes that are not legs (`hold_floor`, `nc_mp_at`, `nc_pm_at`) are read through the legs
    or are rearrangements of them, and are listed as such."""
    with open(_FIXTURE, encoding="utf-8") as f:
        defined = set(json.load(f)["parameters"])
    helpers = {"hold_floor", "nc_mp_at", "nc_pm_at"}
    legs = {n for n in defined if n.startswith(_REFERENCE_LEG_PREFIXES)} - helpers
    read = {_read_name(e.body) for e in _reads()}
    assert set(_UNCOMPARED_LEGS) <= legs, sorted(set(_UNCOMPARED_LEGS) - legs)
    assert helpers <= defined, sorted(helpers - defined)
    assert not read & set(_UNCOMPARED_LEGS), sorted(read & set(_UNCOMPARED_LEGS))
    missing = legs - read - set(_UNCOMPARED_LEGS)
    assert not missing, f"reference legs neither compared nor declared: {sorted(missing)}"


def test_the_reference_prices_only_the_handover_launch():
    """The structural gap, asserted as a fact about coverage rather than a number.

    The reference defines setup/hold ONLY for the t_ZD (handover) launch — `setup_mp`,
    `hold_mp`, `setup_pm`, `hold_pm`, `setup_pp`, `hold_pp` all take a `t_zd`. It has no
    continuous-transmission leg at all. This model carries the full launch-method × (setup,
    hold) grid.

    If the reference ever grows a t_DD-launched leg, this fails and the pairs above should
    gain a row rather than the new leg going unnoticed.
    """
    E = _reference()
    for name in ("setup_mp", "hold_mp", "setup_pm", "hold_pm", "setup_pp", "hold_pp"):
        params = E.parameters(name)
        assert any("zd" in p for p in params), \
            f"{name} must take a t_ZD: the reference prices the handover launch"
        assert not any("t_dd" in p for p in params), \
            f"{name} must not take a t_DD: the reference prices only the handover launch"

    ours = compute(_matched_inputs(E)).breakdowns
    for leg in ("MP_setup", "MP_hold", "PM_setup", "PM_hold", "PP_setup", "PP_hold"):
        assert leg in ours, f"{leg} is the t_DD-launched half of the 2x2 and must stay"


# ---------------------------------------------------------------------------
# The fixture against the live reference
# ---------------------------------------------------------------------------

def test_the_fixture_covers_every_read_and_nothing_else():
    """Each `E.<name>` this file reads is in the fixture, and the fixture holds no value that
    nothing reads. Runs without the reference checkout: the reads are evaluated against the
    fixture itself."""
    with open(_FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    assert {"module", "revision", "regenerate"} <= set(data["reference"])
    dirty = set(data["reference"].get("uncommitted_files", []))
    assert dirty <= _UNLOADED_DIRTY, (
        f"the fixture was taken from a checkout with uncommitted changes the module may "
        f"load: {sorted(dirty - _UNLOADED_DIRTY)}; regenerate it from a clean one: "
        f"{_REGENERATE}")
    seen = {}
    for expr in _reads():
        ast.fix_missing_locations(expr)
        eval(compile(expr, __file__, "eval"), {"E": _Recorder(_RecordedAsModule(data), seen)})
    assert set(seen) == set(data["values"]), (
        f"the reads and the fixture differ; regenerate it: {_REGENERATE}\n"
        f"unrecorded: {sorted(set(seen) - set(data['values']))}\n"
        f"unread: {sorted(set(data['values']) - set(seen))}")


def test_a_constant_missing_from_the_fixture_fails_naming_the_regeneration():
    """A constant the file reads that the fixture does not hold raises KeyError naming the
    regeneration, from the fixture and from its replay in
    `test_the_fixture_covers_every_read_and_nothing_else`, rather than reading as a
    function."""
    with open(_FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    del data["values"]["F_CLK"]
    with pytest.raises(KeyError, match="regenerate"):
        _ = _Recorded(data).F_CLK
    with pytest.raises(KeyError, match="regenerate"):
        _ = _Recorder(_RecordedAsModule(data), {}).F_CLK
    assert _Recorded(data).T_RF_SLOW == data["values"]["T_RF_SLOW"]


class _RecordedAsModule:
    """The fixture dressed as a module, so `_Recorder` can replay a read against it."""

    def __init__(self, data):
        self._recorded = _Recorded(data)

    def __getattr__(self, name):
        return getattr(self._recorded, name)


def test_the_fixture_matches_the_live_reference():
    """When the reference checkout is present, every recorded value is what it computes now,
    its public functions are the ones the fixture lists, each with the parameters recorded, and
    no file the fixture may record as uncommitted (`_UNLOADED_DIRTY`) is one the module loads.
    A function the reference gains or drops therefore fails here, and the leg-coverage check
    reads it once the fixture is regenerated. Skipped only where the checkout has no
    `swtiming` directory."""
    module = _live_module()
    if module is None:
        pytest.skip(f"no reference checkout at {_REF_DIR}; the fixture stands alone")
    loaded = {os.path.relpath(m.__file__, _REF_DIR).replace(os.sep, "/")
              for name, m in list(sys.modules.items())
              if name.split(".")[0] == "swtiming" and getattr(m, "__file__", None)}
    assert not loaded & _UNLOADED_DIRTY, sorted(loaded & _UNLOADED_DIRTY)
    with open(_FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    values, parameters = _collect(module)
    stale = sorted(k for k in set(values) | set(data["values"])
                   if k not in values or k not in data["values"]
                   or values[k] != pytest.approx(data["values"][k], rel=1e-12, abs=1e-12))
    stale += sorted(f"parameters({n})" for n in parameters
                    if n in data["parameters"] and parameters[n] != data["parameters"][n])
    stale += sorted(f"parameters({n}) added" for n in set(parameters) - set(data["parameters"]))
    stale += sorted(f"parameters({n}) removed"
                    for n in set(data["parameters"]) - set(parameters))
    assert not stale, (
        "tests/fixtures/timing_reference.json no longer matches the reference "
        f"({data['reference']['module']} at {data['reference']['revision']}): "
        + ", ".join(stale[:12]) + f". Read the difference, then regenerate: {_REGENERATE}")


def test_a_reference_checkout_that_does_not_import_fails_rather_than_skips(tmp_path,
                                                                           monkeypatch):
    """`_live_module` is None only where the checkout has no `swtiming` directory. A directory
    whose `swtiming.emit_ede` does not import raises ImportError naming the checkout and the
    import's own error, so the staleness check fails instead of skipping as if there were no
    checkout."""
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_REF_DIR", str(tmp_path / "absent"))
    assert _live_module() is None
    pkg = tmp_path / "broken" / "swtiming"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "emit_ede.py").write_text("def x(:\n", encoding="utf-8")
    monkeypatch.setattr(module, "_REF_DIR", str(tmp_path / "broken"))
    for name in [n for n in sys.modules if n.split(".")[0] == "swtiming"]:
        monkeypatch.delitem(sys.modules, name)
    try:
        with pytest.raises(ImportError, match="has a swtiming directory") as raised:
            _live_module()
        assert str(tmp_path / "broken") in str(raised.value), raised.value
        assert "SyntaxError" in str(raised.value), raised.value
    finally:
        for name in [n for n in sys.modules if n.split(".")[0] == "swtiming"]:
            del sys.modules[name]
    assert str(tmp_path / "broken") not in sys.path


if __name__ == "__main__":
    if sys.argv[1:] in (["--regenerate"], ["--regenerate", "--allow-dirty"]):
        regenerate(allow_dirty="--allow-dirty" in sys.argv[1:])
    else:
        raise SystemExit(f"usage: {_REGENERATE} [--allow-dirty]")
