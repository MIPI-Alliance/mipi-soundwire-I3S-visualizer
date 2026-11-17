"""The on-disk results of one large capture's Session: a private temp directory holding the
audio store's memmap files and the command table, removed with the Session.

`ResultsStore` owns the directory. `audio_dir` gives each AudioStore its own subdirectory
(an AudioStore's channel files have fixed names, so two stores never share one);
`save_commands` writes the command table through `command_store` and removes the table any
earlier decode wrote. The directory is removed by `close`, when the ResultsStore is
garbage-collected, and at interpreter exit, whichever comes first.
"""
from __future__ import annotations

import glob
import os
import shutil
import tempfile
import time
import weakref
from typing import Any, Dict, List

_PREFIX = "swi3s-results-"
_COMMANDS = "commands-{}.parquet"
# The waits, in seconds, before each attempt to remove a results directory: a file another
# process briefly holds open (a virus scanner reading a new file, on Windows) cannot be
# removed, so the removal is tried again for about a second.
_REMOVE_WAITS = (0.0, 0.05, 0.1, 0.2, 0.25, 0.4)


def _remove(path: str) -> None:
    """Remove the directory `path` and everything in it, trying again after each wait of
    _REMOVE_WAITS while it is still there."""
    for wait in _REMOVE_WAITS:
        time.sleep(wait)
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return


class ResultsStore:
    """One Session's results directory."""

    def __init__(self) -> None:
        self.path = tempfile.mkdtemp(prefix=_PREFIX)
        self._generation = 0
        self._finalizer = weakref.finalize(self, _remove, self.path)

    @property
    def closed(self) -> bool:
        return not self._finalizer.alive

    def audio_dir(self) -> str:
        """A new, empty subdirectory for one AudioStore's memmap files."""
        return tempfile.mkdtemp(prefix="audio-", dir=self.path)

    def save_commands(self, commands: List[Dict[str, Any]]) -> str:
        """Write `commands` as a Parquet command table (command_store.commands_to_table,
        save_commands) under a new name, remove every other command table here, and return
        the path."""
        from . import command_store
        self._generation += 1
        path = os.path.join(self.path, _COMMANDS.format(self._generation))
        command_store.save_commands(command_store.commands_to_table(commands), path)
        # The directory is escaped: a temp path may hold glob characters ([lab]).
        for old in glob.glob(os.path.join(glob.escape(self.path), _COMMANDS.format("*"))):
            if old != path:
                try:
                    os.remove(old)
                except OSError:
                    pass
        return path

    def close(self) -> None:
        """Remove the directory and everything in it. Safe to call more than once."""
        self._finalizer()
