# Packaging SWI3S Studio

Two artifacts: the **`swi3score`** native wheel (the C++ decode core) and the
**`swi3s-studio`** Python app on top of it. Build the core first.

## 1. Build / install the decode core

```bash
pip install ./native            # scikit-build-core + pybind11 -> swi3score wheel
# offline fallback (no PyPI): ./native/build_local.sh  (puts the .so at repo root)
```

## 2. Run from source

```bash
pip install -e .                # installs deps + the `swi3s-studio` entry point
swi3s-studio                    # or:  python -m swi3s_studio.app --demo
```

`data/registers.json` is the single source of truth for the register map; the
loader (`model/registers.py`) finds it via, in order: `$SWI3S_REGISTERS`, the
repo `data/` dir, the package dir, `sys.prefix/share/swi3s-studio/`, or a
PyInstaller bundle's `data/`.

The demo workspaces File ▸ Open Demo Capture applies (`data/demos/*.swi3s`) are found
the same way by `workspace.demo_dir`: the repo `data/demos`, the package dir,
`sys.prefix/share/swi3s-studio/demos/`, or a PyInstaller bundle's `data/demos`.

A wheel (`pip install .`) carries only what `[tool.setuptools]` names: the packages, the
theme's icons (`package-data`, `ui/assets/*.svg`), the register data and the demo
workspaces (`data-files`).
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
`data/registers.json` and `data/demos/`. Drop a `data/icon.ico` (Windows) or `data/icon.icns` (macOS)
to give it an icon — the spec picks it up if present. (`briefcase` is an
alternative if you prefer per-OS installers.)

## 4. Cross-platform CI (sketch)

Per OS (macOS/Windows/Linux), in a matrix:

1. `pip install ./native` (builds the native wheel with the platform toolchain).
2. `pip install -e . pytest`.
3. Run the headless suite (set `QT_QPA_PLATFORM=offscreen`).
4. `pyinstaller swi3s-studio.spec`; upload `dist/` as an artifact.

The decode core is plain C++17 + pybind11 (no Saleae SDK), so it cross-compiles
cleanly; the same sources also build the Saleae Logic 2 plugin.
