"""Cancelling calls: ``CancelToken``, and Ctrl-C on the main thread.

A call given a token (``cancel=``) stops when the token is cancelled, from
another thread or from a filter: libpandoc's cancel tokens (1.13; with
untrusted options inside ``"untrusted"``, 1.14), and in the directory
sandbox by stopping libpandoc.wasm from outside (wasmtime's epoch, as its
time limit does).

Ctrl-C: Python runs its signal handlers on the main thread, between
bytecodes, so a call into pandoc there (the GIL released, for seconds) took
Ctrl-C only when it returned. So a call made on the main thread runs on a
helper thread while the main thread waits for it, where Ctrl-C arrives: it
cancels the call's token, waits for the call to stop, and raises the
``KeyboardInterrupt``. The helper is one thread, kept for the process (a
thread per call cost 70 µs a call, a tiny conversion's 20%; the kept one
about 8 µs, its 2.5%), which runs each call in a copy of the caller's
context (``contextvars``): Python filters, which pandoc calls back on the
thread that called it, run there, seeing the caller's context variables. A
call made while the helper is busy (from a signal handler during the wait)
runs on the main thread. Not in Pyodide (no threads), nor on other threads
(Ctrl-C doesn't arrive there).
"""

from __future__ import annotations

import contextvars
import os
import queue
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from . import _core  # (in Pyodide, libpandoc.wasm's: the package's _core)
from ._wasmfilter import interrupt

T = TypeVar("T")

__all__ = ["CancelToken"]


class CancelToken:
    """A way to stop calls while they run: give it as ``cancel=`` to
    ``convert``, ``read``, ``write`` or ``read_many`` (any number of calls,
    one after another or at once), and ``cancel()`` it from another thread,
    or from a filter of the call. Each call using it then stops and raises
    ``PandocCancelledError``, as one past its ``timeout`` raises
    ``PandocTimeoutError``, and the process goes on. A token stays cancelled:
    a call given it later fails at once.

    What it stops is what ``timeout`` stops: pandoc's own code and the JSON
    filters pandoc runs, and in the directory sandbox the whole of pandoc,
    Lua included; not a Python filter running (it runs to its end, and the
    call then fails) nor Lua outside the sandbox. A call that finished
    before the cancel keeps its result.

        token = libpandoc.CancelToken()
        threading.Timer(5, token.cancel).start()   # a cancel button
        libpandoc.convert(text, to="html", cancel=token)
    """

    __slots__ = ("_cancelled", "_id")

    def __init__(self) -> None:
        self._cancelled = False
        new = getattr(_core, "cancel_token_new", None)  # (none in Pyodide)
        self._id: int = new() if new is not None else 0

    def cancel(self) -> None:
        """Stop every call using this token, running or to come. From any
        thread (not a signal handler's own C code: Python's handlers, which
        run on the main thread, may call it)."""
        self._cancelled = True
        if self._id:
            _core.cancel(self._id)
        interrupt()

    @property
    def cancelled(self) -> bool:
        """Whether ``cancel()`` was called."""
        return self._cancelled

    def __repr__(self) -> str:
        return f"CancelToken(cancelled={self._cancelled})"

    def __del__(self) -> None:
        if getattr(self, "_id", 0):
            try:
                _core.cancel_token_free(self._id)
            except Exception:  # noqa: BLE001,S110  (at the interpreter's exit)
                pass


def options(opts: Mapping[str, Any], token: CancelToken | None) -> Mapping[str, Any]:
    """The options with libpandoc's ``"cancel-token"`` for ``token``; for
    untrusted ones (any ``"untrusted"`` but false), inside the key the host
    writes, over whatever it was: ``"untrusted": {"cancel-token": N}``
    (libpandoc 1.14), as a top-level one beside ``"untrusted"`` is refused
    (the code could have written it)."""
    if token is None or not token._id:
        return opts
    if opts.get("untrusted", False) is not False:
        return {**opts, "untrusted": {"cancel-token": token._id}}
    return {**opts, "cancel-token": token._id}


def call(fn: Callable[[CancelToken | None], T], token: CancelToken | None) -> T:
    """``fn(token)``, a call into pandoc; on the main thread, on the helper
    while this thread waits, with a token of its own if not given one, so
    that Ctrl-C (or any exception raised by a signal handler) cancels it."""
    if sys.platform == "emscripten" or threading.current_thread() is not threading.main_thread():
        return fn(token)
    if not _BUSY.acquire(blocking=False):
        return fn(token)  # (re-entered: a signal handler's call during the wait)
    try:
        if token is None:
            token = CancelToken()
        job = _Job(contextvars.copy_context(), fn, token)
        _helper().put(job)
        try:
            # (with a timeout: a wait without one isn't interrupted by
            # Ctrl-C on Windows)
            while not job.done.wait(0.25):
                pass
        except BaseException:
            token.cancel()
            while True:
                try:
                    job.done.wait()
                    break
                except BaseException:  # noqa: BLE001,S112  (Ctrl-C again: still stopping)
                    continue
            raise
        if job.error is not None:
            try:
                raise job.error
            finally:
                job.error = None  # (no cycle through this frame)
        return job.result  # type: ignore[return-value]
    finally:
        _BUSY.release()


class _Job:
    __slots__ = ("context", "done", "error", "fn", "result", "token")

    def __init__(self, context: contextvars.Context, fn: Callable[[Any], Any], token: Any) -> None:
        self.context, self.fn, self.token = context, fn, token
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self.context.run(self.fn, self.token)
        except BaseException as e:  # noqa: BLE001  raised on the caller's thread
            self.error = e
        finally:
            self.done.set()


_BUSY = threading.Lock()  # the main thread's call on the helper
_LOCK = threading.Lock()
_QUEUE: queue.SimpleQueue[_Job] | None = None
_PID = 0


def _helper() -> queue.SimpleQueue[_Job]:
    """The helper's queue: its thread started once per process (again in a
    child made by fork, which has none)."""
    global _QUEUE, _PID
    with _LOCK:
        if _QUEUE is None or _PID != os.getpid():
            jobs: queue.SimpleQueue[_Job] = queue.SimpleQueue()

            def serve() -> None:
                while True:
                    jobs.get().run()

            threading.Thread(target=serve, name="libpandoc-calls", daemon=True).start()
            _QUEUE, _PID = jobs, os.getpid()
        return _QUEUE
