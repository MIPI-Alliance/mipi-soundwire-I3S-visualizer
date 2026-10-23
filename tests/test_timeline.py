"""Timeline ribbon tick precedence.

A long capture is ~98% Pings; with millions of samples per timeline pixel, many
commands collide on one x. The ribbon paints ticks in ASCENDING importance so a
significant mark (Write/Read, commit, CRC error) WINS the pixel over a colliding
Ping. This checks the paint-order rank (tick_rank) that drives that sort,
without relying on fragile offscreen pixel rendering.

Run: python3 -m pytest tests/test_timeline.py
"""
from swi3s_studio.ui import timeline as tl
from swi3s_studio.ui.timeline import _kind_color, tick_rank

_PING = {"command": "Ping", "has_manager_packet": True, "crc_valid": True}
_WRITE = {"command": "WriteA32", "has_manager_packet": True, "crc_valid": True}
_READ = {"command": "ReadA32", "has_manager_packet": True, "crc_valid": True}
_COMMIT = {"command": "SSCR", "is_commit": True, "commit_confirmed": True,
           "has_manager_packet": True, "crc_valid": True, "has_sync_point": True}
_ERROR = {"command": "WriteA32", "has_manager_packet": True, "crc_valid": False}


def test_rank_order():
    """Ping ranks below every significant mark; error/commit rank highest."""
    assert tick_rank(_PING) < tick_rank(_WRITE)
    assert tick_rank(_PING) < tick_rank(_READ)
    assert tick_rank(_WRITE) < tick_rank(_COMMIT)
    assert tick_rank(_COMMIT) <= tick_rank(_ERROR)
    assert tick_rank(_ERROR) == max(tick_rank(c) for c in
                                    (_PING, _WRITE, _READ, _COMMIT, _ERROR))


def test_significant_marks_sort_after_pings():
    """Sorting a Ping-heavy command list by tick_rank puts the Write LAST among a
    pile of pings sharing its slot — so it is painted last and wins the pixel."""
    cmds = [_PING] * 20 + [_WRITE] + [_PING] * 20
    ordered = sorted(cmds, key=tick_rank)
    assert ordered[-1] is _WRITE, "Write must paint after all colliding Pings"
    # And an error beats even a write when both collide.
    cmds2 = [_PING, _WRITE, _ERROR, _PING]
    assert sorted(cmds2, key=tick_rank)[-1] is _ERROR


def test_colors_distinct():
    """The colours the ranks map to are the expected, distinct tick colours. Read the
    constants off the module (not import-bound copies): retheme() rebinds these globals
    to new QColor objects, so a prior test that applied a theme would otherwise leave
    the imported names stale and the identity check spuriously failing."""
    assert _kind_color(_PING) is tl._C_PING
    assert _kind_color(_WRITE) is tl._C_OTHER
    assert _kind_color(_ERROR) is tl._C_ERROR


def test_legend_tick_heights_match_what_the_painter_draws():
    """The legend describes ticks by HEIGHT ("tall red", "short blue"), but the painter
    gates height on COLOUR: `tall = color in (_C_ERROR, _C_SSP)`. This ties the two: a
    tall-colour tick's legend says "tall", and no other tick's does.
    """
    tall_tokens = {"SEM_ERROR", "SEM_SSP"}          # mirrors the `tall = ...` gate
    for label, token, desc in tl._MARK_LEGEND_SPEC:
        if "tick" not in desc:
            continue                                 # lines/bands/diamonds aren't ticks
        if token in tall_tokens:
            assert "tall" in desc, (
                f"{label!r} is drawn TALL (colour {token}) but its legend says {desc!r} — "
                "a reader will expect a short tick")
        else:
            assert "tall" not in desc, (
                f"{label!r} is drawn SHORT (colour {token}) but its legend claims 'tall'")


def test_sspa_and_commit_ssp_are_admittedly_indistinguishable():
    """They share colour AND rank, so the ribbon cannot separate them; the legend must not
    pretend otherwise. If SSPA is ever given its own colour/shape, update the legend text
    (and delete this test)."""
    sspa = {"command": "SSPA", "has_manager_packet": True, "crc_valid": True,
            "has_sync_point": True}
    assert tl._kind_label(sspa) == "SSP Announce"
    assert _kind_color(sspa) == _kind_color(_COMMIT)
    assert tl._TICK_RANK["SSP Announce"] == tl._TICK_RANK["Commit + SSP"]
    desc = next(d for lbl, _t, d in tl._MARK_LEGEND_SPEC if lbl == "SSP Announce")
    assert "Commit + SSP" in desc and "hover" in desc, (
        "the SSP Announce legend must say it looks like Commit + SSP and point at hover "
        f"as the way to tell them apart; got {desc!r}")


def test_a_legend_entry_names_no_shade_its_colour_does_not_keep_in_both_palettes():
    """The legend text is the same in every palette, and only the swatch follows it. An
    entry whose colour flips between near-white and near-black (the cursor) must not name
    a shade, or one palette shows a swatch that contradicts its text."""
    from PySide6.QtGui import QColor

    from swi3s_studio.ui import theme
    shades = ("white", "black", "grey", "gray")
    flipped = 0
    for label, token, desc in tl._MARK_LEGEND_SPEC:
        dark = QColor(theme._PALETTES["dark"][token]).lightnessF()
        light = QColor(theme._PALETTES["light"][token]).lightnessF()
        if abs(dark - light) < 0.5:
            continue
        flipped += 1
        assert not any(w in desc.lower() for w in shades), (
            f"{label!r} is {theme._PALETTES['dark'][token]} in dark and "
            f"{theme._PALETTES['light'][token]} in light, but its legend says {desc!r}")
    assert flipped, "the cursor's colour flips between the palettes"
