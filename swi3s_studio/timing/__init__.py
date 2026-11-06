"""SWI3S PHY timing calculator.

Pure Python, no numpy: `compute(CalcInputs) -> CalcResults` evaluates the SWI3S
timing inequalities (`INEQUALITIES`) as term-by-term breakdowns with margins, an
implied F_max, and the binding constraint. Timing mode renders the margins as text.

`CalcInputs` also carries a per-side spec source, so the same inequalities cover a
mixed SWI3S ↔ SoundWire-1.3 bus. Defaults are SWI3S on both sides; see
`spec_source` for what a cross-spec selection changes (`THRESHOLD_NOTE`, `TRF_NOTE`,
`HANDOVER_CROSS_SPEC_NOTE`) and `docs/anchors.md` for the anchors.
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
    RowPick,
    RowPoint,
    Term,
    apply_row_picks,
    compute,
    default_swi3s_rows,
    find_worst_corner_rows,
    find_worst_corner_rows_all,
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
    sw13_anchor_correction,
)

__all__ = [
    "RampShape", "CalcInputs", "CalcResults", "CalcRow", "InequalityBreakdown",
    "Term", "ParamRange", "Inequality", "INEQUALITIES", "Pick", "RowPick", "RowPoint",
    "PPPlacement", "PP_PLACEMENT_LABELS", "pp_net_pd",
    "compute", "apply_row_picks", "find_worst_corner_rows", "find_worst_corner_rows_all",
    "default_swi3s_rows",
    "SpecSource", "SystemSize", "SPEC_SOURCES", "SPEC_LABELS",
    "preset", "sw13_anchor_correction", "fclk_ceiling_MHz",
    "is_soundwire",
]
