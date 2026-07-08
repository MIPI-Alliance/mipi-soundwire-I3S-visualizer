"""Decoded-audio viewer: one stacked min/max waveform per (device, dp, channel).

Renders from the AudioStore render pyramid via `envelope()` on every view-range
change, so pan/zoom cost is O(visible pixels), not O(samples).

Layout: a draggable QSplitter with, on the left, per-stream show/hide checkboxes
and, on the right, a vertical stack of independent PlotWidgets in a scroll area
(one PlotWidget per track scrolls reliably; a nested GraphicsLayoutWidget does
not). X axes are linked across tracks; Y is fit to the data explicitly.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal, QIODevice, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QPushButton,
                               QScrollArea, QSplitter, QVBoxLayout, QWidget)

from .theme import VizTheme, analyzer_stylesheet
from .plot_widgets import XWheelViewBox
from .nav import install_jog_shortcuts, paged_range

# Audio playback is optional: QtMultimedia (and an output device) may be absent
# in a headless/offscreen environment. Guard it so the view still constructs.
try:
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
    _HAVE_AUDIO = True
except Exception:                                  # pragma: no cover - env dependent
    _HAVE_AUDIO = False

_PEN_COLORS = VizTheme.TRACE_PALETTE
_MAX_POINTS = 2000
_FADE_MS = 50.0             # playback de-click ramp (fade-in / fade-out)
_TRACK_H = 120              # min pixels per stacked track so waveforms aren't squashed


class _PcmSource(QIODevice):
    """Pull-mode audio source for QAudioSink (replaces feeding it a QBuffer).

    It serves the decoded PCM and then *silence forever*, so it never returns a
    short read. Two consequences make playback clean:

      * The sink can never underrun -> no glitches.
      * The sink never hits end-of-data -> it never goes Idle, so macOS never runs
        its "repeat the last buffer" loop. Playback ends only when the owner calls
        stop() deliberately while the source is emitting silence (a cut at zero, so
        no click).

    stop_fade() ramps the audio handed to the sink from unity to zero over
    `fade_frames` (continuing the real samples as it ramps, then silence). Because
    the ramp lives in the sample stream -- not the sink's mixer volume, which lags
    badly on macOS -- the fade is smooth, reaches true zero, and is exactly
    `fade_frames` long. readData runs on the sink's audio thread; the only field it
    shares with the GUI thread is `_fade_at` (one idempotent int assignment)."""

    def __init__(self, pcm: bytes, channels: int, sample_bytes: int, fade_frames: int):
        super().__init__()
        self._pcm = pcm                       # keep alive; _buf is a view into it
        self._dtype = np.int32 if sample_bytes == 4 else np.int16
        self._buf = np.frombuffer(pcm, dtype=self._dtype)
        self._nch = max(1, int(channels))
        self._sample_bytes = int(sample_bytes)
        self._total_frames = self._buf.size // self._nch
        self._fade_frames = max(1, int(fade_frames))
        self._frame = 0                       # next source frame (audio thread)
        self._fade_at = None                  # source frame where stop-fade began
        self.open(QIODevice.ReadOnly)

    # -- transport (called from the GUI thread) --
    def stop_fade(self) -> None:
        if self._fade_at is None:
            self._fade_at = self._frame

    @property
    def fade_at(self):
        return self._fade_at

    @property
    def total_frames(self) -> int:
        return self._total_frames

    # -- QIODevice (readData runs on the sink's audio thread) --
    def isSequential(self) -> bool:
        return True

    def bytesAvailable(self) -> int:
        return (4 << 20) + super().bytesAvailable()

    def writeData(self, _data) -> int:        # read-only source
        return -1

    def readData(self, maxlen: int) -> bytes:
        bpf = self._nch * self._sample_bytes
        nframes = int(maxlen) // bpf
        if nframes <= 0:
            return bytes(0)
        out = np.zeros(nframes * self._nch, dtype=self._dtype)
        avail = self._total_frames - self._frame
        take = nframes if avail >= nframes else max(0, avail)
        if take > 0:                          # copy PCM; the rest stays silence
            s = self._frame * self._nch
            out[:take * self._nch] = self._buf[s:s + take * self._nch]
        fa = self._fade_at
        if fa is not None:                    # ramp 1 -> 0 across this block, then 0
            i = np.arange(nframes, dtype=np.float32) + float(self._frame - fa)
            gain = np.clip(1.0 - i / float(self._fade_frames), 0.0, 1.0)
            blk = out.reshape(nframes, self._nch).astype(np.float32) * gain[:, None]
            out = np.rint(blk).astype(self._dtype).reshape(-1)
        self._frame += nframes
        return out.tobytes()


class _TimeAxis(pg.AxisItem):
    """Bottom axis that displays the audio-sample-index coordinate as time. The plot's
    internal x stays in sample index (so zoom/link/export are unchanged); only the tick
    labels are converted via the stream's sample rate. The UNIT adapts to the tick
    spacing — seconds when coarse, else milliseconds or microseconds — so a zoomed-in
    view reads as comma-separated `6,268,390 µs` instead of a tiny fractional second.
    The unit is shown per tick, so the axis label carries no fixed unit."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._rate = 0.0
        self.enableAutoSIPrefix(False)        # we format the time ourselves

    def set_rate(self, rate_hz: float) -> None:
        self._rate = float(rate_hz or 0.0)

    def tickStrings(self, values, scale, spacing):
        if not self._rate:
            return [f"{v:,.0f}" for v in values]
        step_s = (spacing / self._rate) if spacing else 0.0     # tick spacing in seconds
        if step_s == 0 or step_s >= 1.0:
            unit, mul = "s", 1.0
        elif step_s >= 1e-3:
            unit, mul = "ms", 1e3
        else:
            unit, mul = "µs", 1e6
        step_u = step_s * mul
        dec = 0 if step_u >= 1 else int(min(6, np.ceil(-np.log10(step_u)) + 1))
        return [f"{v / self._rate * mul:,.{dec}f} {unit}" for v in values]


class AudioView(QWidget):
    # A click on a waveform emits the capture sample under the pointer, so the
    # shared cursor (timeline / command table / CDS symbols) can follow. 64-bit.
    seeked = Signal('qlonglong')
    # During playback, the play head sweeps; this advances the SHARED cursor so
    # every pane (timeline, registers, grid) tracks the audio position.
    playCursorMoved = Signal('qlonglong')
    # Emitted when playback actually stops (pause / end / rewind). The heavy per-pane
    # updates (registers, grid, symbols) are throttled while playing, so the owner
    # uses this to do one final full refresh at the resting cursor position.
    stopped = Signal()

    def __init__(self) -> None:
        super().__init__()
        # Flat dark item-view + themed (always-visible) scrollbars, shared with the
        # rest of the Analyzer; the child scroll areas inherit the scrollbar style.
        self.setStyleSheet(analyzer_stylesheet())
        pg.setConfigOptions(antialias=False)

        # Left: per-stream channel show/hide checkboxes (in its own scroll).
        self._chan_box = QVBoxLayout()
        self._chan_box.setContentsMargins(4, 4, 4, 4)
        self._chan_box.setSpacing(8)               # room so DP/CH checkboxes don't overlap
        chan_panel = QWidget()
        chan_panel.setLayout(self._chan_box)
        self._chan_scroll = QScrollArea()
        self._chan_scroll.setWidgetResizable(True)
        self._chan_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._chan_scroll.setWidget(chan_panel)

        # Right: a plain widget stack of PlotWidgets inside a scroll area.
        self._tracks_host = QWidget()
        self._tracks_layout = QVBoxLayout(self._tracks_host)
        self._tracks_layout.setContentsMargins(0, 0, 0, 0)
        self._tracks_layout.setSpacing(2)
        self._tracks_scroll = QScrollArea()
        self._tracks_scroll.setWidgetResizable(True)
        self._tracks_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._tracks_scroll.setWidget(self._tracks_host)

        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.addWidget(self._chan_scroll)
        self._splitter.addWidget(self._tracks_scroll)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([180, 900])

        all_btn = QPushButton("All")
        all_btn.clicked.connect(lambda: self._set_all_channels(True))
        none_btn = QPushButton("None")
        none_btn.clicked.connect(lambda: self._set_all_channels(False))
        self._play_btn = QPushButton("▶ Play")
        self._play_btn.clicked.connect(self._toggle_play)
        self._stop_btn = QPushButton("◼ Stop")
        self._stop_btn.clicked.connect(self.stop_and_rewind)
        # Playback settings (output device, bit depth, decimation) live in the
        # Audio menu, not on this toolbar — see MainWindow. They're held here as
        # plain state the menu sets via the setters below.
        self._audio_devices = []
        self._device_index = 0          # index into _audio_devices (-1 / 0 = default)
        self._depth = 16                # playback bit depth: 16 or 24
        # Decimation is per-(device, dataport): {(dev, dp): target Hz}; a stream with
        # no entry plays at its native rate. Set from the Audio ▸ Decimation menu.
        self._play_rate_targets = {}
        self._media_devices = None
        if _HAVE_AUDIO:
            # A live QMediaDevices instance is what makes the backend actually notice
            # devices hot-plugged after startup (the static audioOutputs() otherwise
            # reports the snapshot from process start). Keep it alive and refresh the
            # cached list whenever the system's output set changes.
            self._media_devices = QMediaDevices(self)
            self._media_devices.audioOutputsChanged.connect(self._refresh_devices)
            self._refresh_devices()
        else:
            for b in (self._play_btn, self._stop_btn):
                b.setEnabled(False)
                b.setToolTip("Audio playback unavailable (QtMultimedia not present)")
        if _HAVE_AUDIO:
            self._play_btn.setToolTip("Play the first selected data port from the cursor")
        self._vzoom_btn = QPushButton("Vertical Zoom In")
        self._vzoom_btn.setCheckable(True)
        self._vzoom_btn.setToolTip("Scale each waveform's amplitude to fill the track "
                                   "(fit Y to the visible data) instead of full-scale ±1")
        self._vzoom_btn.toggled.connect(self._on_vzoom_toggled)
        reset = QPushButton("Horizontal Zoom Reset")
        reset.setToolTip("Reset each track's horizontal (time) zoom to the full channel")
        reset.clicked.connect(self.reset_zoom)
        # Jog: page the view one whole visible width left/right (keyboard < and >).
        jog_l = QPushButton("Jog <")
        jog_l.setToolTip("Page one whole visible width left (shortcut: <)")
        jog_l.clicked.connect(lambda: self.jog(-1))
        jog_r = QPushButton("Jog >")
        jog_r.setToolTip("Page one whole visible width right (shortcut: >)")
        jog_r.clicked.connect(lambda: self.jog(+1))
        # Focus-scoped so </> jog THIS pane when it (or a child) has focus, without
        # clashing with the Raw pane's identical shortcuts.
        install_jog_shortcuts(self, self.jog)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        bar.addWidget(all_btn)
        bar.addWidget(none_btn)
        bar.addWidget(self._play_btn)
        bar.addWidget(self._stop_btn)
        bar.addStretch(1)
        bar.addWidget(jog_l)
        bar.addWidget(jog_r)
        bar.addWidget(self._vzoom_btn)
        bar.addWidget(reset)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(bar)
        lay.addWidget(self._splitter, 1)

        self._store = None
        self._vzoom = False       # Vertical Zoom: fit Y to visible data vs full-scale ±1
        self._plots = []          # (plotitem, curve, dev, dp, ch, n)
        self._checks = {}         # (dev, dp, ch) -> QCheckBox
        self._lane_colors = {}    # (dev, dp, ch) -> QColor (stable; checkbox + curve)
        self._link = None
        self._cursor_lines = []   # InfiniteLine per plot, parallel to self._plots
        self._cursor_sample = 0
        # The X axis is in CAPTURE-sample space (not per-channel audio index), so each
        # waveform sits at its true capture-time offset and every track — and the
        # timeline above — lines up. Set from the session before set_store().
        self._capture_rate = 0.0     # capture sample rate (Hz), for the time-axis labels
        self._capture_extent = 1     # full capture length (samples) = the X extent
        self._sink = None         # QAudioSink while playing
        self._source = None       # _PcmSource feeding the sink (kept alive during play)
        # Playback cursor sweep: a timer maps the sink's processed time back to a
        # capture sample and drives the shared cursor so the play head moves. It also
        # ends playback (at the clip end, or once a stop-fade has finished) by
        # stopping the sink while the source is emitting silence -- a clean cut.
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(40)                 # ~25 fps marker update
        self._play_timer.timeout.connect(self._on_play_tick)
        self._play_dev = self._play_dp = None
        self._play_chan0 = None
        self._play_start_idx = 0
        self._play_rate = 0
        self._play_nframes = 0
        self._play_fade_frames = 1       # frames in the de-click / stop ramp
        self._play_idx_per_frame = 1.0   # native-index per played frame (decimation)
        self._want_play = False          # transport intent, drives the Play/Pause button
        self._rewind_on_stop = False     # Stop button: snap cursor to start once stopped

    def _refresh_devices(self) -> None:
        """Re-scan the available output devices. Keeps the currently-selected device
        if it's still present (so a hot-plug elsewhere doesn't reset the choice),
        otherwise falls back to the system default. The Audio menu reads
        device_names()/device_index to build its submenu."""
        prev_id = None
        if 0 <= self._device_index < len(self._audio_devices):
            prev_id = self._audio_devices[self._device_index].id()
        self._audio_devices = list(QMediaDevices.audioOutputs())
        default = QMediaDevices.defaultAudioOutput()
        default_id = None if default.isNull() else default.id()
        self._device_index = 0
        for want in (prev_id, default_id):
            if want is None:
                continue
            hit = next((i for i, d in enumerate(self._audio_devices) if d.id() == want), None)
            if hit is not None:
                self._device_index = hit
                return

    def refresh_devices(self) -> None:
        """Public: force a re-scan of output devices (Audio ▸ Refresh Devices)."""
        self._refresh_devices()

    # ---- playback settings (driven by the Audio menu) ----
    def device_names(self) -> list:
        """Human-readable output-device descriptions (parallel to device_index)."""
        if not _HAVE_AUDIO:
            return []
        if not self._audio_devices:
            self._refresh_devices()
        return [d.description() for d in self._audio_devices]

    @property
    def device_index(self) -> int:
        return self._device_index

    def set_device_index(self, i: int) -> None:
        if 0 <= i < len(self._audio_devices):
            self._device_index = i

    @property
    def play_depth(self) -> int:
        return self._depth

    def set_play_depth(self, bits: int) -> None:
        self._depth = 24 if int(bits) >= 24 else 16

    @property
    def play_rate_targets(self) -> dict:
        """Per-(device, dataport) decimation targets in Hz ({} = all native)."""
        return dict(self._play_rate_targets)

    def play_rate_target_for(self, dev: int, dp: int):
        """Decimation target Hz for one stream, or None for native."""
        return self._play_rate_targets.get((int(dev), int(dp)))

    def set_play_rate_target(self, dev: int, dp: int, rate) -> None:
        """Set the decimation target in Hz for one (device, dataport); None/0 =
        native (the entry is dropped)."""
        key = (int(dev), int(dp))
        if rate:
            self._play_rate_targets[key] = int(rate)
        else:
            self._play_rate_targets.pop(key, None)

    def _selected_device(self):
        """The chosen QAudioDevice, or the system default if none/avail."""
        if 0 <= self._device_index < len(self._audio_devices):
            return self._audio_devices[self._device_index]
        return QMediaDevices.defaultAudioOutput()

    def set_capture_context(self, sample_rate_hz: float, total_samples: int) -> None:
        """Set the CAPTURE sample rate + full length so the waveform X axis spans the
        whole capture in capture-sample space (audio positioned at its true offset,
        aligned with the timeline). Call BEFORE set_store (which rebuilds the plots)."""
        self._capture_rate = float(sample_rate_hz or 0.0)
        self._capture_extent = max(1, int(total_samples or 1))

    def set_store(self, store) -> None:
        self._hard_stop()                          # don't play across a capture swap
        self._store = store
        # A fresh capture starts at full-scale amplitude (Vertical Zoom off).
        self._vzoom = False
        self._vzoom_btn.blockSignals(True)
        self._vzoom_btn.setChecked(False)
        self._vzoom_btn.setText("Vertical Zoom In")
        self._vzoom_btn.blockSignals(False)
        # Decimation targets are keyed by (dev, dp); a new capture's streams may
        # differ, so drop any stale entries.
        self._play_rate_targets = {}
        # Rebuild the channel checkbox list.
        while self._chan_box.count():
            item = self._chan_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._checks = {}
        # Stable per-(dev,dp,ch) colour, assigned in stream order — used for BOTH the
        # waveform pen and the channel checkbox text, so a track is identified by
        # colour, not just its DP name (and the colour doesn't shuffle as channels are
        # toggled, unlike a per-selection index).
        self._lane_colors = {}
        lane_i = 0
        for dev, dp in store.streams():
            for ch in store.channels(dev, dp):
                color = QColor(_PEN_COLORS[lane_i % len(_PEN_COLORS)])
                lane_i += 1
                self._lane_colors[(dev, dp, ch)] = color
                cb = QCheckBox(f"Dev{dev} DP{dp} CH{ch}")
                cb.setChecked(True)
                cb.setStyleSheet(f"QCheckBox {{ color: {color.name()}; }}")
                cb.toggled.connect(lambda _on: self._rebuild_plots())
                self._chan_box.addWidget(cb)
                self._checks[(dev, dp, ch)] = cb
        self._chan_box.addStretch(1)
        self._rebuild_plots(preserve_zoom=False)

    def streams(self):
        """All (device, dataport) audio streams in the current store ([] if none)."""
        return list(self._store.streams()) if self._store is not None else []

    def retheme(self) -> None:
        """Re-apply the palette after a theme switch: refresh the trace-pen colours
        and the item-view stylesheet, then rebuild the plots so each track's
        background / curve / cursor pen pick up the new theme."""
        global _PEN_COLORS
        _PEN_COLORS = VizTheme.TRACE_PALETTE
        self.setStyleSheet(analyzer_stylesheet())
        # Re-assign the stable lane colours from the new palette (stream order) and
        # restyle the channel checkboxes to match, then rebuild so curves follow.
        for i, key in enumerate(self._checks):
            color = QColor(_PEN_COLORS[i % len(_PEN_COLORS)])
            self._lane_colors[key] = color
            self._checks[key].setStyleSheet(f"QCheckBox {{ color: {color.name()}; }}")
        self._rebuild_plots()

    def _selected(self):
        out = []
        for dev, dp in self._store.streams():
            for ch in self._store.channels(dev, dp):
                cb = self._checks.get((dev, dp, ch))
                if cb is None or cb.isChecked():
                    out.append((dev, dp, ch))
        return out

    def channel_selection(self):
        """The set of (dev, dp, ch) lanes currently shown (checked) — captured to hold
        the track selection across a re-decode that keeps the same streams."""
        return {key for key, cb in self._checks.items() if cb.isChecked()}

    def set_channel_selection(self, keys) -> None:
        """Restore a previously captured channel selection so hidden tracks stay hidden
        across a re-decode (e.g. an SSP phase step) instead of set_store re-showing them
        all. Only touches lanes still present; a single rebuild follows. No-op if the
        selection matches none of the current lanes (never blank the view)."""
        if not keys or not any(k in self._checks for k in keys):
            return
        changed = False
        for key, cb in self._checks.items():
            want = key in keys
            if cb.isChecked() != want:
                cb.blockSignals(True)
                cb.setChecked(want)
                cb.blockSignals(False)
                changed = True
        if changed:
            self._rebuild_plots()

    def _clear_tracks(self) -> None:
        while self._tracks_layout.count():
            item = self._tracks_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._plots = []
        self._cursor_lines = []
        self._link = None

    def _rebuild_plots(self, preserve_zoom: bool = True) -> None:
        if self._store is None:
            return
        # Preserve the current X zoom across a channel show/hide (a selection change
        # shouldn't reset the view); only a new capture (set_store) refits from zero.
        prev_range = None
        if preserve_zoom and self._link is not None:
            try:
                prev_range = tuple(self._link.getViewBox().viewRange()[0])
            except Exception:
                prev_range = None
        self._clear_tracks()
        extent = max(1, int(self._capture_extent))
        for dev, dp, ch in self._selected():
            rate = self._store.rate(dev, dp)             # stored/render rate (unused for x now)
            native = self._store.native_rate(dev, dp)    # on-bus rate (label)
            bits = self._store.sample_bits(dev, dp, ch)
            title = (f"Dev{dev} · DP{dp} · CH{ch}"
                     + (f" · {native/1000:.1f} kHz" if native else ""))
            taxis = _TimeAxis(orientation="bottom")
            # X is capture samples, so the axis converts with the CAPTURE rate — the
            # labels are absolute capture time, matching the timeline / other panes.
            taxis.set_rate(self._capture_rate or rate)
            pw = pg.PlotWidget(title=title, background=VizTheme.PLOT_BG,
                               viewBox=XWheelViewBox(),
                               axisItems={"bottom": taxis})
            pw.setMinimumHeight(_TRACK_H)
            plot = pw.getPlotItem()
            plot.getViewBox().setDefaultPadding(0.0)   # waveform starts flush at t=0
            plot.showGrid(x=True, y=True, alpha=0.2)
            plot.setMouseEnabled(y=False)         # Y fit to data; X is zoomable
            plot.setClipToView(True)
            plot.setLabel("bottom", "Time")     # unit (s/ms/µs) is shown per tick
            plot.setLabel("left", "Amplitude")
            # Full-scale Y with -1/0/+1 majors; ±0.5 are minor ticks pyqtgraph only
            # labels when the track is tall enough (short track → just -1,0,+1).
            plot.getAxis("left").setTicks([[(-1.0, "-1"), (0.0, "0"), (1.0, "+1")],
                                           [(-0.5, "-0.5"), (0.5, "0.5")]])
            color = self._lane_colors.get((dev, dp, ch)) or QColor(_PEN_COLORS[0])
            curve = plot.plot(pen=pg.mkPen(color))
            n = self._store.samples(dev, dp, ch).size
            plot.sigXRangeChanged.connect(
                lambda _vb, rng, p=plot, c=curve, a=dev, d=dp, k=ch, b=bits:
                    self._render(p, c, a, d, k, rng, b))
            self._render(plot, curve, dev, dp, ch, (0, extent), bits)
            plot.setXRange(0, extent, padding=0)
            if self._link is not None:
                plot.setXLink(self._link)
            else:
                self._link = plot
            # Shared-cursor line — X is the capture sample directly (no per-channel map).
            line = pg.InfiniteLine(angle=90, movable=False,
                                   pen=pg.mkPen(VizTheme.CURSOR, width=1, style=Qt.DashLine))
            line.setZValue(10)
            plot.addItem(line, ignoreBounds=True)
            # Click a waveform to seek the shared cursor there.
            pw.scene().sigMouseClicked.connect(
                lambda ev, p=plot, a=dev, d=dp, k=ch: self._on_plot_click(ev, p, a, d, k))
            self._tracks_layout.addWidget(pw)
            self._plots.append((plot, curve, dev, dp, ch, n))
            self._cursor_lines.append(line)
        # Bound the X view to the full capture so the user can't zoom OUT past it or pan
        # off either end (the tracks are X-linked, so the same extent applies to all).
        for plot, *_rest in self._plots:
            plot.getViewBox().setLimits(xMin=0, xMax=extent, maxXRange=extent)
        # Restore the pre-rebuild zoom (X-linked, so the link carries every track).
        if prev_range is not None and self._link is not None:
            self._link.setXRange(prev_range[0], prev_range[1], padding=0)
        # Make the host tall enough that the scroll area actually scrolls.
        self._tracks_host.setMinimumHeight(_TRACK_H * max(1, len(self._plots)) + 8)
        self._apply_cursor()                  # position the cursor lines

    def _set_all_channels(self, on: bool) -> None:
        for cb in self._checks.values():
            cb.blockSignals(True)
            cb.setChecked(on)
            cb.blockSignals(False)
        self._rebuild_plots()

    def _render(self, plot, curve, dev, dp, ch, rng, bits=0) -> None:
        if self._store is None:
            return
        # `rng` is the visible X window in CAPTURE samples. Map it to this channel's
        # audio-index slice for the envelope, then map the envelope's index positions
        # back to capture samples so the curve sits at its true capture-time offset.
        cs0, cs1 = int(rng[0]), int(rng[1])
        i0, i1 = self._store.index_range_for_samples(dev, dp, ch, cs0, cs1)
        x_idx, lo, hi = self._store.envelope(dev, dp, ch, i0, i1, max_points=_MAX_POINTS)
        if x_idx.size == 0:
            curve.setData([], [])
            return
        sat = self._store.sample_positions(dev, dp, ch)
        if sat is not None and sat.size:
            xi = np.clip(np.asarray(x_idx, dtype=np.int64), 0, sat.size - 1)
            x = np.asarray(sat, dtype=np.float64)[xi]     # index -> capture sample
        else:
            x = np.asarray(x_idx, dtype=np.float64)
        # Normalise to full-scale amplitude in [-1, +1]: divide by 2**(bits-1)
        # (two's-complement full-scale). Falls back to raw if bits unknown.
        scale = float(1 << (bits - 1)) if bits and bits > 1 else 1.0
        lo = np.asarray(lo, dtype=np.float64) / scale
        hi = np.asarray(hi, dtype=np.float64) / scale
        # Min/max ladder: two y per bin so each column shows its [min,max] extent.
        xs = np.repeat(x, 2)
        ys = np.empty(x.size * 2, dtype=np.float64)
        ys[0::2] = lo
        ys[1::2] = hi
        curve.setData(xs, ys)
        if self._vzoom and lo.size:
            # Vertical Zoom: fit Y to the visible data so a low-amplitude signal fills
            # the track. Let pyqtgraph auto-tick the zoomed range (the fixed ±1/±0.5
            # ticks would fall outside it).
            ymin = float(min(lo.min(), hi.min()))
            ymax = float(max(lo.max(), hi.max()))
            if ymax - ymin < 1e-9:            # flat/silent channel — avoid a zero span
                ymin, ymax = ymin - 1e-3, ymax + 1e-3
            plot.getAxis("left").setTicks(None)
            plot.setYRange(ymin, ymax, padding=0.08)
        else:
            # Fixed full-scale Y range so amplitude is comparable across tracks.
            plot.getAxis("left").setTicks([[(-1.0, "-1"), (0.0, "0"), (1.0, "+1")],
                                           [(-0.5, "-0.5"), (0.5, "0.5")]])
            plot.setYRange(-1.0, 1.0, padding=0.05)

    def _on_vzoom_toggled(self, on: bool) -> None:
        """Toggle amplitude fit-to-data. Re-render every track at its current X range
        so the Y range (and ticks) update immediately."""
        self._vzoom = bool(on)
        # The label reflects what the button will DO next: "In" when off (it will fit
        # Y to the data), "Reset" when on (it will restore full-scale ±1).
        self._vzoom_btn.setText("Vertical Zoom Reset" if on else "Vertical Zoom In")
        for plot, curve, dev, dp, ch, _n in self._plots:
            rng = plot.getViewBox().viewRange()[0]
            self._render(plot, curve, dev, dp, ch, rng, self._store.sample_bits(dev, dp, ch))

    def reset_zoom(self) -> None:
        """Re-fit every track: X to the full capture (Y re-fits via _render)."""
        extent = max(1, int(self._capture_extent))
        for plot, _curve, _dev, _dp, _ch, _n in self._plots:
            plot.setXRange(0, extent, padding=0)

    def jog(self, direction: int) -> None:
        """Page the view one whole visible width left (direction < 0) or right
        (> 0), keeping the zoom span — for stepping across the capture a screen at a
        time. Clamped to the full-capture extent. No-op if nothing is plotted."""
        if self._link is None or not self._plots:
            return
        x0, x1 = self._link.getViewBox().viewRange()[0]
        rng = paged_range(x0, x1, 0.0, float(max(1, self._capture_extent)), direction)
        if rng is not None:
            self._link.setXRange(rng[0], rng[1], padding=0)

    def set_cursor(self, sample: int) -> None:
        """Move the shared-cursor line on every track to the capture `sample`."""
        self._cursor_sample = int(sample)
        self._apply_cursor()

    def _apply_cursor(self) -> None:
        if self._store is None:
            return
        s = self._cursor_sample
        # X is capture-sample space, shared by every track, so the cursor line is at the
        # capture sample directly on all of them (no per-channel index mapping).
        for line in self._cursor_lines:
            line.setPos(s)
        # Follow the shared cursor: if it falls outside the current zoom window, pan
        # (keeping the span — zoom is never changed) so it stays visible. The tracks
        # are X-linked, so panning the link moves them all. Skipped while hidden so a
        # stale/zero view range can't perturb the zoom. Also skipped *while playing*:
        # each pan re-renders every track's envelope (sigXRangeChanged -> _render), and
        # that main-thread burst starves the (Python) pull-source readData on the audio
        # thread -> stutter. During playback the cursor line still moves; the view just
        # doesn't auto-scroll (the user can scroll, or it re-follows once stopped).
        if self._link is not None and self.isVisible() and not self.is_playing:
            x0, x1 = self._link.getViewBox().viewRange()[0]
            span = x1 - x0
            if span > 0 and not (x0 <= s <= x1):
                self._link.setXRange(s - span / 2, s + span / 2, padding=0)

    def _on_plot_click(self, ev, plot, dev, dp, ch) -> None:
        if self._store is None or ev.button() != Qt.LeftButton:
            return
        if not plot.sceneBoundingRect().contains(ev.scenePos()):
            return
        # X is the capture sample under the pointer — seek there directly.
        x = plot.vb.mapSceneToView(ev.scenePos()).x()
        ev.accept()
        self.seeked.emit(int(round(x)))

    # ---- playback ----
    def play(self) -> None:
        """Play the first selected data port (its selected channels) from the
        cursor through the chosen audio output, at the chosen bit depth. The play
        head sweeps the shared cursor as it goes. No-op without QtMultimedia."""
        if not _HAVE_AUDIO or self._store is None:
            return
        sel = self._selected()
        if not sel:
            return
        dev, dp, _ = sel[0]
        chans = [c for (d, p, c) in sel if d == dev and p == dp]
        if not chans:
            return
        i0, _i1 = self._store.index_range_for_samples(
            dev, dp, chans[0], self._cursor_sample, self._cursor_sample)
        pcm, rate, nch, qfmt = self._store.pcm_play(
            dev, dp, chans, start=i0, depth=self._depth,
            target_rate=self._play_rate_targets.get((int(dev), int(dp))))
        if nch == 0 or not pcm:
            return
        device = self._selected_device()
        if device is None or device.isNull():
            return
        self._hard_stop()
        pcm = self._declick(pcm, int(rate), int(nch), qfmt)
        fmt = QAudioFormat()
        fmt.setSampleRate(int(rate))
        fmt.setChannelCount(int(nch))
        fmt.setSampleFormat(getattr(QAudioFormat.SampleFormat, qfmt))
        self._sink = QAudioSink(device, fmt)
        sample_bytes = 4 if qfmt == "Int32" else 2
        fade_frames = max(1, int(rate * _FADE_MS / 1000.0))
        # Feed from a never-short-reading source (see _PcmSource): no underrun
        # glitches, no end-of-data Idle/repeat, and stop() fades in software.
        self._source = _PcmSource(pcm, int(nch), sample_bytes, fade_frames)
        self._sink.start(self._source)
        # Remember the mapping so the sweep timer can turn elapsed audio frames
        # back into a capture sample for the shared cursor. With decimation the
        # playback frames are in the target-rate domain, so scale them back to the
        # native audio-sample index by native_rate/play_rate.
        self._play_dev, self._play_dp, self._play_chan0 = dev, dp, chans[0]
        self._play_start_idx = int(i0)
        self._play_rate = int(rate)
        native = int(round(self._store.rate(dev, dp))) or int(rate)
        self._play_idx_per_frame = (native / float(rate)) if rate else 1.0
        self._play_nframes = len(pcm) // (nch * sample_bytes)
        self._play_fade_frames = fade_frames
        self._play_timer.start()
        self._want_play = True
        self._set_play_button(True)

    def _toggle_play(self) -> None:
        """Play button doubles as Pause while playing."""
        if self._want_play:
            self._want_play = False
            self._set_play_button(False)
            self.stop()                              # pause: keep the playhead where it is
        else:
            self.play()

    def stop_and_rewind(self) -> None:
        """Stop button: fade out cleanly, then return the playhead to the start of
        the decoded audio once the fade has released (see _hard_stop)."""
        self._want_play = False
        self._set_play_button(False)
        self._rewind_on_stop = True
        self.stop()

    def _audio_start_sample(self) -> int:
        """Capture sample of the first decoded audio sample (a selected stream if
        any, else the first stream in the store)."""
        if self._store is None:
            return 0
        streams = self._selected()
        if not streams:
            streams = [(d, p, self._store.channels(d, p)[0])
                       for (d, p) in self._store.streams()
                       if self._store.channels(d, p)]
        for dev, dp, ch in streams:
            s = self._store.sample_at_index(dev, dp, ch, 0)
            if s is not None:
                return int(s)
        return 0

    def _set_play_button(self, playing: bool) -> None:
        self._play_btn.setText("❚❚ Pause" if playing else "▶ Play")

    def _on_play_tick(self) -> None:
        """Map the sink's processed audio time to a capture sample and drive the
        shared cursor, so the play head sweeps every pane. Also ends playback: the
        source feeds silence forever (never Idle/repeat), so we stop the sink here
        once the *output* has passed the clip end -- or, after a stop-fade, once the
        ramp has fully played out. Either way the cut lands in silence, so no click."""
        if self._sink is None or self._source is None or self._store is None:
            self._play_timer.stop()
            return
        from PySide6.QtMultimedia import QAudio
        if self._sink.state() == QAudio.State.StoppedState:   # external stop
            self._hard_stop()
            return
        # Frames the sink has actually output so far (= processedUSecs * rate).
        out_frames = int(self._sink.processedUSecs() * self._play_rate // 1_000_000)
        fa = self._source.fade_at
        if fa is not None:
            if out_frames >= fa + self._play_fade_frames:     # stop-fade fully played
                self._hard_stop()
            return                                            # freeze the cursor while fading
        if out_frames >= self._play_nframes:                  # clip end (incl. baked fade)
            self._hard_stop()
            return
        # Scale resampled output frames back to the native audio-sample index, then
        # to the capture sample the shared cursor expects.
        frames = max(0, min(out_frames, max(0, self._play_nframes - 1)))
        idx = self._play_start_idx + int(frames * self._play_idx_per_frame)
        samp = self._store.sample_at_index(self._play_dev, self._play_dp,
                                           self._play_chan0, idx)
        if samp is not None:
            self._cursor_sample = int(samp)
            self._apply_cursor()                 # move our own lines immediately
            self.playCursorMoved.emit(int(samp))  # and the shared cursor

    def _declick(self, pcm: bytes, rate: int, nch: int, qfmt: str) -> bytes:
        """Apply a 50 ms fade-in and fade-out to the PLAYBACK pcm buffer (a copy
        extracted for output — the decoded audio is left untouched), so playback
        starts and ends at zero instead of a discontinuity that pops. Start/stop
        points are arbitrary within the decoded waveform, hence the ramp lives on
        the playback copy here, not in the stored samples."""
        dtype = np.int32 if qfmt == "Int32" else np.int16
        a = np.frombuffer(pcm, dtype=dtype)
        frames = a.size // nch if nch else 0
        if frames < 2:
            return pcm
        nfade = max(1, min(int(rate * _FADE_MS / 1000.0), frames // 2))
        block = a.reshape(frames, nch).astype(np.float32)
        block[:nfade] *= np.linspace(0.0, 1.0, nfade, dtype=np.float32)[:, None]
        block[frames - nfade:] *= np.linspace(1.0, 0.0, nfade, dtype=np.float32)[:, None]
        return np.rint(block).astype(dtype).tobytes()

    def stop(self) -> None:
        """User-initiated stop/pause: ask the source to ramp to silence over 50 ms
        (a software fade in the sample stream -- see _PcmSource -- not the mixer
        volume, which lags on macOS), then let the sweep timer release the sink once
        the ramp has played out. Both the Pause toggle and the Stop button route
        here. (The natural end-of-clip is de-clicked by the fade-out baked into the
        playback copy and likewise released by the sweep timer.)"""
        if self._source is not None and _HAVE_AUDIO:
            self._source.stop_fade()
            if not self._play_timer.isActive():
                self._play_timer.start()         # ensure the release tick is running
            return
        self._hard_stop()

    def _hard_stop(self) -> None:
        """Stop playback immediately and release the sink/source. If a Stop (rewind)
        is pending, snap the shared cursor to the start of the audio now that the
        fade has played out."""
        self._play_timer.stop()
        was_playing = self._sink is not None
        if self._sink is not None:
            self._sink.stop()
            self._sink = None
        if self._source is not None:
            self._source.close()
            self._source = None
        self._want_play = False
        self._set_play_button(False)
        if self._rewind_on_stop:
            self._rewind_on_stop = False
            if self._store is not None:
                self.playCursorMoved.emit(int(self._audio_start_sample()))
        # Heavy per-pane updates are throttled while playing; now that we've stopped
        # (is_playing is False above), ask the owner for one full refresh at the
        # resting cursor so the registers/grid/symbols aren't left slightly stale.
        if was_playing:
            self.stopped.emit()

    @property
    def is_playing(self) -> bool:
        if self._sink is None or not _HAVE_AUDIO:
            return False
        # Treat anything but a stopped sink as "playing". A pull-mode QAudioSink
        # oscillates between Active and Idle on macOS as it pulls buffers, so
        # checking == ActiveState is flaky and would intermittently let the heavy
        # cursor-follow work run mid-playback (glitching audio). The sink is set to
        # None on a real stop (_hard_stop), so this is False once playback ends.
        from PySide6.QtMultimedia import QAudio
        return self._sink.state() != QAudio.State.StoppedState

    @property
    def plot_count(self) -> int:
        return len(self._plots)

    def visible_index_range(self):
        """The visible window as the first plot channel's [i0, i1) audio-sample-index
        slice (the export dialog windows playback by index). The plot X is capture
        samples, so map it back through the channel's sample positions."""
        if not self._plots or self._store is None:
            return None
        plot, _c, dev, dp, ch, _n = self._plots[0]
        x0, x1 = plot.viewRange()[0]
        i0, i1 = self._store.index_range_for_samples(
            dev, dp, ch, int(max(0, x0)), int(max(0, x1)))
        return int(i0), int(i1)

    def set_visible_index_range(self, rng) -> None:
        """Restore a previously captured index-domain window (used to hold the zoom
        across a re-decode that keeps the same streams, e.g. an SSP phase step). The
        plot X is capture samples, so convert the index range through the first plot
        channel's positions. No-op if there are no plots or the range is degenerate."""
        if not self._plots or self._link is None or not rng or self._store is None:
            return
        i0, i1 = rng
        if i1 <= i0:
            return
        _plot, _c, dev, dp, ch, _n = self._plots[0]
        s0 = self._store.sample_at_index(dev, dp, ch, int(i0))
        s1 = self._store.sample_at_index(dev, dp, ch, int(i1))
        if s0 is None or s1 is None or s1 <= s0:
            return
        self._link.setXRange(int(s0), int(s1), padding=0)
