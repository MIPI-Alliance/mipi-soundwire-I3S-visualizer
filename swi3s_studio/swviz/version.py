"""Application version constants.

`APP_VERSION` is the FALLBACK stamped into saved configs when the package isn't
importable; `app_version()` prefers the live `swi3s_studio.__version__`. It is one of the
three version literals the release checklist bumps, and it silently missed 3.0.9, 3.0.10
and 3.0.11 — sitting at 3.0.8 while the app shipped 3.0.11. A wrong version string is
worse than none, so tests/test_release_gate.py now pins it to `__version__`; the bump is
enforced rather than remembered.

`MIN_COMPATIBLE_CSV_VERSION` documents the oldest CSV the loader reads without
conversion. NOTE: it is currently advisory only — the loader does not gate on it (the
CSV format hasn't had a breaking change, so every v2.x config loads directly, and
older v1.73 files are auto-converted on import). Wire a `parse_version` comparison
into `csv_handler.load_csv` if/when a breaking format change needs to reject old files.
"""

APP_VERSION = '3.0.15'      # pinned to swi3s_studio.__version__ by tests
MIN_COMPATIBLE_CSV_VERSION = '2.1.11'


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
