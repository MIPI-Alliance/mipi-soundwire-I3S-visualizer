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

APP_VERSION = '3.0.0'
MIN_COMPATIBLE_CSV_VERSION = '2.1.11'
