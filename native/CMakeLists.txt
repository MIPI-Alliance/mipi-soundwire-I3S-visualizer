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
