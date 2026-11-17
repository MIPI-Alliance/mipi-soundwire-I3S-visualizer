"""The version the application shows: `__version__`, and in a development checkout the commits
since that release.

The version sites keep the last release's number until the release commit
(docs/DEVELOPMENT.md, Branch model), so a build from a checkout past the release tag shows
which commit it is: ``3.3.0+12 (1a2b3c4)``, with ``, modified`` while tracked files differ
from that commit. A build at the release tag shows `__version__` alone, or
``3.3.0 (1a2b3c4, modified)`` with tracked files changed; an installed package and a bundle
show `__version__` alone. The checkout is read once, with git; no git, no checkout, no release tag in the checkout or
a slow git gives `__version__` alone.
"""
from __future__ import annotations

import functools
import pathlib
import re
import subprocess

from . import __version__

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# `git describe --long --dirty` against the release tag: v<version>-<n>-g<sha>[-dirty].
_DESCRIBE = re.compile(r"^v(?P<version>[^-]+)-(?P<n>\d+)-g(?P<sha>[0-9a-f]+)(?P<dirty>-dirty)?$")


def describe_label(described: str, version: str = __version__) -> str:
    """The shown version for `git describe` output `described`, or `version` alone when the
    output is not against `version`'s tag or names the tagged commit unmodified."""
    m = _DESCRIBE.match(described.strip())
    if m is None or m["version"] != version:
        return version
    n, dirty = int(m["n"]), bool(m["dirty"])
    if n == 0 and not dirty:
        return version
    extra = ", modified" if dirty else ""
    return f"{version}+{n} ({m['sha']}{extra})" if n else f"{version} ({m['sha']}{extra})"


@functools.lru_cache(maxsize=1)
def display_version() -> str:
    """`__version__`, with the commits since its release tag in a development checkout."""
    if not (_ROOT / ".git").exists():
        return __version__
    try:
        out = subprocess.run(
            ["git", "-C", str(_ROOT), "describe", "--tags", "--long", "--dirty",
             "--match", f"v{__version__}"],
            capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return __version__
    return describe_label(out.stdout.decode("utf-8", errors="replace")) if out.returncode == 0 else __version__
