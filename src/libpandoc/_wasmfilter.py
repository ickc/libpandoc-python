"""Wasm filters, run in this process by wasmtime (``pip install libpandoc[wasm]``).

A wasm filter is a pandoc JSON filter built for WASI (``wasm32-wasip1``),
such as a panir Rust filter: the document on stdin, the new one on stdout,
the output format as its first argument, pandoc's variables in its
environment. It runs sandboxed: it sees only the directories it is given
(the current one, read-only, by default), no network, no programs. It may
call pandoc through the imports of module ``libpandoc`` (libpandoc-rs's
``guest.rs``), which this host answers on this process's pandoc, marked
``"untrusted": true``: libpandoc then runs them in pandoc's sandbox, with no
options that read or write files, fetch or run anything (its list, which
every host shares).

Limits, which pandoc has for no filter: a timeout (wall-clock, pandoc's
calls included) and a memory limit (the filter's own memory), from
``$LIBPANDOC_WASM_TIMEOUT`` (seconds, as pandoc-server's ``--timeout``) and
``$LIBPANDOC_WASM_MAX_MEMORY`` (bytes, or with k, m or g, as pandoc's
``+RTS -M``), or per filter; none by default, as pandoc.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
from collections.abc import Sequence
from typing import Any

TIMEOUT_VAR = "LIBPANDOC_WASM_TIMEOUT"
MAX_MEMORY_VAR = "LIBPANDOC_WASM_MAX_MEMORY"

# How often the engine's epoch advances, once a filter has a timeout (seconds).
_TICK = 0.01


def parse_timeout(s: str) -> float | None:
    """A timeout in seconds (``$LIBPANDOC_WASM_TIMEOUT``): None if empty or 0."""
    s = s.strip()
    if not s:
        return None
    try:
        t = float(s)
    except ValueError:
        t = -1.0
    if not (0 <= t < float("inf")):
        raise ValueError(f"{TIMEOUT_VAR}: seconds, not {s!r}")
    return t or None


def parse_memory(s: str) -> int | None:
    """A size in bytes, or with k, m or g (``$LIBPANDOC_WASM_MAX_MEMORY``, as
    pandoc's ``+RTS -M``): None if empty or 0."""
    s = s.strip()
    if not s:
        return None
    m = re.fullmatch(r"([0-9]+)([kKmMgG]?)", s)
    if m is None:
        raise ValueError(f"{MAX_MEMORY_VAR}: bytes, or with k, m or g, not {s!r}")
    n = int(m[1]) << {"": 0, "k": 10, "m": 20, "g": 30}[m[2].lower()]
    return n or None


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


def _engine() -> Any:
    """The engine every filter shares: compiled code cached on disk (e.g.
    ~/.cache/wasmtime), and wasm code checking an epoch, for timeouts."""
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
            _ENGINE = wasmtime.Engine(config)
        return _ENGINE


def _ticking() -> None:
    """The epoch's clock: a thread advancing it every _TICK (wasm runs
    without the GIL), started by the first filter with a timeout."""
    global _CLOCK
    engine = _engine()
    with _LOCK:
        if _CLOCK is None:
            def tick() -> None:
                while True:
                    time.sleep(_TICK)
                    engine.increment_epoch()

            _CLOCK = threading.Thread(target=tick, name="wasm-filter-clock", daemon=True)
            _CLOCK.start()


class WasmFilter:
    """A wasm filter: a pandoc JSON filter built for WASI (``wasm32-wasip1``),
    run in this process by wasmtime, sandboxed.

    It sees the directories ``dirs`` (host, guest pairs; by default the
    current one), read-only unless ``writable``, no network, and pandoc's
    filter variables. It may call pandoc: in pandoc's sandbox, with options
    that name no files.

    It fails when it runs longer than ``timeout`` seconds (its calls to
    pandoc included) or its memory would grow past ``max_memory`` bytes.
    None (the default) takes what ``$LIBPANDOC_WASM_TIMEOUT`` and
    ``$LIBPANDOC_WASM_MAX_MEMORY`` say when it runs (none if unset); 0 is no
    limit.

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
        self.name = os.fspath(path)
        try:
            self._module = wasmtime.Module.from_file(_engine(), self.name)
        except wasmtime.WasmtimeError as e:
            raise ValueError(f"{self.name}: not a wasm filter: {e}") from None
        self.dirs = [(os.fspath(h), g) for h, g in dirs]
        self.writable = writable
        self.timeout = timeout
        self.max_memory = max_memory
        self._pandoc_version = pandoc_version()  # now, not from inside a conversion

    def __repr__(self) -> str:
        return f"WasmFilter({self.name!r})"

    def __call__(self, doc: bytes, context: bytes) -> bytes:
        """Run the filter on a document (pandoc's JSON), given the context
        libpandoc gives a callback: as a libpandoc callback."""
        return self.run(doc, json.loads(context))

    def run(self, doc: bytes, ctx: dict[str, Any]) -> bytes:
        wasmtime = _wasmtime()
        timeout = (parse_timeout(os.environ.get(TIMEOUT_VAR, ""))
                   if self.timeout is None else self.timeout or None)
        max_memory = (parse_memory(os.environ.get(MAX_MEMORY_VAR, ""))
                      if self.max_memory is None else self.max_memory or None)
        if timeout is not None:
            _ticking()
        engine = _engine()

        wasi = wasmtime.WasiConfig()
        wasi.argv = [self.name, ctx.get("format") or ""]
        env = [("PANDOC_VERSION", self._pandoc_version),
               ("PANDOC_READER_OPTIONS", json.dumps(ctx.get("reader-options") or {}))]
        for var, key in (("PANDOC_INPUT_FORMAT", "input-format"),
                         ("PANDOC_OUTPUT_FORMAT", "output-format")):
            if ctx.get(key):
                env.append((var, ctx[key]))
        wasi.env = env
        _stdin_bytes(wasi, doc)
        out: list[bytes] = []
        wasi.stdout_custom = out.append
        wasi.inherit_stderr()
        for host, guest in self.dirs:
            try:
                wasi.preopen_dir(host, guest, self.writable)
            except wasmtime.WasmtimeError:
                raise RuntimeError(f"{self.name}: can't give it {host}") from None

        store = wasmtime.Store(engine)
        store.set_wasi(wasi)
        if max_memory is not None:
            store.set_limits(memory_size=max_memory)
        ticks = (2**63 if timeout is None else int(-(-timeout // _TICK)) + 1)
        store.set_epoch_deadline(ticks)
        linker = wasmtime.Linker(engine)
        linker.define_wasi()
        _define_libpandoc(wasmtime, linker)
        started = time.monotonic()

        def failed(msg: str, interrupted: bool = False) -> WasmFilterError:
            if interrupted:
                return WasmFilterError(
                    f"{self.name}: stopped after {time.monotonic() - started:.1f} s, "
                    f"its time limit ({TIMEOUT_VAR}: {timeout})")
            if max_memory is not None:
                msg += f" (its memory limit: {max_memory} bytes, {MAX_MEMORY_VAR})"
            return WasmFilterError(msg)

        try:
            instance = linker.instantiate(store, self._module)
            start = instance.exports(store).get("_start")
            if start is None:
                raise WasmFilterError(f"{self.name}: not a WASI command (no _start)")
            start(store)
        except wasmtime.ExitTrap as e:
            if e.code != 0:
                raise failed(f"{self.name} exited with status {e.code}") from None
        except wasmtime.Trap as e:
            interrupted = timeout is not None and e.trap_code == wasmtime.TrapCode.INTERRUPT
            raise failed(f"{self.name}: {e}", interrupted) from None
        except wasmtime.WasmtimeError as e:
            raise failed(f"{self.name}: {e}") from None
        return b"".join(out)


def _stdin_bytes(wasi: Any, data: bytes) -> None:
    """The filter's standard input: ``data`` (wasmtime's C API has it;
    wasmtime-py only files)."""
    from wasmtime import _ffi as ffi

    vec = ffi.wasm_byte_vec_t()
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    ffi.wasm_byte_vec_new(ctypes.byref(vec), len(data), buf)
    ffi.wasi_config_set_stdin_bytes(wasi.ptr(), ctypes.byref(vec))


# -- the libpandoc imports ---------------------------------------------------------


def _define_libpandoc(wasmtime: Any, linker: Any) -> None:
    """The ``libpandoc`` imports (libpandoc-rs's ``guest.rs``), on this
    process's pandoc; the result of a run's last call kept for
    ``result_len`` and ``result_read``."""
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
            status, output, kind, message, log = 1, b"", "PandocOptionError", str(e), b"[]"
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
            opts = _untrusted(guest_json(caller, o, ol))
            if not has:
                raise _Refused("a wasm filter gives convert its input")
            return _core.convert(json.dumps(opts).encode(), guest_bytes(caller, i, il))
        return answer(call)

    def read_many(caller: Any, p: int, length: int) -> int:
        def call() -> Any:
            req = guest_json(caller, p, length)
            if not isinstance(req, dict):
                raise _Refused(_refusal("a request that isn't an object"))
            req["options"] = _untrusted(req.get("options", {}))
            return _core.read_many(json.dumps(req).encode())
        return answer(call)

    def query(caller: Any, p: int, length: int) -> int:
        return answer(lambda: _core.query(
            json.dumps(_untrusted(guest_json(caller, p, length))).encode()))

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
    """A call pandoc isn't asked: its answer is a PandocOptionError."""


def _refusal(what: str) -> str:
    return f"not allowed for untrusted code: {what}"


def _untrusted(what: Any) -> dict[str, Any]:
    """What a filter gives pandoc (options, or a query), marked
    ``"untrusted": true`` for libpandoc (1.7) to check: it accepts only what
    reads and writes no files, fetches nothing and runs nothing, and turns
    pandoc's sandbox on. The list is libpandoc's, the same for every host."""
    from . import _core

    v = _core.abi_version()
    if v < 1007:
        raise _Refused(_refusal(
            f'a call from a wasm filter needs libpandoc 1.7 ("untrusted"), '
            f"not {v // 1000}.{v % 1000}"))
    if not isinstance(what, dict):
        raise _Refused(_refusal("options that aren't an object"))
    return {**what, "untrusted": True}
