"""Wasm filters, run in this process by wasmtime (``pip install libpandoc[wasm]``).

A wasm filter is a pandoc JSON filter built for WASI (``wasm32-wasip1``),
such as a panir Rust filter: the document on stdin, the new one on stdout,
the output format as its first argument, the variables libpandoc gives
JSON filters in its environment (``PANDOC_WRITER_OPTIONS`` among them). It
runs sandboxed: it sees only the directories it is given (the current one,
read-only, by default), no network, no programs. It may
call pandoc through the imports of module ``libpandoc`` (libpandoc-rs's
``guest.rs``), which this host answers on this process's pandoc, marked
``"untrusted": true``: libpandoc then runs them in pandoc's sandbox, with no
options that read or write files, fetch or run anything (its list, which
every host shares).

Limits, which pandoc has for no filter: a timeout (wall-clock time, its
calls to pandoc included: each runs with the time the filter has left as
its ``"timeout"``) and a memory limit (the filter's own memory), from
``$LIBPANDOC_WASM_TIMEOUT`` (seconds, as pandoc-server's ``--timeout``) and
``$LIBPANDOC_WASM_MAX_MEMORY`` (bytes, or with k, m or g, as pandoc's
``+RTS -M``), or per filter; none by default, as pandoc. The memory limit
also caps what the filter writes to its standard output (kept here until
it ends) and to its standard error (passed on to ``sys.stderr``), each.
"""

from __future__ import annotations

import atexit
import codecs
import contextlib
import ctypes
import errno
import json
import math
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from ._scripts import filter_env

TIMEOUT_VAR = "LIBPANDOC_WASM_TIMEOUT"
MAX_MEMORY_VAR = "LIBPANDOC_WASM_MAX_MEMORY"

# How often the engine's epoch advances, while a filter with a timeout runs (seconds).
_TICK = 0.01
# The largest limits wasmtime takes: a deadline in ticks (a u64, with the
# epoch counted on from it) and a memory size (an i64). A larger one is no
# limit in practice (2**62 ticks: 10**9 years), and wrapped around before:
# a time limit of 1e300 s stopped a filter at once.
_MAX_TICKS = 2**62
_MAX_BYTES = 2**62


def checked_timeout(t: Any, what: str = "timeout") -> float | None:
    """A time limit, in seconds, 0 or more (None: none, as 0)."""
    if t is None:
        return None
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t < math.inf:
        raise ValueError(f"{what}: seconds, 0 or more (0: no limit), not {t!r}")
    return t or None


def checked_memory(n: Any, what: str = "max_memory") -> int | None:
    """A memory limit, in bytes, 0 or more (None: none, as 0)."""
    if n is None:
        return None
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f"{what}: bytes, 0 or more (0: no limit), not {n!r}")
    return min(n, _MAX_BYTES) or None


def parse_timeout(s: str) -> float | None:
    """A timeout in seconds (``$LIBPANDOC_WASM_TIMEOUT``): None if empty or 0."""
    s = s.strip()
    if not s:
        return None
    try:
        return checked_timeout(float(s), TIMEOUT_VAR)
    except ValueError:
        raise ValueError(f"{TIMEOUT_VAR}: seconds, 0 or more (0: no limit), not {s!r}") from None


def parse_memory(s: str) -> int | None:
    """A size in bytes, or with k, m or g (``$LIBPANDOC_WASM_MAX_MEMORY``, as
    pandoc's ``+RTS -M``): None if empty or 0."""
    s = s.strip()
    if not s:
        return None
    m = re.fullmatch(r"([0-9]+)([kKmMgG]?)", s)
    if m is None:
        raise ValueError(f"{MAX_MEMORY_VAR}: bytes, or with k, m or g, not {s!r}")
    return checked_memory(int(m[1]) << {"": 0, "k": 10, "m": 20, "g": 30}[m[2].lower()])


def limits(timeout: Any = None, max_memory: Any = None) -> tuple[float | None, int | None]:
    """A run's limits: those given, else the environment's (None: none)."""
    return (parse_timeout(os.environ.get(TIMEOUT_VAR, "")) if timeout is None
            else checked_timeout(timeout),
            parse_memory(os.environ.get(MAX_MEMORY_VAR, "")) if max_memory is None
            else checked_memory(max_memory))


def limit(store: Any, timeout: float | None, max_memory: int | None) -> None:
    """Give a store its limits: its memory, and an epoch deadline (none
    in practice without a timeout); the clock must tick (``ticking``)."""
    if max_memory is not None:
        store.set_limits(memory_size=max_memory)
    store.set_epoch_deadline(
        _MAX_TICKS if timeout is None else min(int(-(-timeout // _TICK)) + 1, _MAX_TICKS))


class WasmFilterError(RuntimeError):
    """A wasm filter failed: it trapped, exited with a nonzero status, or met
    one of its limits."""


def _wasmtime() -> Any:
    try:
        import wasmtime
    except ImportError as e:
        raise ImportError(
            "wasm filters need wasmtime: pip install 'libpandoc[wasm]'"
        ) from e
    return wasmtime


_LOCK = threading.Lock()
_ENGINE: Any = None
_CLOCK: threading.Thread | None = None
_TIMED = 0  # runs with a timeout going on: the clock ticks only while there are some
_WAKE = threading.Condition(_LOCK)


def engine() -> Any:
    """The engine every filter shares, and libpandoc.wasm in a sandbox:
    compiled code cached on disk (e.g. ~/.cache/wasmtime), wasm code
    checking an epoch, for timeouts, and wasm's exception handling (which
    libpandoc.wasm's Lua is built with)."""
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            wasmtime = _wasmtime()
            config = wasmtime.Config()
            try:
                config.cache = True
            except wasmtime.WasmtimeError:
                pass
            config.epoch_interruption = True
            config.wasm_exceptions = True
            _ENGINE = wasmtime.Engine(config)
            atexit.register(_drop_engine)
        return _ENGINE


def _drop_engine() -> None:
    """At exit, while wasmtime-py can still free it (later, in the teardown
    of modules, its finalizer fails and says so)."""
    global _ENGINE
    with _LOCK:
        _ENGINE = None


@contextlib.contextmanager
def ticking() -> Iterator[None]:
    """While a filter with a timeout runs: the epoch's clock, a thread
    advancing it by the time that has passed, in _TICKs (wasm runs without
    the GIL), started by the first such filter, and waiting while none
    runs (it woke 100 times a second for the process's life). By the time
    passed, not by its sleeps: a system may stretch a short sleep (macOS
    does), and a limit then came late (a 0.3 s one after 1 s)."""
    global _CLOCK, _TIMED
    eng = engine()

    def tick() -> None:
        while True:
            with _WAKE:
                _WAKE.wait_for(lambda: _TIMED > 0)
            started, done = time.monotonic(), 0
            while True:
                time.sleep(_TICK)
                if _TIMED == 0:
                    break
                due = int((time.monotonic() - started) / _TICK)
                for _ in range(due - done):
                    eng.increment_epoch()
                done = due

    with _WAKE:
        _TIMED += 1
        if _CLOCK is None:
            _CLOCK = threading.Thread(target=tick, name="wasm-filter-clock", daemon=True)
            _CLOCK.start()
        _WAKE.notify()
    try:
        yield
    finally:
        with _WAKE:
            _TIMED -= 1


def _capped(limit: int | None, write: Callable[[bytes], None]) -> tuple[Callable[[bytes], int], list[bool]]:
    """A filter's standard output or error: each write passed to ``write``
    until ``limit`` bytes in all; past it, the write fails (EFBIG) and the
    flag is set. (It must not raise: wasmtime-py prints a callback's
    exception to sys.stdout.)"""
    size, over = [0], [False]

    def sink(data: bytes) -> int:
        if limit is not None and size[0] + len(data) > limit:
            over[0] = True
            return -errno.EFBIG
        size[0] += len(data)
        write(data)
        return len(data)

    return sink, over


def _to_stderr(decoder: Any, data: bytes) -> None:
    with contextlib.suppress(AttributeError, OSError, ValueError):  # (none, or closed)
        sys.stderr.write(decoder.decode(data))


class WasmFilter:
    """A wasm filter: a pandoc JSON filter built for WASI (``wasm32-wasip1``),
    run in this process by wasmtime, sandboxed.

    It sees the directories ``dirs`` (host, guest pairs; by default the
    current one), read-only unless ``writable``, no network, and pandoc's
    filter variables. It may call pandoc: in pandoc's sandbox, with options
    that name no files.

    It fails when it runs longer than ``timeout`` seconds (its calls to
    pandoc included: each runs with the time the filter has left as its
    ``"timeout"``, and one made after that fails at once) or its memory
    would grow past ``max_memory`` bytes.
    None (the default) takes what ``$LIBPANDOC_WASM_TIMEOUT`` and
    ``$LIBPANDOC_WASM_MAX_MEMORY`` say when it runs (none if unset); 0 is no
    limit; a negative one is a ValueError. The memory limit also caps what
    it writes to its standard output and to its standard error (passed on
    to ``sys.stderr``), each.

    Compiled once (and cached on disk by wasmtime); a fresh instance per
    run. In ``filters=``, a path ending in ``.wasm`` is one too.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        dirs: Sequence[tuple[str, str]] = ((".", "."),),
        writable: bool = False,
        timeout: float | None = None,
        max_memory: int | None = None,
    ) -> None:
        from . import pandoc_version

        wasmtime = _wasmtime()
        self.name = os.fsdecode(path)
        try:
            self._module = wasmtime.Module.from_file(engine(), self.name)
        except wasmtime.WasmtimeError as e:
            raise ValueError(f"{self.name}: not a wasm filter: {e}") from None
        self.dirs = [(os.fsdecode(h), g) for h, g in dirs]
        self.writable = writable
        checked_timeout(timeout)
        checked_memory(max_memory)
        self.timeout = timeout
        self.max_memory = max_memory
        self._pandoc_version = pandoc_version()  # now, not from inside a conversion

    def __repr__(self) -> str:
        return f"WasmFilter({self.name!r})"

    def __call__(self, doc: bytes, context: bytes) -> bytes:
        """Run the filter on a document (pandoc's JSON), given the context
        libpandoc gives a callback: as a libpandoc callback."""
        return self.run(doc, json.loads(context))

    def run(self, doc: bytes, ctx: dict[str, Any],
            preopens: Sequence[tuple[str, str, bool]] | None = None) -> bytes:
        """Run the filter on a document (pandoc's JSON) in a conversion
        (``ctx``: libpandoc's context); ``preopens`` (host, guest, writable)
        instead of its own directories (a sandboxed conversion's)."""
        wasmtime = _wasmtime()
        timeout, max_memory = limits(self.timeout, self.max_memory)
        if preopens is None:
            preopens = [(host, guest, self.writable) for host, guest in self.dirs]
        with ticking() if timeout is not None else contextlib.nullcontext():
            return self._run(wasmtime, doc, ctx, timeout, max_memory, preopens)

    def _run(self, wasmtime: Any, doc: bytes, ctx: dict[str, Any], timeout: float | None,
             max_memory: int | None, preopens: Sequence[tuple[str, str, bool]]) -> bytes:
        wasi = wasmtime.WasiConfig()
        # (WASI's text: a file name that isn't UTF-8 with U+FFFD)
        wasi.argv = [os.fsencode(self.name).decode("utf-8", "replace"), ctx.get("format") or ""]
        wasi.env = list(filter_env(ctx, self._pandoc_version).items())
        _stdin_bytes(wasi, doc)
        out: list[bytes] = []
        wasi.stdout_custom, out_over = _capped(max_memory, out.append)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        wasi.stderr_custom, err_over = _capped(max_memory, lambda b: _to_stderr(decoder, b))
        for host, guest, writable in preopens:
            try:
                wasi.preopen_dir(host, guest, writable)
            except (wasmtime.WasmtimeError, UnicodeEncodeError):  # (wasmtime: UTF-8 paths)
                raise RuntimeError(f"{self.name}: can't give it {host!r}") from None

        store = wasmtime.Store(engine())
        store.set_wasi(wasi)
        limit(store, timeout, max_memory)
        started = time.monotonic()
        clock = _Clock(None if timeout is None else started + timeout)
        linker = wasmtime.Linker(engine())
        linker.define_wasi()
        _define_libpandoc(wasmtime, linker, clock)

        def failed(msg: str, interrupted: bool = False) -> WasmFilterError:
            if interrupted:
                return WasmFilterError(
                    f"{self.name}: stopped after {time.monotonic() - started:.1f} s, "
                    f"its time limit ({TIMEOUT_VAR}: {timeout})")
            if max_memory is not None:
                msg += f" (its memory limit: {max_memory} bytes, {MAX_MEMORY_VAR})"
            return WasmFilterError(msg)

        # The store is freed when the run ends: kept by the traceback of
        # its error (a cycle, through this frame) until the interpreter
        # exits, its WASI streams' callbacks into Python aborted 3.11 then.
        try:
            error = None
            try:
                instance = linker.instantiate(store, self._module)
                start = instance.exports(store).get("_start")
                if start is None:
                    raise WasmFilterError(f"{self.name}: not a WASI command (no _start)")
                start(store)
            except wasmtime.ExitTrap as e:
                if e.code != 0:
                    error = failed(f"{self.name} exited with status {e.code}")
            except wasmtime.Trap as e:
                interrupted = timeout is not None and e.trap_code == wasmtime.TrapCode.INTERRUPT
                error = failed(f"{self.name}: {e}", interrupted)
            except wasmtime.WasmtimeError as e:
                error = failed(f"{self.name}: {e}")
            if clock.out_of_time:
                # its time ran out in a call to pandoc: stopped, whatever it did after
                error = failed("", interrupted=True)
            for over, stream in ((out_over, "output"), (err_over, "error")):
                if over[0]:  # (the failed write made it fail, whatever it said)
                    error = WasmFilterError(
                        f"{self.name}: wrote more than its memory limit ({max_memory} bytes, "
                        f"{MAX_MEMORY_VAR}) to its standard {stream}")
            if error is not None:
                raise error
            return b"".join(out)
        finally:
            store.close()
            linker.close()


def _stdin_bytes(wasi: Any, data: bytes) -> None:
    """The filter's standard input: ``data`` (wasmtime's C API has it;
    wasmtime-py only files)."""
    from wasmtime import _ffi as ffi

    vec = ffi.wasm_byte_vec_t()
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    ffi.wasm_byte_vec_new(ctypes.byref(vec), len(data), buf)
    ffi.wasi_config_set_stdin_bytes(wasi.ptr(), ctypes.byref(vec))


# -- the libpandoc imports ---------------------------------------------------------


class _Clock:
    """A run's time limit, for its calls to pandoc: its deadline (a
    ``time.monotonic()``; None for none), and whether a call ended past it."""

    def __init__(self, deadline: float | None) -> None:
        self.deadline = deadline
        self.out_of_time = False

    def left(self) -> float | None:
        """The seconds the filter has left (None: no limit); a call made
        with none left is refused as a ``"Timeout"``."""
        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left <= 0:
            self.out_of_time = True
            raise _Refused("the filter's time limit is reached", "Timeout")
        return left

    def check(self) -> None:
        """After a call: past the deadline, the filter is to stop."""
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.out_of_time = True


def _within(options: dict[str, Any], left: float | None) -> dict[str, Any]:
    """Options a filter gives pandoc, within its time: their ``"timeout"``
    the time it has left, or its own if shorter."""
    own = options.get("timeout")
    if left is not None and not (
        isinstance(own, (int, float)) and not isinstance(own, bool) and 0 < own < left
    ):
        options["timeout"] = left
    return options


def _define_libpandoc(wasmtime: Any, linker: Any, clock: _Clock) -> None:
    """The ``libpandoc`` imports (libpandoc-rs's ``guest.rs``), on this
    process's pandoc, within the filter's time (``clock``); the result of a
    run's last call kept for ``result_len`` and ``result_read``."""
    from . import _core

    i32 = wasmtime.ValType.i32()
    last: list[tuple[bytes, bytes, bytes, bytes]] = [(b"", b"", b"", b"")]

    def memory(caller: Any) -> Any:
        m = caller["memory"]
        if not isinstance(m, wasmtime.Memory):
            raise wasmtime.WasmtimeError("the filter exports no memory")
        return m

    def guest_bytes(caller: Any, ptr: int, length: int) -> bytes:
        ptr, length = ptr & 0xFFFFFFFF, length & 0xFFFFFFFF
        m = memory(caller)
        if ptr + length > m.data_len(caller):
            raise wasmtime.WasmtimeError("out of bounds memory access")
        return bytes(m.read(caller, ptr, ptr + length))

    def answer(call: Any) -> int:
        try:
            status, output, kind, message, log = call()
        except _Refused as e:
            status, output, kind, message, log = 1, b"", e.kind, str(e), b"[]"
        clock.check()
        if status != 0:
            last[0] = (b"", b"", (kind or "Exception").encode(), (message or "").encode())
            return 1
        last[0] = (output, log or b"[]", b"", b"")
        return 0

    def guest_json(caller: Any, ptr: int, length: int) -> Any:
        b = guest_bytes(caller, ptr, length)
        try:
            return json.loads(b)
        except ValueError as e:
            raise _Refused(str(e)) from None

    def convert(caller: Any, o: int, ol: int, i: int, il: int, has: int) -> int:
        def call() -> Any:
            left = clock.left()
            opts = _within(_untrusted(guest_json(caller, o, ol)), left)
            if not has:
                raise _Refused("a wasm filter gives convert its input")
            return _core.convert(json.dumps(opts).encode(), guest_bytes(caller, i, il))
        return answer(call)

    def read_many(caller: Any, p: int, length: int) -> int:
        def call() -> Any:
            left = clock.left()
            req = guest_json(caller, p, length)
            if not isinstance(req, dict):
                raise _Refused(_refusal("a request that isn't an object"))
            req["options"] = _within(_untrusted(req.get("options", {})), left)
            return _core.read_many(json.dumps(req).encode())
        return answer(call)

    def query(caller: Any, p: int, length: int) -> int:
        def call() -> Any:
            clock.left()
            return _core.query(json.dumps(_untrusted(guest_json(caller, p, length))).encode())
        return answer(call)

    def result_len(caller: Any, part: int) -> int:
        return len(last[0][part]) if 0 <= part < 4 else 0

    def result_read(caller: Any, part: int, to: int) -> None:
        data = last[0][part] if 0 <= part < 4 else b""
        to &= 0xFFFFFFFF
        m = memory(caller)
        if to + len(data) > m.data_len(caller):
            raise wasmtime.WasmtimeError("out of bounds memory access")
        m.write(caller, data, to)

    def define(name: str, fn: Any, params: int, results: int) -> None:
        ty = wasmtime.FuncType([i32] * params, [i32] * results)
        linker.define_func("libpandoc", name, ty, fn, access_caller=True)

    define("convert", convert, 5, 1)
    define("read_many", read_many, 2, 1)
    define("query", query, 2, 1)
    define("result_len", result_len, 1, 1)
    define("result_read", result_read, 2, 0)


# -- what a wasm filter may ask of pandoc --------------------------------------------


class _Refused(Exception):
    """A call pandoc isn't asked: its answer is an error of this kind (a
    PandocOptionError, or a Timeout)."""

    def __init__(self, message: str, kind: str = "PandocOptionError") -> None:
        super().__init__(message)
        self.kind = kind


def _refusal(what: str) -> str:
    return f"not allowed for untrusted code: {what}"


def _untrusted(what: Any) -> dict[str, Any]:
    """What a filter gives pandoc (options, or a query), marked
    ``"untrusted": true`` for libpandoc to check: it accepts only what
    reads and writes no files, fetches nothing and runs nothing, and turns
    pandoc's sandbox on. The list is libpandoc's, the same for every host."""
    if not isinstance(what, dict):
        raise _Refused(_refusal("options that aren't an object"))
    return {**what, "untrusted": True}
