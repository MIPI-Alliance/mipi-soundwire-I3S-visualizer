"""Analysis helpers over the decoded command/audio streams (errors, measurements)."""
from .compare import grid_diff, grid_diff_report, register_diff, register_diff_report
from .errors import command_error, count_errors, error_rows, is_error
from .issues import ERROR, INFO, WARNING, Issue, sort_issues
from .link_control import LinkControlResult, decode_link_control
from .link_events import link_events
from .measurements import capture_measurements
from .responses import manager_response_name, peripheral_response_name, ping_name, response_summary

# NOTE: the standalone clash/validate hand-port was removed — Bus-Visualizer mode's
# placement, clash detection, and validation now come from the first-party Visualizer
# engine (swi3s_studio/swviz, see model/viz_engine.py), verified by test_visualizer_engine.

__all__ = ["command_error", "is_error", "count_errors", "error_rows",
           "capture_measurements", "grid_diff", "grid_diff_report", "register_diff",
           "register_diff_report",
           "link_events",
           "decode_link_control", "LinkControlResult",
           "peripheral_response_name", "manager_response_name", "ping_name",
           "response_summary",
           "Issue", "sort_issues", "ERROR", "WARNING", "INFO"]
