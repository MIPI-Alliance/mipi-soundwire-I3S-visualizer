"""Region-scoped forced column count, and the CSV-import geometry bug behind it.

Two defects, one user report ("the Analyzer opens the CSV as 7 columns vs 16 in the
Visualizer"):

1. **The stale Safe-Lock seed.** `_configure_source_for_phy` seeded
   `forced_column_count` from the cold-start Safe-Lock width and stored it on the
   session. That store is sticky, and `forcedColumnCount` outranks the CSV in
   `Decoder::run`. So importing a config CSV onto an already-open cold-start capture
   kept decoding at the Safe-Lock width (2), and the grid — sized
   `max(column_count, furthest placed cell + 1)` — collapsed to the furthest cell.
   Skipping the seed when a CSV is present was not enough: it had to be CLEARED,
   because an earlier decode had already stored it.

2. **No way to say "use this width here".** Even decoding correctly, a capture whose
   geometry the wire never carried (post-commit, no NumColumns commit to snoop) or
   whose blind detection mis-locked had no override that didn't misframe every other
   region. Hence `Session.force_column_count_at` — pinned per config region, because a
   capture that genuinely reconfigures mid-stream has several real widths.
"""
import json

import pytest

from swi3s_studio.session import Session

CSV_COLUMNS = 16          # NumColumns_REG 15 -> 16 columns


def _write_config_csv(path, *, columns=CSV_COLUMNS, second_port=True):
    """A minimal v3 config CSV: two 3-channel ports, DP1 optional.

    With DP1 the furthest placed cell is column 15, so a collapsed grid still spans 16
    and the bug is invisible in the width alone. Dropping DP1 moves the furthest cell to
    column 6 — which is exactly the "7 columns" the user reported.
    """
    enable = "0b111,0b111" if second_port else "0b111,0b0"
    rows = [
        "AppVersion,3.0.10",
        f"NumColumns_REG,{columns - 1}",
        "SkippingDenominator_REG,1",
        "PHY3Enabled,False",
        "S0Width,1",
        "S1TailWidth_REG,0",
        "EnforceS1Handover,True",
        "CDS_BitWidth_REG,0",
        "CDS_GuardEnabled_REG,False",
        "CDS_GuardPolarity_REG,False",
        "CDS_TailWidth_REG,0",
        "EnforceCDSHandover,False",
        "RowRate,3072.0",
        "Description,\"forced-column-count directed test\"",
        "RowsToDraw,8",
        "DeviceNumber_REG," + ",".join(["0"] * 12),
        "ManagerDataport," + ",".join(["False"] * 12),
        f"EnableCh_REG,{enable}," + ",".join(["0b0"] * 10),
        "SampleSize_REG," + ",".join(["0"] * 12),
        "SampleGrouping_REG,1,1," + ",".join(["0"] * 10),
        "ChannelGrouping_REG,2,2," + ",".join(["0"] * 10),
        "Spacing_REG," + ",".join(["0"] * 12),
        "Interval_REG,11,11," + ",".join(["0"] * 10),
        "Offset_REG," + ",".join(["0"] * 12),
        "HorizontalStart_REG,4,13," + ",".join(["0"] * 10),
        "HorizontalCount_REG,2,2," + ",".join(["0"] * 10),
        "TailWidth_REG," + ",".join(["0"] * 12),
        "BitWidth_REG," + ",".join(["0"] * 12),
        "SkippingNumerator_REG," + ",".join(["0"] * 12),
        "PortDirection_REG," + ",".join(["False"] * 12),
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return str(path)


def _grid_width(session, sample):
    """What the Bus Grid actually draws: max(axis width, furthest placed cell + 1).

    Mirrors GridView.set_cells so the assertion is about the rendered grid, not just the
    session attribute the bug happened to corrupt.
    """
    cells = session.grid_cells_at(int(sample), 8)
    axis = int(session.column_count_at(int(sample)))
    return max(axis, max((c["col"] for c in cells), default=-1) + 1)


# --------------------------------------------------------------------------
# 1. The stale Safe-Lock seed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("phy,cold_start", [(2, True), (2, False), (1, False)])
def test_config_csv_import_decodes_at_the_csv_width(tmp_path, phy, cold_start):
    """Importing a config CSV must decode at the CSV's column count.

    Parametrized over the cold start specifically: only that path recovers a Safe-Lock
    width to go stale, so a single-PHY test would have passed throughout the bug.
    """
    csv = _write_config_csv(tmp_path / "cfg.csv")
    s = Session.from_demo(48, phy=phy, cold_start=cold_start)
    s.apply_config_csv(csv)
    assert s.column_count == CSV_COLUMNS
    assert [seg["column_count"] for seg in s.segments] == [CSV_COLUMNS]


def test_config_csv_import_clears_a_stale_safe_lock_seed(tmp_path):
    """The regression itself: the seed must be cleared, not merely skipped.

    `_configure_source_for_phy` had `if not self._config_csv and safe_lock_columns:` —
    correct for a session OPENED with a CSV, wrong for one imported later, because the
    earlier decode already stored the seed on the session.
    """
    csv = _write_config_csv(tmp_path / "cfg.csv")
    s = Session.from_demo(48, phy=2, cold_start=True)
    assert s._forced_column_count == 2, "cold start should seed the Safe-Lock width"
    s.apply_config_csv(csv)
    assert s._forced_column_count == 0, "an imported CSV must clear the stale seed"


def test_config_csv_import_does_not_collapse_the_grid(tmp_path):
    """The user-visible symptom: a 16-column config drawn as 7 columns.

    DP1 is disabled so the furthest placed cell is column 6; with the bug the grid is
    sized off that cell (7) instead of the CSV's 16.
    """
    csv = _write_config_csv(tmp_path / "cfg.csv", second_port=False)
    s = Session.from_demo(48, phy=2, cold_start=True)
    s.apply_config_csv(csv)
    sample = int(s.segments[0]["start_sample"]) + 1000
    assert _grid_width(s, sample) == CSV_COLUMNS


def test_config_csv_import_keeps_commands_decodable(tmp_path):
    """Framing at the wrong width doesn't just mis-draw — it mis-parses.

    At 2 columns the demo's commands came out CRC-red; the grid was the visible half of
    a decode-wide failure. Pins the parse, not just the geometry.
    """
    csv = _write_config_csv(tmp_path / "cfg.csv")
    s = Session.from_demo(48, phy=2, cold_start=True)
    s.apply_config_csv(csv)
    bad = [c for c in s.commands if not c.get("crc_valid", True)]
    assert not bad, f"{len(bad)} CRC-invalid command(s) after importing the config CSV"


def test_cold_start_without_csv_still_seeds_safe_lock():
    """Guard the fix's blast radius: the seed exists so the blind detector can't
    mis-lock on a tightly-packed layout. Only the CSV path may clear it."""
    s = Session.from_demo(48, phy=2, cold_start=True)
    assert s._forced_column_count == 2
    assert [seg["column_count"] for seg in s.segments][0] == 2


def test_dlv_still_forces_its_own_column_count():
    """PHY3 returns before the FBCSE branch and must keep forcing its recovered width —
    the DlvSampleSource subdivides each row by it."""
    s = Session.from_demo(48, phy=3, cold_start=True)
    assert s._forced_column_count == 4


# --------------------------------------------------------------------------
# 2. Region-scoped pin
# --------------------------------------------------------------------------

def test_force_column_count_pins_only_the_cursor_region():
    """The whole point of region scoping: pinning one region must leave the earlier
    ones at the width read off the wire."""
    s = Session.from_demo(48, phy=2, cold_start=True)
    assert len(s.segments) >= 2, "need a multi-geometry capture"
    first_width = int(s.segments[0]["column_count"])
    sample = int(s.segments[1]["start_sample"]) + 500

    sect = s.force_column_count_at(sample, 12)

    assert sect == 1
    assert int(s.segments[0]["column_count"]) == first_width, "region 0 must be untouched"
    assert 12 in [int(x["column_count"]) for x in s.segments]


def test_force_column_count_reports_the_pin_back():
    s = Session.from_demo(48, phy=2, cold_start=True)
    sample = int(s.segments[1]["start_sample"]) + 500
    s.force_column_count_at(sample, 12)
    assert s.forced_column_count_at(sample) == 12


def test_force_column_count_zero_removes_the_pin():
    s = Session.from_demo(48, phy=2, cold_start=True)
    before = [int(x["column_count"]) for x in s.segments]
    sample = int(s.segments[1]["start_sample"]) + 500
    s.force_column_count_at(sample, 12)
    assert [int(x["column_count"]) for x in s.segments] != before
    s.force_column_count_at(sample, 0)
    assert [int(x["column_count"]) for x in s.segments] == before
    assert s.forced_column_sections == {}


def test_clear_forced_column_sections_restores_the_wire_geometry():
    s = Session.from_demo(48, phy=2, cold_start=True)
    before = [int(x["column_count"]) for x in s.segments]
    s.force_column_count_at(int(s.segments[1]["start_sample"]) + 500, 12)
    s.clear_forced_column_sections()
    assert [int(x["column_count"]) for x in s.segments] == before
    assert s.source.get("forced_column_sections") is None


def test_force_column_count_rejects_an_illegal_width():
    s = Session.from_demo(48, phy=2, cold_start=True)
    with pytest.raises(ValueError):
        s.force_column_count_at(int(s.segments[0]["start_sample"]), 33)
    with pytest.raises(ValueError):
        s.force_column_count_at(int(s.segments[0]["start_sample"]), 1)


def test_repinning_the_same_width_is_a_no_op():
    """Re-decoding a large capture is expensive; setting a pin to what it already is
    must not trigger one (mirrors set_ssp_row / set_register_overrides)."""
    s = Session.from_demo(48, phy=2, cold_start=True)
    sample = int(s.segments[1]["start_sample"]) + 500
    s.force_column_count_at(sample, 12)
    marker = object()
    s._sentinel = marker                      # survives only if no re-decode happens
    decoder_before = s.decoder
    s.force_column_count_at(sample, 12)
    assert s.decoder is decoder_before, "unchanged pin should skip the re-decode"


def test_pin_survives_a_workspace_round_trip():
    """The pin changes the DECODE, so a reopen has to carry it into run() — and the
    descriptor round-trips through JSON, which turns the int section keys into strings.
    """
    s = Session.from_demo(48, phy=2, cold_start=True)
    sample = int(s.segments[1]["start_sample"]) + 500
    s.force_column_count_at(sample, 12)
    live = [int(x["column_count"]) for x in s.segments]

    reopened = Session.from_source(json.loads(json.dumps(s.source)))

    assert reopened.forced_column_sections == {1: 12}
    assert [int(x["column_count"]) for x in reopened.segments] == live


def test_pin_drives_the_timeline_forced_badge():
    """The band must be able to say '(forced)' — a pinned width that looks snooped is
    the failure mode this label exists to prevent."""
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timeline import TimelineRibbon

    QApplication.instance() or QApplication([])
    s = Session.from_demo(48, phy=2, cold_start=True)
    sample = int(s.segments[1]["start_sample"]) + 500
    sect = s.force_column_count_at(sample, 12)

    tl = TimelineRibbon()
    assert tl._segment_label(sect, 12, True) == "12col (forced)"
    assert tl._segment_label(sect, 12, False) == "12col"


def test_forced_outline_is_confined_to_the_pinned_region():
    """Render check: the '(forced)' outline must cover the pinned band and nothing else.

    Drawn with drawRect, which FILLS with the painter's current brush — an easy way to
    paint a solid rectangle across the whole strip instead of an outline. Diffing a
    forced against an unforced render localises exactly which pixels the flag changes,
    without asserting on a colour the theme is free to restyle.
    """
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timeline import TimelineRibbon

    QApplication.instance() or QApplication([])

    def render(forced):
        tl = TimelineRibbon()
        tl.resize(900, 92)
        segs = [{"start_sample": 0, "column_count": 2, "row_base": 0},
                {"start_sample": 500_000, "column_count": 12, "row_base": 100}]
        if forced:
            segs[1]["forced"] = True
        # set_events before set_segments: only set_events resets the view window, and
        # without it everything maps off-screen and the render is blank (load_session
        # calls them in this order for the same reason).
        tl.set_events([{"start_sample": 0}, {"start_sample": 999_999}], 1_000_000)
        tl.set_segments(segs, 1_000_000)
        return tl, tl.grab().toImage()

    tl, plain = render(False)
    _, forced = render(True)

    diff_x = [x for y in range(plain.height()) for x in range(plain.width())
              if plain.pixelColor(x, y) != forced.pixelColor(x, y)]
    assert diff_x, "the forced flag changed nothing on screen"

    band_start = tl._x_for(500_000)
    track_right = tl._track_rect().right()
    assert min(diff_x) >= band_start - 2, (
        f"outline starts at x={min(diff_x)}, before the pinned band at x={band_start}")
    assert max(diff_x) <= track_right + 2


def test_menu_action_pins_the_cursor_region_and_labels_it():
    """The whole feature through the real UI: Analyzer ▸ Force Column Count re-decodes
    off the GUI thread, and when it lands the timeline band says '(forced)'.

    Covers the wiring the unit tests can't: that main_window tags the band segments,
    that the async `done` callback runs, and that an untouched region keeps its own
    label (a global force would have relabelled region 0 too).
    """
    import time

    from PySide6.QtWidgets import QApplication, QInputDialog

    from swi3s_studio.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    try:
        w.load_session(Session.from_demo(48, phy=2, cold_start=True))
        app.processEvents()
        assert len(w._session.segments) >= 2
        w.cursor.set_sample(int(w._session.segments[1]["start_sample"]) + 500)
        app.processEvents()

        original = QInputDialog.getInt
        QInputDialog.getInt = staticmethod(lambda *a, **k: (12, True))
        try:
            w.force_column_count()
            deadline = time.time() + 60
            while getattr(w, "_load_thread", None) is not None and time.time() < deadline:
                app.processEvents()
                time.sleep(0.01)
            app.processEvents()
        finally:
            QInputDialog.getInt = original

        assert w._session.forced_column_sections == {1: 12}
        bands = w._timeline._segments
        labels = [w._timeline._segment_label(i, int(b["column_count"]),
                                             bool(b.get("forced")))
                  for i, b in enumerate(bands)]
        assert "12col (forced)" in labels
        assert not bands[0].get("forced"), "an untouched region must not read as forced"
    finally:
        w.join_worker_threads()


def test_pin_applies_to_an_imported_csv_region(tmp_path):
    """The two halves together: import a CSV, then pin the region to its width."""
    csv = _write_config_csv(tmp_path / "cfg.csv", second_port=False)
    s = Session.from_demo(48, phy=2, cold_start=True)
    s.apply_config_csv(csv)
    sample = int(s.segments[0]["start_sample"]) + 1000
    s.force_column_count_at(sample, CSV_COLUMNS)
    assert s.forced_column_count_at(sample) == CSV_COLUMNS
    assert _grid_width(s, sample) == CSV_COLUMNS
