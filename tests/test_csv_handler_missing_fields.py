"""CSVHandler.load_csv's missing_fields for the scalar CDS registers.

save_csv writes the per-source CDS rows and not the scalar CDS_GuardEnabled_REG,
CDS_GuardPolarity_REG and CDS_TailWidth_REG, which load_csv derives from those rows. A
scalar is reported missing only when the per-source row it is derived from is absent too.
"""
import csv
import os

from swi3s_studio.swviz.io.csv_handler import (
    CDS_GUARD_ENABLE_PER_SOURCE,
    CDS_GUARD_POLARITY_PER_SOURCE,
    CDS_TAIL_PER_SOURCE,
    CSVHandler,
)
from swi3s_studio.swviz.models.interface import Interface
from swi3s_studio.swviz.viz import VizConfig

_SCALARS = ("CDS_GuardEnabled_REG", "CDS_GuardPolarity_REG", "CDS_TailWidth_REG")


def _saved(tmp_path) -> str:
    path = os.path.join(str(tmp_path), "saved.csv")
    CSVHandler.save_csv(path, Interface(), VizConfig())
    return path


def _load(path):
    result = CSVHandler.load_csv(path, Interface(), VizConfig())
    assert result.success, result.error_message
    return result


def _without_rows(src: str, dst: str, names) -> None:
    with open(src, encoding="utf8", newline="") as f:
        rows = [r for r in csv.reader(f) if not (r and r[0] in names)]
    with open(dst, "w", encoding="utf8", newline="") as f:
        csv.writer(f).writerows(rows)


def test_a_file_the_app_saved_loads_with_no_missing_fields(tmp_path):
    assert _load(_saved(tmp_path)).missing_fields == []


def test_the_saver_writes_no_scalar_cds_row(tmp_path):
    with open(_saved(tmp_path), encoding="utf8", newline="") as f:
        names = {r[0] for r in csv.reader(f) if r}
    assert not names & set(_SCALARS)


def test_a_scalar_is_missing_when_its_per_source_row_is_absent_too(tmp_path):
    """Without the per-source rows the scalar is the source of the derived rows, so its
    absence does default them."""
    stripped = os.path.join(str(tmp_path), "stripped.csv")
    _without_rows(_saved(tmp_path), stripped,
                  {CDS_GUARD_ENABLE_PER_SOURCE, CDS_GUARD_POLARITY_PER_SOURCE,
                   CDS_TAIL_PER_SOURCE})
    assert _load(stripped).missing_fields == [f"Interface: {n}" for n in _SCALARS]


def test_only_the_tail_scalar_is_missing_when_only_the_tail_row_is_absent(tmp_path):
    stripped = os.path.join(str(tmp_path), "stripped.csv")
    _without_rows(_saved(tmp_path), stripped, {CDS_TAIL_PER_SOURCE})
    assert _load(stripped).missing_fields == ["Interface: CDS_TailWidth_REG"]
