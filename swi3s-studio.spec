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
         # The project licence, beside the licenses/ directory.
         (os.path.join(HERE, "LICENSE.md"), ".")]

# Each distribution the bundle redistributes (the runtime requirements and what they
# require, transitively) ships its licence and notice files in its metadata. A binary
# redistribution reproduces them, so each file goes under licenses/<distribution>/ in the
# bundle at its path inside the distribution, so that same-named files (a distribution
# bundling the licences of several components) do not overwrite one another. The runtime
# requirements are the names requirements.txt lists, and each must be installed.
# The notices of every wheel of the Qt for Python project (shiboken6, PySide6 and each
# PySide6-* wheel it requires) are kept in data/licenses/Qt-for-Python/ and none of the
# wheels' own files ships (the Windows wheels carry a commercial-licence statement,
# dist-info/licenses/LicenseRef-Qt-Commercial.txt; this bundle uses the LGPL-3.0 option). They are
# shipped as licenses/Qt-for-Python/: the Qt for Python notice, the licence texts it names,
# and THIRD-PARTY.txt, the third-party components the Qt libraries and plugins contain. The
# bundle holds the Qt modules the app imports and
# the ones they load (_UNSHIPPED_QT, below). A runtime distribution with no notice from either
# source, or a data/licenses/ project lacking a file _REQUIRED_NOTICES names, stops the build.
# The Qt for Python notice names the release series it was reviewed for and the licence the
# wheels state; a Qt for Python wheel of another series, or stating another licence, stops
# the build. tools/bundle_constraints.txt pins a reviewed release.
# The app imports QtMultimedia, whose PySide6 libraries include the FFmpeg libraries
# (libavcodec, libavformat, libavutil, libswresample, libswscale); the bundle then also ships
# data/licenses/FFmpeg/ as licenses/FFmpeg/. The bundle freezes the building interpreter and
# its standard library, so that interpreter's own licence text ships as licenses/Python/; an
# interpreter whose installation holds none stops the build. That licence does not cover the
# OpenSSL and ncurses libraries an interpreter's ssl, hashlib, readline and curses extensions
# may link: when Analysis collects them, their notices in data/licenses/Python-extras/ ship as
# licenses/Python-extras/ (_interpreter_notices, applied to a.binaries after Analysis), and an
# OpenSSL release whose notice it does not hold stops the build. The bundle leaves out the
# readline module (Analysis' excludes); a GNU readline library (GPL) that Analysis still
# collects stops the build.
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


# The files each data/licenses/ project must hold; one that lacks any stops the build.
# THIRD-PARTY.txt lists the third-party components the Qt libraries and plugins contain.
_REQUIRED_NOTICES = {
    "Qt-for-Python": ("NOTICE.txt", "LGPL-3.0.txt", "GPL-3.0.txt", "THIRD-PARTY.txt"),
    "FFmpeg": ("NOTICE.txt", "LGPL-2.1.txt"),
}


_QT_NOTICE = os.path.join(HERE, "data", "licenses", "Qt-for-Python", "NOTICE.txt")
_QT_REVIEWED = re.compile(r"^Qt for Python versions reviewed for this notice: (.+)$", re.M)
_SERIES = re.compile(r"^(\d+)\.(\d+)$")


def _qt_reviewed(path=_QT_NOTICE):
    """(the (major, minor) series the Qt for Python notice was reviewed for, its text)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    found = _QT_REVIEWED.findall(text)
    if len(found) != 1:
        raise SystemExit(f"{path} must carry exactly one 'reviewed for this notice' line")
    series = set()
    for item in found[0].split(","):
        m = _SERIES.match(item.strip())
        if not m:
            raise SystemExit(f"{path}: {item.strip()!r} is not a major.minor release series")
        series.add((int(m.group(1)), int(m.group(2))))
    return series, text


def _unreviewed_qt(dists):
    """The Qt for Python distributions, with the reason, that the notice was not reviewed for:
    a release series it does not name, or a licence statement it does not contain."""
    series, text = _qt_reviewed()
    out = []
    for dist in dists:
        if _repo_notices(_canon(dist.metadata["Name"])) != "Qt-for-Python":
            continue
        name, version = dist.metadata["Name"], dist.version
        m = re.match(r"(\d+)\.(\d+)", version)
        licence = dist.metadata.get("License-Expression") or dist.metadata.get("License")
        if not m or (int(m.group(1)), int(m.group(2))) not in series:
            out.append(f"{name} {version} (release series not reviewed)")
        elif not licence or licence not in text:
            out.append(f"{name} {version} (licence {licence!r} not in the notice)")
    return out


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


# The distributions a build environment may hold beside the runtime closure: the build tools,
# swi3score, and the build tools of one platform. Any other installed distribution stops the build, since PyInstaller's
# hooks collect what the frozen code can reach: build in a fresh virtual environment
# (docs/PACKAGING.md, section 3). SWI3S_BUNDLE_ALLOW_EXTRA names, comma-separated, the extra
# distributions a build accepts; each is matched by name.
_BUILD_DISTRIBUTIONS = ("pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging",
                        "setuptools", "pip", "wheel", "swi3score")
_PLATFORM_BUILD_TOOLS = {"darwin": ("macholib",), "win32": ("pefile", "pywin32-ctypes")}
_ALLOW_EXTRA = "SWI3S_BUNDLE_ALLOW_EXTRA"


def _search_path():
    """The directories the build's distributions are read from: sys.path, and the user
    site-packages directory when the interpreter enables it."""
    import site
    import sys
    path = list(sys.path)
    if site.ENABLE_USER_SITE:
        user = site.getusersitepackages()
        if user not in path:
            path.append(user)
    return path


def _extra_distributions(closure, dists=None, platform=None, environ=None):
    """The names of the installed distributions outside the runtime closure, the build tools and
    the ones SWI3S_BUNDLE_ALLOW_EXTRA names, sorted."""
    import sys
    if dists is None:
        dists = importlib.metadata.distributions(path=_search_path())
    environ = os.environ if environ is None else environ
    allowed = {_canon(d.metadata["Name"]) for d in closure}
    allowed.update(_BUILD_DISTRIBUTIONS, _PLATFORM_BUILD_TOOLS.get(platform or sys.platform, ()))
    allowed.update(_canon(n.strip()) for n in environ.get(_ALLOW_EXTRA, "").split(",") if n.strip())
    extra = set()
    for dist in dists:
        name = dist.metadata["Name"]
        if not name:
            extra.add(f"an unnamed distribution at {getattr(dist, '_path', '?')}")
        elif _canon(name) not in allowed:
            extra.add(name)
    return sorted(extra, key=str.lower)


def _refuse_extra_distributions(closure):
    extra = _extra_distributions(closure)
    if extra:
        raise SystemExit(
            "the build environment holds distributions outside the runtime requirements, "
            "swi3score and the build tools: " + ", ".join(extra) + ". PyInstaller collects what "
            "the frozen code can reach from them. Build in a fresh virtual environment as "
            f"docs/PACKAGING.md, section 3, describes, or name each one to keep in {_ALLOW_EXTRA} "
            "(comma-separated).")


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


# OpenSSL's libssl and libcrypto as macOS, Windows and Linux name them, with the release
# (major, minor) their name carries: libssl.1.1.dylib, libcrypto-1_1-x64.dll, libssl.so.3.
_OPENSSL_LIB = re.compile(
    r"^lib(?:ssl|crypto)(?:[-.](\d+)(?:[._](\d+))?(?:[._]\d+)*(?:-x64|-arm64)?)?\.(?:dylib|dll)$"
    r"|^lib(?:ssl|crypto)\.so(?:\.(\d+)(?:\.(\d+))?(?:\.\d+)*)?$", re.I)
# ncurses' libraries: libncursesw.5.dylib, libtinfo.so.6, libpanelw.so.6. GNU readline's:
# libreadline.8.dylib, libreadline.so.8.
_NCURSES_LIB = re.compile(
    r"^lib(?:ncurses|tinfo|panel|form|menu)(?:w|t|tw)?(?:[.-]\d+)*\.(?:dylib|so)(?:\.[\d.]+)?$", re.I)
_GNU_READLINE_LIB = re.compile(r"^libreadline(?:[.-]\d+)*\.(?:dylib|so|dll)(?:\.[\d.]+)?$", re.I)
# The data/licenses/Python-extras/ notice of each OpenSSL release the bundle may carry, by
# (major, minor), or by (major, None) for every release of that major.
_OPENSSL_NOTICES = {("1", "1"): "OpenSSL-1.1.txt", ("3", None): "OpenSSL-3.txt"}


def _interpreter_notices(binaries):
    """The data/licenses/Python-extras/ notices, as (source, bundle directory), of the OpenSSL
    and ncurses libraries among `binaries`, Analysis' (bundle path, source path, kind) entries.
    A library whose notice is not held, or a GNU readline library, stops the build."""
    held = os.path.join(HERE, "data", "licenses", "Python-extras")
    names = set()
    for entry in binaries:
        names.update(os.path.basename(str(p).replace("\\", "/")) for p in entry[:2])
    out, bare, refused = [], [], []
    for name in sorted(names):
        ssl, curses = _OPENSSL_LIB.match(name), _NCURSES_LIB.match(name)
        if _GNU_READLINE_LIB.match(name):
            refused.append(name)
            continue
        if ssl:
            release = (ssl.group(1), ssl.group(2)) if ssl.group(1) else (ssl.group(3), ssl.group(4))
            notice = _OPENSSL_NOTICES.get(release) or _OPENSSL_NOTICES.get((release[0], None))
            if notice is None:
                shown = ".".join(r for r in release if r) or "release not identified"
                bare.append(f"{name} (OpenSSL {shown}: data/licenses/Python-extras/ holds no notice)")
                continue
        elif curses:
            notice = "ncurses.txt"
        else:
            continue
        path = os.path.join(held, notice)
        if not os.path.isfile(path):
            bare.append(f"{name} (data/licenses/Python-extras/{notice} is missing)")
        elif (path, os.path.join("licenses", "Python-extras")) not in out:
            out.append((path, os.path.join("licenses", "Python-extras")))
    problems = []
    if refused:
        problems.append("the bundle collects GNU readline (GPL): " + ", ".join(refused)
                        + "; the interpreter's readline module must link libedit, or be excluded")
    if bare:
        problems.append("no licence or notice file for: " + ", ".join(bare))
    if problems:
        raise SystemExit("; ".join(problems))
    return out


def _notice_datas():
    out, bare, repo = [], [], set()
    licence = _python_licence()
    if licence is None:
        bare.append("Python (the interpreter's installation holds no licence text)")
    else:
        out.append((licence, os.path.join("licenses", "Python")))
    closure = list(_runtime_closure())
    unreviewed = _unreviewed_qt(closure)
    if unreviewed:
        raise SystemExit("data/licenses/Qt-for-Python/NOTICE.txt was not reviewed for: "
                         + ", ".join(sorted(unreviewed)))
    for dist in closure:
        name = dist.metadata["Name"]
        project = _repo_notices(_canon(name))
        if project:
            repo.add(project)
            continue
        found = 0
        for f in dist.files or []:
            if (".." in f.parts or not _NOTICE_FILE.search(str(f))
                    or f.suffix.lower() not in ("", ".txt", ".md", ".rst")):
                continue
            src = dist.locate_file(f)
            if os.path.isfile(src):
                found += 1
                out.append((str(src), os.path.join("licenses", name, str(f.parent))))
        if not found:
            bare.append(name)
    if _imports_qt_multimedia():
        repo.add("FFmpeg")
    for project in sorted(repo):
        held = os.path.join(HERE, "data", "licenses", project)
        if not os.path.isdir(held) or not os.listdir(held):
            bare.append(f"{project} (data/licenses/{project} is missing or empty)")
        else:
            lacking = [f for f in _REQUIRED_NOTICES.get(project, ())
                       if not os.path.isfile(os.path.join(held, f))]
            if lacking:
                bare.append(f"{project} (data/licenses/{project} lacks {', '.join(lacking)})")
        out.append((held, os.path.join("licenses", project)))
    if bare:
        raise SystemExit("no licence or notice file for: " + ", ".join(sorted(bare)))
    return out


datas += _notice_datas()
_refuse_extra_distributions(list(_runtime_closure()))

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
    excludes=["tkinter", "readline"],
    cipher=block_cipher,
)
# The Qt modules the app imports are QtCore, QtGui, QtWidgets, QtSvg and QtMultimedia, which
# loads QtNetwork; pyqtgraph imports QtOpenGL, QtOpenGLWidgets and QtTest. PyInstaller's Qt
# hooks collect whole plugin directories, and the virtual-keyboard input-context plugin and
# the PDF image-format plugin bring QtVirtualKeyboard, QtQuick, QtQml and QtPdf with them.
# Those plugins and modules are left out of the bundle by name.
_UNSHIPPED_QT = re.compile(
    r"(?i)(qtvirtualkeyboard|qt6?quick|qt6?qml|qt6?pdf|qpdf|qtuiotouchplugin)")
a.binaries = [b for b in a.binaries if not _UNSHIPPED_QT.search(b[0].replace("\\", "/"))]
a.datas = [d for d in a.datas if not _UNSHIPPED_QT.search(d[0].replace("\\", "/"))]
# The notices of the interpreter's OpenSSL and ncurses libraries that Analysis collected.
a.datas += [(os.path.join(dest, os.path.basename(src)), src, "DATA")
            for src, dest in _interpreter_notices(a.binaries)]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SWI3S Studio",
          console=False, icon=icon)            # console=False → GUI app (no console on Windows)
coll = COLLECT(exe, a.binaries, a.datas, name="SWI3S Studio")

# Wrap the COLLECT as a macOS .app only on macOS; BUNDLE does nothing on other
# platforms, where the COLLECT folder (containing the "SWI3S Studio" executable) is
# itself the distributable.
if IS_MAC:
    app = BUNDLE(coll, name="SWI3S Studio.app", icon=icon)
