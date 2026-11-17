"""The Timing page's column inputs from its seed tables, without building the page (a helper,
not a test file).

`column(man, per, side, ...)` is what `TimingView._typ_baseline(side)` returns on a page Reset
to a Manager on `man` and a Peripheral on `per`, analog launch: every row seeded by
`TimingView._seed_range` and read back through its box (the box's decimals, rounding a tie
away from zero, and its range), only the rows the column holds (`_side_rows`), every other
input as Reset leaves it. `worst(base, rows)` is
each inequality's result at its own worst corner, from `find_worst_corner_rows_all` and
`compute` (the calculator module's, so a test module's shared memo serves it), as the page's
recompute gives it.

The page module is imported here, at module level, for its tables and pure functions; no
function here builds a widget, so a test using them is core.
`test_timing_view_columns.py::test_the_page_reads_the_inputs_its_seed_tables_give` pins
`column` against the page.
"""
from decimal import ROUND_HALF_UP, Decimal

from swi3s_studio.timing import (
    INEQUALITIES,
    CalcInputs,
    CalcRow,
    ParamRange,
    apply_row_picks,
    calculator,
    default_swi3s_rows,
    find_worst_corner_rows_all,
)
from swi3s_studio.timing.spec_source import (
    SPEC_SOURCES,
    LaunchMode,
    default_handover_UIs,
    is_ede,
    is_proposed,
    preset,
)
from swi3s_studio.ui import timing_view as page

# The specs each spec selector offers, in its order.
OFFERED = tuple(spec for spec in SPEC_SOURCES if not is_proposed(spec))


def _boxed(value: float, decimals: int, lo: float, hi: float) -> float:
    """`value` as a spin box with `decimals` places and range lo..hi holds it."""
    q = Decimal(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    return min(max(float(q), lo), hi)


def column(man: str, per: str, side: str, *, size: str = "small", fclk=None,
           man_shape: str = "linear", per_shape: str = "linear"):
    """(base, rows) of `side` ("spec" or "proposed") on the page Reset to `man` and `per` at
    system size `size`, its ramp models then set to `man_shape` and `per_shape` and its rate
    box to `fclk` (None: the rate Reset seeds)."""
    rows = []
    held = {r.name for r in page._side_rows(side)}
    for row in default_swi3s_rows(lock_mgr_lanes=True):
        if row.name not in held:
            continue
        rng = page.TimingView._seed_range(side, row, man, per, size)
        sc = page._display_scale(row)
        lo = -10000.0 if row.name in page._SIGNED_ROWS else 0.0
        dec = page._decimals_for(row)
        mn, mx = sorted(_boxed(float(v) * sc, dec, lo, 10000.0) / sc for v in (rng.min, rng.max))
        rows.append(CalcRow(row.name, row.binds, ParamRange(mn, 0.5 * (mn + mx), mx),
                            row.linked_to, row.is_window))
    floors = {name: float(page.TimingView._seed_range(side, page._ROW_BY_NAME[name], man,
                                                      per, size).min)
              for name in page.TimingView._LAUNCH_ROWS}
    rate = page._default_fclk_for(man, per, size) if fclk is None else fclk
    base = CalcInputs(
        Man_shape=man_shape, Per_shape=per_shape,
        F_CLK_target_MHz=_boxed(rate, 3, 0.001, 27.0),
        Man_spec=man, Per_spec=per, system_size=size,
        launch_mode=LaunchMode.ANALOG,
        Man_t_DD_floor_ns=floors["Man_t_DD"],
        Man_t_ZD_floor_ns=floors["Man_t_ZD"],
        Man_t_DZ_floor_ns=floors["Man_t_DZ"],
        handover_UIs=1.0 if default_handover_UIs(man, per) > 0 else 0.0,
        pp_placement="unknown",
        t_keeper_ns=preset(man, "Man", size).t_keeper_ns,
        Man_ede=is_ede(man) and side == "proposed",
        Per_ede=is_ede(per) and side == "proposed",
        Man_keeper_forced=False,
    )
    typ = {r.name: "typ" for r in rows if r.linked_to is None}
    return apply_row_picks(base, rows, typ), rows


def worst(base, rows):
    """({inequality: CalcResults at its own worst corner}, {inequality: its row picks})."""
    all_picks = find_worst_corner_rows_all(INEQUALITIES, rows, base)
    return ({ineq: calculator.compute(apply_row_picks(base, rows, all_picks[ineq]))
             for ineq in INEQUALITIES},
            {ineq: dict(all_picks[ineq]) for ineq in INEQUALITIES})
