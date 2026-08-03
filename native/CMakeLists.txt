cmake_minimum_required(VERSION 3.15)
project(swi3score LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)

# Decode is per-clock-edge (millions of UIs per capture), so it must be built
# optimised. Default to a Release (-O3) build when the caller didn't pick a type,
# and enable link-time optimisation so the hot loop — which spans several
# translation units (Decoder -> CPayloadEngine -> CDataPort -> C8b10bDecoder) —
# can inline across them. (No -march=native: keep the wheel portable; it measured
# no gain here anyway.)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()
include(CheckIPOSupported)
check_ipo_supported(RESULT _ipo_ok OUTPUT _ipo_msg)

# Use CMake's modern FindPython rather than pybind11's legacy
# FindPythonInterp/FindPythonLibs shim. Two reasons, both bugs we hit in the field:
#
#  * The legacy finder locates the interpreter and the LIBRARY but never checks for
#    the development HEADERS, so on a machine without python3-devel installed it
#    configures happily, hands the compiler an include dir with no Python.h in it,
#    and the build dies ~20 lines into compiling bindings.cpp with
#    "fatal error: Python.h: No such file or directory". FindPython's
#    Development.Module component checks the headers, so a missing -devel package
#    fails AT CONFIGURE TIME with a message that names what's missing.
#  * It resolves against the venv's BASE prefix. A venv on a system python whose
#    headers aren't installed (/usr/include/python3.12) therefore looks fine to
#    CMake and isn't.
#
# pybind11 only defaults to FindPython when policy CMP0148 is NEW, which our
# cmake_minimum_required(3.15) leaves unset — so ask for it explicitly. scikit-build-core
# passes Python_EXECUTABLE/Python_ROOT_DIR, so the right interpreter is still used.
set(PYBIND11_FINDPYTHON ON)
find_package(Python QUIET COMPONENTS Interpreter Development.Module)
if(NOT Python_Development.Module_FOUND)
    # Name the actual missing piece. "Could NOT find Python (missing:
    # Development.Module)" is accurate but sends people hunting for a Python that is
    # plainly installed and running the build — what's absent is the -devel package.
    message(FATAL_ERROR
        "Python development HEADERS not found (Python.h).\n"
        "  Interpreter: ${Python_EXECUTABLE}\n"
        "  Version    : ${Python_VERSION}\n"
        "  Headers    : ${Python_INCLUDE_DIRS}\n"
        "The interpreter is fine; the C API headers it was built against are not "
        "installed. This is what a venv on a headerless system Python looks like — "
        "the venv inherits its base prefix, so the include dir points at a "
        "/usr/include/pythonX.Y that doesn't exist.\n"
        "Install the matching development package, then re-run:\n"
        "  RHEL / CentOS / Fedora : sudo dnf install python3-devel\n"
        "  Debian / Ubuntu        : sudo apt install python3-dev\n"
        "  SUSE                   : sudo zypper install python3-devel\n"
        "If you cannot install system packages, create the venv from a Python that "
        "ships its own headers (e.g. a pyenv/conda build) and re-run.")
endif()
find_package(pybind11 CONFIG REQUIRED)

# The verified decode classes (8b/10b, command/transport parser, register model,
# column detector, data-port / flow-control / payload engines) are vendored into
# swi3score/core/ so this repo builds STANDALONE. They originated in the Saleae
# SwI3s analyzer plugin; the SDK-coupled files (CBitstreamDecoder + the
# analyzer/settings/results/simulation glue) are NOT vendored — our ISampleSource
# + Decoder replace CBitstreamDecoder/WorkerThread, and compat/ supplies an
# SDK-free <LogicPublicTypes.h>. See swi3score/core/README.md for provenance.
set(PLUGIN_SRC "${CMAKE_CURRENT_SOURCE_DIR}/swi3score/core")

pybind11_add_module(swi3score
    swi3score/bindings.cpp
    swi3score/Decoder.cpp
    swi3score/Demo.cpp
    ${PLUGIN_SRC}/C8b10bDecoder.cpp
    ${PLUGIN_SRC}/CCommandTransportParser.cpp
    ${PLUGIN_SRC}/CRegisterModel.cpp
    ${PLUGIN_SRC}/CColumnDetector.cpp
    ${PLUGIN_SRC}/CDpConfig.cpp
    ${PLUGIN_SRC}/CDataPort.cpp
    ${PLUGIN_SRC}/CFlowControlPort.cpp
    ${PLUGIN_SRC}/CPayloadEngine.cpp
    ${PLUGIN_SRC}/SwI3sProtocolDefs.cpp
)

# compat/ provides our SDK-free <LogicPublicTypes.h> and MUST precede any SDK
# include dir (we add none), so the reused sources build with zero SDK linkage.
target_include_directories(swi3score PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/swi3score/compat"
    "${CMAKE_CURRENT_SOURCE_DIR}/swi3score"
    "${PLUGIN_SRC}"
)

# Force -O3 even if the toolchain's Release default is -O2, and enable LTO when the
# toolchain supports it (cross-TU inlining of the per-edge decode path).
target_compile_options(swi3score PRIVATE $<$<CONFIG:Release>:-O3>)
if(_ipo_ok)
    set_target_properties(swi3score PROPERTIES INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)
endif()

install(TARGETS swi3score LIBRARY DESTINATION .)
