# SWI3S Studio

A **MIPI SoundWire I3S (SWI3S)** suite with three modes, selected from a switcher at
the top of the window:

- **Visualization** — plan a bus: author an Interface + up to 12 data ports, see the
  placed Rows×Columns grid, and catch clashes / spec violations before you build.
- **Timing** — dial in PHY settings: compute the SWI3S setup / hold / Z-handover
  margins and F_max for your bus, term by term.
- **Analysis** — confirm it works: decode a captured PHY2 bus (clock + bidirectional
  data), reconstruct the Control Data Stream and audio payload, and explore it in
  linked panes — 2D bus grid, per-device register maps, 8b/10b symbols, command table,
  and decoded-audio waveforms with WAV export.

The three modes share one window, one bus-grid renderer, and one workspace file
(capture source + authored config + timing inputs + view state). A Visualizer config
can be pushed into Analysis as the *expected* config (**Compare ▸ Compare with
Visualizer**).

See [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) for a pane-by-pane walkthrough (including
partial captures and finding the SSP), [`architecture.md`](architecture.md) for the
design, and [`docs/TESTING.md`](docs/TESTING.md) for the test strategy.

> **Successor to the SWI3S Traffic Visualizer.** SWI3S Studio grew out of, and
> supersedes, the MIPI SoundWire I3S Traffic Visualizer — the **Visualization** mode
> is that tool, and Studio adds the **Timing** and **Analysis** modes around it. It is
> this repository's next major line (v3); the visualizer's v1/v2 releases remain
> available as their `v1.*` / `v2.*` tags.

## Tech

PySide6 (Qt 6) + pyqtgraph UI · pybind11 C++ decode core · Apache Arrow + NumPy memmap
results store · macOS-first, cross-platform.

## Layout

```
swi3s_studio/   Python app: ingest, store, model, analysis, dsp, timing, ui
  ui/                  three-mode UI (Visualizer editor, Timing readout, Analysis panes)
  ui/grid_view.py      shared bus-grid renderer (decoded + authored)
  model/bus_config.py  authored Interface + 12 DataPort/FCP config (v2.0 CSV)
  swviz/               SWI3S Visualizer engine (placement / clash / warnings)
  timing/              SWI3S PHY timing calculator
native/         swi3score C++ decode core + pybind11 bindings (scikit-build-core)
data/registers.json    SWI3S register map (source of truth)
docs/           USER_GUIDE, TESTING, saleae_sal_format, STYLE_GUIDE
tests/          Python suites + run_all.sh
architecture.md
```

## Running

Requires Python 3.11+ with the data stack (`numpy`, `pyarrow`) and Qt GUI stack
(`PySide6`, `pyqtgraph`).

**Quick start (recommended).** A launch script does the whole setup — creates a
`.venv`, installs the deps, builds the decode core, and starts the app. Re-run it any
time; the venv and build are reused (pass `--rebuild` / `-Rebuild` to force a fresh
build of the native core):

```bash
./run.sh                              # macOS / Linux
```
```powershell
.\run.ps1                             # Windows (PowerShell) — see the Windows note below
```

**Manual.** From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows:  .venv\Scripts\activate
python3 -m pip install -r requirements.txt
python3 -m pip install ./native      # build the decode core (offline: bash native/build_local.sh)
PYTHONPATH=. python3 -m swi3s_studio.app
```

After pulling changes that touch `native/`, rebuild the core
(`python3 -m pip install ./native`). The compiled module isn't tracked in git; the app
reports an "out of date — rebuild" message if it detects a stale build.

### Windows

- **Run the launcher.** `.\run.ps1` from a PowerShell prompt in the repo root does the
  full setup. If you see *"running scripts is disabled on this system"*, either allow
  local scripts once for your user (this only trusts local scripts — remote/unsigned
  ones still require a signature):

  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  ```

  or run it without changing the policy: `powershell -ExecutionPolicy Bypass -File .\run.ps1`.
- **A C++ compiler is needed to build the decode core.** Install the free
  **[Visual Studio Build Tools](https://visualstudio.microsoft.com/downloads/)** with
  the *"Desktop development with C++"* workload (MSVC + Windows SDK). Without it,
  `pip install .\native` fails with a "Microsoft Visual C++ 14.0 or greater is required"
  error.
- **Use the `py` launcher** to get a supported version if several Pythons are installed.
  `run.ps1` auto-selects the newest **3.11–3.13** (PySide6/pyarrow/numpy have no 3.14+
  wheels yet, so a 3.14 default would fail to install).
- **Windows on ARM (Parallels, Surface Pro X).** Install the **x64** build of Python, not
  the arm64 one: `pyarrow` ships no `win_arm64` wheel, so pip would try (and fail) to
  build Arrow from source. Windows-on-ARM runs x64 Python fine under emulation, and all
  wheels resolve as `win_amd64`. Get it from python.org (the "Windows installer (64-bit)")
  or `winget install --id Python.Python.3.12 --architecture x64`.
- **Long paths / OneDrive.** Cloning under a deeply nested or cloud-synced folder can
  trip the build or file locks; a short local path like `C:\src\swi3s-studio` is safest.
- **Running the tests on Windows.** Install `pytest` (one suite uses it) and set
  `PYTHONUTF8=1` so the suites' Unicode console output (e.g. `▸`, `→`) doesn't trip the
  legacy cp1252 code page when stdout is redirected:
  `$env:PYTHONUTF8=1; .venv\Scripts\python -m pytest` (or run individual `tests\test_*.py`).


## Opening captures

**File ▸ Open Capture** reads a Logic 2 `.sal` project, a digital CSV (`Time, Ch…`
columns), or a per-channel `<SALEAE>` binary pair. Clock vs data is auto-assigned by
transition count (the forwarded clock toggles every UI, so it has the most edges), so
file order doesn't matter; select both `.bin` files together to skip the second-file
prompt. Sample rate is auto-detected from binary timestamps. Large captures decode on a
worker thread behind a progress dialog. **File ▸ Load Demo** runs a synthetic capture:
two devices × two stereo data ports on a 16-column grid with a §5.1.2 Cold Start in front.

**File ▸ Open Capture with Config CSV…** opens a capture plus a Visualizer config CSV
and decodes with that data-port config from row 0 — for a capture that begins *after*
the setup commit, where the port geometry isn't on the wire to snoop. See the User
Guide's *Partial captures & finding the SSP* for the full workflow.

## Workspaces, compare, export

- **Save / Open Workspace** — a JSON sidecar (capture source, config CSV, SSP row,
  what-if overlay, bookmarks, cursor, authored config, timing inputs, view state);
  results re-decode on open, so it stays portable.
- **Compare** — diff an expected config (CSV or the authored config) against the decode:
  differing grid cells outlined, a per-cell report, expected register values overlaid.
- **Audio ▸ Per-Dataport Scrambler** — override the descrambler per (device, data port):
  Auto / On / Off, then re-decode (fixes a mis-snooped `ScramblerEn`, which otherwise
  turns the stream into noise).
- **Export** — Audio as WAV (choose streams + range, optional band-limited resample),
  Commands as CSV, Bus Grid as SVG/PNG. See [`PACKAGING.md`](PACKAGING.md) for a
  standalone bundle.

## Tests

```bash
bash tests/run_all.sh                 # all Python suites (exit-code pass/fail summary)
bash tests/run_all.sh --build         # rebuild the core first
bash tests/run_all.sh --native        # also run the native C++ suite
```

A single suite runs directly (GUI suites need the Qt offscreen platform):

```bash
PYTHONPATH=. python3 tests/test_swi3score.py
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_gui_smoke.py
```

See [`docs/TESTING.md`](docs/TESTING.md) for the approach and coverage matrix.

## Contributing

This is a MIPI Alliance Open Source Software project. Contributions go through pull
requests reviewed per [`GOVERNANCE.md`](GOVERNANCE.md); non-members must sign the
[CLA](.github/CLA.md) (the CLA Assistant bot prompts on your first PR). See
[`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md) for the full flow and
[`.github/SECURITY.md`](.github/SECURITY.md) for reporting vulnerabilities.

## License

BSD 3-Clause License — see [`LICENSE.md`](LICENSE.md).
Copyright (c) MIPI Alliance and other contributors.

