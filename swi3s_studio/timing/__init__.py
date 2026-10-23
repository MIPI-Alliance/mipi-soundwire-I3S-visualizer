"""SWI3S PHY timing calculator.

Pure Python, no numpy: `compute(CalcInputs) -> CalcResults` evaluates the SWI3S
timing inequalities (`INEQUALITIES`) as term-by-term breakdowns with margins, an
implied F_max, and the binding constraint. Timing mode renders the margins as text.

`CalcInputs` also carries a per-side spec source, so the same inequalities cover a
mixed SWI3S ↔ SoundWire-1.3 bus. Defaults are SWI3S on both sides; see
`spec_source` for what a cross-spec selection changes and for the divergences it
leaves unreconciled.
"""
from .calculator import (
    INEQUALITIES,
    PP_PLACEMENT_LABELS,
    CalcInputs,
    CalcResults,
    CalcRow,
    Inequality,
    InequalityBreakdown,
    ParamRange,
    Pick,
    PPPlacement,
    Term,
    apply_row_picks,
    compute,
    default_swi3s_rows,
    find_worst_corner_rows,
    pp_net_pd,
)
from .delta_tpd import RampShape
from .spec_source import (
    SPEC_LABELS,
    SPEC_SOURCES,
    SpecSource,
    SystemSize,
    fclk_ceiling_MHz,
    is_soundwire,
    preset,
    pure_output_delay,
    schmitt_delta_ns,
)

__all__ = [
    "RampShape", "CalcInputs", "CalcResults", "CalcRow", "InequalityBreakdown",
    "Term", "ParamRange", "Inequality", "INEQUALITIES", "Pick",
    "PPPlacement", "PP_PLACEMENT_LABELS", "pp_net_pd",
    "compute", "apply_row_picks", "find_worst_corner_rows", "default_swi3s_rows",
    "SpecSource", "SystemSize", "SPEC_SOURCES", "SPEC_LABELS",
    "preset", "pure_output_delay", "schmitt_delta_ns", "fclk_ceiling_MHz",
    "is_soundwire",
]
