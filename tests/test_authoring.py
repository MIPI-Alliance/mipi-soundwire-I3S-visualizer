"""Visualization-mode authoring tests (pure model, no Qt):

- BusConfig serialises to the v2.0 CSV the engine/core read, and CSV / dict
  round-trip.
- An authored config is placed by the verified C++ cascade (`grid_from_csv`), with
  wide-bit held columns carrying the bit identity.
- Every data port gets a name in the CSV (defaults like the Visualizer).

Clash detection / validation parity is covered end-to-end by
`test_visualizer_engine.py` (the Visualizer's own 89-config JSON testsuite), since
Bus-Visualizer mode is now driven by the vendored Visualizer engine.

Run: PYTHONPATH=. python3 -m pytest tests/test_authoring.py
"""
import os
import tempfile

import swi3score

from swi3s_studio.model import viz_engine
from swi3s_studio.model.bus_config import BusConfig, demo_config


def test_notifications_use_renamed_data_port():
    """A per-DP notification must identify the port by device + DataPortNumber +
    user-assigned name (e.g. 'Dev0 DP3 (LeftMic)'), not the bare default 'DP{index}'. And
    the no-channel case must read 'drawn but no channel enabled' — the port is DRAWN, not
    enabled (enabled means >=1 channel-enable bit set). Regression for both."""
    cfg = demo_config()
    dp = cfg.dataports[3]
    dp.enabled = True                     # drawn on the grid...
    dp.enable_ch = 0                      # ...but no channel enabled -> the notification
    dp.device_number = 0
    dp.name = "LeftMic"
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "renamed.csv"))
        _cells, _clashes, issues, _ncols, _nrows = viz_engine.render_payload(path)
    hits = [i for i in issues if "no channel enabled" in i.message]
    assert hits, [(i.source, i.message) for i in issues]
    src, msg = hits[0].source, hits[0].message
    assert "DP3" in src and "(LeftMic)" in src and "Dev0" in src, src
    assert msg == "drawn but no channel enabled", msg


def test_notification_names_the_dataport_number_not_the_slot_index():
    """The number in a notification label is the port's DataPortNumber, not its position in
    the config.

    They coincide in every example config, so this only shows up where a port is numbered
    independently of its slot — a peripheral using its own DP0 in a later column, as the
    flow-control demo's four devices do. Labelling by index there names a port that does not
    exist on that device.

    3.0.13 fixed the same defect for grid CELLS and missed this path, which kept saying
    'Dev1 DP5' for device 1's DP0. Two boundaries, one mapping — this pins the second."""
    cfg = demo_config()
    dp = cfg.dataports[5]                 # slot 5 ...
    dp.enabled = True
    dp.enable_ch = 0                      # -> emits the 'no channel enabled' notification
    dp.device_number = 1
    dp.dp_number = 0                      # ... carrying DataPortNumber 0 on device 1
    dp.name = ""                          # no rename, so the label is device + number only
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "renumbered.csv"))
        _cells, _clashes, issues, _ncols, _nrows = viz_engine.render_payload(path)
    hits = [i for i in issues if "no channel enabled" in i.message]
    assert hits, [(i.source, i.message) for i in issues]
    src = hits[0].source
    assert "Dev1 DP0" in src, f"expected 'Dev1 DP0', got {src!r} — the slot index leaked"
    assert "DP5" not in src, f"slot index 5 present in {src!r}"


def test_csv_and_dict_roundtrip():
    cfg = demo_config()
    cfg.dataports[2].enabled = True
    cfg.dataports[2].enable_ch = 0b1111
    cfg.dataports[2].device_number = 3
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "c.csv"))
        back = BusConfig.from_csv(path)
    assert back.num_columns == cfg.num_columns
    assert back.column_count() == cfg.column_count()
    assert back.dataports[0].enable_ch == cfg.dataports[0].enable_ch
    assert back.dataports[2].device_number == 3
    assert back.dataports[2].enable_ch == 0b1111
    # dict round-trip (workspace persistence)
    back2 = BusConfig.from_dict(cfg.to_dict())
    assert back2.dataports[2].enable_ch == 0b1111
    assert back2.dataports[0].scrambler_en == cfg.dataports[0].scrambler_en


def test_authored_config_places_via_core():
    cfg = demo_config()
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "c.csv"))
        cells = swi3score.grid_from_csv(path, 48)
        writes = swi3score.registers_from_csv(path)
    data = [c for c in cells if c["slot"] == 1 and c["dp"] == 0]
    assert data, "DP0 should place data cells"
    # stereo: both channels present; data in columns 2-9 (CDS col 0, handover col 1).
    assert {c["channel"] for c in data} == {0, 1}
    assert min(c["col"] for c in data) >= 2 and max(c["col"] for c in data) <= 9
    assert writes, "config should yield register writes"


def test_wide_bit_held_columns_carry_identity():
    # Wide bits (BitWidth>0) replay the held bit across columns; every column must
    # carry the same channel/sample/bit (no channel = -1 holes) so the renderer can
    # merge + label them like the Visualizer.
    cfg = demo_config()
    dp = cfg.dataports[0]
    dp.enable_ch = 0b1; dp.sample_size = 3; dp.bit_width = 1
    dp.horizontal_start = 2; dp.horizontal_count = 15
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "c.csv"))
        cells = swi3score.grid_from_csv(path, 2)
    data = sorted((c for c in cells if c["slot"] == 1 and c["dp"] == 0),
                  key=lambda c: c["col"])
    assert data and all(c["channel"] == 0 for c in data), [c["channel"] for c in data]
    # the two columns of each wide bit share the same bit number
    assert data[0]["bit"] == data[1]["bit"] == 3


def test_csv_writes_all_dataport_names():
    # Every DP gets a name in the CSV (default "DP{i}"), like the Visualizer.
    cfg = demo_config()
    cfg.dataports[1].name = "Spk"
    name_row = next(l for l in cfg.to_csv().splitlines() if l.startswith("Name"))
    assert name_row == "Name,DP0,Spk,DP2,DP3,DP4,DP5,DP6,DP7,DP8,DP9,DP10,DP11"


def test_export_decoder_config_as_visualizer_csv():
    # The analyzer "export bus grid as visualizer CSV" path: the decoder's snooped
    # config (sparse, may exceed 12 slots) packs its ENABLED ports into the 12
    # visualizer columns, preserving device + DP number, and writes a CSV the core
    # can place. (Mirrors swi3score.config_dataports() output.)
    decoded = {
        "num_columns": 15, "skipping_denominator": 16, "phy3_enabled": False,
        "row_rate_khz": 781.25,
        "dataports": (
            [{"enabled": False} for _ in range(12)] +      # 12 reserved, disabled
            [{"enabled": True, "device_number": 4, "dp_number": 0, "enable_ch": 0x1,
              "sample_size": 0, "sample_grouping": 1, "horizontal_start": 1, "horizontal_count": 1},
             {"enabled": True, "device_number": 6, "dp_number": 0, "enable_ch": 0x1,
              "sample_size": 0, "sample_grouping": 3, "horizontal_start": 2, "horizontal_count": 3}]
        ),
    }
    cfg, n = BusConfig.from_decoder_config(decoded)
    assert n == 2
    assert cfg.row_rate_khz == 781.25
    # Enabled ports packed into the first columns, device/DP-number preserved.
    assert (cfg.dataports[0].device_number, cfg.dataports[0].number(0)) == (4, 0)
    assert (cfg.dataports[1].device_number, cfg.dataports[1].number(1)) == (6, 0)
    # Writes a visualizer CSV the C++ cascade can place.
    p = os.path.join(tempfile.mkdtemp(), "exported.csv")
    cfg.to_csv_file(p)
    assert "RowRate,781.25" in open(p).read()
    assert len(swi3score.grid_from_csv(p, 32)) > 0


def test_cds_settings_dialog_owns_every_cds_field():
    """The interface column's four CDS rows are one "CDS Settings" button now, and this
    pins what that collapse must not lose.

    WHAT IT GUARDS:

    1. `cds_guard_enabled` / `cds_tail_enabled` are READ-ONLY properties derived from the
       per-source lists. `_on_iface_edit` loops over every interface row and setattr's it;
       the CDS rows are gone but `_populate` still loops the same dict, so a skip is still
       needed — for the button, which answers to no attribute at all (`_IFACE_NON_ATTR`).
    2. NOTHING COMMITS UNTIL OK, including a change made two dialogs deep. The per-source
       dialogs are opened BY the settings dialog and hand their result back to it, so a
       cancelled outer dialog must discard an accepted inner one.
    3. The row's tooltip carries the at-a-glance read the four rows used to give.

    exec() blocks headless, so accepted dialogs are simulated by driving the widgets and
    the accept path directly.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton

    from swi3s_studio.model.bus_config import (
        CDS_DRIVE_NORMAL,
        CDS_DRIVE_SPECIAL,
        CDS_GUARD_G1,
    )
    from swi3s_studio.ui.authoring.authoring_panel import AuthoringPanel
    from swi3s_studio.ui.authoring.dialogs import CdsSettingsDialog

    QApplication.instance() or QApplication([])
    panel = AuthoringPanel()
    _lab, btn = panel._iface["cds_settings"]
    assert isinstance(btn, QPushButton)
    # The four old rows are gone — a stale one would KeyError in _populate.
    for gone in ("cds_bit_width", "cds_guard_enabled", "cds_tail_enabled",
                 "enforce_cds_handover"):
        assert gone not in panel._iface, gone
    # Every source Normal, not the register's reset of 0 (Special).
    assert panel._cfg.cds_drive_type == [CDS_DRIVE_NORMAL] * 13

    dlg = CdsSettingsDialog(panel, bit_width=panel._cfg.cds_bit_width,
                            drive_type=list(panel._cfg.cds_drive_type),
                            end_drive_early=list(panel._cfg.cds_end_drive_early),
                            enforce_handover=panel._cfg.enforce_cds_handover,
                            guard=list(panel._cfg.cds_guard),
                            tail=list(panel._cfg.cds_tail))
    dlg._pick_bit(3)
    dlg.drive_type[2] = CDS_DRIVE_SPECIAL   # as an accepted CdsDriveTypeDialog would
    dlg.guard[1] = CDS_GUARD_G1             # ...and an accepted CdsGuardDialog
    dlg._refresh()
    assert dlg._summaries["guard"].text() == "1 device G1"
    assert dlg._summaries["drive"].text() == "1 device Special"
    dlg.accept()
    panel._cfg.cds_bit_width = dlg.bit_width
    panel._cfg.cds_drive_type = list(dlg.drive_type)
    panel._cfg.cds_guard = list(dlg.guard)
    panel._update_cds_settings_row()

    tip = btn.toolTip()
    assert "Bit width 3" in tip and "1 device Special" in tip and "1 device G1" in tip, tip

    # (1) The interface-edit and repopulate paths leave all of it alone.
    panel._on_iface_edit()
    panel._populate()
    assert panel._cfg.cds_drive_type[2] == CDS_DRIVE_SPECIAL
    assert panel._cfg.cds_bit_width == 3
    assert panel._cfg.cds_guard[1] == CDS_GUARD_G1
    assert panel._cfg.cds_guard_enabled is True     # still derived, still read-only

    # (2) A CANCELLED outer dialog discards an inner accepted change.
    before_guard = list(panel._cfg.cds_guard)
    before_drive = list(panel._cfg.cds_drive_type)
    dlg2 = CdsSettingsDialog(panel, bit_width=panel._cfg.cds_bit_width,
                             drive_type=list(panel._cfg.cds_drive_type),
                             end_drive_early=list(panel._cfg.cds_end_drive_early),
                             enforce_handover=panel._cfg.enforce_cds_handover,
                             guard=list(panel._cfg.cds_guard),
                             tail=list(panel._cfg.cds_tail))
    dlg2.guard[4] = CDS_GUARD_G1
    dlg2.drive_type[5] = CDS_DRIVE_SPECIAL
    dlg2.reject()
    assert panel._cfg.cds_guard == before_guard, "a cancelled dialog wrote through"
    assert panel._cfg.cds_drive_type == before_drive


def test_cds_dialogs_are_laid_out_within_their_own_bounds():
    """The layout defects that only a RENDER shows — pinned as geometry, since the first
    version of these dialogs passed every property assertion while being visibly broken.

    Each check is a real defect that shipped:

      * CLIPPED CONTROL. The settings dialog's handover checkbox carried its own text in
        the narrow value column, so "Enforce CDS Handover" was cut off at the window edge.
        Every child must fit inside the dialog.
      * A WINDOW THAT RESIZED ON SELECTION. The drive-type note used to be per-selection,
        and the dialog re-widened when the wording changed length, so picking a value made
        the window jump. Width is fixed and must not depend on the values shown.
      * COLUMN ALIGNMENT. Controls in the value column started at different x — the
        checkbox centred itself, and the two per-source buttons sized to their own text so
        the summaries beside them were ragged.
      * ROWS CUT IN HALF / OVERLAPPING SCROLLBAR. The per-source dialogs put 13 rows in a
        320 px scroll area, which sliced the last visible row and drew its scrollbar over
        the right-hand button column. There is no scroll area now: all 13 rows must be
        inside the dialog.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QLabel,
        QPushButton,
        QStyle,
        QStyleOptionButton,
    )

    from swi3s_studio.model.bus_config import CDS_DRIVE_NORMAL, CDS_DRIVE_SPECIAL
    from swi3s_studio.ui.authoring.dialogs import (
        CdsDriveTypeDialog,
        CdsGuardDialog,
        CdsSettingsDialog,
        CdsTailDialog,
        _ElidingLabel,
    )
    from swi3s_studio.ui.theme import authoring_stylesheet

    QApplication.instance() or QApplication([])

    def squeezed(dlg):
        """Visible children narrower than the width their own content needs.

        THIS IS THE CHECK THAT CATCHES ELIDED TEXT, and an overflow test does not. Given a
        fixed dialog width Qt does not let a control overhang the window — it SHRINKS the
        control and clips the text inside it. So the checkbox whose label read "Enforce CDS
        Handc…" had a geometry entirely within the dialog; what was wrong is that its width
        was below its sizeHint. Comparing against the dialog rectangle passes that defect,
        which was confirmed by re-injecting it.

        Word-wrapped labels are exempt: their sizeHint is the one-line width by design, and
        wrapping is what they are for.
        """
        dlg.show()
        QApplication.instance().processEvents()
        out = []
        for cls in (QLabel, QPushButton, QCheckBox):
            for w in dlg.findChildren(cls):
                if not w.isVisible():
                    continue
                if isinstance(w, QLabel) and (w.wordWrap() or isinstance(w, _ElidingLabel)):
                    # Word-wrapped and self-eliding labels are exempt BY DESIGN — their
                    # sizeHint is the one-line width and shrinking is what they do. The
                    # eliding ones are checked separately below, on their tooltip, so
                    # nothing is silently dropped.
                    continue
                need = w.sizeHint().width()
                if w.width() < need:
                    out.append((cls.__name__, w.text(), w.width(), need))
        dlg.close()
        return out

    def settings(**kw):
        base = dict(bit_width=0, drive_type=[CDS_DRIVE_NORMAL] * 13,
                    end_drive_early=[0] * 13, enforce_handover=True,
                    guard=[0] * 13, tail=[0] * 13)
        base.update(kw)
        return CdsSettingsDialog(None, **base)

    plain = settings()
    assert not squeezed(plain), f"clipped in the settings dialog: {squeezed(plain)}"

    # AND AT A WIDER FONT, which is the whole point of measuring rather than eyeballing:
    # every width here used to be a constant tuned on one platform's metrics, so the Windows
    # CI job clipped "Guard 0" by 4 px while macOS passed. The dialogs now size their button
    # groups and their label column from sizeHint, so a font change moves the layout instead
    # of breaking it. 13pt is comfortably past what any runner picks.
    app = QApplication.instance()
    base = QFont(app.font())
    try:
        wide = QFont(base); wide.setPointSize(13); app.setFont(wide)
        for dlg in (settings(), CdsGuardDialog(None, [0] * 13),
                    CdsTailDialog(None, [0] * 13), CdsDriveTypeDialog(None, [CDS_DRIVE_NORMAL] * 13)):
            assert not squeezed(dlg), f"{type(dlg).__name__} clips at 13pt: {squeezed(dlg)}"
    finally:
        app.setFont(base)

    # Width must not depend on the values — the note is static and the summaries live in a
    # stretched cell, so a long summary cannot widen the window.
    busy = settings(bit_width=7, drive_type=[CDS_DRIVE_SPECIAL] * 13,
                    guard=[2] * 13, tail=[3] * 13)
    assert busy.width() == plain.width(), "the dialog resized with its content"
    assert not squeezed(busy), f"clipped with full values: {squeezed(busy)}"

    # One value column: the bit-width strip, the checkbox and every per-source button all
    # start at the same x.
    #
    # THE CHECKBOX IS MEASURED AT ITS INDICATOR, not at its widget rectangle. The widget was
    # aligned and the drawn square was not: the panel sets
    # `QCheckBox::indicator { subcontrol-position: center }` for its own 64 px bool rows, this
    # dialog inherits it from its parent, and centring makes the square's position depend on
    # the widget's width — so under a different font it drifts off the column edge while every
    # widget-level assertion still passes. That is what the first version of this test missed.
    plain.setStyleSheet(authoring_stylesheet())    # the rule only exists via the panel
    plain.show()
    QApplication.instance().processEvents()
    xs = {plain._bit[0].mapTo(plain, plain._bit[0].rect().topLeft()).x()}
    cb = plain._handover
    opt = QStyleOptionButton()
    opt.initFrom(cb)
    ind = cb.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, cb)
    xs.add(cb.mapTo(plain, ind.topLeft()).x())
    for kind in ("drive", "ede", "guard", "tail"):
        lab = plain._summaries[kind]
        btn = lab.parent().findChild(QPushButton)
        xs.add(btn.mapTo(plain, btn.rect().topLeft()).x())
    assert len(xs) == 1, f"the value column is not aligned: {sorted(xs)}"
    # ...and the summaries line up with each other, which they did not when each button
    # sized to its own text.
    sxs = {plain._summaries[k].mapTo(plain, plain._summaries[k].rect().topLeft()).x()
           for k in ("drive", "ede", "guard", "tail")}
    assert len(sxs) == 1, f"summaries ragged: {sorted(sxs)}"
    plain.close()

    # A summary long enough to need eliding must still be RECOVERABLE. Summary length is
    # unbounded (a guard config can name three polarities across twelve devices), so it
    # elides rather than widening the window — but the whole string has to be in the
    # tooltip, or the shortening is just a nicer-looking truncation. This case was found by
    # this test, not by looking: "Manager tail 3; 12 devices tail 3" overran by 2 px.
    long = settings(tail=[3] * 13)
    long.show()
    QApplication.instance().processEvents()
    lab = long._summaries["tail"]
    assert isinstance(lab, _ElidingLabel)
    assert lab.full_text() == "Manager tail 3; 12 devices tail 3"
    assert lab.toolTip() == lab.full_text(), "the elided text is not recoverable"
    long.close()

    # All 13 source rows visible inside every per-source dialog, none half-drawn.
    for cls, values in ((CdsGuardDialog, [0] * 13), (CdsTailDialog, [0] * 13),
                        (CdsDriveTypeDialog, [CDS_DRIVE_NORMAL] * 13)):
        dlg = cls(None, values)
        assert len(dlg._rows) == 13
        assert not squeezed(dlg), f"{cls.__name__} clips a control: {squeezed(dlg)}"


def test_one_selected_colour_across_every_selector_including_off():
    """Green marks the CHOSEN option in a group — including when that option means "off".

    A previous revision gave a selected "Off"/"0" its own blue-grey, reasoning that green
    reads as on/active and that thirteen green "Off" buttons look like everything is enabled.
    That broke a convention rather than clarifying one, and broke it INCONSISTENTLY: the
    per-source CDS guard dialog went blue-grey while `GuardSelectorDialog` — the per-DP guard,
    one click away, offering the identical Off / Guard 0 / Guard 1 — stayed green.

    So this pins the convention itself, across the per-source dialogs AND the older per-DP one
    that established it, because the failure mode was two dialogs disagreeing rather than one
    dialog being wrong. There is exactly ONE selected style; the label carries the value and
    the colour carries the selection. A bus-wide "nothing is set" reading belongs in the
    summary TEXT, which `cds_summary` provides and which this asserts alongside.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.model.bus_config import (
        CDS_DRIVE_NORMAL,
        CDS_DRIVE_SPECIAL,
        CDS_EDE_FULL_UI,
        CDS_EDE_HALF_EARLY,
    )
    from swi3s_studio.ui.authoring import dialogs as dlg_mod
    from swi3s_studio.ui.authoring.dialogs import (
        _SEL_CSS,
        _UNSEL_CSS,
        CdsDriveTypeDialog,
        CdsEndDriveEarlyDialog,
        CdsGuardDialog,
        CdsTailDialog,
        GuardSelectorDialog,
        cds_summary,
    )

    QApplication.instance() or QApplication([])

    # Exactly ONE selected colour and one selected style exist. A second constant is how the
    # divergence started, so the count is the guard — `_SELECTED`/`_UNSELECTED` are the two
    # base colours and `_SEL_CSS`/`_UNSEL_CSS` their stylesheets; anything else is a third.
    sel_names = sorted(n for n in vars(dlg_mod) if "SEL" in n)
    assert sel_names == ["_SELECTED", "_SEL_CSS", "_UNSELECTED", "_UNSEL_CSS"], sel_names

    # The per-DP guard dialog, which set the convention: a chosen "Off" is green.
    per_dp = GuardSelectorDialog(None, False, 0, 0)
    assert _SEL_CSS in per_dp._off.styleSheet(), "the per-DP guard changed its Off colour"

    # ...and the per-SOURCE dialogs agree, on every off-ish value.
    guard = CdsGuardDialog(None, [0] * 13)
    assert guard._rows[0][0].styleSheet() == _SEL_CSS, "per-source Off differs from per-DP Off"
    assert guard._rows[0][1].styleSheet() == _UNSEL_CSS
    tail = CdsTailDialog(None, [0] * 13)
    assert tail._rows[0][0].styleSheet() == _SEL_CSS
    ede = CdsEndDriveEarlyDialog(None, [CDS_EDE_FULL_UI] * 13)
    assert ede._rows[0][CDS_EDE_FULL_UI].styleSheet() == _SEL_CSS
    drive = CdsDriveTypeDialog(None, [CDS_DRIVE_NORMAL] * 13)
    assert drive._rows[0][CDS_DRIVE_NORMAL].styleSheet() == _SEL_CSS

    # Selecting a non-default keeps the same colour — it marks selection, not activation.
    ede._set_all(CDS_EDE_HALF_EARLY)
    assert ede._rows[0][CDS_EDE_HALF_EARLY].styleSheet() == _SEL_CSS
    assert ede._rows[0][CDS_EDE_FULL_UI].styleSheet() == _UNSEL_CSS
    assert ede.values == [CDS_EDE_HALF_EARLY] * 13, "All sources did not set every row"
    drive._set_all(CDS_DRIVE_SPECIAL)
    assert drive._rows[0][CDS_DRIVE_SPECIAL].styleSheet() == _SEL_CSS

    # The "nothing is set" reading lives in the summary text, which is what the second
    # colour was being stretched to say.
    assert cds_summary([0] * 13, "guard") == "off"
    assert cds_summary([CDS_EDE_FULL_UI] * 13, "ede") == "All Full"
    assert cds_summary([CDS_DRIVE_NORMAL] * 13, "drive") == "all Normal"


def test_cds_guard_polarity_roundtrip():
    """Regression: to_csv() used to hardcode CDS_GuardPolarity_REG=False on export
    (Studio didn't model polarity), silently dropping Guard 1 on every save. Both
    cds_guard_enabled and cds_guard_polarity must now survive CSV and dict
    round-trips, with the polarity row written exactly once. cds_guard_enabled/polarity
    are now derived (read-only) from the per-source cds_guard list — drive them via the
    Manager's slot (index 0), which is what the legacy singular properties reflect."""
    from swi3s_studio.model.bus_config import CDS_GUARD_G1

    cfg = BusConfig()
    cfg.cds_guard[0] = CDS_GUARD_G1
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "c.csv"))
        text = open(path).read()
        back = BusConfig.from_csv(path)
    assert sum(1 for l in text.splitlines()
               if l.startswith("CDS_GuardPolarityPerSource")) == 1
    assert back.cds_guard_enabled is True
    assert back.cds_guard_polarity is True
    # dict round-trip (workspace persistence)
    back2 = BusConfig.from_dict(cfg.to_dict())
    assert back2.cds_guard_enabled is True
    assert back2.cds_guard_polarity is True


def test_wide_bit_interval_overflow_detected():
    """A wide-bit DP (BitWidth>0) whose data can't fit its interval must raise an
    interval-overflow warning. Regression: expected_bits omitted the wide-bit span,
    so once wide-bit repeats inflated the placed-bit count past the un-widened
    expected, a real overflow was silently missed."""
    from swi3s_studio.model import viz_engine

    def _build(bit_width, interval):
        cfg = BusConfig(num_columns=7, row_rate_khz=3072.0, rows_to_draw=8)
        dp = cfg.dataports[0]
        dp.enabled = True
        dp.enable_ch = 0b1            # one channel
        dp.sample_size = 7            # 8-bit sample
        dp.sample_grouping = 0
        dp.bit_width = bit_width      # 1 => each bit held for 2 UIs (wide)
        dp.interval = interval        # interval rows = interval + 1
        dp.horizontal_start = 0
        dp.horizontal_count = 6
        with tempfile.TemporaryDirectory() as d:
            bm, _i, _v = viz_engine.build_bus_model(cfg.to_csv_file(os.path.join(d, "o.csv")))
        return bm.warnings.interval_overflow_warnings

    # bit_width=1 over a 2-row interval (14 UIs): an 8-bit sample needs 16 UIs wide.
    # 14 placed bits already meet the OLD un-widened expected (8), so the overflow
    # was hidden; now expected is the widened 16 and the warning fires.
    overflow = _build(bit_width=1, interval=1)
    assert overflow == [("DP0", 16, 14)], overflow
    # The same geometry with narrow bits (BitWidth=0) genuinely fits — no warning
    # (guards against the widening introducing a false positive).
    assert _build(bit_width=0, interval=1) == []


def test_per_source_cds_handover_placement_and_clash():
    """Each CDS source hands the bus over right AFTER its own last-driven CDS column,
    not uniformly at the region end. A guard-only source (no tail) therefore hands
    over immediately after the guard, where a *different* source may still be driving
    its (longer) tail — that coexists (time-multiplex, no clash), while the source
    that drove the tail hands over at the region end, reconciling with a same-device
    data port there.

    Regression: a Device-0 guard's handover was dumped at the region-final column,
    spuriously clashing with a Manager data port that legitimately owns that column
    (its own tail + handover reconcile). See the four cases below."""
    import tempfile as _tf

    from swi3s_studio.model import viz_engine

    def _clash_cols(guard, tail, dp_dev, dp_col):
        cfg = BusConfig(num_columns=15, row_rate_khz=3072.0)
        cfg.cds_bit_width = 0                       # CDS col0, guard col1, tails col2..
        for i, g in guard.items():
            cfg.cds_guard[i] = g
        for i, t in tail.items():
            cfg.cds_tail[i] = t
        dp = cfg.dataports[0]
        dp.enabled = True
        dp.device_number = dp_dev
        dp.enable_ch = 0b1
        dp.sample_size = 0
        dp.horizontal_start = dp_col
        dp.horizontal_count = 0
        with _tf.TemporaryDirectory() as d:
            path = cfg.to_csv_file(os.path.join(d, "o.csv"))
            _cells, clashes, _i, _c, _r = viz_engine.render_payload(path)
        return sorted({col for (_row, col) in (clashes or {})})

    # THE reported case: Manager G1 + Device-0 G0 guards, Manager tail width 3, and a
    # MANAGER data port at the region-end column (col5). Same device as the Manager's
    # own tail+handover there => no clash. Device-0 (guard only) hands over at col2.
    assert _clash_cols({0: 2, 1: 1}, {0: 3}, dp_dev=-1, dp_col=5) == []
    # Manager guard-only (no tail) => Manager handover at col2; a DEVICE-0 data port
    # there is a different device => clash.
    assert _clash_cols({0: 2}, {}, dp_dev=0, dp_col=2) == [2]
    # Device-0 guard-only => Device-0 handover at col2; a DEVICE-0 data port there is
    # the same device => reconciles, no clash.
    assert _clash_cols({1: 1}, {}, dp_dev=0, dp_col=2) == []
    # A DEVICE-0 data port landing on the Manager's tail columns is a different-device
    # write => still a real clash (the CDS exemption is for turnarounds, not writes).
    assert _clash_cols({0: 2}, {0: 3}, dp_dev=0, dp_col=3) == [3, 4]


def test_cds_drive_type_reaches_both_engines_and_the_grid_label():
    """CDS_DriveType, end to end: the field, both CSV engines, and the cell label.

    PER SOURCE, not bus-wide. The register lives in each device's own CDS block, so this is
    a 13-entry list on the same index convention as the guard and the tail (0 = Manager,
    i = Device i-1) — one device may hold a passive 1 while another drives both levels.

    THE DEFAULT IS NORMAL FOR EVERY SOURCE, NOT THE REGISTER'S RESET. registers.json gives
    CDS 0x86 bit 7 a reset of 0 (Special), but a config that never mentions the field must
    keep reading the way it always has — otherwise every file written before the field
    existed, including all 89 example configs, would start drawing CDS_SP. The deviation is
    deliberate and is asserted here so it is not "corrected" later.

    TWO CSV ENGINES READ THIS FILE FORMAT and both must carry the field: BusConfig
    (Studio's authoring model) and swviz's CSVHandler (the placement engine's). A field
    added to one and not the other round-trips through a save and vanishes on load.

    THE LABEL IS THE POINT. Special is annotated ("CDS_SP") and Normal is not, because a
    passively-driven one is a decode hazard while an actively driven CDS is what every
    reader already assumes. The CDS bit is ONE cell and the sources may disagree, so a
    mixed config reads "CDS_SPx" — the same `x`-means-they-differ convention the guard
    labels already use.
    """
    from swi3s_studio.model.bus_config import (
        CDS_DRIVE_NORMAL,
        CDS_DRIVE_SPECIAL,
        CDS_NUM_SOURCES,
        cds_symbol,
    )
    from swi3s_studio.swviz.io.csv_handler import CSVHandler
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig

    assert BusConfig().cds_drive_type == [CDS_DRIVE_NORMAL] * CDS_NUM_SOURCES
    assert Interface().CDS_DriveType_PerSource == [CDS_DRIVE_NORMAL] * CDS_NUM_SOURCES

    # Drive type alone (no end-drive-early argument) contributes the SP flag on line 2; the
    # full two-field vocabulary is pinned in
    # test_the_cds_cell_shows_both_flags_on_a_second_line_and_widens_only_itself.
    assert cds_symbol([CDS_DRIVE_NORMAL] * 13) == "CDS"
    assert cds_symbol([CDS_DRIVE_SPECIAL] * 13) == "CDS\nSP"
    assert cds_symbol([CDS_DRIVE_SPECIAL] + [CDS_DRIVE_NORMAL] * 12) == "CDS\nSPx"
    assert cds_symbol(CDS_DRIVE_SPECIAL) == "CDS\nSP", "a bare int must still work"

    cfg = demo_config()
    cfg.cds_drive_type = [CDS_DRIVE_SPECIAL] * CDS_NUM_SOURCES
    text = cfg.to_csv()
    rows = [l for l in text.splitlines() if l.startswith("CDS_DriveTypePerSource")]
    assert len(rows) == 1, "written exactly once"
    assert len(rows[0].split(",")) == CDS_NUM_SOURCES + 1, f"not 13-wide: {rows[0]}"

    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "sp.csv"))
        assert BusConfig.from_csv(path).cds_drive_type == [CDS_DRIVE_SPECIAL] * 13
        iface, viz = Interface(), VizConfig()
        result = CSVHandler.load_csv(path, iface, viz)
        assert result.success and not result.unrecognized_fields
        assert iface.CDS_DriveType_PerSource == [CDS_DRIVE_SPECIAL] * 13
        # The scalar register is the MANAGER's, derived — not an any()/max(), because
        # "some source is Special" is not a value one register bit can hold.
        assert iface.CDS_DriveType_REG == CDS_DRIVE_SPECIAL

        # A LEGACY FILE — no such row — loads clean as all-Normal on both engines.
        legacy = os.path.join(d, "legacy.csv")
        with open(legacy, "w") as f:
            f.write("\n".join(l for l in text.splitlines()
                              if not l.startswith("CDS_DriveTypePerSource")))
        assert BusConfig.from_csv(legacy).cds_drive_type == [CDS_DRIVE_NORMAL] * 13
        iface2, viz2 = Interface(), VizConfig()
        result2 = CSVHandler.load_csv(legacy, iface2, viz2)
        assert result2.success, result2.error_message
        assert iface2.CDS_DriveType_PerSource == [CDS_DRIVE_NORMAL] * 13

        # A file carrying the brief SCALAR spelling broadcasts to every source rather
        # than landing in unrecognized_fields.
        scalar = os.path.join(d, "scalar.csv")
        with open(scalar, "w") as f:
            f.write(open(legacy).read() + "\nCDS_DriveType,0\n")
        assert BusConfig.from_csv(scalar).cds_drive_type == [CDS_DRIVE_SPECIAL] * 13
        iface3, viz3 = Interface(), VizConfig()
        result3 = CSVHandler.load_csv(scalar, iface3, viz3)
        assert result3.success and not result3.unrecognized_fields
        assert iface3.CDS_DriveType_PerSource == [CDS_DRIVE_SPECIAL] * 13

    # Dict (workspace) round-trip — a separate attr list from the CSV one, and the field
    # has to be in both. A workspace holding the old SCALAR must broadcast, not crash.
    assert BusConfig.from_dict(cfg.to_dict()).cds_drive_type == [CDS_DRIVE_SPECIAL] * 13
    assert BusConfig.from_dict({"cds_drive_type": 0}).cds_drive_type == \
        [CDS_DRIVE_SPECIAL] * 13


def test_cds_summary_cannot_report_an_all_special_bus_as_off():
    """The `any()` shortcut the guard and tail summaries share is WRONG for drive type.

    Guard and tail encode "not set" as 0, so an all-zero list means off. Drive type encodes
    SPECIAL as 0 — so the same test reports a bus where every source holds a passive one as
    "off", the exact opposite of the truth, and the summary that is supposed to warn you
    would reassure you instead. Drive type reports the exception against Normal.
    """
    from swi3s_studio.model.bus_config import CDS_DRIVE_NORMAL as N
    from swi3s_studio.model.bus_config import CDS_DRIVE_SPECIAL as S
    from swi3s_studio.ui.authoring.dialogs import cds_summary

    assert cds_summary([S] * 13, "drive") == "all Special"
    assert cds_summary([N] * 13, "drive") == "all Normal"
    assert cds_summary([S] + [N] * 12, "drive") == "Manager Special"
    assert cds_summary([N, S, S] + [N] * 10, "drive") == "2 devices Special"
    assert cds_summary([S, S] + [N] * 11, "drive") == "Manager + 1 device Special"
    # ...and the other two keep their own vocabulary.
    assert cds_summary([0] * 13, "guard") == "off"
    assert cds_summary([0] * 13, "tail") == "off"


def _all_texts(gv):
    """Every text item's string in the scene — the CDS label is two items now."""
    out = set()
    for item in gv._scene.items():
        f = getattr(item, "text", None)
        if f is not None:
            out.add(f() if callable(f) else f)
    return out


def test_special_drive_labels_the_cds_cells_and_defeats_the_render_cache():
    """The grid draws CDS_SP for Special and CDS for Normal — and REDRAWS on the toggle.

    The cache is the subtle half. `set_bus_model` skips a rebuild when its content key
    matches, and CDS_DriveType moves no cell: same bits, same clashes, same dimensions. So
    the symbol has to be IN the key, or flipping drive type would leave the old label on
    screen until some unrelated edit happened to invalidate the cache — a bug that would
    look like the setting doing nothing.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.model.bus_config import CDS_DRIVE_SPECIAL, cds_symbol
    from swi3s_studio.ui.grid_view import GridView

    QApplication.instance() or QApplication([])
    cfg = demo_config()
    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "g.csv"))
        cells, clashes, _issues, ncols, nrows = viz_engine.render_payload(path, 4)

    gv = GridView()

    def cds_texts():
        out = set()
        for item in gv._scene.items():
            t = getattr(item, "text", None)
            if t is None:
                continue
            s = t() if callable(t) else t
            if "CDS" in s:
                out.add(s)
        return out

    gv.set_bus_model(cells, clashes, ncols, nrows, cds_symbol="CDS")
    assert cds_texts() == {"CDS"}
    # Identical cells; only the symbol differs. A cache hit here would keep "CDS". The label
    # is two lines now and each is its own text item, so the flags line shows up separately.
    gv.set_bus_model(cells, clashes, ncols, nrows,
                     cds_symbol=cds_symbol(CDS_DRIVE_SPECIAL))
    assert cds_texts() == {"CDS"} and "SP" in _all_texts(gv), \
        "the render cache swallowed the drive-type change"
    gv.set_bus_model(cells, clashes, ncols, nrows, cds_symbol="CDS")
    assert "SP" not in _all_texts(gv), "and back again"


def test_cds_end_drive_early_is_per_source_through_both_engines():
    """CDS_EndDriveEarly (registers.json CDS 0x87 bit 4), the second per-device CDS field.

    0 = drive the CDS bit the full UI, 1 = stop half a UI early (mandatory on PHY3). Per
    source because the register is in each device's own CDS block, exactly like the drive
    type — so this asserts the same round-trips through both CSV engines.

    THE TWO NEIGHBOURS DEFAULT OPPOSITELY RELATIVE TO THEIR RESETS, and that is the trap
    worth pinning: CDS_DriveType defaults to 1 AGAINST its reset of 0 (a file with no row
    must keep reading as it always did), while CDS_EndDriveEarly defaults to 0 WITH its
    reset (full-UI drive is the ordinary behaviour). One looks like a bug next to the other,
    so both are asserted here together.

    NOT the timing calculator's `Man_ede`/`Per_ede`, which model EndDriveEarly for a DATA
    handover. Same idea, different register, different consumer — asserted nowhere, but said
    here because the names collide.
    """
    from swi3s_studio.model.bus_config import (
        CDS_DRIVE_NORMAL,
        CDS_EDE_FULL_UI,
        CDS_EDE_HALF_EARLY,
        CDS_NUM_SOURCES,
    )
    from swi3s_studio.swviz.io.csv_handler import CSVHandler
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig

    # The opposite-defaults pair, side by side.
    assert BusConfig().cds_end_drive_early == [CDS_EDE_FULL_UI] * CDS_NUM_SOURCES
    assert BusConfig().cds_drive_type == [CDS_DRIVE_NORMAL] * CDS_NUM_SOURCES
    assert Interface().CDS_EndDriveEarly_PerSource == [CDS_EDE_FULL_UI] * CDS_NUM_SOURCES

    cfg = demo_config()
    cfg.cds_end_drive_early = [CDS_EDE_HALF_EARLY] + [CDS_EDE_FULL_UI] * 12  # Manager only
    text = cfg.to_csv()
    rows = [l for l in text.splitlines() if l.startswith("CDS_EndDriveEarlyPerSource")]
    assert len(rows) == 1, "written exactly once"
    assert len(rows[0].split(",")) == CDS_NUM_SOURCES + 1, f"not 13-wide: {rows[0]}"

    with tempfile.TemporaryDirectory() as d:
        path = cfg.to_csv_file(os.path.join(d, "ede.csv"))
        back = BusConfig.from_csv(path)
        assert back.cds_end_drive_early == [CDS_EDE_HALF_EARLY] + [CDS_EDE_FULL_UI] * 12
        iface, viz = Interface(), VizConfig()
        result = CSVHandler.load_csv(path, iface, viz)
        assert result.success and not result.unrecognized_fields
        assert iface.CDS_EndDriveEarly_PerSource == \
            [CDS_EDE_HALF_EARLY] + [CDS_EDE_FULL_UI] * 12
        assert iface.CDS_EndDriveEarly_REG == CDS_EDE_HALF_EARLY   # the Manager's

        # A file predating the field loads clean at all-full-UI on both engines.
        legacy = os.path.join(d, "legacy.csv")
        with open(legacy, "w") as f:
            f.write("\n".join(l for l in text.splitlines()
                              if not l.startswith("CDS_EndDriveEarlyPerSource")))
        assert BusConfig.from_csv(legacy).cds_end_drive_early == [CDS_EDE_FULL_UI] * 13
        iface2, viz2 = Interface(), VizConfig()
        result2 = CSVHandler.load_csv(legacy, iface2, viz2)
        assert result2.success, result2.error_message
        assert iface2.CDS_EndDriveEarly_PerSource == [CDS_EDE_FULL_UI] * 13

    assert BusConfig.from_dict(cfg.to_dict()).cds_end_drive_early == \
        [CDS_EDE_HALF_EARLY] + [CDS_EDE_FULL_UI] * 12


def test_a_truncated_per_source_row_takes_the_fields_default_not_zero():
    """A short 13-wide row must fall back per FIELD, because 0 is not always "unset".

    Both engines used to read a missing cell through a plain int parse, which yields 0. On
    the tail that is right (no tail). On CDS_DriveType 0 means SPECIAL, so a row truncated
    by a hand edit or a partial write would silently relabel the whole bus as passively
    driven — the one case where "absent" and "zero" are different answers. The per-source
    tables carry each field's default for exactly this.
    """
    from swi3s_studio.model.bus_config import (
        CDS_DRIVE_NORMAL,
        CDS_DRIVE_SPECIAL,
        CDS_EDE_FULL_UI,
    )

    # Two cells given, eleven missing: the given ones parse, the rest take Normal.
    cfg = BusConfig.from_csv_text("CDS_DriveTypePerSource,0,0")
    assert cfg.cds_drive_type == [CDS_DRIVE_SPECIAL] * 2 + [CDS_DRIVE_NORMAL] * 11
    # A blank cell mid-row is a gap, not a zero.
    cfg2 = BusConfig.from_csv_text("CDS_DriveTypePerSource,0,,0" + ",1" * 10)
    assert cfg2.cds_drive_type[1] == CDS_DRIVE_NORMAL, cfg2.cds_drive_type
    # Where zero IS the default, the fallback is indistinguishable — and that is correct.
    assert BusConfig.from_csv_text("CDS_TailWidthPerSource,3").cds_tail == [3] + [0] * 12
    assert BusConfig.from_csv_text(
        "CDS_EndDriveEarlyPerSource,1").cds_end_drive_early == [1] + [CDS_EDE_FULL_UI] * 12


def test_manager_dataport_beats_device_number_in_either_row_order_in_both_engines():
    """`DeviceNumber_REG` and `ManagerDataport` are a register value and a role the register
    cannot hold — and the role must win regardless of which row is read first.

    WHY THERE ARE TWO ROWS AT ALL: DeviceNumber_REG mirrors a hardware register whose legal
    values are 0-11. "In the Manager" is not one of them, so the CSV writes device 0 plus a
    flag. They only look like rival encodings of one field; in fact the flag decides whether
    the number applies at all.

    THE BUG THIS PINS: BusConfig.from_csv_text applied the flag the instant it read it, on
    the stated assumption that the flag row is "written after DeviceNumber_REG". True of files
    it writes itself, false of a hand-edited or third-party one — and in that case the later
    DeviceNumber row overwrote the Manager assignment, so a Manager port silently became a
    peripheral. swviz's loader was always order-independent, so THE SAME FILE decoded two
    different ways depending on which engine read it, which is the divergence CLAUDE.md warns
    about ("treat a bare device number from a CSV with suspicion").

    Asserted in BOTH orders and BOTH engines, because a fix to one engine that leaves the
    other alone recreates the divergence rather than fixing it.
    """
    from swi3s_studio.swviz.io.csv_handler import CSVHandler
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig

    base = [l for l in demo_config().to_csv().splitlines()
            if not l.startswith(("DeviceNumber_REG,", "ManagerDataport,"))]
    dev_row = "DeviceNumber_REG," + ",".join(["7"] * 12)
    mgr_row = "ManagerDataport," + ",".join(["True"] * 12)

    with tempfile.TemporaryDirectory() as d:
        for name, rows in (("dev_first", [dev_row, mgr_row]),
                           ("mgr_first", [mgr_row, dev_row])):
            text = "\n".join(base + rows) + "\n"

            cfg = BusConfig.from_csv_text(text)
            assert cfg.dataports[0].device_number == -1, (
                f"{name}: BusConfig lost the Manager flag — device number won")

            path = os.path.join(d, f"{name}.csv")
            with open(path, "w") as f:
                f.write(text)
            iface, viz = Interface(), VizConfig()
            assert CSVHandler.load_csv(path, iface, viz).success
            assert iface.is_dp_in_manager(0), f"{name}: swviz lost the Manager flag"
            # The two engines must agree, which is the actual invariant.
            assert iface.get_dp_device(0) == cfg.dataports[0].device_number, name

    # And a device number still applies where the flag is FALSE — the fix must not have
    # turned every port into a Manager port.
    mixed = "ManagerDataport,True," + ",".join(["False"] * 11)
    cfg = BusConfig.from_csv_text("\n".join(base + [dev_row, mixed]) + "\n")
    assert cfg.dataports[0].device_number == -1
    assert cfg.dataports[1].device_number == 7, cfg.dataports[1].device_number


def _cds_write(dev, addr, val):
    return {"is_write": True, "device_mask": 1 << dev, "address": addr, "data": bytes([val])}


def test_cds_crosses_the_csv_register_boundary_in_both_directions():
    """The CDS reaches the Analyzer's registers, and comes back from them — both ways.

    THE GAP THIS CLOSES was total, not partial: the C++ core's CSV loader recognised no
    `CDS_*` field and `registersFromConfig` emitted only the DP block plus NumColumns /
    SkippingDenominator, so *Import Visualizer CSV* produced no CDS register state at all —
    not the per-source drive type or end-drive-early, and not the pre-existing bit width,
    guard or tail either. Nothing was wrong with the numbers; the whole block was absent.

    DIRECTION 1 (config -> registers): an authored CSV must emit CDS 0x1186 (drive type) and
    0x1187 (bit width / end-drive-early / guard enable+polarity / tail) for each device, with
    the value taken from that DEVICE's own per-source slot — the CDS is time-multiplexed and
    every source drives it under its own registers, so slot d+1 belongs to device d.

    DIRECTION 2 (registers -> config): replayed writes must decode back into the same
    per-source lists, so a decoded capture exports an authoring CSV with its CDS intact.

    THE MANAGER SLOT (index 0) IS UNRECOVERABLE and asserted to stay at its default. Only
    peripherals have an addressable register block, so nothing on the wire says how the
    Manager drives the CDS. Leaving it at the default is the honest answer — deriving it from
    a peripheral's value would invent a fact the capture does not contain.
    """
    import swi3score

    from swi3s_studio.model.bus_config import (
        CDS_DRIVE_NORMAL,
        CDS_DRIVE_SPECIAL,
        CDS_EDE_FULL_UI,
        CDS_EDE_HALF_EARLY,
        CDS_GUARD_G1,
    )

    # ---- direction 1: authored CSV -> register writes ----
    cfg = demo_config()                       # DP0 lives on device 0
    cfg.cds_bit_width = 3
    cfg.cds_drive_type[1] = CDS_DRIVE_SPECIAL          # device 0
    cfg.cds_end_drive_early[1] = CDS_EDE_HALF_EARLY
    cfg.cds_guard[1] = CDS_GUARD_G1
    cfg.cds_tail[1] = 2
    with tempfile.TemporaryDirectory() as d:
        regs = swi3score.registers_from_csv(cfg.to_csv_file(os.path.join(d, "c.csv")))
    cds = {(dev, addr): val for dev, addr, val in regs if 0x1100 <= addr < 0x1200}
    assert cds, "no CDS register reached the Analyzer — the whole block is missing again"
    assert cds[(0, 0x1186)] == 0x00, "drive type Special is bit 7 = 0"
    # bit width 3 -> [7:5]=0x60, EDE 1 -> 0x10, guard enable -> 0x08, G1 polarity -> 0x04,
    # tail 2 -> 0x02.
    assert cds[(0, 0x1187)] == 0x7E, hex(cds[(0, 0x1187)])

    # Normal drive type sets bit 7, so the two encodings cannot be confused.
    cfg.cds_drive_type[1] = CDS_DRIVE_NORMAL
    with tempfile.TemporaryDirectory() as d:
        regs = swi3score.registers_from_csv(cfg.to_csv_file(os.path.join(d, "n.csv")))
    assert dict(((dv, a), v) for dv, a, v in regs)[(0, 0x1186)] == 0x80

    # ---- direction 2: replayed register writes -> config ----
    cmds = [
        _cds_write(0, 0x1081, 15),            # NumColumns, so BuildConfig has geometry
        _cds_write(0, 0x2081, 0x80 | 1),      # DP0 EnableCh0 -> an enabled port exists
        _cds_write(0, 0x1186, 0x00),          # Special
        _cds_write(0, 0x1187, 0x7E),          # bw 3, EDE early, G1, tail 2
        {"is_commit": True, "commit_confirmed": True, "device_mask": 0xFFF,
         "group_mask": 0xF},
    ]
    got = swi3score.config_from_commands(cmds)
    assert got["cds_bit_width"] == 3
    assert got["cds_drive_type"][1] == CDS_DRIVE_SPECIAL
    assert got["cds_end_drive_early"][1] == CDS_EDE_HALF_EARLY
    assert got["cds_guard"][1] == CDS_GUARD_G1
    assert got["cds_tail"][1] == 2
    # The Manager slot is untouched by anything on the wire.
    assert got["cds_drive_type"][0] == CDS_DRIVE_NORMAL
    assert got["cds_end_drive_early"][0] == CDS_EDE_FULL_UI
    assert got["cds_guard"][0] == 0 and got["cds_tail"][0] == 0
    # A device that wrote nothing keeps the config default, not a half-read register.
    assert got["cds_drive_type"][5] == CDS_DRIVE_NORMAL

    # ...and it survives all the way out to an authoring CSV.
    exported, _n = BusConfig.from_decoder_config(got)
    assert exported.cds_drive_type[1] == CDS_DRIVE_SPECIAL
    assert exported.cds_end_drive_early[1] == CDS_EDE_HALF_EARLY
    text = exported.to_csv()
    assert "CDS_DriveTypePerSource,1,0," in text, [
        l for l in text.splitlines() if "DriveType" in l]
    assert "CDS_EndDriveEarlyPerSource,0,1," in text


def test_the_cds_csv_round_trips_through_the_cpp_loader_too():
    """A third CSV reader now parses the CDS rows, and must agree with the other two.

    `SwI3sConfig::LoadCsv` recognised no CDS field, so its view of a config silently differed
    from BusConfig's and swviz's. Two engines disagreeing on one file is the defect this
    repo's CLAUDE.md already warns about; three is worse. Checked through the register
    emission, which is the only window onto the C++ struct from Python.

    The legacy scalar rows are covered too: a pre-per-source file broadcasts its single
    guard/tail to every source, which is what both Python loaders do.
    """
    import swi3score

    from swi3s_studio.model.bus_config import CDS_GUARD_G0

    cfg = demo_config()
    cfg.cds_tail = [1] * 13
    cfg.cds_guard = [CDS_GUARD_G0] * 13
    with tempfile.TemporaryDirectory() as d:
        text = cfg.to_csv()
        regs = swi3score.registers_from_csv(cfg.to_csv_file(os.path.join(d, "a.csv")))
        by = {(dev, a): v for dev, a, v in regs}
        # guard enabled (bit3) + G0 (bit2 clear) + tail 1
        assert by[(0, 0x1187)] & 0x0F == 0x09, hex(by[(0, 0x1187)])

        # A LEGACY file: no per-source rows, one scalar guard/tail for the whole bus.
        legacy = [l for l in text.splitlines() if "PerSource" not in l]
        legacy += ["CDS_GuardEnabled_REG,True", "CDS_GuardPolarity_REG,True",
                   "CDS_TailWidth_REG,3"]
        lp = os.path.join(d, "legacy.csv")
        with open(lp, "w") as f:
            f.write("\n".join(legacy) + "\n")
        lregs = {(dev, a): v for dev, a, v in swi3score.registers_from_csv(lp)}
        # broadcast to device 0: guard enable + G1 polarity + tail 3
        assert lregs[(0, 0x1187)] & 0x0F == 0x0F, hex(lregs[(0, 0x1187)])
        # ...and the Python loader agrees on the same file.
        assert BusConfig.from_csv(lp).cds_tail == [3] * 13


def test_the_cds_cell_shows_both_flags_on_a_second_line_and_widens_only_itself():
    """`CDS_SPx` overflowed a 44 px cell, and end-drive-early had no representation at all.

    TWO PROBLEMS, ONE CELL. The single-line label ran out of room the moment the sources
    disagreed about drive type (58 px of text in a 44 px column), and a per-device setting with
    real decode consequences — end-drive-early — was invisible on the grid entirely. So the
    cell reads "CDS" over a flags line, and the flags carry both fields with the same
    `x`-means-they-disagree suffix the guard labels use.

    ONLY THE CDS COLUMN GROWS. Widening every column would cost horizontal room on a view whose
    job is fitting up to 32 columns on screen, so the extra width is confined to the CDS run —
    and is taken from the RENDERED text width, so it is right for whatever the flags say rather
    than a guessed constant. A config with nothing unusual to report keeps a plain "CDS" and an
    unchanged, uniform grid, which is every one of the 89 example configs.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.model.bus_config import (
        CDS_DRIVE_NORMAL,
        CDS_DRIVE_SPECIAL,
        CDS_EDE_FULL_UI,
        CDS_EDE_HALF_EARLY,
        cds_symbol,
    )
    from swi3s_studio.ui.grid_view import _CW, GridView

    N, SP = CDS_DRIVE_NORMAL, CDS_DRIVE_SPECIAL
    F, E = CDS_EDE_FULL_UI, CDS_EDE_HALF_EARLY

    # The label vocabulary: nothing to say -> one line; each field contributes a flag; a
    # mixed bus gets the x suffix. EDE is what had no representation before.
    assert cds_symbol([N] * 13, [F] * 13) == "CDS"
    assert cds_symbol([SP] * 13, [F] * 13) == "CDS\nSP"
    assert cds_symbol([SP] + [N] * 12, [F] * 13) == "CDS\nSPx"
    assert cds_symbol([N] * 13, [E] * 13) == "CDS\nEDE"
    assert cds_symbol([N] * 13, [E] + [F] * 12) == "CDS\nEDEx"
    assert cds_symbol([SP] * 13, [E] * 13) == "CDS\nSP EDE"
    assert cds_symbol([SP] + [N] * 12, [E] + [F] * 12) == "CDS\nSPx EDEx"

    QApplication.instance() or QApplication([])

    def render(drive, ede, bit_width=0):
        cfg = demo_config()
        cfg.cds_drive_type, cfg.cds_end_drive_early = list(drive), list(ede)
        cfg.cds_bit_width = bit_width
        with tempfile.TemporaryDirectory() as d:
            path = cfg.to_csv_file(os.path.join(d, "c.csv"))
            cells, clashes, _i, ncols, nrows = viz_engine.render_payload(path, 3)
        gv = GridView()
        gv.set_bus_model(cells, clashes, ncols, nrows,
                         cds_symbol=cds_symbol(cfg.cds_drive_type, cfg.cds_end_drive_early))
        return gv

    # Nothing unusual: a uniform grid, exactly as before this feature existed.
    plain = render([N] * 13, [F] * 13)
    assert plain._col_extra == {}, plain._col_extra
    assert plain._colw(0) == _CW and plain._cx(1) - plain._cx(0) == _CW

    # Flags present: column 0 grows, and NOTHING else does.
    flagged = render([SP] + [N] * 12, [E] + [F] * 12)
    assert set(flagged._col_extra) == {0}, flagged._col_extra
    assert flagged._colw(0) > _CW
    assert all(flagged._colw(c) == _CW for c in range(1, 12))
    # Every later column shifts by exactly that one column's extra — no compounding.
    extra = flagged._col_extra[0]
    assert abs((flagged._cx(1) - plain._cx(1)) - extra) < 0.01
    assert abs((flagged._cx(9) - plain._cx(9)) - extra) < 0.01
    # ...and the widened column is wide enough for the wider LINE, not the whole string.
    from PySide6.QtGui import QFontMetricsF
    need = max(QFontMetricsF(flagged._f_label).horizontalAdvance(s)
               for s in ("CDS", "SPx EDEx"))
    assert flagged._colw(0) >= need

    # A merged (wide-bit) CDS already has the room across its span, so it grows by nothing.
    wide = render([SP] + [N] * 12, [E] + [F] * 12, bit_width=2)
    assert wide._col_extra == {}, wide._col_extra
    assert wide._cds_label(3) == "CDS x3\nSPx EDEx", wide._cds_label(3)

    # The run count goes on the FIRST line, beside the name — appending it to the flags
    # would read as a third flag ("SPx x2").
    assert flagged._cds_label(1) == "CDS\nSPx EDEx"
    assert flagged._cds_label(2) == "CDS x2\nSPx EDEx"
    assert plain._cds_label(2) == "CDS x2"
