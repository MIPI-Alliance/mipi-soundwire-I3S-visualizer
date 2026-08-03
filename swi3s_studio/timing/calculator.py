"""SWI3S spec calculator: 4 inequalities, t_cross, F_max, term-by-term breakdown.

Used by app/spec_calculator.py.  Independent of emit_numerics.py / the doc
pipeline; the doc keeps using single-shape compute_delta_tpd_envelope.

Per-lane t_RF (Man_CLK, Man_DATA, Per_DATA) and per-device ramp shape
(Man, Per) are first-class inputs.  Inequality structure follows
docs/SWI3S_Timing_Analysis.tex §3 (eqs 1-4) and §6 with V=0/V=V_DD anchor:
t_DD,pure = pure device latency from clock event to start of physical
slew at the TX pin; the slew portion lives in t_cross / Σ_cross.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

from .delta_tpd import (
    DeltaTpdEnvelope,
    RampShape,
    compute_delta_tpd_envelope_per_device,
    per_tdd_offset_frac,
)

Inequality = Literal["MP_setup", "MP_setup_ho", "MP_hold",
                     "PM_setup", "PM_setup_ho", "PM_hold"]
# Each setup constraint has a handover ("_ho") variant that applies when the
# previous UI was a Z-handover: the driver turns on from high-Z, so t_DD is
# replaced by t_ZD (its max corner is the setup worst case).
INEQUALITIES: tuple[Inequality, ...] = ("MP_setup", "MP_setup_ho", "MP_hold",
                                        "PM_setup", "PM_setup_ho", "PM_hold")
Pick = Literal["min", "typ", "max"]


# ---------------------------------------------------------------------------
# Parameter ranges (a typed view over min/typ/max for one spec parameter)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamRange:
    """min/typ/max numeric values for one spec parameter."""
    min: float
    typ: float
    max: float

    def at(self, pick: Pick) -> float:
        return {"min": self.min, "typ": self.typ, "max": self.max}[pick]


# ---------------------------------------------------------------------------
# Calculator inputs (one column: Spec or Proposed, evaluated at one corner)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalcInputs:
    """All scalar inputs needed to evaluate the 4 SWI3S inequalities."""

    # Voltage thresholds (fractions of V_SEOS)
    V_OH_min_frac: float = 0.80
    V_OL_max_frac: float = 0.20
    V_IH_min_frac: float = 0.45
    V_IH_max_frac: float = 0.65
    V_IL_min_frac: float = 0.35
    V_IL_max_frac: float = 0.55

    # Supply
    V_SEOS_nom_V: float = 1.2
    V_SEOS_tol_frac: float = 0.05            # δ

    # Noise budget (α = noise_pp / V_SEOS_nom)
    V_noise_pp_frac: float = 0.10            # α

    # Slew tolerance (τ)
    tRF_tolerance_frac: float = 0.05

    # t_RF per lane (ns) — Mgr CLK/DATA may differ; Per CLK doesn't exist (Mgr drives CLK)
    tRF_Man_CLK_ns: float = 5.0
    tRF_Man_DATA_ns: float = 5.0
    tRF_Per_DATA_ns: float = 5.0

    # Ramp shape per device
    Man_shape: RampShape = "linear"
    Per_shape: RampShape = "linear"

    # Output timing (ns)
    Man_t_DD_max_ns: float = 18.0
    Man_t_DD_min_ns: float = 7.0
    Per_t_DD_max_ns: float = 20.0
    Per_t_DD_min_ns: float = 2.0

    # Input timing requirements (ns)
    Man_t_IS_max_ns: float = 3.0
    Man_t_IH_max_ns: float = 0.0
    Per_t_IS_max_ns: float = 4.0
    Per_t_IH_max_ns: float = 4.5

    # Z-handover (ns). t_ZD is a single parameter (min/typ/max range); the setup
    # handover inequalities use its max corner.
    Man_t_DZ_max_ns: float = 10.0
    Per_t_DZ_max_ns: float = 10.0
    Man_t_ZD_ns: float = 10.0
    Per_t_ZD_ns: float = 10.0

    # Bus & topology
    bus_length_cm: float = 30.0
    vPCB_cm_per_ns: float = 15.0
    vPCB_err_frac: float = 0.10              # peak-to-peak trace mismatch as fraction of t_PD

    # Operating point
    F_CLK_target_MHz: float = 13.2
    duty_min: float = 0.45

    # ---- derived ----

    def t_PD_ns(self) -> float:
        return self.bus_length_cm / self.vPCB_cm_per_ns

    def t_PD_mis_ns(self) -> float:
        return self.vPCB_err_frac * self.t_PD_ns()

    def UI_ns(self) -> float:
        """Half-period × duty floor at the target F_CLK."""
        return 1000.0 * self.duty_min / self.F_CLK_target_MHz

    def envelope(self) -> DeltaTpdEnvelope:
        return compute_delta_tpd_envelope_per_device(
            Man_shape=self.Man_shape,
            Per_shape=self.Per_shape,
            V_OH_frac=self.V_OH_min_frac,
            V_OL_frac=self.V_OL_max_frac,
            V_IH_min_frac=self.V_IH_min_frac,
            V_IH_max_frac=self.V_IH_max_frac,
            V_IL_min_frac=self.V_IL_min_frac,
            V_IL_max_frac=self.V_IL_max_frac,
            VDD_tol=self.V_SEOS_tol_frac,
            noise_budget_V=self.V_noise_pp_frac * self.V_SEOS_nom_V,
            V_nom_V=self.V_SEOS_nom_V,
            tRF_tolerance=self.tRF_tolerance_frac,
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Term:
    """One term in a margin breakdown.  ``value_ns`` is signed (positive
    adds to the margin, negative subtracts) and feeds margin sums.
    ``op`` ('+' or '-') is the inequality-level sign of the term, kept
    explicit so equation rendering shows the right operator even when
    the numerical value is zero (e.g. Man_t_IH,max = 0 in PM_hold)."""
    symbol: str
    value_ns: float
    op: Literal["+", "-"] = "+"


@dataclass(frozen=True)
class InequalityBreakdown:
    name: Inequality
    terms: list[Term]
    required_ns: float          # sum of consumed terms (UI side); 0 for hold inequalities
    margin_ns: float            # sum of all signed terms
    F_max_MHz: float            # bound implied by required UI; math.inf if F_CLK-independent
    is_setup: bool              # True for setup, False for hold


@dataclass(frozen=True)
class CalcResults:
    inputs: CalcInputs
    delta_cross_MP_ns: float
    sigma_pm_setup_ns: float
    sigma_pm_hold_ns: float
    breakdowns: dict[Inequality, InequalityBreakdown]

    @property
    def F_max_binding_MHz(self) -> float:
        """min over the 4 inequalities. Hold inequalities contribute inf if
        passing, 0 if failing (they don't bound F_CLK directly)."""
        vals = []
        for ineq, b in self.breakdowns.items():
            if b.is_setup:
                vals.append(b.F_max_MHz)
            else:
                vals.append(math.inf if b.margin_ns >= 0 else 0.0)
        return min(vals) if vals else 0.0

    @property
    def binding_inequality(self) -> Inequality:
        """The inequality with the smallest setup F_max (or first failing hold)."""
        # Failing hold trumps anything else
        for ineq, b in self.breakdowns.items():
            if (not b.is_setup) and b.margin_ns < 0:
                return ineq
        # Otherwise pick smallest setup F_max
        setup_items = [(ineq, b.F_max_MHz) for ineq, b in self.breakdowns.items() if b.is_setup]
        return min(setup_items, key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# compute()
# ---------------------------------------------------------------------------

def _per_tdd_pure_max(inp: CalcInputs) -> float:
    """Per_t_DD,max,pure = Per_t_DD,max − offset · t_RF_Per_DATA."""
    off = per_tdd_offset_frac(inp.Per_shape)
    return inp.Per_t_DD_max_ns - off * inp.tRF_Per_DATA_ns


def _per_tdd_pure_min(inp: CalcInputs) -> float:
    """Per_t_DD,min,pure = Per_t_DD,min − offset · t_RF_Per_DATA."""
    off = per_tdd_offset_frac(inp.Per_shape)
    return inp.Per_t_DD_min_ns - off * inp.tRF_Per_DATA_ns


def _per_tzd_pure_max(inp: CalcInputs) -> float:
    """Per_t_ZD,max,pure = Per_t_ZD,max − offset · t_RF_Per_DATA. Same ramp-offset
    back-out as Per_t_DD (a Peripheral data-output delay measured to the crossing)."""
    off = per_tdd_offset_frac(inp.Per_shape)
    return inp.Per_t_ZD_ns - off * inp.tRF_Per_DATA_ns


def compute(inp: CalcInputs) -> CalcResults:
    """Evaluate all four SWI3S inequalities at the supplied corner.

    Returns t_cross values (Δ_cross,MP, Σ_cross,PM,setup, Σ_cross,PM,hold)
    and a per-inequality breakdown with terms in ns and signs baked in.
    """
    env = inp.envelope()
    t_PD = inp.t_PD_ns()
    t_PDmis = inp.t_PD_mis_ns()
    UI = inp.UI_ns()

    delta_MP_ns = env.delta_cross_MP_ns(inp.tRF_Man_CLK_ns, inp.tRF_Man_DATA_ns)
    sigma_setup_ns = env.sigma_pm_setup_ns(inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns)
    sigma_hold_ns = env.sigma_pm_hold_ns(inp.tRF_Man_CLK_ns, inp.tRF_Per_DATA_ns)

    per_tdd_max_pure = _per_tdd_pure_max(inp)
    per_tdd_min_pure = _per_tdd_pure_min(inp)

    breakdowns: dict[Inequality, InequalityBreakdown] = {}

    # ----- MP setup: UI ≥ Man_t_DD,max + Per_t_IS,max + t_PD,mis + Δ_cross,MP -----
    mp_setup_required = (
        inp.Man_t_DD_max_ns + inp.Per_t_IS_max_ns + t_PDmis + delta_MP_ns
    )
    mp_setup_terms = [
        Term("UI", +UI, op="+"),
        Term("Man_t_DD,max", -inp.Man_t_DD_max_ns, op="-"),
        Term("Per_t_IS,max", -inp.Per_t_IS_max_ns, op="-"),
        Term("t_PD,mis", -t_PDmis, op="-"),
        Term("Δ_cross,MP", -delta_MP_ns, op="-"),
    ]
    breakdowns["MP_setup"] = InequalityBreakdown(
        name="MP_setup",
        terms=mp_setup_terms,
        required_ns=mp_setup_required,
        margin_ns=UI - mp_setup_required,
        F_max_MHz=(1000.0 * inp.duty_min / mp_setup_required) if mp_setup_required > 0 else math.inf,
        is_setup=True,
    )

    # ----- MP setup, handover UI: as MP_setup but Man_t_DD,max → Man_t_ZD,max -----
    mp_setup_ho_required = (
        inp.Man_t_ZD_ns + inp.Per_t_IS_max_ns + t_PDmis + delta_MP_ns
    )
    mp_setup_ho_terms = [
        Term("UI", +UI, op="+"),
        Term("Man_t_ZD,max", -inp.Man_t_ZD_ns, op="-"),
        Term("Per_t_IS,max", -inp.Per_t_IS_max_ns, op="-"),
        Term("t_PD,mis", -t_PDmis, op="-"),
        Term("Δ_cross,MP", -delta_MP_ns, op="-"),
    ]
    breakdowns["MP_setup_ho"] = InequalityBreakdown(
        name="MP_setup_ho",
        terms=mp_setup_ho_terms,
        required_ns=mp_setup_ho_required,
        margin_ns=UI - mp_setup_ho_required,
        F_max_MHz=(1000.0 * inp.duty_min / mp_setup_ho_required) if mp_setup_ho_required > 0 else math.inf,
        is_setup=True,
    )

    # ----- MP hold: Man_t_DD,min ≥ Per_t_IH,max + t_PD,mis + Δ_cross,MP -----
    mp_hold_terms = [
        Term("Man_t_DD,min", +inp.Man_t_DD_min_ns, op="+"),
        Term("Per_t_IH,max", -inp.Per_t_IH_max_ns, op="-"),
        Term("t_PD,mis", -t_PDmis, op="-"),
        Term("Δ_cross,MP", -delta_MP_ns, op="-"),
    ]
    mp_hold_margin = sum(t.value_ns for t in mp_hold_terms)
    breakdowns["MP_hold"] = InequalityBreakdown(
        name="MP_hold",
        terms=mp_hold_terms,
        required_ns=0.0,
        margin_ns=mp_hold_margin,
        F_max_MHz=math.inf if mp_hold_margin >= 0 else 0.0,
        is_setup=False,
    )

    # ----- PM setup: UI ≥ Per_t_DD,max,pure + Man_t_IS,max + 2·t_PD + Σ_PM,setup -----
    pm_setup_required = (
        per_tdd_max_pure + inp.Man_t_IS_max_ns + 2.0 * t_PD + sigma_setup_ns
    )
    pm_setup_terms = [
        Term("UI", +UI, op="+"),
        Term("Per_t_DD,max,pure", -per_tdd_max_pure, op="-"),
        Term("Man_t_IS,max", -inp.Man_t_IS_max_ns, op="-"),
        Term("2×t_PD", -2.0 * t_PD, op="-"),
        Term("Σ_cross,PM,setup", -sigma_setup_ns, op="-"),
    ]
    breakdowns["PM_setup"] = InequalityBreakdown(
        name="PM_setup",
        terms=pm_setup_terms,
        required_ns=pm_setup_required,
        margin_ns=UI - pm_setup_required,
        F_max_MHz=(1000.0 * inp.duty_min / pm_setup_required) if pm_setup_required > 0 else math.inf,
        is_setup=True,
    )

    # ----- PM setup, handover UI: as PM_setup but Per_t_DD → Per_t_ZD,max,pure -----
    per_tzd_max_pure = _per_tzd_pure_max(inp)
    pm_setup_ho_required = (
        per_tzd_max_pure + inp.Man_t_IS_max_ns + 2.0 * t_PD + sigma_setup_ns
    )
    pm_setup_ho_terms = [
        Term("UI", +UI, op="+"),
        Term("Per_t_ZD,max,pure", -per_tzd_max_pure, op="-"),
        Term("Man_t_IS,max", -inp.Man_t_IS_max_ns, op="-"),
        Term("2×t_PD", -2.0 * t_PD, op="-"),
        Term("Σ_cross,PM,setup", -sigma_setup_ns, op="-"),
    ]
    breakdowns["PM_setup_ho"] = InequalityBreakdown(
        name="PM_setup_ho",
        terms=pm_setup_ho_terms,
        required_ns=pm_setup_ho_required,
        margin_ns=UI - pm_setup_ho_required,
        F_max_MHz=(1000.0 * inp.duty_min / pm_setup_ho_required) if pm_setup_ho_required > 0 else math.inf,
        is_setup=True,
    )

    # ----- PM hold: Per_t_DD,min,pure − Man_t_IH,max + 2·t_PD + Σ_PM,hold ≥ 0 -----
    pm_hold_terms = [
        Term("Per_t_DD,min,pure", +per_tdd_min_pure, op="+"),
        Term("Man_t_IH,max", -inp.Man_t_IH_max_ns, op="-"),
        Term("2×t_PD", +2.0 * t_PD, op="+"),
        Term("Σ_cross,PM,hold", +sigma_hold_ns, op="+"),
    ]
    pm_hold_margin = sum(t.value_ns for t in pm_hold_terms)
    breakdowns["PM_hold"] = InequalityBreakdown(
        name="PM_hold",
        terms=pm_hold_terms,
        required_ns=0.0,
        margin_ns=pm_hold_margin,
        F_max_MHz=math.inf if pm_hold_margin >= 0 else 0.0,
        is_setup=False,
    )

    return CalcResults(
        inputs=inp,
        delta_cross_MP_ns=delta_MP_ns,
        sigma_pm_setup_ns=sigma_setup_ns,
        sigma_pm_hold_ns=sigma_hold_ns,
        breakdowns=breakdowns,
    )


# ---------------------------------------------------------------------------
# Worst-case corner solver (per inequality)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Worst-case corner solver (per inequality, row-centric)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalcRow:
    """A UI-level parameter row.  The picked value (one of min/typ/max from
    ``range``) is applied to every CalcInputs field in ``binds``.

    For ranged spec parameters (V_IH, V_IL, t_DD), ``binds`` lists both the
    min and max bound fields, so picking a single value collapses the band
    to that value across all inequalities.  Per-inequality auto-worst then
    selects the band edge that worst-cases each inequality independently.

    ``linked_to``, if set, names another row in the same list whose pick
    this row mirrors.  Linked rows have their own min/typ/max numeric
    values (so the two lanes can be calibrated independently) but share a
    single corner pick — used to tie Man CLK and Man DATA t_RF so
    auto-worst can't put them at opposite spec edges on the same chip.
    """
    name: str
    binds: tuple[str, ...]
    range: ParamRange
    linked_to: str | None = None


def apply_row_picks(
    base: CalcInputs,
    rows: list[CalcRow],
    picks: dict[str, Pick],
) -> CalcInputs:
    """Apply each row's picked value to all of its bound CalcInputs fields.
    Linked rows use the master row's pick."""
    overrides: dict[str, float] = {}
    for row in rows:
        master = row.linked_to if row.linked_to else row.name
        pick = picks.get(master, "typ")
        val = row.range.at(pick)
        for field in row.binds:
            overrides[field] = val
    return replace(base, **overrides)


def find_worst_corner_rows(
    inequality: Inequality,
    rows: list[CalcRow],
    base: CalcInputs,
) -> dict[str, Pick]:
    """For each MASTER row, pick min/typ/max to minimize the inequality's
    margin.  Linked rows aren't perturbed independently; their pick
    follows the master row.  Returns picks keyed by master row name only.
    """
    masters = [r for r in rows if r.linked_to is None]
    base_picks: dict[str, Pick] = {row.name: "typ" for row in masters}
    out: dict[str, Pick] = {}
    for row in masters:
        margins: dict[Pick, float] = {}
        for pick in ("min", "typ", "max"):
            test_picks = {**base_picks, row.name: pick}
            test_inputs = apply_row_picks(base, rows, test_picks)
            r = compute(test_inputs)
            margins[pick] = r.breakdowns[inequality].margin_ns
        smallest = min(margins.values())
        if math.isclose(margins["typ"], smallest, abs_tol=1e-12):
            out[row.name] = "typ"
        else:
            out[row.name] = min(("min", "max"), key=lambda p: margins[p])
    return out


def default_swi3s_rows(lock_mgr_lanes: bool = True) -> list[CalcRow]:
    """Every parameter the calculator UI exposes, as a row.

    Ranged spec parameters (V_IH, V_IL, Man/Per_t_DD) bind to BOTH the
    min and max fields of CalcInputs — picking a value collapses the band
    to that value.  Single-bound spec parameters (t_IS, t_IH, t_DZ, t_ZD)
    bind to one field; the picker selects min/typ/max for what-if
    exploration.  Operating-point parameters (α, δ, τ, t_RF lanes, bus
    geometry) bind to one field each with the natural corner range.

    When ``lock_mgr_lanes`` is True (default), the Man DATA t_RF row links
    its corner pick to the Man CLK row, so auto-worst can't drive the
    same Mgr chip's CLK and DATA lanes to opposite spec edges.  Numeric
    min/typ/max values for the two lanes remain independently editable.
    """
    return [
        # Voltage thresholds (band edges; picker collapses band)
        CalcRow("V_IH", ("V_IH_min_frac", "V_IH_max_frac"),
                ParamRange(0.45, 0.55, 0.65)),
        CalcRow("V_IL", ("V_IL_min_frac", "V_IL_max_frac"),
                ParamRange(0.35, 0.45, 0.55)),

        # Output timing range (picker collapses min/max to one value)
        CalcRow("Man_t_DD", ("Man_t_DD_min_ns", "Man_t_DD_max_ns"),
                ParamRange(7.0, 12.5, 18.0)),
        CalcRow("Per_t_DD", ("Per_t_DD_min_ns", "Per_t_DD_max_ns"),
                ParamRange(2.0, 11.0, 20.0)),

        # Input timing demands (single-bound, max RX requirement). The ",max" is
        # dropped from the label — the min/max columns already convey the corner.
        CalcRow("Man_t_IS", ("Man_t_IS_max_ns",),
                ParamRange(0.0, 1.5, 3.0)),
        CalcRow("Man_t_IH", ("Man_t_IH_max_ns",),
                ParamRange(0.0, 0.0, 0.0)),
        CalcRow("Per_t_IS", ("Per_t_IS_max_ns",),
                ParamRange(0.0, 2.0, 4.0)),
        CalcRow("Per_t_IH", ("Per_t_IH_max_ns",),
                ParamRange(0.0, 2.25, 4.5)),

        # Z-handover. t_ZD is a single parameter; the handover setup inequalities
        # take its max corner. (t_DZ is snoop-to-Z, unused by the inequalities.)
        CalcRow("Man_t_DZ", ("Man_t_DZ_max_ns",),
                ParamRange(0.0, 5.0, 10.0)),
        CalcRow("Per_t_DZ", ("Per_t_DZ_max_ns",),
                ParamRange(0.0, 5.0, 10.0)),
        CalcRow("Man_t_ZD", ("Man_t_ZD_ns",),
                ParamRange(2.0, 10.0, 18.0)),
        CalcRow("Per_t_ZD", ("Per_t_ZD_ns",),
                ParamRange(2.0, 10.0, 18.0)),

        # Slew per lane.  Mgr DATA's corner pick is tied to Mgr CLK when
        # lock_mgr_lanes is on; numeric values remain independent.
        CalcRow("t_RF Man CLK",  ("tRF_Man_CLK_ns",),  ParamRange(3.0, 5.0, 8.3)),
        CalcRow("t_RF Man DATA", ("tRF_Man_DATA_ns",), ParamRange(3.0, 5.0, 8.3),
                linked_to="t_RF Man CLK" if lock_mgr_lanes else None),
        CalcRow("t_RF Per DATA", ("tRF_Per_DATA_ns",), ParamRange(3.0, 5.0, 8.3)),

        # Noise / supply / slew tolerance
        CalcRow("δ (V_SEOS tol)",       ("V_SEOS_tol_frac",),
                ParamRange(0.0, 0.025, 0.05)),
        CalcRow("α (V_noise / V_SEOS)", ("V_noise_pp_frac",),
                ParamRange(0.0, 0.05, 0.10)),
        CalcRow("τ (per-edge slew)",    ("tRF_tolerance_frac",),
                ParamRange(0.0, 0.025, 0.05)),

        # Bus & topology
        CalcRow("bus length (cm)",     ("bus_length_cm",),
                ParamRange(5.0, 30.0, 60.0)),
        CalcRow("t_PD,mis fraction",   ("vPCB_err_frac",),
                ParamRange(0.0, 0.05, 0.10)),
    ]
