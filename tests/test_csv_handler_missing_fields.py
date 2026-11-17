"""CSVHandler.load_csv's missing_fields for the CDS rows.

save_csv writes the per-source CDS rows and not the scalar CDS_GuardEnabled_REG,
CDS_GuardPolarity_REG, CDS_TailWidth_REG and CDS_DriveType, which load_csv reads in place of
an absent per-source row. A per-source row is reported missing, by its per-source name as
BusConfig and the native loader name it, when the file carries neither it nor its scalar.
"""
import csv
import os

import pytest

from swi3s_studio.swviz.io.csv_handler import (
    CDS_DRIVE_TYPE_PER_SOURCE,
    CDS_EDE_PER_SOURCE,
    CDS_GUARD_ENABLE_PER_SOURCE,
    CDS_GUARD_POLARITY_PER_SOURCE,
    CDS_TAIL_PER_SOURCE,
    CSVHandler,
)
from swi3s_studio.swviz.models.interface import Interface
from swi3s_studio.swviz.viz import VizConfig

_SCALARS = ("CDS_GuardEnabled_REG", "CDS_GuardPolarity_REG", "CDS_TailWidth_REG",
            "CDS_DriveType")
_PER_SOURCE = (CDS_GUARD_ENABLE_PER_SOURCE, CDS_GUARD_POLARITY_PER_SOURCE, CDS_TAIL_PER_SOURCE,
               CDS_DRIVE_TYPE_PER_SOURCE, CDS_EDE_PER_SOURCE)


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


def test_every_cds_row_is_missing_when_neither_spelling_is_present(tmp_path):
    stripped = os.path.join(str(tmp_path), "stripped.csv")
    _without_rows(_saved(tmp_path), stripped, set(_PER_SOURCE))
    assert _load(stripped).missing_fields == [f"Interface: {n}" for n in _PER_SOURCE]


@pytest.mark.parametrize("row", _PER_SOURCE)
def test_a_per_source_row_dropped_alone_is_missing(tmp_path, row):
    stripped = os.path.join(str(tmp_path), "stripped.csv")
    _without_rows(_saved(tmp_path), stripped, {row})
    assert _load(stripped).missing_fields == [f"Interface: {row}"]


@pytest.mark.parametrize("row, scalar", list(zip(_PER_SOURCE, _SCALARS)))
def test_a_legacy_scalar_stands_in_for_its_per_source_row(tmp_path, row, scalar):
    stripped = os.path.join(str(tmp_path), "stripped.csv")
    _without_rows(_saved(tmp_path), stripped, {row})
    with open(stripped, "a", encoding="utf8", newline="") as f:
        csv.writer(f).writerow([scalar, "1"])
    assert _load(stripped).missing_fields == []
