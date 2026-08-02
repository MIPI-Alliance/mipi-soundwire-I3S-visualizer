"""Application version constants.

`APP_VERSION` is imported by the CSV handler to stamp an `AppVersion` row into saved
configs. This engine is now part of **SWI3S Studio** (was the standalone SWI3S
Visualizer), so it tracks the Studio version.

`MIN_COMPATIBLE_CSV_VERSION` documents the oldest CSV the loader reads without
conversion. NOTE: it is currently advisory only — the loader does not gate on it (the
CSV format hasn't had a breaking change, so every v2.x config loads directly, and
older v1.73 files are auto-converted on import). Wire a `parse_version` comparison
into `csv_handler.load_csv` if/when a breaking format change needs to reject old files.
"""

APP_VERSION = '3.0.8'
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
