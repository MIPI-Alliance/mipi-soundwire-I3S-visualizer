"""SWI3S PHY timing calculator (ported from the standalone timing-analysis app).

Pure-numpy-free compute: `compute(CalcInputs) -> CalcResults` evaluates the four
SWI3S timing inequalities (MP/PM × setup/hold) as term-by-term breakdowns with
margins, an implied F_max, and the binding constraint. The source app validated
this math against two reference spreadsheets (56/56, 58/58); we reuse it verbatim
(only the intra-package import was relativised) and render the margins as text in
Timing mode — no plots, no SoundWire-1.3 interop.
"""
from .delta_tpd import RampShape
from .calculator import (
    CalcInputs, CalcResults, CalcRow, InequalityBreakdown, Term, ParamRange,
    Inequality, INEQUALITIES, Pick,
    compute, apply_row_picks, find_worst_corner_rows, default_swi3s_rows,
)

__all__ = [
    "RampShape", "CalcInputs", "CalcResults", "CalcRow", "InequalityBreakdown",
    "Term", "ParamRange", "Inequality", "INEQUALITIES", "Pick",
    "compute", "apply_row_picks", "find_worst_corner_rows", "default_swi3s_rows",
]
