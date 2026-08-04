# PyInstaller spec for SWI3S Studio — cross-platform.
#   macOS:         produces "SWI3S Studio.app"
#   Windows/Linux: produces a "SWI3S Studio" folder with the executable inside
# Build with:  pyinstaller swi3s-studio.spec
#
# Bundles the app, the swi3score extension module (must be importable in the
# build env — `pip install ./native` first), and data/registers.json under data/
# where the runtime loader checks sys._MEIPASS/data. (init.csv is a user-supplied
# local file, not bundled; Load Init falls back to the built-in demo config.)
import os
import sys
from PyInstaller.utils.hooks import collect_dynamic_libs

block_cipher = None
HERE = os.path.abspath(".")
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

datas = [(os.path.join(HERE, "data", "registers.json"), "data")]
# Ship the compiled swi3score extension (.pyd on Windows, .so/.dylib elsewhere)
# plus any shared libs it links, alongside the app.
binaries = collect_dynamic_libs("swi3score")

# Optional app icon, if an asset is present (none is required to build).
_icon = os.path.join(HERE, "data", "icon.ico" if IS_WIN else "icon.icns")
icon = _icon if os.path.exists(_icon) else None

a = Analysis(
    [os.path.join("swi3s_studio", "app.py")],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=["swi3score"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SWI3S Studio",
          console=False, icon=icon)            # console=False → GUI app (no console on Windows)
coll = COLLECT(exe, a.binaries, a.datas, name="SWI3S Studio")

# Wrap the COLLECT as a macOS .app only on macOS; BUNDLE does nothing on other
# platforms, where the COLLECT folder (containing the "SWI3S Studio" executable) is
# itself the distributable.
if IS_MAC:
    app = BUNDLE(coll, name="SWI3S Studio.app", icon=icon,
                 bundle_identifier="com.example.swi3sstudio")
