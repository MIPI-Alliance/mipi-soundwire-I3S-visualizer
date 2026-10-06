"""Command-line interface: argument parsing and the headless model dump.

Qt-FREE ON PURPOSE. `-o/--output` implies headless mode, so nothing on this path may need a
display, a QApplication, or the UI package — `app.main()` dispatches here and returns before it
builds any of them. Keeping the parser here too means the whole surface is testable without Qt.

    swi3s-studio                                 # empty window
    swi3s-studio --demo                          # synthetic demo capture
    swi3s-studio -c config.csv                   # open a config CSV in Bus-Visualizer mode
    swi3s-studio -c config.csv -o model.json     # headless: write the bus model, no window

The `-o` flag is restored from the v2 Visualizer CLI, where it was the batch entry point for
regression tooling. Two things about it are deliberately NOT copied from v2:

  * v2's help text called the payload a "frame model" while its code wrote the BUS model
    (`BusModelJSONEncoder`). The behaviour is kept — that is what every consumer of those files
    expects, and `JSONHandler.load_bus_model` round-trips it — and the help text is corrected to
    match. `save_frame_model` still exists for the legacy shape; nothing here emits it.
  * v2 accepted `-o` with no input and then complained. Here `-c` is required by the parser, so
    the error arrives from argparse with usage attached rather than from application code.
"""
from __future__ import annotations

import argparse
import os
import sys

_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_CRITICAL = 2      # a model was written, but it contains a bus clash / truncated DRQ

# The same ceiling the UI's Export applies to RowsToDraw (main_window.export_frame_json). A CSV
# may ask for any number of rows and the model is built row by row, so an unbounded value is a
# way to make a batch run allocate for a very long time. Clamped rather than refused — and
# REPORTED when it bites, because a silently smaller model looks like a complete one.
_MAX_ROWS = 1024


class _Parser(argparse.ArgumentParser):
    """argparse that exits 1, not 2, on a usage error.

    Each exit code has to mean exactly ONE thing, and 2 is already spoken for: "the model was
    written and it contains a bus clash or a truncated DRQ". argparse's default is
    SystemExit(2) for a usage error, which made a misspelled flag indistinguishable from a
    config with a physical bus collision — the caller most likely to care is a batch script
    branching on the code, and it would have treated a typo as a finding about the bus.
    """

    def error(self, message: str):        # noqa: A003 - argparse's own name
        self.print_usage(sys.stderr)
        self.exit(_EXIT_ERROR, f"{self.prog}: error: {message}\n")


def program_name() -> str:
    """What to call this program in `usage:`, from the caller's point of view.

    A usage line names a command the reader can retype. There are three ways in, and a single
    hardcoded name is wrong for two of them: `./run.sh -h` printed `usage: swi3s-studio …`, which
    is not a command that exists on a fresh checkout — the launcher is how this project is
    documented to be run, and `swi3s-studio` only exists once the package is pip-installed.

    So the launcher says who it is via $SWI3S_LAUNCHER, and the fallbacks cover the rest:

      ./run.sh / .\\run.ps1     -> "run.sh" / ".\\run.ps1"   (the launcher exports it)
      swi3s-studio             -> "swi3s-studio"           (argv[0] IS the console script)
      python -m swi3s_studio.app -> "python -m swi3s_studio.app"

    The last one matters: argv[0] there is the full path to app.py, so argparse's own default
    would print `usage: app.py …`, which is not runnable as written either.
    """
    launcher = os.environ.get("SWI3S_LAUNCHER", "").strip()
    if launcher:
        return launcher
    argv0 = sys.argv[0] if sys.argv else ""
    # `python -m pkg.mod` sets argv[0] to the module's own file path.
    if not argv0 or argv0.endswith((os.sep + "app.py", "/app.py", "app.py")):
        exe = os.path.basename(sys.executable) or "python3"
        return f"{exe} -m swi3s_studio.app"
    return os.path.basename(argv0)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog=program_name(),
        description="SWI3S Studio — desktop MIPI SoundWire I3S bus analyzer.",
        epilog="With -o, no window is opened: the bus model is written and the process exits "
               "0 (clean), 1 (error) or 2 (model written, but it contains a bus clash or a "
               "truncated DRQ).",
    )
    p.add_argument("-c", "--config", metavar="FILE",
                   help="Input v2.0 CSV configuration file")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="Output bus model to JSON file (implies headless mode; requires -c)")
    p.add_argument("--demo", action="store_true", help="Open the synthetic demo capture")
    p.add_argument("--demo-bringup", action="store_true",
                   help="Open the demo capture with a §5.1.2 Cold Start")
    return p


def parse(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse our flags, RETURNING the rest for Qt.

    parse_known_args, not parse_args: Qt reads its own options off argv (-style, -platform,
    -stylesheet …) and a strict parser would reject them, breaking documented Qt invocations
    that have nothing to do with this program's flags.
    """
    args, rest = build_parser().parse_known_args(argv[1:])
    if args.output and not args.config:
        build_parser().error("-o/--output requires -c/--config (nothing to build a model from)")
    return args, [argv[0], *rest]


def run_headless(config: str, output: str, out=None) -> int:
    """Build the bus model from `config` and write it to `output` as JSON.

    Returns the process exit code. Imports the engine lazily so that merely parsing arguments
    does not drag in the model layer.
    """
    out = sys.stdout if out is None else out
    if not os.path.isfile(config):
        print(f"error: configuration file not found: {config}", file=sys.stderr)
        return _EXIT_ERROR

    from .model.viz_engine import build_bus_model, issues_from_model
    from .swviz.io.csv_handler import CSVHandler
    from .swviz.io.json_handler import JSONHandler
    from .swviz.models.interface import Interface
    from .swviz.viz import VizConfig

    # Read RowsToDraw first so the clamp can be REPORTED rather than applied behind the
    # caller's back. Parsing the CSV twice is cheap next to building the model.
    rows: int | None = None
    try:
        probe = VizConfig()
        CSVHandler.load_csv(config, Interface(), probe)
        wanted = int(probe.rows_to_draw)
        if wanted > _MAX_ROWS:
            print(f"note: {config} asks for {wanted} rows; building {_MAX_ROWS} "
                  f"(the same ceiling the UI's Export applies)", file=sys.stderr)
            rows = _MAX_ROWS
    except Exception:                                     # noqa: BLE001 - the real load reports
        rows = None

    try:
        bus_model, _iface, _viz = build_bus_model(config, rows)
    except Exception as exc:                              # noqa: BLE001 - report, don't traceback
        print(f"error: could not build a model from {config}: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    try:
        # batch_mode creates the parent directory — a batch caller writing into a fresh
        # results/ tree should not have to mkdir first.
        JSONHandler.save_bus_model(output, bus_model, batch_mode=True)
    except (OSError, TypeError) as exc:
        print(f"error: could not write {output}: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    print(f"Bus model written to {output}", file=out)
    print(f"  rows    {bus_model.num_rows}", file=out)
    print(f"  columns {bus_model.num_columns}", file=out)
    print(f"  bits    {len(bus_model.bits)}", file=out)

    # The SAME issue list the UI's notifications panel shows, from the same function, so a
    # batch run and the window cannot disagree about what is wrong with a config. v2 grew a
    # parallel hand-written report here and it drifted from the GUI panel.
    issues = issues_from_model(bus_model)
    for issue in issues:
        print(f"  {issue.severity.upper():7} {issue.source}: {issue.message}", file=out)

    critical = bool(bus_model.bus_clashes) or bool(bus_model.warnings.drq_truncation_warnings)
    return _EXIT_CRITICAL if critical else _EXIT_OK
