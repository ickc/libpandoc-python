"""Python filter scripts, run in this process: by pandocpy and libpandoc.

A filter that is a Python script (``.py``, or a Python ``#!`` line) runs in
this process instead of a new one: as its own ``__main__``, with the
document as its standard input, the output format as its argument, the
environment variables libpandoc gives JSON filters (pandoc's, and the
formats with extensions and ``PANDOC_WRITER_OPTIONS``), and its standard
output as the result. That serves
any framework (panflute, pandocfilters, panir, plain JSON) exactly as its
own code runs, minus starting a Python process and importing it all again.
A panir script's ``f.main()`` hands its ``Filter`` over instead, which then
runs on the document directly, knowing the whole conversion.

Scripts may run in several threads at once (several conversions): while any
runs, ``sys.stdin``, ``sys.stdout``, ``sys.argv`` and ``os.environ`` are
stand-ins that are the running script's in its thread, and the real ones in
every other thread. Each run has a fresh module namespace.

What it can import is what ``python script.py`` gives it: the script's
folder (symbolic links resolved) is first on ``sys.path`` while it runs, so
a module or package beside it imports; afterwards the folder is taken out
again (also when the script fails), and so are the modules the run imported
from it, so that another script's module of the same name (two filters each
with a ``util.py``) is that script's own. ``__file__`` is the script's
absolute path, ``__name__`` is ``"__main__"`` and ``__spec__`` ``None``, the
working directory is this process's: all as ``python script.py`` has them.
What differs, as it would for any code run in a process already running:

- ``sys.argv[0]`` is the absolute path too (``runpy`` sets it to
  ``__file__``), where ``python script.py`` has the path as given;
- a module this process has already imported (the standard library's, or
  libpandoc's and panir's dependencies) is the one the script gets, where
  ``python script.py`` would find one of the same name beside it first;
- scripts running at once in several threads share ``sys.path`` and
  ``sys.modules``: each sees the other's folder on the path while both
  run, and two such scripts' modules of the same name are one;
- what a script changes in the process stays changed: its own ``sys.path``
  entries, the working directory, ``sys.setrecursionlimit``, logging and
  warnings filters, modules it imported from elsewhere; ``atexit``
  handlers run when this process exits, not when the script ends;
- ``sys.modules["__main__"]`` is the script's module only while it runs
  (``runpy``'s), and ``__loader__`` is ``None``.

Either way a script runs with this Python and its packages, rather than
whichever ``python`` is first on ``PATH`` (as pandoc would). Not in process,
it runs as a subprocess:

- when its author says so, with a line ``# pandocpy: subprocess`` near its
  top (for scripts that need a process of their own);
- when the user says so: ``PANDOCPY_SUBPROCESS=foo.py,bar`` (paths, file
  names or names without ``.py``; ``*`` for all);
- when it fails in process: it is run again as a subprocess, on the same
  document, with a warning.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import runpy
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import panir
import panir.filter

ENV = "PANDOCPY_SUBPROCESS"
_MARKER = re.compile(r"^#\s*pandocpy:\s*subprocess\b", re.MULTILINE)
_HEAD = 4096  # the marker is looked for in the first bytes of the script


def is_python_script(path: str) -> bool:
    """A file ending in ``.py``, or with a Python ``#!`` line."""
    p = Path(path)
    if not p.is_file():
        return False
    if p.suffix.lower() == ".py":
        return True
    try:
        with p.open("rb") as f:
            first = f.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def opted_out(path: str, env: Mapping[str, str] | None = None) -> str | None:
    """Why this script runs as a subprocess, if it does."""
    env = os.environ if env is None else env
    names = {n.strip() for n in env.get(ENV, "").split(",") if n.strip()}
    p = Path(path)
    if names & {"*", path, p.name, p.stem}:
        return f"${ENV}"
    try:
        with p.open("rb") as f:
            head = f.read(_HEAD).decode("utf-8", "replace")
    except OSError:
        return None
    if _MARKER.search(head):
        return "# pandocpy: subprocess"
    return None


class ScriptFailed(Exception):
    pass


def filter_env(ctx: Mapping[str, Any], pandoc_version: str) -> dict[str, str]:
    """The environment variables a JSON filter run by libpandoc gets, from a
    callback's context: pandoc's (``PANDOC_VERSION``,
    ``PANDOC_READER_OPTIONS``) and libpandoc's (the formats with extensions,
    ``PANDOC_WRITER_OPTIONS``: without ``variables`` if it would be longer
    than 32,000 bytes, an environment variable's limit on Windows, as
    libpandoc does)."""
    env = {
        "PANDOC_VERSION": pandoc_version,
        "PANDOC_READER_OPTIONS": json.dumps(ctx.get("reader-options") or {}),
    }
    for var, key in (("PANDOC_INPUT_FORMAT", "input-format"),
                     ("PANDOC_OUTPUT_FORMAT", "output-format")):
        if ctx.get(key):
            env[var] = ctx[key]
    writer = ctx.get("writer-options")
    if writer is not None:
        text = json.dumps(writer)
        if len(text) > 32_000:
            text = json.dumps({k: v for k, v in writer.items() if k != "variables"})
        env["PANDOC_WRITER_OPTIONS"] = text
    return env


def callback(
    path: str,
    pandoc_version: str,
    conversion: Callable[..., Any],
    *,
    options: Mapping[str, Any] | None = None,
    in_process: bool = True,
) -> Any:
    """A libpandoc callback running the script ``path`` in this process
    (falling back to a subprocess), or as a subprocess; with this Python
    either way, rather than whichever ``python`` is on ``PATH``."""

    def run(doc: bytes, context: bytes) -> bytes:
        ctx = json.loads(context)
        env = filter_env(ctx, pandoc_version)
        fmt = ctx.get("format") or ""
        if not in_process:
            return subprocess_run(path, doc, fmt, env)
        try:
            return _in_process(
                path, doc, fmt, env, lambda fs: _run_filters(fs, doc, ctx, conversion, options)
            )
        except Exception as e:  # noqa: BLE001  whatever the script raised
            print(
                f"[WARNING] {path} failed in process ({type(e).__name__}: {e}); "
                f"running it as a subprocess. If it needs a process of its own, add the "
                f"line '# pandocpy: subprocess' to it, or set {ENV}={Path(path).name}.",
                file=sys.stderr,
            )
            return subprocess_run(path, doc, fmt, env)

    return run


def _run_filters(
    fs: list[panir.Filter], doc: bytes, ctx: Mapping[str, Any], conversion: Any, options: Any
) -> bytes:
    """A panir script's Filters, handed over (one for each filter of its
    ``main([f, g])``): run on the document directly, in order."""
    user = None if options is None else {k: v for k, v in options.items() if k != "filters"}
    out = panir.loads(doc)
    for f in fs:
        out = f(out, conversion=conversion(ctx, user))
    return panir.dumps(out).encode()


def _in_process(
    path: str,
    doc: bytes,
    fmt: str,
    env: Mapping[str, str],
    handed: Callable[[list[panir.Filter]], bytes],
) -> bytes:
    filters: list[panir.Filter] = []
    out = io.BytesIO()
    token = panir.filter.handoff.set(filters.append)
    with _redirected(
        stdin=io.TextIOWrapper(io.BytesIO(doc), encoding="utf-8"),
        stdout=io.TextIOWrapper(out, encoding="utf-8", write_through=True),
        argv=[path, fmt],
        env=dict(env),
    ) as streams, _beside(os.path.dirname(os.path.realpath(path))):
        try:
            try:
                # (absolute, as python script.py makes __file__)
                runpy.run_path(os.path.abspath(path), run_name="__main__")
            except SystemExit as e:
                if e.code not in (None, 0):
                    raise ScriptFailed(f"it exited with {e.code}") from None
            try:
                streams.stdout.flush()
            except ValueError:
                pass  # the script closed or detached it
            result = out.getvalue()
        finally:
            panir.filter.handoff.reset(token)
    if filters:
        return handed(filters)
    try:
        json.loads(result)
    except ValueError:
        raise ScriptFailed("its output isn't a document (pandoc's JSON)") from None
    return result


@contextlib.contextmanager
def _beside(folder: str) -> Iterator[None]:
    """While a script runs, its folder first on ``sys.path``, as ``python
    script.py`` has it; after, the folder and the modules the run imported
    from it gone again."""
    before = sys.modules.copy()
    with _lock:
        sys.path.insert(0, folder)
    try:
        yield
    finally:
        with _lock, contextlib.suppress(ValueError):  # (unless the script took it out)
            sys.path.remove(folder)
        inside = os.path.join(folder, "")
        for name, module in sys.modules.copy().items():
            if name not in before and _imported_from(module, inside):
                sys.modules.pop(name, None)


def _imported_from(module: Any, folder: str) -> bool:
    """Whether a module (or a package, its folder) is in ``folder``."""
    places = [getattr(module, "__file__", None), *(getattr(module, "__path__", None) or ())]
    return any(isinstance(p, str) and os.path.realpath(p).startswith(folder) for p in places)


# -- per-thread standard streams, argv and environment -------------------------

_local = threading.local()
_lock = threading.Lock()
_running = 0
_real: dict[str, Any] = {}


class _Streams:
    def __init__(self, stdin: Any, stdout: Any) -> None:
        self.stdin, self.stdout = stdin, stdout


class _redirected:
    """While a script runs in this thread: its stdin, stdout, argv and
    environment here, the real ones elsewhere."""

    def __init__(self, stdin: Any, stdout: Any, argv: list[str], env: dict[str, str]) -> None:
        self.streams = _Streams(stdin, stdout)
        self.argv, self.env = argv, env

    def __enter__(self) -> _Streams:
        global _running
        with _lock:
            if _running == 0:
                _real.update(stdin=sys.stdin, stdout=sys.stdout, argv=sys.argv, environ=os.environ)
                sys.stdin = _Stream("stdin")  # type: ignore[assignment]
                sys.stdout = _Stream("stdout")  # type: ignore[assignment]
                sys.argv = _Argv()
                os.environ = _Environ(_real["environ"])  # noqa: B003  a view, not a new env
            _running += 1
        self.saved = [getattr(_local, k, None) for k in ("stdin", "stdout", "argv", "env")]
        _local.stdin, _local.stdout = self.streams.stdin, self.streams.stdout
        _local.argv, _local.env = self.argv, self.env
        return self.streams

    def __exit__(self, *exc: object) -> None:
        global _running
        _local.stdin, _local.stdout, _local.argv, _local.env = self.saved
        with _lock:
            _running -= 1
            if _running == 0:
                # put the real ones back, unless something else replaced ours
                if isinstance(sys.stdin, _Stream):
                    sys.stdin = _real["stdin"]
                if isinstance(sys.stdout, _Stream):
                    sys.stdout = _real["stdout"]
                if isinstance(sys.argv, _Argv):
                    sys.argv = _real["argv"]
                if isinstance(os.environ, _Environ):
                    os.environ = _real["environ"]  # noqa: B003


class _Stream:
    """``sys.stdin`` or ``sys.stdout``: this thread's script's, else the real one."""

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def _target(self) -> Any:
        t = getattr(_local, self._name, None)
        return t if t is not None else _real[self._name]

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._target(), attr)

    def __setattr__(self, attr: str, value: Any) -> None:
        setattr(self._target(), attr, value)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._target())

    def __next__(self) -> Any:
        return next(self._target())


class _Argv(list):
    """``sys.argv``: this thread's script's, else the real one."""

    def _target(self) -> list[str]:
        t = getattr(_local, "argv", None)
        return t if t is not None else _real["argv"]

    def __getitem__(self, i: Any) -> Any:
        return self._target()[i]

    def __setitem__(self, i: Any, v: Any) -> None:
        self._target()[i] = v

    def __len__(self) -> int:
        return len(self._target())

    def __iter__(self) -> Iterator[str]:
        return iter(self._target())

    def __contains__(self, x: object) -> bool:
        return x in self._target()

    def __repr__(self) -> str:
        return repr(self._target())

    def __eq__(self, other: object) -> bool:
        return self._target() == other

    __hash__ = None  # type: ignore[assignment]

    def __getattribute__(self, name: str) -> Any:
        if name in ("_target", "__class__") or name.startswith("__"):
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, "_target")(), name)


class _Environ(os._Environ):  # type: ignore[name-defined]
    """``os.environ``: the real one, with this thread's script's variables
    (pandoc's ``PANDOC_*``) over it."""

    def __init__(self, real: Any) -> None:
        super().__init__(real._data, real.encodekey, real.decodekey,
                         real.encodevalue, real.decodevalue)

    @staticmethod
    def _over() -> dict[str, str]:
        return getattr(_local, "env", None) or {}

    def __getitem__(self, key: str) -> str:
        over = self._over()
        return over[key] if key in over else super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        return key in self._over() or super().__contains__(key)

    def __iter__(self) -> Iterator[str]:
        over = self._over()
        yield from over
        yield from (k for k in super().__iter__() if k not in over)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def copy(self) -> dict[str, str]:
        return dict(self)


def subprocess_run(path: str, doc: bytes, fmt: str, env: Mapping[str, str]) -> bytes:
    """As pandoc runs a Python JSON filter (with this Python)."""
    proc = subprocess.run(
        [sys.executable, path, fmt],
        input=doc,
        capture_output=True,
        env={**os.environ, **env},
        check=False,
    )
    sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    if proc.returncode != 0:
        raise RuntimeError(f"{path} returned error status {proc.returncode}")
    return proc.stdout
