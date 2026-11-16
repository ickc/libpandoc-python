"""Conversions in a directory sandbox: the whole of pandoc runs inside
libpandoc.wasm (pandoc built for WebAssembly), under wasmtime, which gives
it the directories the caller lists and nothing else, as a container's
mounts do. libpandoc-rs's host (``libpandoc/src/sandbox.rs``), in Python.

The setting: ``"sandbox": {"read": [dirs], "write": [dirs]}`` in a
conversion's options (with, if given, ``"timeout"`` in seconds and
``"max-memory"`` in bytes), or ``$LIBPANDOC_SANDBOX`` and
``$LIBPANDOC_SANDBOX_WRITE`` (lists as ``PATH`` is) for ``run`` and
``pandocpy``. ``"sandbox": True`` keeps meaning pandoc's own sandbox in the
native library.

What the module sees:

- each listed directory at its own absolute path (on Windows ``C:\\x`` is
  ``/c/x``), read-only, or writable if in the write list;
- nothing else: not the user data directory, not ``PANDOC_DATA_DIRS``,
  unless the caller lists them (pandoc inside is then told where they are);
- a private, writable temporary directory, removed after;
- as its working directory, the process's, if that is inside a listed
  directory (else an empty one: relative names find nothing);
- no network (libpandoc.wasm is built without HTTP), no programs; of the
  environment, ``SOURCE_DATE_EPOCH`` only.

The options and arguments go to pandoc untouched. Lua filters run inside;
filters that are programs (JSON filters) fail, as a PDF engine does; Python
filters (functions, ``panir.Filter``s, filter scripts) and wasm filters run
in this process, called back by the module on the calling thread, a wasm
filter seeing the same directories. Limits: ``$LIBPANDOC_WASM_TIMEOUT`` and
``$LIBPANDOC_WASM_MAX_MEMORY`` (or the sandbox's ``"timeout"`` and
``"max-memory"``), as for wasm filters, counted from the module's start.

The module: ``$LIBPANDOC_WASM``, else libpandoc's release's, downloaded
once into the user's cache and checked against the release's
``SHA256SUMS``. Compiled once per process, and cached on disk by wasmtime
(the first compilation takes about 20 s and 8 GB of memory).

Threads: the module runs on the calling thread, and so do the filters it
calls back. Its stack needs little: GHC's code keeps its stack in the
module's memory (documents nested 100,000 deep run in 16 KiB of wasm
stack), and wasmtime stops any recursion past 512 KiB (Lua's parser nested
200 deep, the deepest seen, needs less) with a trap; Python's own threads
have 8 MiB (Linux), 16 MiB (macOS) or about 2 MiB (Windows).
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple

from ._wasmfilter import (
    MAX_MEMORY_VAR,
    TIMEOUT_VAR,
    WasmFilter,
    engine,
    limit,
    limits,
    ticking,
)

READ_VAR = "LIBPANDOC_SANDBOX"
WRITE_VAR = "LIBPANDOC_SANDBOX_WRITE"
WASM_VAR = "LIBPANDOC_WASM"

# The host's own directories in the module: the private temporary
# directory, and an empty one, read-only, as the working directory when the
# process's isn't mounted (pandoc's command needs one).
OWN = "/.libpandoc"
TMP = "/.libpandoc/tmp"
NOWHERE = "/.libpandoc/nowhere"

# libpandoc's release whose libpandoc.wasm is downloaded when none is given.
RELEASE = "continuous"
RELEASE_URL = f"https://github.com/ickc/libpandoc/releases/download/{RELEASE}/"

logger = logging.getLogger("libpandoc")

Result = tuple[int, bytes, "str | None", "str | None", bytes]


def _refused(message: str, kind: str = "PandocOptionError") -> Exception:
    from . import _error

    return _error(kind, message)


class Dirs(NamedTuple):
    """A sandbox: the directories to read, those to write (and read), and
    its limits (None: the environment's)."""

    read: Sequence[Any] = ()
    write: Sequence[Any] = ()
    timeout: Any = None
    max_memory: Any = None


def from_options(options: Mapping[str, Any]) -> Dirs | None:
    """The sandbox of ``"sandbox": {...}``, if the options have one."""
    s = options.get("sandbox")
    if not isinstance(s, Mapping):
        return None  # (True, False: pandoc's own, in the native library)
    lists: dict[str, list[Any]] = {}
    for k, v in s.items():
        if k in ("read", "write"):
            if isinstance(v, (str, bytes)) or not isinstance(v, Sequence) or not all(
                    isinstance(d, (str, bytes, os.PathLike)) for d in v):
                raise _refused(f"sandbox: {json.dumps(k)} lists directories as strings")
            lists[k] = list(v)
        elif k not in ("timeout", "max-memory"):
            raise _refused(f'sandbox: {json.dumps(k)}: only "read", "write", "timeout" and '
                           f'"max-memory"')
    timeout, max_memory = s.get("timeout"), s.get("max-memory")
    limits(timeout if timeout is not None else 0, max_memory if max_memory is not None else 0)
    return Dirs(lists.get("read", ()), lists.get("write", ()), timeout, max_memory)


def from_env() -> Dirs | None:
    """The sandbox of ``$LIBPANDOC_SANDBOX`` and ``$LIBPANDOC_SANDBOX_WRITE``,
    if either is set (set but empty: no directory)."""
    read, write = os.environ.get(READ_VAR), os.environ.get(WRITE_VAR)
    if read is None and write is None:
        return None
    split = lambda v: [d for d in (v or "").split(os.pathsep) if d]
    return Dirs(split(read), split(write))


# -- the mounts ----------------------------------------------------------------


def _guest(path: str) -> str:
    """An absolute host path as the module names it: ``/`` and its parts
    (``C:\\x`` is ``/c/x``)."""
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    drive = drive.removeprefix("\\\\?\\")
    if drive and not (len(drive) == 2 and drive[1] == ":" and drive[0].isalpha()):
        raise _refused(f"{path}: not supported in a sandbox")
    parts = [drive[:1].lower()] if drive else []
    parts += [p for p in rest.replace("\\", "/").split("/") if p not in ("", ".")]
    guest = "/" + "/".join(parts)
    try:
        guest.encode("utf-8")
    except UnicodeEncodeError:
        raise _refused(f"{path!r}: not UTF-8") from None
    return guest


def _within(p: str, d: str) -> bool:
    """Whether the module's path ``p`` is ``d`` or inside it."""
    return d == "/" or p == d or p.startswith(d + "/")


def _data_dir() -> str | None:
    """pandoc's default user data directory, if it exists:
    ``$XDG_DATA_HOME/pandoc`` (``~/.local/share/pandoc``), else
    ``~/.pandoc``; ``%APPDATA%\\pandoc`` on Windows."""
    env = lambda k: os.environ.get(k) or None
    if sys.platform == "win32":
        candidates = [os.path.join(env("APPDATA") or "", "pandoc")] if env("APPDATA") else []
    else:
        home = env("HOME")
        xdg = env("XDG_DATA_HOME") or (home and os.path.join(home, ".local", "share"))
        candidates = [os.path.join(d, n) for d, n in ((xdg, "pandoc"), (home, ".pandoc")) if d]
    return next((d for d in candidates if os.path.isdir(d)), None)


class Mounts:
    """What one conversion's module sees: ``mounts`` (the module's path:
    the host's, whether writable), its environment, its working directory
    and whether it is inside a mount (None if not, else whether writable),
    and a private directory, removed by ``close``."""

    def __init__(self, dirs: Dirs) -> None:
        self.root = tempfile.mkdtemp(prefix="libpandoc-sandbox-")  # (mode 0o700)
        try:
            for d in ("tmp", "nowhere"):
                os.mkdir(os.path.join(self.root, d))
            self.mounts: dict[str, tuple[str, bool]] = {}
            self.env = [("TMPDIR", TMP)]
            # pandoc's own way to a reproducible docx, odt or epub; nothing
            # else of the environment goes in
            if "SOURCE_DATE_EPOCH" in os.environ:
                self.env.append(("SOURCE_DATE_EPOCH", os.environ["SOURCE_DATE_EPOCH"]))
            for d in dirs.read:
                self._add(os.fsdecode(d), False)
            for d in dirs.write:
                self._add(os.fsdecode(d), True)
            # The user data directory and PANDOC_DATA_DIRS are not mounted
            # by themselves: pandoc inside is told where they are only if
            # the caller listed them.
            data = _data_dir()
            if data is not None and self._mounted(data):
                var = "HOME" if os.path.basename(data) == ".pandoc" else "XDG_DATA_HOME"
                self.env.append((var, _guest(os.path.dirname(os.path.abspath(data)))))
            inside = [_guest(d) for d in os.environ.get("PANDOC_DATA_DIRS", "").split(os.pathsep)
                      if os.path.isabs(d) and os.path.isdir(d) and self._mounted(d)]
            if inside:
                self.env.append(("PANDOC_DATA_DIRS", ":".join(inside)))
            try:
                here = os.getcwd()
            except OSError as e:
                raise _refused(f"the working directory: {e.strerror}", "PandocIOError") from None
            self.cwd = _guest(os.path.realpath(here))
            within = [g for g in self.mounts if _within(self.cwd, g)]
            self.cwd_mounted = self.mounts[max(within, key=len)][1] if within else None
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def note(self) -> str | None:
        """What a failure says when the working directory isn't mounted."""
        if self.cwd_mounted is not None:
            return None
        return (f"(the working directory, {self.cwd}, is not in the sandbox: "
                f"relative names find nothing)")

    def _mounted(self, d: str) -> bool:
        """Whether ``d`` is inside a directory the caller listed."""
        names = {_guest(os.path.realpath(d)), _guest(d)}
        return any(_within(n, g) for n in names for g in self.mounts)

    def _add(self, d: str, writable: bool) -> None:
        """A directory at its own absolute path (its real one, and as named
        if that differs, as macOS's ``/tmp`` for ``/private/tmp``);
        writable if listed so anywhere."""
        try:
            real = os.path.realpath(d, strict=True)
        except OSError as e:
            raise _refused(f"sandbox: {d}: {e.strerror}") from None
        if not os.path.isdir(real):
            raise _refused(f"sandbox: {d}: not a directory")
        for g in (_guest(real), _guest(d)):
            if _within(g, OWN):
                raise _refused(f"sandbox: {d}: reserved")
            host, was = self.mounts.get(g, (real, False))
            self.mounts[g] = (host, was or writable)


# -- the module ----------------------------------------------------------------


_LOCK = threading.Lock()
_MODULES: dict[str, Any] = {}  # compiled, by path
# (dropped before wasmtime-py's own globals are, which its finalizer needs)
atexit.register(_MODULES.clear)


def _cache_dir() -> str:
    """Where a downloaded libpandoc.wasm is kept: the user's cache
    (``$XDG_CACHE_HOME``, ``~/.cache``; ``~/Library/Caches`` on macOS,
    ``%LOCALAPPDATA%`` on Windows), ``libpandoc/RELEASE``."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    if not os.path.isabs(base):
        raise _refused(f"no cache directory for libpandoc.wasm: set {WASM_VAR}", "PandocAppError")
    return os.path.join(base, "libpandoc", RELEASE)


def _download(path: str) -> None:
    """libpandoc.wasm of the release, checked against its SHA256SUMS,
    written beside ``path`` and then moved there (never half there; two
    processes at once each write their own)."""
    import hashlib
    import urllib.request

    logger.warning("downloading libpandoc.wasm (libpandoc %s, about 60 MB) into %s, once; "
                   "its first compilation takes about 20 s and 8 GB of memory", RELEASE, path)
    try:
        with urllib.request.urlopen(RELEASE_URL + "SHA256SUMS", timeout=60) as r:
            sums = r.read().decode()
        want = next((f[0] for f in map(str.split, sums.splitlines())
                     if f[1:] in (["libpandoc.wasm"], ["*libpandoc.wasm"])), None)
        if want is None:
            raise _refused(f"libpandoc.wasm is not in {RELEASE_URL}SHA256SUMS", "PandocAppError")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = f"{path}.{os.getpid()}-{threading.get_ident()}.part"
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(RELEASE_URL + "libpandoc.wasm", timeout=60) as r, \
                    open(part, "wb") as f:
                while chunk := r.read(1 << 20):
                    digest.update(chunk)
                    f.write(chunk)
            if digest.hexdigest() != want:
                raise _refused(
                    f"{RELEASE_URL}libpandoc.wasm: SHA-256 {digest.hexdigest()}, but SHA256SUMS "
                    f"says {want} (the release may be being replaced: try again in a few "
                    f"minutes)", "PandocAppError")
            os.replace(part, path)
        finally:
            with contextlib.suppress(OSError):
                os.remove(part)
    except OSError as e:  # (urllib's errors among them)
        raise _refused(f"downloading libpandoc.wasm: {e} (or set {WASM_VAR})",
                       "PandocAppError") from None


def _module() -> tuple[Any, str]:
    """The module and its path, compiled once (and cached by wasmtime)."""
    try:
        import wasmtime
    except ImportError:
        raise _refused(
            'a directory sandbox ("sandbox": {"read": [...]}, LIBPANDOC_SANDBOX) runs pandoc '
            "in libpandoc.wasm under wasmtime: pip install 'libpandoc[wasm]'") from None
    path = os.environ.get(WASM_VAR) or os.path.join(_cache_dir(), "libpandoc.wasm")
    with _LOCK:
        module = _MODULES.get(path)
        if module is None:
            if not os.environ.get(WASM_VAR) and not os.path.isfile(path):
                _download(path)
            try:
                module = _MODULES[path] = wasmtime.Module.from_file(engine(), path)
            except (OSError, wasmtime.WasmtimeError) as e:
                raise _refused(f"{path}: not libpandoc.wasm: {e}", "PandocAppError") from None
    return module, path


def _checked_abi(abi: int, path: str) -> None:
    """libpandoc's rule: the same major version, a minor at least this
    package's."""
    from . import _core

    need = _core.MIN_ABI_VERSION
    if abi // 1000 != need // 1000 or abi % 1000 < need % 1000:
        raise _refused(
            f"libpandoc.wasm {abi // 1000}.{abi % 1000} is loaded, and this package needs "
            f"libpandoc {need // 1000}.{need % 1000} or a later {need // 1000}.x: "
            + (f"set {WASM_VAR} to another" if os.environ.get(WASM_VAR)
               else f"remove {path}, and the current one is downloaded"), "PandocAppError")


class _Hooks:
    """The filters of this process the module calls back (``libpandoc.filter``
    with their index), on the calling thread, and the first exception one
    raised (raised from the call, as natively)."""

    def __init__(self, callbacks: Sequence[Callable[[bytes, bytes], bytes]], m: Mounts) -> None:
        self.callbacks, self.mounts = callbacks, m
        self.error: BaseException | None = None

    def taken(self) -> BaseException | None:
        """The exception, no longer kept here: its traceback goes through
        the frames that hold this object (no cycle to wait for)."""
        error, self.error = self.error, None
        return error

    def run(self, index: int, doc: bytes, context: bytes) -> bytes:
        f = self.callbacks[index]
        if isinstance(f, WasmFilter):  # the same directories, and the working one as "."
            m = self.mounts
            preopens = [(host, g, w) for g, (host, w) in m.mounts.items()]
            if m.cwd_mounted is not None:
                preopens.append((".", ".", m.cwd_mounted))
            return f.run(doc, json.loads(context), preopens)
        return f(doc, context)

    def define(self, wasmtime: Any, linker: Any) -> None:
        """``libpandoc.filter(index, doc, doc_len, context, context_len, out)``,
        answered with ``pandoc_buffer_set``. It never raises (wasmtime-py
        keeps a callback's exception in a global)."""
        i32 = wasmtime.ValType.i32()

        def filter_(caller: Any, index: int, doc: int, dl: int, ctx: int, cl: int, out: int) -> int:
            try:
                mem = caller["memory"]
                answer, status = self.run(index, _read(mem, caller, doc, dl),
                                          _read(mem, caller, ctx, cl)), 0
            except BaseException as e:  # noqa: BLE001  raised from the call
                if self.error is None:
                    self.error = e
                answer, status = str(e).encode("utf-8", "replace"), 1
            with contextlib.suppress(Exception):  # (then pandoc's message is empty)
                p = caller["malloc"](caller, max(len(answer), 1)) & 0xFFFFFFFF
                if p:
                    mem.write(caller, answer, p)
                    caller["pandoc_buffer_set"](caller, out, p, len(answer))
                    caller["free"](caller, p)
            return status

        linker.define_func("libpandoc", "filter", wasmtime.FuncType([i32] * 6, [i32]),
                           filter_, access_caller=True)


def _read(mem: Any, store: Any, p: int, n: int) -> bytes:
    p, n = p & 0xFFFFFFFF, n & 0xFFFFFFFF
    if p + n > mem.data_len(store):
        raise ValueError("libpandoc.wasm: out of its memory")
    return bytes(mem.read(store, p, p + n))


def _cstring(mem: Any, store: Any, p: int) -> bytes | None:
    """A NUL-terminated string in the module's memory (None for NULL)."""
    if p == 0:
        return None
    out, size = b"", mem.data_len(store)
    while p < size:
        chunk = bytes(mem.read(store, p, min(p + 65536, size)))
        if b"\0" in chunk:
            return out + chunk[:chunk.index(b"\0")]
        out, p = out + chunk, p + len(chunk)
    raise ValueError("libpandoc.wasm: a string out of its memory")


def _run(m: Mounts, call: tuple[Any, ...], input: bytes | None,
         callbacks: Sequence[Any], dirs: Dirs) -> tuple[Result, BaseException | None]:
    """Run ``call`` in a fresh instance of the module seeing the mounts:
    ``("options", bytes)`` (pandoc_convert_filters), ``("args", argv)``
    (pandoc_convert_args) or ``("main", argv, filters_json)`` (pandoc_main:
    its status only); its result as ``_core``'s, and the first exception a
    filter raised (to be raised instead, as natively). A PandocError if the
    module stopped (its time limit; out of memory) or can't start."""
    timeout, max_memory = limits(dirs.timeout, dirs.max_memory)
    module, path = _module()  # (first: without wasmtime, the setting is refused)
    import wasmtime

    wasi = wasmtime.WasiConfig()
    wasi.argv = ["libpandoc"]
    wasi.env = m.env
    wasi.inherit_stdout()
    wasi.inherit_stderr()
    if input is None:
        wasi.inherit_stdin()  # pandoc reads it when it has no input file, as the command does
    preopens = [(os.path.join(m.root, "tmp"), TMP, True),
                (os.path.join(m.root, "nowhere"), NOWHERE, False)]
    for g, (host, writable) in m.mounts.items():
        preopens.append((host, g, writable))
    for host, g, writable in preopens:
        try:
            wasi.preopen_dir(host, g, writable)
        except (wasmtime.WasmtimeError, UnicodeEncodeError):
            raise _refused(f"sandbox: {host}: wasmtime can't open it") from None
    store, linker = wasmtime.Store(engine()), wasmtime.Linker(engine())
    hooks = _Hooks(callbacks, m)
    started = time.monotonic()
    try:
        store.set_wasi(wasi)
        limit(store, timeout, max_memory)
        linker.define_wasi()
        hooks.define(wasmtime, linker)
        with ticking() if timeout is not None else contextlib.nullcontext():
            result = _call(store, linker.instantiate(store, module), call, input,
                           m.cwd if m.cwd_mounted is not None else NOWHERE, len(callbacks), path)
    except wasmtime.ExitTrap as e:
        raise _stopped(f"its runtime exited with status {e.code}", max_memory) from None
    except wasmtime.Trap as e:
        if timeout is not None and e.trap_code == wasmtime.TrapCode.INTERRUPT:
            given = TIMEOUT_VAR if dirs.timeout is None else 'the sandbox\'s "timeout"'
            raise _refused(f"the conversion was stopped after {time.monotonic() - started:.1f} s, "
                           f"its time limit ({given}: {timeout:g})", "Timeout") from None
        # Past its memory limit, the module's runtime faults (wasmtime-py
        # can't tell a refused growth from other faults): most likely that.
        oom = max_memory is not None and e.trap_code != wasmtime.TrapCode.STACK_OVERFLOW
        raise _stopped(str(e).strip().splitlines()[-1].strip(), max_memory if oom else 0) from None
    except (wasmtime.WasmtimeError, ValueError) as e:
        raise _refused(f"libpandoc.wasm stopped: {e}", "Exception") from None
    finally:
        # the instance's memory freed now, not when the last traceback
        # through this frame goes
        store.close()
        linker.close()
    return result, hooks.taken()


def _stopped(why: str, max_memory: int | None) -> Exception:
    """The module stopped: ``why``, and most likely out of memory unless
    ``max_memory`` is 0."""
    if max_memory == 0:
        return _refused(f"libpandoc.wasm stopped: {why}", "Exception")
    what = "wasm's 4 GiB" if max_memory is None else f"{max_memory} bytes, {MAX_MEMORY_VAR}"
    return _refused(f"libpandoc.wasm stopped ({why}), most likely out of memory "
                    f"(its limit: {what})", "Exception")


def _call(store: Any, instance: Any, call: tuple[Any, ...], input: bytes | None, cwd: str,
          nhooks: int, path: str) -> Result:
    ex = instance.exports(store)
    ex["_initialize"](store)
    _checked_abi(ex["pandoc_abi_version"](store), path)
    if ex["pandoc_init"](store) != 0:
        raise ValueError("pandoc didn't start")
    mem, malloc = ex["memory"], ex["malloc"]

    def put(b: bytes) -> int:
        p = malloc(store, max(len(b), 1)) & 0xFFFFFFFF  # (wasmtime-py's i32: signed)
        if p == 0:
            raise ValueError("out of memory")
        mem.write(store, b, p)
        return p

    def cstr(s: bytes) -> bytes:
        if b"\0" in s:
            raise _refused("an argument contains NUL")
        return s + b"\0"

    def argv(args: Sequence[bytes]) -> int:
        return put(b"".join(put(cstr(a)).to_bytes(4, "little") for a in args))

    # the callback filters, all the host's: pandoc_filter {fn = NULL, userdata = i}
    filters = b"".join((0).to_bytes(4, "little") + i.to_bytes(4, "little") for i in range(nhooks))
    ex["chdir"](store, put(cstr(cwd.encode())))
    ip, il = (put(input), len(input)) if input is not None else (0, 0)
    if call[0] == "options":
        r = ex["pandoc_convert_filters"](store, put(call[1]), len(call[1]), ip, il,
                                         put(filters), nhooks)
    elif call[0] == "args":
        r = ex["pandoc_convert_args"](store, len(call[1]), argv(call[1]), ip, il)
    else:
        _, args, fj = call
        jp, jl = (put(fj), len(fj)) if fj is not None else (0, 0)
        status = ex["pandoc_main"](store, len(args), argv(args), jp, jl, put(filters), nhooks)
        return status, b"", None, None, b"[]"
    r &= 0xFFFFFFFF
    if r == 0:
        raise ValueError("no result")
    # pandoc_result on wasm32: status, output, output_len, error_kind,
    # error_message, log; 4 bytes each
    w = [int.from_bytes(_read(mem, store, r + 4 * i, 4), "little") for i in range(6)]
    text = lambda b: None if b is None else b.decode("utf-8", "replace")
    return (w[0] - (w[0] >> 31 << 32), _read(mem, store, w[1], w[2]),
            text(_cstring(mem, store, w[3])), text(_cstring(mem, store, w[4])),
            _cstring(mem, store, w[5]) or b"[]")


# -- the entry points ------------------------------------------------------------


def _noted(r: Result, m: Mounts) -> Result:
    """A failure's message says so when the working directory isn't mounted."""
    status, output, kind, message, log = r
    note = m.note()
    if status != 0 and note is not None:
        message = f"{message or ''}\n{note}"
    return status, output, kind, message, log


def convert(options: Mapping[str, Any], input: bytes | None, dirs: Dirs,
            callbacks: Sequence[Any] = (),
            dumps: Callable[[Any], bytes] = lambda o: json.dumps(o).encode()) -> Result:
    """``convert`` in the sandbox: the options as they are but for
    ``"sandbox"``, with ``"filters"`` naming ``callbacks`` by index."""
    opts = {k: v for k, v in options.items() if k != "sandbox"}
    m = Mounts(dirs)
    try:
        result, error = _run(m, ("options", dumps(opts)), input, callbacks, dirs)
    finally:
        m.close()
    if error is not None:
        try:
            raise error
        finally:
            del error  # (no cycle through this frame)
    return _noted(result, m)


def convert_args(args: Sequence[bytes], input: bytes | None, dirs: Dirs) -> Result:
    """``run`` in the sandbox: the arguments as they are."""
    m = Mounts(dirs)
    try:
        return _noted(_run(m, ("args", args), input, (), dirs)[0], m)
    finally:
        m.close()


def main(argv: Sequence[bytes], filters_json: bytes | None, callbacks: Sequence[Any],
         dirs: Dirs) -> int:
    """The module's pandoc command (``argv``: the program's name first), on
    this process's standard input, output and error; its exit status. A
    failure of the sandbox's own (a directory, a limit) is printed, status
    1. ``filters_json`` (if given) replaces the filters pandoc found, naming
    ``callbacks``: their first exception is raised."""
    from . import PandocError

    try:
        m = Mounts(dirs)
        try:
            (status, *_), error = _run(m, ("main", argv, filters_json), None, callbacks, dirs)
        finally:
            m.close()
    except PandocError as e:
        print(f"{os.fsdecode(argv[0])}: {e}", file=sys.stderr)
        return 1
    if error is not None:
        try:
            raise error
        finally:
            del error  # (no cycle through this frame)
    note = m.note()
    if status != 0 and note is not None:
        print(note, file=sys.stderr)
    return status
