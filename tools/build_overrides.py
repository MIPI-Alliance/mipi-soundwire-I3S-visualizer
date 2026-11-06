"""The SWI3S_* environment variables of native/build_local.sh that change the binary it builds.

Defined once, loaded by path (tools/ is not a package) by tools/gate.py and
tools/publish_tree.py, which remove them so the core they build is the default build.
tests/test_release_gate.py asserts the tuple names every SWI3S_* variable build_local.sh
reads. The toolchain selectors (PYTHON, CXX, PYBIND11_INCLUDE, PYTHON_INCLUDE) are kept: the
build needs them to find its compiler and headers.
"""
from __future__ import annotations

BUILD_OVERRIDES = ("SWI3S_NATIVE_OUT", "SWI3S_NATIVE_FLAGS")
