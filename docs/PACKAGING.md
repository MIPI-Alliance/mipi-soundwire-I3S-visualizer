# Packaging SWI3S Studio

Two artifacts: the **`swi3score`** native wheel (the C++ decode core) and the
**`swi3s-studio`** Python app on top of it. Build the core first.

## 1. Build / install the decode core

```bash
pip install ./native            # scikit-build-core + pybind11 -> swi3score wheel
# offline fallback (no PyPI): bash native/build_local.sh  (puts the .so at repo root)
```

## 2. Run from source

```bash
python3 -m pip install ./native # swi3score first
python3 -m pip install -e .     # installs deps + the console scripts
swi3s-studio                    # or:  python -m swi3s_studio.app --demo
```

`pyproject.toml` does not list `swi3score`, so the core is always installed first.
`swi3s-studio-cli` is the console script for headless use
(`-c config.csv -o model.json`). Its exit codes are 0 (done), 1 (an error) and 2 (the model
was written and contains a bus clash or a truncated DRQ). `swi3s-studio` is the GUI script,
which has no console on Windows.

`data/registers.json` is the single source of truth for the register map; the
loader (`model/registers.py`) finds it via, in order: `$SWI3S_REGISTERS`, a
PyInstaller bundle's `data/`, the repo `data/` dir, the package dir, then
`sys.prefix/share/swi3s-studio/`.

The demo workspaces File ▸ Open Demo Capture applies (`data/demos/*.swi3s`) are found
in the same order by `workspace.demo_dir`: a PyInstaller bundle's `data/demos`, the repo
`data/demos`, the package dir, then `sys.prefix/share/swi3s-studio/demos/`.

A wheel (`pip install .`) carries only what `[tool.setuptools]` names: the packages, the
theme's icons (`package-data`, `ui/assets/*.svg`), the register data, the example register map
(`data/regmaps/example_amp.json`), the demo workspaces and `docs/USER_GUIDE.md`
(`data-files`, installed under `share/swi3s-studio/` and `share/swi3s-studio/docs`).
A new file read from beside a module needs a `package-data` glob;
`tests/test_release_gate.py` fails until it has one.

## 3. Standalone desktop bundle (PyInstaller)

```bash
pip install ./native            # swi3score must be importable in the build env
pip install pyinstaller
pyinstaller swi3s-studio.spec    # macOS -> dist/"SWI3S Studio.app";
                                 # Windows/Linux -> dist/"SWI3S Studio"/ (run the exe inside)
```

The spec is cross-platform: it wraps the build as a `.app` only on macOS (a plain
COLLECT folder on Windows/Linux) and is a GUI app (no console window on Windows).
It bundles the app, the `swi3score` extension (`.pyd`/`.so`/`.dylib`),
`data/registers.json`, `data/demos/`, the example register map (`data/regmaps/`), the User
Guide (`docs/USER_GUIDE.md`) and the UI assets (`swi3s_studio/ui/assets/`); the spec's `datas`
list is the list of record. It carries `LICENSE.md` at its top level and the
licence and notice files of the runtime distributions under
`licenses/<distribution>/<path inside the distribution>/`, so same-named files all ship. The
runtime distributions are the names `requirements.txt` lists and what they require; each
listed one must be installed, or the spec stops. Every wheel of the Qt for Python project
(shiboken6, PySide6 and each PySide6-* wheel it requires, such as PySide6_WebEngine and
PySide6_Pdf) carries no notice, so their notices are kept in `data/licenses/Qt-for-Python/`
and ship as `licenses/Qt-for-Python/`; the bundle holds only the Qt modules the app imports.
The app imports QtMultimedia, whose PySide6 libraries include the FFmpeg libraries (libavcodec,
libavformat, libavutil, libswresample, libswscale; LGPL-2.1-or-later), so while any module of
`swi3s_studio/` imports `PySide6.QtMultimedia` the spec also ships `data/licenses/FFmpeg/`
(`NOTICE.txt`, `LGPL-2.1.txt`) as `licenses/FFmpeg/`.
The bundle freezes the building interpreter and its standard library, so the spec also ships
that interpreter's licence text as `licenses/Python/`: `LICENSE.txt` from its standard library
directory or its installation prefix, or `share/doc/pythonX.Y/copyright` for a Debian-family
system interpreter. The spec stops when the interpreter's installation holds none of them.
The spec stops when a runtime distribution has no notice file from either source.

The macOS bundle identifier is derived by PyInstaller; set `bundle_identifier` in the spec
to the publisher's own reverse-DNS identifier before distributing.

Drop a `data/icon.ico` (Windows) or `data/icon.icns` (macOS) to give the bundle an icon —
the spec picks it up if present. (`briefcase` is an alternative if you prefer per-OS
installers.)

## 4. Cross-platform CI

`.github/workflows/ci.yml` builds the native core per OS and runs the headless suite;
`docs/DEVELOPMENT.md`, "CI", says what it runs and what it leaves to the local gate. It
builds no PyInstaller bundle: run `pyinstaller swi3s-studio.spec` by hand (section 3).

The decode core is plain C++17 + pybind11, so it cross-compiles cleanly.
