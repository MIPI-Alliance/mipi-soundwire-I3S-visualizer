"""Workspaces: File ▸ Save Workspace and Open Workspace, and the apply of a workspace's
Links, cursors, cursor, mode, Bus Visualizer and Timing settings and view onto the window.

`WorkspaceController` writes the window's state as a `Workspace`, decodes a workspace's
Links through the window's load queue, asks about a capture it cannot find, and applies
the rest once every Link has settled. Its own state is the name of the workspace being
applied, which its import warnings are filed under; everything else is the window's.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .. import import_notes
from ..model.bookmarks import BookmarkSet
from ..model.bus_config import BusConfig
from ..session import Session
from ..workspace import (
    WORKSPACE_SUFFIX,
    LinkSpec,
    Workspace,
    link_lead,
    session_from_source,
)
from . import line_style, timers, view_state
from .command_table import COLUMN_NAMES
from .mode_controller import ANALYSIS, MODES

if TYPE_CHECKING:
    from .main_window import MainWindow

def processing_from_json(rows) -> dict:
    """A workspace's [device, dp, {...}] stream settings as {(device, dp): StreamProcessing},
    skipping any row that does not read as one (a hand-edited or damaged workspace)."""
    from ..store.audio_store import StreamProcessing
    out = {}
    for row in rows or []:
        try:
            dev, dp, d = int(row[0]), int(row[1]), row[2]
        except (TypeError, ValueError, IndexError):
            continue
        proc = StreamProcessing.from_json(d)
        if proc is not None and not proc.is_default():
            out[(dev, dp)] = proc
    return out


def processing_rows_unread(rows) -> int:
    """How many of a workspace's [device, dp, {...}] Filter & Gain rows processing_from_json
    cannot read (a row whose settings read as no processing is not counted)."""
    from ..store.audio_store import StreamProcessing
    n = 0
    for row in rows or []:
        try:
            int(row[0]), int(row[1])
            ok = StreamProcessing.from_json(row[2]) is not None
        except (TypeError, ValueError, IndexError):
            ok = False
        n += not ok
    return n


def widths_by_name(widths, names, report=None) -> dict:
    """Saved Commands column widths as {column name: width}: from a dict by name, or from a
    positional list only when it has one entry per name. Entries that are not a
    view_state.valid_width are dropped; anything else gives {}. `report(text)` is given what
    was left out, as a clause that starts with "column width" and reads after "the saved
    Commands "."""
    if isinstance(widths, dict):
        pairs = list(widths.items())
    elif isinstance(widths, list) and len(widths) == len(names):
        pairs = list(zip(names, widths))
    else:
        if widths is not None and report is not None:
            report("column widths are not a mapping, or a list with one width per column")
        return {}
    out = {str(k): int(w) for k, w in pairs if view_state.valid_width(w)}
    dropped = [str(k) for k, w in pairs if not view_state.valid_width(w)]
    if dropped and report is not None:
        which = ", ".join(repr(k) for k in dropped)
        report((f"column widths of {which} are not widths" if len(dropped) > 1 else
                f"column width of {which} is not a width") + f" of 1..{view_state.MAX_PX} pixels")
    return out


class WorkspaceController:
    """The workspaces of `win`, a MainWindow. `source` is the file name of the workspace
    `apply` is applying, for its import warnings; None otherwise."""

    def __init__(self, win: "MainWindow") -> None:
        self._win = win
        self.source: Optional[str] = None

    def save(self) -> None:
        if self._win._session is None:
            QMessageBox.information(self._win, "No session", "Load a capture first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self._win, "Save workspace",
            os.path.join(self._win._last_capture_dir("analyzer"), f"workspace{WORKSPACE_SUFFIX}"),
            f"Workspace (*{WORKSPACE_SUFFIX})")
        if not path:
            return
        path = self._win._suffixed_save_path(path, WORKSPACE_SUFFIX, "Save workspace", (".json",))
        if path is None:
            return
        self._win._remember_capture_dir(path, "analyzer")
        self.keep_shown_views()                  # the panes on screen, into their Links
        ws = Workspace(
            links=[self.link_spec(link) for link in self._win._links],
            active_link=self._win._links.active_index,
            bookmarks=self._win._bookmarks_ctl.bookmarks.to_json(),
            cursor=self._win.cursor.sample,
            mode=self._win._mode_mgr.current() or ANALYSIS,
            authoring=self._win._authoring.config().to_dict(),
            timing=self._win._timing_page.to_dict(),
            view={**self._link_view_prefs(),
                  "tx_map": self._win._tx_map, "tx_persist": self._win._tx_persist,
                  "grid_rows": self._win._grid_rows, "tx_start_row": self._win._tx_start_row,
                  "show_clock": self._win._raw_view.show_clock,
                  **self._window_view_state()},
        )
        try:
            ws.save(path)
        except OSError as exc:
            QMessageBox.critical(self._win, "Save failed", str(exc))
            return
        self._win._status.setText(f"Workspace saved: {path}")

    def _link_view_prefs(self) -> dict:
        """The multi-Link layout, saved only when there is one: each pane group's Link,
        whether Commands shows All Links, and the Timeline dock's height."""
        if len(self._win._links) < 2:
            return {}
        return {"pane_links": dict(self._win._pane_link), "commands_all": bool(self._win._cmd_all),
                "bottom_all": bool(self._win._bottom_all),
                "timeline_height": int(self._win._timeline_dock.height())}

    def _apply_link_view_prefs(self, view: dict, active: int) -> None:
        if len(self._win._links) < 2:
            return
        saved_panes = view.get("pane_links")
        panes = dict(saved_panes) if isinstance(saved_panes, dict) else {}
        # A workspace with no "grid" entry shows the Bus Grid on the bottom group's Link.
        panes.setdefault("grid", panes.get("bottom", active))
        from .main_window import _PANE_GROUPS  # main_window imports this module
        for group in _PANE_GROUPS:
            try:
                i = int(panes.get(group, active))
            except (TypeError, ValueError, OverflowError):   # a hand-edited/corrupt workspace
                self.note(f"the {group} panes' saved Link {panes.get(group)!r} is not "
                          f"a number; they show the active Link")
                continue
            if 0 <= i < len(self._win._links) and i != self._win._pane_link[group]:
                self._win.set_group_link(group, i)
        if view.get("commands_all"):
            self._win.set_commands_scope_all(True)
        if view.get("bottom_all"):
            self._win.set_bottom_scope_all(True)
        self._win._activate_link(active)            # the saved active Link, not the last set
        self._win._update_link_switcher()
        if "timeline_height" in view:
            h = view_state.bounded_number(view["timeline_height"], 0, view_state.MAX_PX)
            if h is None:
                self.note(f"the saved timeline height {view['timeline_height']!r} is not "
                          f"a height of 0..{view_state.MAX_PX} pixels; it is left as it is")
            elif h > 0:
                self._win.resizeDocks([self._win._timeline_dock], [h], Qt.Vertical)

    @staticmethod
    def link_spec(link) -> LinkSpec:
        """What a workspace records to rebuild one Link."""
        sess = link.session
        return LinkSpec(
            source=sess.source, name=link.name, offset_ps=int(link.offset_ps),
            overlay=[[int(s), int(d), int(a), int(v)]
                     for (s, d, a), v in sess.register_overrides.items()],
            device_regmaps={str(d): pm.to_json_dict() for d, pm in sess.peripheral_maps.items()},
            device_names={str(d): n for d, n in sess.device_names.items()},
            device_hub_depths={str(d): v for d, v in sess.hub_depths.items() if v},
            device_scramblers=[[int(d), int(p), bool(on)]
                               for (d, p), on in sess.scrambler_overrides.items()],
            stream_colors=line_style.overrides_to_json(
                getattr(link.view, "stream_colors", None) or {}),
            stream_processing=[[int(d), int(p), proc.to_json()] for (d, p), proc in
                               sorted((getattr(link.view, "stream_processing", None)
                                       or {}).items())],
            even_timing=[[int(d), int(p)] for d, p in
                         sorted(getattr(link.view, "even_timing", None) or ())],
            view={"commands": {"filters": view_state.filters_to_json(link.view.filters),
                               "col_widths": dict(link.view.col_widths or {})},
                  "audio": link.view.audio_view, "capture": link.view.capture_view},
        )

    def open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self._win, "Open workspace", self._win._last_capture_dir("analyzer"),
            f"Workspace (*{WORKSPACE_SUFFIX} *.json)")
        if not path:
            return
        self._win._remember_capture_dir(path, "analyzer")
        name = import_notes.source_name(path)
        try:
            with import_notes.collect() as ws_notes:
                ws = Workspace.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self._win, "Open failed", str(exc))
            return
        skipped = self._locate_missing_captures(ws, path)
        if skipped is None:
            return
        order = [i for i in range(len(ws.links)) if i not in skipped]
        if not order:
            self._win._status.setText("Open workspace: every Link was skipped; nothing opened.")
            self._win.show_import_warnings(ws_notes, name)
            return
        self.load_links(ws, name, ws_notes, order)

    def load_links(self, ws: Workspace, name: str, ws_notes, order: list) -> None:
        """Decode the saved Links `order` names (indices into ws.links) through the load
        queue, then apply `ws` to them. `ws_notes` are the warnings reading the workspace
        gave, shown with the apply's under `name`."""
        first_i, rest = order[0], order[1:]
        # Apply saved hub depths AND what-if register overrides inside the worker-
        # thread factory (both change the decode), in a single off-GUI-thread decode per
        # Link. Doing it in apply_workspace (GUI thread) would re-decode again and freeze
        # the UI. The Links decode one after another through the load queue: the first
        # replaces the analysis, the rest are added (never dropped by the queue), and the
        # view state is applied once the LAST one has loaded.
        #
        # A Link skipped at the missing-capture prompt is left out of the load list: the
        # first Link not skipped is the open, the others are added. The Links after the
        # first are queued only once the first has LOADED: if it fails, nothing else runs.
        # Each Link that loads records which saved Link it is and its
        # Session, and the view state is applied once every Link has settled, loaded or
        # failed, to the Link that holds each Session then (LinkSet.index_of). A failure
        # in the middle, a Link added from Open Capture while these load, or a Link removed,
        # leaves each saved Link's names, offsets and bookmarks on its own capture.
        # (saved Link, Session) for each Link loaded; how many are still loading.
        state: Dict[str, Any] = {"loaded": [], "waiting": 0}

        def finish(w=ws):
            # The workspace's own warnings, its parse's and the apply's, in one window.
            where = {spec_i: self._win._links.index_of(sess)
                     for spec_i, sess in state["loaded"]}
            with import_notes.collect() as applied:
                self.apply(w, where={k: v for k, v in where.items() if v >= 0}, source=name)
            self._win.show_import_warnings(list(ws_notes) + applied, name)

        def settle(i: int, session) -> None:
            if session is not None:
                state["loaded"].append((i, session))
            state["waiting"] -= 1
            if state["waiting"] == 0:
                finish()

        def first_loaded(session, w=ws) -> None:
            state["loaded"].append((first_i, session))
            state["waiting"] = len(rest)
            if not rest:
                finish()
            for i in rest:
                self._win._load_async(
                    lambda spec=w.links[i], **kw: self.build_link_session(spec, **kw),
                    after=lambda s, i=i: settle(i, s), kind="add",
                    on_fail=lambda i=i: settle(i, None))

        # Through _after_open, as Open Capture's: a newer open asked for while the first
        # Link decodes must not get this workspace's other Links added on top of it.
        self._win._load_async(
            lambda spec=ws.links[first_i], **kw: self.build_link_session(spec, **kw),
            after=self._win._after_open(first_loaded, opening=True), kind="open",
            on_fail=lambda: self._win.show_import_warnings(ws_notes, name))

    def build_link_session(self, w: LinkSpec, **kw) -> Session:
        """Rebuild one saved Link's Session, decode inputs and all, on the load worker."""
        # **kw absorbs decoder_ready (see _factory_takes_decoder_ready). The load worker
        # runs this inside announcing_decoders, so the first decode and each re-decode a
        # setter below starts reach its cancel() and progress poll.
        sess = session_from_source(w.source, register_map=self._win._rmap)
        depths = ({int(d): int(v) for d, v in w.device_hub_depths.items()}
                  if w.device_hub_depths else None)
        scram = {}
        for row in (w.device_scramblers or []):
            try:                                  # [device, dp, on]
                d, p, on = int(row[0]), int(row[1]), bool(row[2])
            except (IndexError, TypeError, ValueError):
                continue
            scram[(d, p)] = on
        overs = {}
        for row in (w.overlay or []):
            try:                                  # [section, device, address, value]
                sect, d, a, v = int(row[0]), int(row[1]), int(row[2]), int(row[3])
            except (IndexError, TypeError, ValueError):
                continue
            overs[(sect, d, a)] = v & 0xFF
        # Stage every decode input (hub depths + scrambler overrides), then do a
        # SINGLE re-decode, so reopening doesn't fan out into several decodes: the
        # register overrides' setter when there are any, else a re-decode of its own.
        if depths:
            sess.hub_depths = sess._checked_hub_depths(depths)
        if scram:
            sess.scrambler_overrides = sess._checked_scrambler_overrides(scram)
        if overs:
            sess.set_register_overrides(overs)    # sets overrides + one re-decode
        elif depths or scram:
            sess._redecode()
        return sess

    def apply(self, ws: Workspace, loaded=None, where=None, source: str = "") -> None:
        """Apply view state (bookmarks, cursor, active Link, pane layout) and each Link's
        name, offset and device metadata onto the loaded Links.

        A saved setting that does not apply (a stream colour or Filter & Gain row that does
        not read, a register map's unreadable entries, an unknown mode, a pane's Link that
        is not open, a view value that is not a number) is reported through import_notes
        under `source`, the workspace file's name.

        `where` maps each loaded saved Link's index to the index of the Link it loaded as.
        Without it, `loaded` lists, in Link order, which of the workspace's Links each
        loaded Link is (default: all of them, in order). A saved Link that failed to load is
        simply absent, and everything that referred to it (its bookmarks, a pane group
        showing it) is dropped or falls back, instead of landing on its neighbour."""
        self.source = source
        try:
            self._apply(ws, loaded, where, source)
        finally:
            self.source = None

    def _apply(self, ws: Workspace, loaded, where, source: str) -> None:
        if where is None:
            loaded = list(range(len(ws.links))) if loaded is None else list(loaded)
            where = {spec_i: link_i for link_i, spec_i in enumerate(loaded)
                     if link_i < len(self._win._links)}
        else:
            where = {int(k): int(v) for k, v in where.items()
                     if 0 <= int(k) < len(ws.links) and 0 <= int(v) < len(self._win._links)}
        active = where.get(ws.active_link, 0)
        for spec_i, link_i in where.items():
            link, spec = self._win._links[link_i], ws.links[spec_i]
            link.name = spec.name
            link.offset_ps = int(spec.offset_ps)
            if link.view is not None:
                link.view.stream_colors = line_style.overrides_from_json(spec.stream_colors)
                link.view.stream_processing = processing_from_json(spec.stream_processing)
                link.view.even_timing = {(int(e[0]), int(e[1])) for e in spec.even_timing or ()
                                         if isinstance(e, list) and len(e) == 2}
                lost_c = len(spec.stream_colors or ()) - len(link.view.stream_colors)
                lost_p = processing_rows_unread(spec.stream_processing)
                for n, what in ((lost_c, "stream colour"), (lost_p, "Filter & Gain setting")):
                    if n > 0:
                        import_notes.warn(source, f"{link_lead(spec_i, spec.name)}: {n} saved "
                                          f"{what}{'s' if n != 1 else ''} could not "
                                          f"be read and {'was' if n == 1 else 'were'} "
                                          f"not restored")
                if link.view.stream_processing or link.view.even_timing:   # panes drawn first
                    self._win._apply_stream_processing(link.view)
                    self._win._refresh_processed_views(link_i)
            # Device names and register maps are UI metadata (cheap). Hub depths were
            # already applied in the worker-thread factory and reflected by load_session,
            # so there's no re-decode here.
            if spec.device_names:
                link.session.device_names = {int(d): n for d, n in spec.device_names.items()}
            if spec.device_regmaps:
                from ..model.regmap_import import PeripheralRegisterMap
                for dev_str, doc in spec.device_regmaps.items():
                    link.session.set_peripheral_map(
                        int(dev_str), PeripheralRegisterMap.from_json_dict(doc, source))
                # The Commands table took a copy of the maps when the Link loaded, before
                # these: give it them, or its vendor register names stay unresolved.
                if link.view is not None and link.view.cmd_model is not None:
                    link.view.cmd_model.set_peripheral_maps(link.session.peripheral_maps)
        if self._win._cmd_all:                       # it draws through the per-Link models
            self._win._refresh_all_model()
        # The offsets are assigned directly, and each band's domain was computed as its Link
        # was added, at offset 0: recompute it once for all of them, or every band but the
        # first draws unshifted until a zoom re-syncs it.
        self._win._update_timeline_domain()
        if 0 <= active < len(self._win._links):
            self._win.switch_link(active)
        self._win._push_stream_colors()              # switch_link is a no-op on the shown Link
        self._win._update_link_switcher()
        self._win._update_title()
        bms = BookmarkSet.from_json(ws.bookmarks)
        # A bookmark on a Link that did not load has nowhere to be shown; keep the rest,
        # renumbered to the Links as loaded.
        kept = []
        for b in bms:
            if b.link in where:
                b.link = where[b.link]
                kept.append(b)
        if len(kept) != len(bms):
            n = len(bms) - len(kept)
            import_notes.warn(source, f"{n} saved cursor{'s' if n != 1 else ''} on a Link "
                              f"that was not opened {'was' if n == 1 else 'were'} "
                              f"not restored")
        # Cursors placed on the Links while they loaded are kept after the saved ones: the
        # first Link's load cleared the window's earlier ones.
        restored = BookmarkSet(kept)
        restored.merge(self._win._bookmarks_ctl.bookmarks)
        self._win._bookmarks_ctl.bookmarks = restored
        self._win._bookmarks_ctl.push()
        # The saved cursor is clamped into the active capture, whose length may differ from
        # the one it was saved against.
        cur = int(ws.cursor)
        if self._win._session is not None:
            cur = min(cur, max(0, int(self._win._session.last_sample())))
        cur = max(0, cur)
        self._win.cursor.set_sample(cur)
        shown = ws.links[ws.active_link] if ws.active_link in where else None
        if self._win._session is not None and shown is not None:
            if shown.device_names:
                self._win._reg_view.set_device_names(self._win._session.device_names)
            if shown.device_regmaps:
                self._win._reg_view.set_peripheral_maps(self._win._session.peripheral_maps)
                self._win._reg_view.set_files(
                    self._win._session.register_files_at(self._win.cursor.sample))
        if ws.authoring:
            self._win._authoring.set_config(BusConfig.from_dict(ws.authoring, source))
        if ws.timing:
            try:
                self._win._timing_page.set_from_dict(ws.timing, source=source)
            except (TypeError, ValueError, OverflowError) as exc:
                import_notes.warn(source, f"the saved Timing settings could not be applied "
                                  f"({exc}); the rest are kept")
        self._apply_view_prefs(ws.view)
        view = dict(ws.view or {})
        if view.get("pane_links") is not None and not isinstance(view["pane_links"], dict):
            import_notes.warn(source, "the saved Links of the panes are not a mapping; the "
                              "panes show the active Link")
        if isinstance(view.get("pane_links"), dict):
            odd = sorted(str(g) for g, i in view["pane_links"].items()
                         if not isinstance(i, int) or isinstance(i, bool))
            if odd:
                import_notes.warn(source, f"the saved Link of the {', '.join(odd)} panes is "
                                  f"not a Link number; they show the active Link")
            gone = sorted(str(g) for g, i in view["pane_links"].items()
                          if isinstance(i, int) and not isinstance(i, bool) and i not in where)
            if gone:
                import_notes.warn(source, f"the saved Link of the {', '.join(gone)} panes is "
                                  f"not a Link that was opened; they show the active "
                                  f"Link")
            view["pane_links"] = {g: where[i] for g, i in view["pane_links"].items()
                                  if isinstance(i, int) and not isinstance(i, bool)
                                  and i in where}
        self._apply_link_view_prefs(view, active)
        if ws.mode in MODES:
            self._win._mode_mgr.switch_to(ws.mode)
        else:
            import_notes.warn(source, f"the saved mode {ws.mode!r} is not one of "
                              f"{', '.join(MODES)}; the mode is left as it is")
        self._apply_saved_views(ws, where)
        # The offsets moved the Links' mapping, so a remembered instant no longer applies,
        # and an unchanged cursor sample emits nothing: re-sync every pane and playhead at
        # the saved cursor on the saved active Link, before the saved zooms are restored.
        if 0 <= active < len(self._win._links) and self._win._session is not None:
            self._win._activate_link(active, resync=False)
            self._win._cursor_instant = None
            self._win._restore_cursor(cur)
        self._apply_window_view_state(view)

    def note(self, message: str) -> None:
        """An import warning for the workspace being applied (none outside apply_workspace)."""
        source = self.source
        if source is not None:
            import_notes.warn(source, message)

    def _locate_missing_captures(self, ws: Workspace, ws_path: str) -> Optional[set]:
        """Ask about each capture file a workspace names that is neither where the workspace
        says (relative to it) nor where it was when saved: Locate it, Skip that Link (it is
        not loaded and the rest open), or Cancel the open. Returns the indices of the Links
        skipped, or None on Cancel: nothing opens."""
        skipped: set = set()
        while True:
            missing = [m for m in ws.missing_files() if m[0] not in skipped]
            if not missing:
                return skipped
            i, key, saved = missing[0]
            answer = self._ask_missing_capture(ws.links[i].name, saved, len(missing) - 1)
            if answer == "cancel":
                return None
            if answer == "skip":
                skipped.add(i)
                continue
            name = import_notes.source_name(saved)
            ext = os.path.splitext(name)[1]
            found, _ = QFileDialog.getOpenFileName(
                self._win, f"Locate {name}", os.path.dirname(os.path.abspath(ws_path)),
                f"{name} (*{ext});;All files (*)" if ext else "All files (*)")
            if found:
                ws.locate(i, key, found)

    def _ask_missing_capture(self, link_name: str, saved: str, others: int) -> str:
        """"locate", "skip" or "cancel" for one capture file a workspace cannot find."""
        box = QMessageBox(QMessageBox.Warning, "Capture not found",
                          f"{link_name}'s capture file was not found:\n{saved}", parent=self._win)
        more = f" {others} other file(s) are also missing." if others else ""
        box.setInformativeText("Locate it, or skip this Link and open the rest." + more)
        locate = box.addButton("Locate…", QMessageBox.AcceptRole)
        skip = box.addButton("Skip This Link", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(locate)
        box.exec()
        clicked = box.clickedButton()
        return "locate" if clicked is locate else "skip" if clicked is skip else "cancel"

    def keep_shown_views(self) -> None:
        """Leave what the panes on screen show with the Links they show it for."""
        for _k, raw, audio in self._win._bottom_lanes():
            self._win._keep_lane_view(raw, audio)
        if not self._win._cmd_all:
            self._win._save_commands_view(self._win._pane_link["commands"])

    def _window_view_state(self) -> dict:
        """What a workspace records of the window, beyond each Link's own view."""
        from base64 import b64encode
        a = self._win._links.active_index
        timeline = None
        if 0 <= a < len(self._win._ribbons) and a < len(self._win._links):
            r = self._win._ribbons[a]
            lo, hi = r.view_range
            timeline = [self._win._links[a].to_ps(round(lo)),
                        self._win._links[a].to_ps(round(hi))]
        max_pane = self._win._max_pane
        window: Dict[str, Any] = {
            "geometry": b64encode(bytes(self._win.saveGeometry())).decode("ascii"),
            "docks": b64encode(bytes(max_pane["state"] if max_pane
                                     else self._win.saveState())).decode("ascii")}
        settle = self._win._max_pane["settle"] if self._win._max_pane else None
        if settle is not None:
            # All Links turned off while a pane is maximized: the arrangement put back is
            # resized after it is restored, so the workspace carries that resize too.
            window["bottom_settle"] = [settle[0].objectName(), int(settle[1])]
        out = {
            "window": window,
            "timeline_ps": timeline,
            "commands_header": view_state.commands_header_state(self._win._cmd_view),
            "samples": view_state.samples_state(self._win._sample_view),
            "registers": view_state.registers_state(self._win._reg_view),
            "statistics": view_state.statistics_state(self._win._meas_view),
            "timing_pane": view_state.timing_state(self._win._eye_view),
        }
        if self._win._cmd_all:
            out["all_links_commands"] = {
                "filters": view_state.filters_to_json(self._win._filter_snapshot()),
                "col_widths": self._win._command_column_widths()}
        return out

    def _apply_saved_views(self, ws: Workspace, where: dict) -> None:
        """Each Link's own view from the workspace: kept with the Link, and shown on the
        panes that show it now."""
        for spec_i, link_i in where.items():
            spec, st = ws.links[spec_i], self._win._links[link_i].view
            v = spec.view if isinstance(spec.view, dict) else {}
            raw_cmd = v.get("commands")
            cmd: dict = raw_cmd if isinstance(raw_cmd, dict) else {}
            st.filters = view_state.filters_from_json(
                cmd.get("filters"), self.source,
                f"{link_lead(spec_i, spec.name)} Commands filter") or st.filters
            widths = widths_by_name(
                cmd.get("col_widths"), COLUMN_NAMES,
                lambda t, i=spec_i, n=spec.name: self.note(
                    f"{link_lead(i, n)}: the saved Commands {t}; the columns keep "
                    f"their widths"))
            if widths:
                st.col_widths = widths
            st.audio_view = v.get("audio") if isinstance(v.get("audio"), dict) else st.audio_view
            st.capture_view = (v.get("capture") if isinstance(v.get("capture"), dict)
                               else st.capture_view)
        for k, raw, audio in self._win._bottom_lanes():
            st = self._win._links[k].view
            src = self.source
            view_state.restore_audio(audio, st.audio_view, zoom=not self._win._bottom_all,
                                     source=src)
            view_state.restore_capture(raw, st.capture_view, zoom=not self._win._bottom_all,
                                       source=src)
            audio.shown_state = raw.shown_state = st
        if not self._win._cmd_all:
            k = self._win._pane_link["commands"]
            st = self._win._links[k].view
            with self._win._as_link(k):            # its kinds, not the active Link's
                if st.filters is not None:
                    self._win._restore_filters(st.filters)
                self._win._apply_column_widths(st.col_widths)

    def _apply_window_view_state(self, view: dict) -> None:
        """The window-wide part of a saved view, after every Link is bound and the mode
        is set: the panes' own state, the timeline's zoom, and last the window and dock
        layout, applied again once the deferred layout passes have run."""
        allc = view.get("all_links_commands")
        if self._win._cmd_all and isinstance(allc, dict):
            snap = view_state.filters_from_json(allc.get("filters"), self.source,
                                                "All Links Commands filter")
            if snap is not None:
                self._win._restore_filters(snap, kinds=self._win._all_kinds())     # every Link's
            self._win._apply_column_widths(allc.get("col_widths"), report=self.note)
        src = self.source
        view_state.restore_commands_header(self._win._cmd_view, view.get("commands_header"),
                                           source=src)
        if "show_clock" in view:                   # lanes made after _apply_view_prefs ran
            show = view["show_clock"]
            for v in self._win._raw_lanes.views:
                v.set_show_clock(show if isinstance(show, bool) else True)
        view_state.restore_samples(self._win._sample_view, view.get("samples"), source=src)
        view_state.restore_registers(self._win._reg_view, view.get("registers"), source=src)
        view_state.restore_statistics(self._win._meas_view, view.get("statistics"), source=src)
        view_state.restore_timing(self._win._eye_view, view.get("timing_pane"), source=src)
        self._apply_timeline_zoom(view.get("timeline_ps"))
        win = view.get("window")
        if isinstance(win, dict):
            self._win._analysis_laid_out = True         # not the one-time default over it
            self._restore_window(win)

            def again() -> None:
                self._restore_window(win, report=False)
            timers.after(0, self._win, again)
        elif win is not None:
            self.note("the saved window layout is not a mapping; the default layout is shown")

    def _apply_timeline_zoom(self, rng) -> None:
        a = self._win._links.active_index
        if rng is None:
            return
        try:
            lo_ps, hi_ps = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError, KeyError, OverflowError):
            self.note(f"the saved timeline range {rng!r} is not two times; the zoom is "
                      f"left as it is")
            return
        if hi_ps <= lo_ps:
            self.note(f"the saved timeline range {rng!r} ends before it starts; the "
                      f"zoom is left as it is")
            return
        if not 0 <= a < min(len(self._win._ribbons), len(self._win._links)):
            return
        link = self._win._links[a]
        self._win._ribbons[a].zoom_to(link.to_sample(lo_ps), link.to_sample(hi_ps))

    def _restore_window(self, win: dict, report: bool = True) -> None:
        """Restore the saved window geometry and pane layout. A layout that cannot be read
        leaves the default one and, when `report`, is named in the import warnings."""
        from base64 import b64decode
        from binascii import Error as B64Error

        from PySide6.QtCore import QByteArray
        try:
            geometry = b64decode(str(win.get("geometry", "")), validate=True)
            docks = b64decode(str(win.get("docks", "")), validate=True)
        except (B64Error, ValueError):
            if report:
                self.note("the saved window layout could not be read; the default "
                          "layout is shown")
            return
        if geometry:
            self._win.restoreGeometry(QByteArray(geometry))
        if docks:
            self._win.restore_panes()
            self._win.restoreState(QByteArray(docks))
            settle = win.get("bottom_settle")
            if isinstance(settle, list) and len(settle) == 2 \
                    and isinstance(settle[1], int) and settle[1] > 0:
                dock = next((d for d in self._win._dock_area if d.objectName() == settle[0]), None)
                if dock is not None:
                    self._win._settle_bottom_height(dock, settle[1])

    def _apply_view_prefs(self, view: dict) -> None:
        """Restore the transient Bus-Grid / Raw-Capture pane state from a workspace.
        Always establishes a DEFINITE state (defaults for missing keys) — open_workspace
        reuses the current window, so an early return would leak the previous capture's
        TX-map/clock state; a pre-v2 workspace (empty view) restores defaults. Drives the
        widgets so their handlers run, then forces one render so the result is correct
        regardless of which setters happened to change (robust to the reused-window
        case where a setChecked to the same value fires no signal)."""
        view = view or {}

        def _int(key, default, lo, hi):
            """The saved whole number `key` within lo..hi; a value outside is clamped, and
            one that is not a whole number is `default`, each with a note."""
            if key not in view:
                return default
            raw = view[key]
            n = view_state.whole_number(raw)
            if n is None:
                self.note(f"the saved '{key}' {raw!r} is not a number; {default} is used")
                return default
            if not lo <= n <= hi:
                self.note(f"the saved '{key}' {n} is outside {lo}..{hi}; "
                          f"{min(max(n, lo), hi)} is used")
            return min(max(n, lo), hi)

        def _on(key, default):
            """The saved on/off `key`: a JSON true or false; anything else is `default`,
            with a note."""
            raw = view.get(key, default)
            if isinstance(raw, bool):
                return raw
            self.note(f"the saved '{key}' {raw!r} is not true or false; "
                      f"{'on' if default else 'off'} is used")
            return default

        from .main_window import DEFAULT_GRID_ROWS, MAX_GRID_ROWS
        self._win._grid_rows_edit.setText(
            str(_int("grid_rows", DEFAULT_GRID_ROWS, 1, MAX_GRID_ROWS)))
        self._win._on_grid_rows_edit()
        show_clock = _on("show_clock", True)
        for v in self._win._raw_lanes.views:
            v.set_show_clock(show_clock)
        tx_on, persist = _on("tx_map", False), _on("tx_persist", False)
        self._win._toggles_btn.setChecked(tx_on)
        # Persistence only matters while toggles are shown; force it off otherwise so a
        # disabled-but-checked button / stale _tx_persist can't be restored or re-saved.
        self._win._persist_btn.setChecked(tx_on and persist)
        if tx_on:
            self._win._tx_scroll.setValue(_int("tx_start_row", 0, 0, 2 ** 31 - 1))
        self._win._refresh_grid()
