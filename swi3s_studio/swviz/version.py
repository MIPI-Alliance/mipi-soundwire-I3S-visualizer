"""Application version constants.

`APP_VERSION` is the FALLBACK stamped into saved configs when the package isn't
importable; `app_version()` prefers the live `swi3s_studio.__version__`. It is one of the
three version literals the release checklist bumps; tests/test_release_gate.py pins it to
`__version__`.

The CSV loader does not compare a file's AppVersion with this one: every v2.x config loads
directly, and a v1.73 file is converted on import.
"""

APP_VERSION = '3.3.0'      # pinned to swi3s_studio.__version__ by tests


def app_version() -> str:
    """The live Studio version to stamp into saved CSVs. Reads the single source of
    truth (swi3s_studio.__version__, kept in sync with pyproject.toml) lazily so
    importing this module never pulls in the full package; falls back to the
    APP_VERSION literal if the package isn't importable."""
    try:
        from swi3s_studio import __version__
        return __version__
    except Exception:  # noqa: BLE001 — stamping a version must never block a save
        return APP_VERSION
