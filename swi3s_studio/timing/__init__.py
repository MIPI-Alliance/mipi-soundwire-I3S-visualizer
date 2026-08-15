"""SWI3S PHY timing calculator (ported from the standalone timing-analysis app).

Pure-numpy-free compute: `compute(CalcInputs) -> CalcResults` evaluates the four
SWI3S timing inequalities (MP/PM × setup/hold) as term-by-term breakdowns with
margins, an implied F_max, and the binding constraint. The source app validated
this math against two reference spreadsheets (56/56, 58/58); we reuse it verbatim
(only the intra-package import was relativised) and render the margins as text in
Timing mode — no plots.

`CalcInputs` also carries a per-side spec source, so the same inequalities cover a
mixed SWI3S ↔ SoundWire-1.3 bus. Defaults are SWI3S on both sides and reproduce the
validated single-spec numbers bit-for-bit; see `spec_source` for what a cross-spec
selection changes and, importantly, for the divergences left deliberately
unreconciled.
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
