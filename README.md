# SWI3S Studio

A **MIPI SoundWire I3S (SWI3S)** suite with three modes, selected from a switcher at
the top of the window:

- **Visualization** — plan a bus: author an Interface + up to 12 data ports, see the
  placed Rows×Columns grid, and catch clashes / spec violations before you build.
- **Timing** — dial in PHY settings: compute the SWI3S setup / hold / Z-handover
  margins and F_max for your bus, term by term.
- **Analysis** — confirm it works: decode a captured SWI3S bus — forwarded-clock
  **PHY1/PHY2** (FBCSE) or the differential **PHY3 (DLV)**, whose bit clock is recovered
  from the row edges by a virtual PLL — reconstruct the Control Data Stream and audio
  payload, and explore it in linked panes — 2D bus grid, per-device register maps, 8b/10b
  symbols, command table, and decoded-audio waveforms with WAV export.

The three modes share one window, one bus-grid renderer, and one workspace file
(capture source + authored config + timing inputs + view state). A Visualizer config
can be pushed into Analysis as the *expected* config (**Compare ▸ Compare with
Visualizer**).

See [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) for a pane-by-pane walkthrough (including
partial captures and finding the SSP), [`docs/architecture.md`](docs/architecture.md) for
the design, and [`docs/TESTING.md`](docs/TESTING.md) for the test strategy.

> **Successor to the SWI3S Traffic Visualizer.** SWI3S Studio grew out of, and
> supersedes, the MIPI SoundWire I3S Traffic Visualizer — the **Visualization** mode
> is that tool, and Studio adds the **Timing** and **Analysis** modes around it. It is
> this repository's next major line (v3); the visualizer's v1/v2 releases remain
> available as their `v1.*` / `v2.*` tags.

## Running

Needs **Python 3.11 or newer**. Everything else — the virtualenv, the data stack (`numpy`,
`pyarrow`), the Qt stack (`PySide6`, `pyqtgraph`) and the C++ decode core — is set up for you:

```bash
./run.sh                              # macOS / Linux
```
```powershell
.\run.ps1                             # Windows (PowerShell) — see the Windows notes below
```

Re-run it any time; the venv and build are reused. Pass `--rebuild` / `-Rebuild` to force a
fresh build of the native core — which is what you want after pulling changes that touch
`native/`. The compiled module isn't tracked in git, and the app reports an "out of date —
rebuild" message if it spots a stale one.

Both launchers pick the newest **3.11–3.13** they can find, because PySide6 / pyarrow / numpy
have no 3.14+ wheels yet and pip would otherwise try to build Arrow from source. On 3.14 use
the manual setup below, once wheels exist for your platform.

**The virtualenv lives outside the repo** — `~/.cache/swi3s-studio/…`, or
`%LOCALAPPDATA%\swi3s-studio\…` on Windows — so it is safe to clone under a
cloud-synced folder (iCloud Drive / OneDrive). A venv on those file systems breaks Qt:
it can't enumerate the plugin directories, so the GUI dies with *"Could not find the Qt
platform plugin"* even though everything is installed. Override the location with
`SWI3S_VENV=/path` (`$env:SWI3S_VENV` on Windows).

That is the whole story on macOS and Windows, and on any Linux that has the Python
development headers. **If a launcher stops with a message about `Python.h` or a C++
compiler**, open the next section: both launchers pre-flight those and name what to
install, rather than failing part-way through a compile.

<details>
<summary><b>Build prerequisites — and building with no root</b> (locked-down build server, EDA/CAD farm, shared VM)</summary>

**Build prerequisites.** The decode core is C++, so building it needs a **C++17
compiler** *and* the **Python development headers** (`Python.h`). The headers are a
separate package on most Linux distributions, and a virtualenv does **not** provide
them — a venv inherits its base prefix, so a venv on a headerless system Python still
has nowhere to find `Python.h`:

```bash
sudo dnf install python3-devel     # RHEL / CentOS / Fedora
sudo apt  install python3-dev      # Debian / Ubuntu
sudo zypper install python3-devel  # SUSE
```

macOS (Xcode command-line tools) and the python.org Windows installers ship the headers
already. If they're missing, the build now stops at configure time and says so; it used
to fail confusingly part-way through compiling with `fatal error: Python.h: No such file
or directory`.

**No root / no sudo?** You don't need it — you need *a* Python that has headers, or a private
copy of them. Pick the first option that works; each is self-contained in your home directory.

`./run.sh` helps here: it prefers an interpreter that has the headers when several are on
`PATH`, and if none does it stops **before** creating the venv or downloading any wheels
and prints these same options. `run.ps1` runs the same pre-flight check on Windows — the
python.org and Store installers both ship headers, but an *embeddable package* zip does
not. Either launcher stands aside if `SKBUILD_CMAKE_DEFINE` already points the build at
headers of your own (Option 2 below).

**Option 0 — check for a Python that already has them.** Managed build hosts often have
several. This prints the header path for whatever `python3` resolves to and whether it's
usable:

```bash
python3 -c 'import sysconfig,os; p=sysconfig.get_paths()["include"]; \
print(p, "OK" if os.path.isfile(p+"/Python.h") else "MISSING Python.h")'
```

Try the alternatives on the box (`module avail python`, `ls /usr/bin/python3.*`,
`scl enable`, a toolchain Python under `/tools` or `/org/...`) and run the same check
against each. If one reports OK, build with it:

```bash
PYTHON=/path/to/that/python3 ./run.sh
```

**Option 1 — install a standalone Python with `uv` (recommended, no root, ~30 s).**
`uv`'s Pythons are self-contained and always include the headers:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs to ~/.local/bin
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.12
PYTHON="$(uv python find 3.12)" ./run.sh
```

Miniforge/conda works the same way (`conda create -n swi3s python=3.12`, then point
`PYTHON` at `$CONDA_PREFIX/bin/python`) — its `include/python3.12` has the headers.

**Option 2 — extract the headers from the RPM, no install.** Keeps your current Python.
`rpm2cpio | cpio` unpacks into the current directory; nothing touches the system. Match
the version *exactly* (`python3 -V`):

```bash
mkdir -p ~/pyhdr && cd ~/pyhdr
# Fetch the python3-devel RPM matching your distro + Python version, e.g. via
#   dnf download --downloadonly --destdir . python3-devel     # if dnf is usable read-only
# or copy it from an internal mirror / another host, then:
rpm2cpio python3-devel-3.12*.rpm | cpio -idmv
ls usr/include/python3.12/Python.h        # confirm before continuing
```

Then point the build at them. Both build paths accept an override:

```bash
# pip / scikit-build-core path (what run.sh uses):
export SKBUILD_CMAKE_DEFINE="Python_INCLUDE_DIR=$HOME/pyhdr/usr/include/python3.12"
./run.sh

# or the offline path, which takes a plain env var:
PYTHON_INCLUDE="$HOME/pyhdr/usr/include/python3.12" bash native/build_local.sh
```

Keep `SKBUILD_CMAKE_DEFINE` exported for later rebuilds, or add it to your shell profile
— `run.sh` rebuilds the core whenever `native/` changes.

**If the GUI then can't start over VNC**, that's a separate (Qt) problem, not the build.
Qt needs a platform plugin; on a bare VNC session try `QT_QPA_PLATFORM=xcb`, and if the
X libraries are absent you can still run headless work (`QT_QPA_PLATFORM=offscreen`) to
confirm the install:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -c \
  'import swi3score; print("decode core OK, abi", swi3score.score_abi)'
```

</details>

**Manual setup**, if you would rather not use a launcher — this is also what CI does:

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

## Tech

PySide6 (Qt 6) + pyqtgraph UI · pybind11 C++ decode core · Apache Arrow + NumPy memmap
results store · macOS-first, cross-platform.

## Layout

```
swi3s_studio/   Python app: ingest, store, model, analysis, dsp, timing, ui
  ui/                  three-mode UI (Visualizer editor, Timing readout, Analysis panes)
  ui/grid_view.py      shared bus-grid renderer (decoded + authored)
  model/bus_config.py  authored Interface + 12 DataPort/FCP + per-source CDS config (CSV)
  swviz/               SWI3S Visualizer engine (placement / clash / warnings)
  timing/              SWI3S PHY timing calculator
native/         swi3score C++ decode core + pybind11 bindings (scikit-build-core)
data/registers.json    SWI3S register map (source of truth)
docs/           USER_GUIDE, DEVELOPMENT, TESTING, architecture, PACKAGING, TECH_DEBT, …
tests/          Python suites + run_all.sh
```

## Opening captures

**File ▸ Analyzer ▸ Open Capture** reads a Logic 2 `.sal` project (internal blob versions
3 and 4, plus the documented v0 Binary export), a digital CSV
(`Time, Ch…` columns), a simulation `.vcd`, a per-channel `<SALEAE>` binary pair, or a
Tektronix analog scope export — a `.wfm` channel pair or an analog `.csv` (volts). Analog
sources are thresholded to logic automatically (mid-rail + 10% hysteresis); digital-vs-
analog CSV is decided by the data values (strictly 0/1 = digital). Clock vs data is
auto-assigned by transition count (the forwarded clock toggles every UI, so it has the
most edges), so file order doesn't matter; select both files of a `.bin`/`.wfm` pair
together to skip the second-file prompt (an analog CSV with more than two channels prompts
for which is clock vs data). Sample rate is auto-detected from timestamps. Large captures
decode on a worker thread behind an n/N progress dialog. A `.sal` too big to hold in
memory is not attempted: the peak is predicted from the ZIP directory before anything is
inflated, and past an absolute budget you are offered a **time window** instead — so a
machine with more RAM does not silently load more. **Open Capture Time Window…** asks for
one on purpose at any size; see `docs/USER_GUIDE.md` ▸ *Large captures*. **File ▸ Load Demo Capture ▸
PHY1/PHY2/PHY3** runs a synthetic capture for the chosen PHY, each with a §5.1.2 Cold Start
spliced in front and the same audio content (so they decode identically): PHY1 a slow
4-column FBCSE bus whose ports are repositioned mid-capture, PHY2 an FBCSE bus stepping
2→8→16 columns, PHY3 the differential DLV variant with a recovered bit clock. All sample at
500 MHz (a real analyzer's fixed rate — a non-integer number of samples per UI).

**File ▸ Analyzer ▸ Open Visualizer Config…** applies a Visualizer config CSV (or the
authored config) to the open capture — decoding with that data-port config from row 0
(for a capture that begins *after* the setup commit, where the port geometry isn't on the
wire to snoop) and/or comparing it against the decode. See the User Guide's *Partial
captures & finding the SSP* for the full workflow.

**File ▸ Analyzer ▸ Locate Sub-Capture…** finds every recurrence of a smaller reference
capture and bookmarks each; **Export .sal…** writes the open capture out as a portable
Logic 2 project.

## Workspaces, compare, export

- **Save / Open Workspace** — a JSON sidecar (capture source, config CSV, SSP row,
  what-if overlay, bookmarks, cursor, authored config, timing inputs, view state);
  results re-decode on open, so it stays portable.
- **Compare** — **File ▸ Analyzer ▸ Open Visualizer Config…** can diff an expected config
  (CSV or the authored config) against the decode: differing grid cells outlined, a
  per-cell report, expected register values overlaid (*Clear Comparison* removes it).
- **Audio ▸ Per-Dataport Scrambler** — override the descrambler per (device, data port):
  Auto / On / Off, then re-decode (fixes a mis-snooped `ScramblerEn`, which otherwise
  turns the stream into noise).
- **Export** — Audio as WAV (choose streams + range, optional band-limited resample),
  Commands as CSV, Bus Grid as SVG/PNG. See [`docs/PACKAGING.md`](docs/PACKAGING.md) for a
  standalone bundle.

## Tests

```bash
bash tests/gate.sh                    # THE release gate: suite + perf + ruff + mypy + ABI
bash tests/gate.sh --quick            # dev loop (skips perf + the process-isolated pass)
bash tests/run_all.sh                 # all Python suites (per-suite summary + counts)
bash tests/run_all.sh --build         # rebuild the core first
bash tests/run_all.sh --perf          # the perf benchmarks instead
bash tests/run_all.sh --native        # also run the native C++ suite
```

`gate.sh` is what a release must pass, on macOS **and** Windows, at the release commit — it
rebuilds the native core and asserts `score_abi`, then runs the suite, the perf gate, the
process-isolated pass, `ruff`, and a mypy non-regression check, reporting every failure
rather than stopping at the first. It needs `ruff` and `mypy` on `PATH`
(`brew install ruff mypy`), and treats a missing tool as a failure rather than a silent skip.

The runner is a pytest wrapper, so it covers exactly what `ci.yml` declares. CI runs on this
repository, and its lint/type job became blocking in 3.0.12 — but it covers neither the perf
gate nor Windows, so the enforced gate remains `tools/gate.py`, run by hand on macOS and
Windows at the release commit. See docs/DEVELOPMENT.md.

A single suite runs directly (GUI suites need the Qt offscreen platform):

```bash
PYTHONPATH=. python3 -m pytest tests/test_swi3score.py
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_gui_smoke.py
```

See [`docs/TESTING.md`](docs/TESTING.md) for the approach and coverage matrix.

## Contributing

This is a MIPI Alliance Open Source Software project. Contributions go through pull
requests reviewed per [`GOVERNANCE.md`](GOVERNANCE.md); non-members must sign the
[CLA](.github/CLA.md) (the CLA Assistant bot prompts on your first PR). The MIPI
Alliance organisation-level contributing guide is shown by GitHub when you open a pull
request, and vulnerabilities go through the repository's **Security** tab, which serves
the organisation-level security policy — please do not file them as public issues. See
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for the branch / CI / review / release
workflow.

## License

BSD 3-Clause License — see [`LICENSE.md`](LICENSE.md).
Copyright (c) MIPI Alliance and other contributors.
