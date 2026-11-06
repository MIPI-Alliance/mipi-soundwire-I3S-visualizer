# PyInstaller spec for SWI3S Studio — cross-platform.
#   macOS:         produces "SWI3S Studio.app"
#   Windows/Linux: produces a "SWI3S Studio" folder with the executable inside
# Build with:  pyinstaller swi3s-studio.spec
#
# Bundles the app, the swi3score extension module (must be importable in the
# build env — `pip install ./native` first), and data/registers.json and data/demos/
# under data/, where the runtime loaders check sys._MEIPASS/data. (init.csv is a user-supplied
# local file, not bundled; Load Init falls back to the built-in demo config.)
import importlib.metadata
import os
import re
import sys

block_cipher = None
HERE = os.path.abspath(".")
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

datas = [(os.path.join(HERE, "data", "registers.json"), "data"),
         # File ▸ Open Demo Capture's workspaces (workspace.demo_dir reads _MEIPASS/data/demos).
         (os.path.join(HERE, "data", "demos"), os.path.join("data", "demos")),
         # The stylesheet's SVGs (checkbox tick, combo chevrons), found beside theme.py
         # (os.path.dirname(__file__)), so they go where the frozen module lives.
         (os.path.join(HERE, "swi3s_studio", "ui", "assets"), os.path.join("swi3s_studio", "ui", "assets")),
         # Help ▸ User Guide renders it (MainWindow.show_user_guide reads _MEIPASS/docs).
         (os.path.join(HERE, "docs", "USER_GUIDE.md"), "docs"),
         # library_dir() finds the bundled example register map under _MEIPASS/data/regmaps.
         (os.path.join(HERE, "data", "regmaps"), os.path.join("data", "regmaps")),
         # The project licence sits at the bundle's top level.
         (os.path.join(HERE, "LICENSE.md"), ".")]

# Each distribution the bundle redistributes (the runtime requirements and what they
# require, transitively) ships its licence and notice files in its metadata. A binary
# redistribution reproduces them, so each file goes under licenses/<distribution>/ in the
# bundle at its path inside the distribution, so that same-named files (a distribution
# bundling the licences of several components) do not overwrite one another. The runtime
# requirements are the names requirements.txt lists, and each must be installed.
# Every wheel of the Qt for Python project (shiboken6, PySide6 and each PySide6-* wheel
# it requires) carries none: their notices are kept in data/licenses/Qt-for-Python/ and
# shipped as licenses/Qt-for-Python/. The bundle holds only the Qt modules the app
# imports. A runtime distribution with no notice from either source stops the build.
# The app imports QtMultimedia, whose PySide6 libraries include the FFmpeg libraries
# (libavcodec, libavformat, libavutil, libswresample, libswscale); the bundle then also ships
# data/licenses/FFmpeg/ as licenses/FFmpeg/. The bundle freezes the building interpreter and
# its standard library, so that interpreter's own licence text ships as licenses/Python/; an
# interpreter whose installation holds none stops the build.
_REQUIREMENTS = os.path.join(HERE, "requirements.txt")
_NOTICE_FILE = re.compile(r"(^|[/\\])(licen[cs]es?|copying|notice|authors)([-_.][^/\\]*)?$", re.I)


def _canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _runtime_requirements(path=_REQUIREMENTS):
    """The distribution names a requirements file lists, in order."""
    names = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                names.append(re.split(r"[\s;<>=!~(\[]", line, maxsplit=1)[0])
    return tuple(names)


_RUNTIME = _runtime_requirements()


_QT_MULTIMEDIA_IMPORT = re.compile(r"^\s*(from|import)\s+PySide6\.QtMultimedia\b", re.M)


def _imports_qt_multimedia(root=HERE):
    """True when a module of the application package imports PySide6.QtMultimedia."""
    for base, _dirs, files in os.walk(os.path.join(root, "swi3s_studio")):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(base, name), encoding="utf-8") as f:
                    if _QT_MULTIMEDIA_IMPORT.search(f.read()):
                        return True
    return False


def _repo_notices(key):
    """The data/licenses/ project holding the notices of canonical distribution `key`, or
    None when its own metadata must carry them."""
    if key == "shiboken6" or key == "pyside6" or key.startswith("pyside6-"):
        return "Qt-for-Python"
    return None


def _runtime_closure():
    seen, todo, missing = {}, [(name, True) for name in _RUNTIME], []
    while todo:
        name, top = todo.pop()
        key = _canon(name)
        if key in seen:
            continue
        try:
            dist = importlib.metadata.distribution(key)
        except importlib.metadata.PackageNotFoundError:
            if top:
                missing.append(name)
            continue                       # a dependency its marker leaves out
        seen[key] = dist
        for req in dist.requires or []:
            if "extra ==" in req:
                continue
            todo.append((re.split(r"[\s;<>=!~(\[]", req.strip(), maxsplit=1)[0], False))
    if missing:
        raise SystemExit("runtime requirement not installed: " + ", ".join(sorted(missing)))
    return seen.values()


def _python_licence():
    """The licence text of the interpreter the bundle freezes, or None: LICENSE.txt in its
    standard library or at its installation prefix, or the copyright file of a Debian-family
    system interpreter."""
    import sys
    import sysconfig
    version = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    for path in (os.path.join(sysconfig.get_path("stdlib"), "LICENSE.txt"),
                 os.path.join(sys.base_prefix, "LICENSE.txt"),
                 os.path.join(sys.base_prefix, "share", "doc", version, "copyright")):
        if os.path.isfile(path):
            return path
    return None


def _notice_datas():
    out, bare, repo = [], [], set()
    licence = _python_licence()
    if licence is None:
        bare.append("Python (the interpreter's installation holds no licence text)")
    else:
        out.append((licence, os.path.join("licenses", "Python")))
    for dist in _runtime_closure():
        name = dist.metadata["Name"]
        found = 0
        for f in dist.files or []:
            if (".." in f.parts or not _NOTICE_FILE.search(str(f))
                    or f.suffix.lower() not in ("", ".txt", ".md", ".rst")):
                continue
            src = dist.locate_file(f)
            if os.path.isfile(src):
                found += 1
                out.append((str(src), os.path.join("licenses", name, str(f.parent))))
        project = _repo_notices(_canon(name))
        if project:
            repo.add(project)
        elif not found:
            bare.append(name)
    if _imports_qt_multimedia():
        repo.add("FFmpeg")
    for project in sorted(repo):
        held = os.path.join(HERE, "data", "licenses", project)
        if not os.path.isdir(held) or not os.listdir(held):
            bare.append(f"{project} (data/licenses/{project} is missing or empty)")
        out.append((held, os.path.join("licenses", project)))
    if bare:
        raise SystemExit("no licence or notice file for: " + ", ".join(sorted(bare)))
    return out


datas += _notice_datas()

# The compiled swi3score extension (.pyd on Windows, .so elsewhere) is a top-level module;
# hiddenimports below carries it into the bundle.
binaries = []

# Optional app icon, if an asset is present (none is required to build).
_icon = os.path.join(HERE, "data", "icon.ico" if IS_WIN else "icon.icns")
icon = _icon if os.path.exists(_icon) else None

a = Analysis(
    [os.path.join("swi3s_studio", "__main__.py")],
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
    app = BUNDLE(coll, name="SWI3S Studio.app", icon=icon)
