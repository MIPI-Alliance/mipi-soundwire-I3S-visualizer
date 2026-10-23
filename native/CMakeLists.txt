cmake_minimum_required(VERSION 3.15)
project(swi3score LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)

# Decode runs per clock edge, so build optimised: default to Release (-O3) when the
# caller picks no type, and enable link-time optimisation so the hot loop
# (Decoder -> CPayloadEngine -> CDataPort -> C8b10bDecoder) inlines across translation
# units. No -march=native: the wheel stays portable.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()
include(CheckIPOSupported)
check_ipo_supported(RESULT _ipo_ok OUTPUT _ipo_msg)

# Use CMake's FindPython, not pybind11's legacy FindPythonInterp/FindPythonLibs shim:
# its Development.Module component checks for the development headers (Python.h), so a
# missing -devel package fails at configure time with a message naming it.
#
# pybind11 defaults to FindPython only when policy CMP0148 is NEW, which
# cmake_minimum_required(3.15) leaves unset, so request it explicitly. scikit-build-core
# passes Python_EXECUTABLE/Python_ROOT_DIR, which select the interpreter.
set(PYBIND11_FINDPYTHON ON)
find_package(Python QUIET COMPONENTS Interpreter Development.Module)
if(NOT Python_Development.Module_FOUND)
    # Name the missing piece: the -devel package, not the interpreter.
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
# swi3score/core/ so this project builds standalone. ISampleSource + Decoder take the
# place of the analyzer plugin's CBitstreamDecoder/WorkerThread, and compat/ supplies
# an SDK-free <LogicPublicTypes.h>. See swi3score/core/README.md for the file list.
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

# compat/ provides the SDK-free <LogicPublicTypes.h> and must precede any SDK
# include dir (none is added), so the vendored sources build with no SDK linkage.
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
