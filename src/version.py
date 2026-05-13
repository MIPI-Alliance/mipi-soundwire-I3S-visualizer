"""Application version constants.

Single source of truth — imported by the entrypoint, the CSV handler (so
it can stamp the version into saved files), and the conversion tool.

`MIN_COMPATIBLE_CSV_VERSION` is the oldest version whose saved CSVs the
current app loads without requiring conversion. Bump this when the CSV
format gets a breaking change.
"""

APP_VERSION = '2.1.12'
MIN_COMPATIBLE_CSV_VERSION = '2.1.11'


def parse_version(s: str) -> tuple:
    """Parse a dotted version string into a tuple of ints for comparison."""
    return tuple(int(p) for p in s.split('.'))
