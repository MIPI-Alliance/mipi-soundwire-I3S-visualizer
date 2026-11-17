"""A computation run off the UI thread, of which only the latest request's result is applied.

`LatestJob.submit(fn)` runs `fn(cancelled)` on a worker thread and returns the request's
generation. Every submit, `discard` and `stop` bumps the generation, and a result whose
generation is not the latest is dropped, so a stale result never overwrites a newer one. One
worker runs at a time: a submit while one runs asks it to stop (its `cancelled()` turns True)
and starts the newest request once it has returned; a request superseded before it started
never runs.

Rules for `fn`:

- It captures plain data only, never a Qt object or an object holding one: whatever the
  worker holds last is freed on the worker thread.
- It returns its result, or raises. A result is handed to `apply(result)` on the UI thread;
  an exception is handed to `fail(exc)`, when given, and then raised on the UI thread, from
  the poll timer's slot or from `wait()`.
- It may poll `cancelled()` and return early; a cancelled request's result is never applied.

The UI thread collects a finished result on a poll timer parented to `parent`. Deleting
`parent` deletes the timer, so nothing is collected, and turns `cancelled()` True for the
request running. `wait()` collects the latest result now, blocking the caller until it is
in. `stop()` drops every request and joins the worker; a closing view calls it.

Automatic garbage collection is off while any worker runs (`_COLLECTOR`). A collection runs
on whichever thread allocates when one falls due, and on a worker it would free Qt objects
there; with it off, every collection runs on a thread that is not a worker.
"""
from __future__ import annotations

import gc
import threading
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QCoreApplication, QObject, QTimer

# How often the UI thread looks for a finished result while a request runs (ms).
POLL_MS = 15
THREAD_NAME = "latest-job"


class _CollectorHold:
    """Keeps automatic garbage collection off from the start of the first worker to the end of
    the last, switched on the UI thread only: `hold` before a worker starts, `release_ended`
    once workers have ended (each LatestJob calls it as it collects, and a timer on the
    application while any worker runs, for a worker whose LatestJob is gone). Collection is
    switched back on only if it was on at the first hold."""

    def __init__(self) -> None:
        self._threads: List[threading.Thread] = []
        self._restore = False
        self._timer: Optional[QTimer] = None

    def hold(self, thread: threading.Thread) -> None:
        if not self._threads:
            self._restore = gc.isenabled()
            gc.disable()
        self._threads.append(thread)
        app = QCoreApplication.instance()
        if self._timer is None and app is not None:
            self._timer = QTimer(app)
            self._timer.setInterval(4 * POLL_MS)
            self._timer.timeout.connect(self.release_ended)
        if self._timer is not None:
            self._timer.start()

    def release_ended(self) -> None:
        self._threads = [t for t in self._threads if t.is_alive()]
        if self._threads:
            return
        if self._timer is not None:
            self._timer.stop()
        if self._restore:
            self._restore = False
            gc.enable()

    @property
    def holding(self) -> bool:
        return bool(self._threads)


_COLLECTOR = _CollectorHold()


class _Request:
    """One submitted computation: its generation, its function and, once run, its outcome."""

    def __init__(self, generation: int, fn: Callable[[Callable[[], bool]], Any],
                 orphaned: threading.Event) -> None:
        self.generation = generation
        self.cancel = threading.Event()
        self._orphaned = orphaned
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self._fn: Optional[Callable[[Callable[[], bool]], Any]] = fn
        self.thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=False)

    def _run(self) -> None:
        fn, self._fn = self._fn, None
        assert fn is not None
        try:
            self.result = fn(self.cancelled)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the UI thread
            self.error = exc

    def cancelled(self) -> bool:
        """Whether the request was superseded, dropped, or its parent deleted."""
        return self.cancel.is_set() or self._orphaned.is_set()

    def start(self) -> None:
        """Start the worker; where no thread can be started, run on the caller's thread."""
        _COLLECTOR.hold(self.thread)
        try:
            self.thread.start()
        except RuntimeError:
            self._run()
            _COLLECTOR.release_ended()


class LatestJob:
    """See the module docstring."""

    def __init__(self, parent: QObject, apply: Callable[[Any], None],
                 fail: Optional[Callable[[BaseException], None]] = None) -> None:
        self._apply = apply
        self._fail = fail
        self._generation = 0
        self._running: Optional[_Request] = None
        self._pending: Optional[_Request] = None
        self._timer = QTimer(parent)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        # Set when `parent` (and so the timer) is deleted. The connection holds the event
        # alone, never this object, so it keeps nothing of the parent alive.
        self._orphaned = threading.Event()
        self._timer.destroyed.connect(self._orphaned.set)

    @property
    def generation(self) -> int:
        """The latest request's generation: only its result is applied."""
        return self._generation

    @property
    def busy(self) -> bool:
        """Whether a request runs or waits to run."""
        return self._running is not None or self._pending is not None

    def submit(self, fn: Callable[[Callable[[], bool]], Any]) -> int:
        """Run `fn` on the worker, superseding every earlier request; its generation."""
        self._generation += 1
        request = _Request(self._generation, fn, self._orphaned)
        if self._running is None:
            self._running = request
            request.start()
        else:
            self._running.cancel.set()
            self._pending = request
        self._timer.start()
        return self._generation

    def discard(self) -> None:
        """Drop every request without waiting: nothing submitted so far is applied."""
        self._generation += 1
        self._pending = None
        if self._running is not None:
            self._running.cancel.set()

    def stop(self) -> bool:
        """Drop every request and join the worker. Whether a request was dropped."""
        dropped = self.busy
        self.discard()
        running, self._running = self._running, None
        if running is not None:
            running.thread.join()
        self._timer.stop()
        _COLLECTOR.release_ended()
        return dropped

    def wait(self) -> None:
        """Block until the latest request has finished, and apply its result."""
        while self._running is not None:
            self._running.thread.join()
            self._collect()
        self._timer.stop()

    def _poll(self) -> None:
        if self._running is not None and not self._running.thread.is_alive():
            self._collect()
        if self._running is None:
            self._timer.stop()

    def _collect(self) -> None:
        """Take the finished worker's outcome: apply it if it is the latest request's, else
        start the request waiting."""
        done, self._running = self._running, None
        assert done is not None
        if self._pending is not None:
            self._running, self._pending = self._pending, None
            self._running.start()
        _COLLECTOR.release_ended()
        if done.generation != self._generation:     # superseded, so cancelled too
            return
        if done.error is not None:
            if self._fail is not None:
                self._fail(done.error)
            raise done.error
        self._apply(done.result)
