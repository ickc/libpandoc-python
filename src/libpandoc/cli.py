"""pandocpy: pandoc's command line, in process, through libpandoc.

    pandocpy [pandoc's arguments]

It is the pandoc command (libpandoc's ``pandoc_main``: pandoc parses the
arguments, reads and writes, and reports errors itself, with its exit
status), with one difference: a filter named with ``-F``/``--filter``, on
the command line or in a defaults file, that is an installed Python filter,
an entry point in the ``panir.filters`` group, runs in this process, inside
the conversion, instead of as a separate program:

    # pyproject.toml of a filter package
    [project.entry-points."panir.filters"]
    pantable = "pantable:filter"      # a panir.Filter, or a function

    $ pandocpy -F pantable input.md -o output.html

Python filter scripts (``-F foo.py``) run in this process too, unless they or
the user say otherwise (``# pandocpy: subprocess``, ``$PANDOCPY_SUBPROCESS``):
see ``libpandoc._scripts``. So do wasm filters (``-F foo.wasm``, with
``libpandoc[wasm]``), sandboxed, seeing no directory unless
``$LIBPANDOC_WASM_DIRS`` gives some: see ``libpandoc.WasmFilter``. Other
filters run as with pandoc.

Consecutive filters of this process are one exchange with pandoc (one
callback, ``libpandoc._run``); ``$LIBPANDOC_SEPARATE_FILTERS=1`` makes each
its own, so that pandoc reads and checks the document between any two.

With ``$LIBPANDOC_SANDBOX`` or ``$LIBPANDOC_SANDBOX_WRITE`` set, it is the
pandoc command of libpandoc.wasm, in the directory sandbox those list (see
``libpandoc._sandbox``): the filters above run in this process as without
it, Lua filters inside, JSON filters not at all.

Ctrl-C ends it at once, as it ends pandoc: while pandoc runs, SIGINT has
its default action (Python's handler would run only when pandoc returned).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, distributions, version
from typing import Any

from . import PandocError, _callbacks, _core, _run, _sandbox, query
from ._wasmfilter import WasmFilterError

GROUP = "panir.filters"
PROG = "pandocpy"

# pandoc's exit status for a failed filter
FILTER_FAILED = 83


def installed_filters() -> dict[str, Any]:
    """The installed Python filters, by name (not yet loaded).

    Installed ones only: the current directory, which ``python -m`` puts on
    ``sys.path``, isn't searched (it may be large, and isn't installed)."""
    here = {"", os.getcwd()}
    path = [p for p in sys.path if p not in here]
    found: dict[str, Any] = {}
    for dist in distributions(path=path):
        for ep in dist.entry_points.select(group=GROUP):
            found.setdefault(ep.name, ep)
    return found


def plan(
    filters: Sequence[Mapping[str, Any]], available: Mapping[str, Any],
    args: Sequence[str] = (), separate: bool = False,
) -> tuple[list[Any], tuple[Callable[..., Any], ...]]:
    """pandoc's filter list with installed Python filters (loaded), Python
    filter scripts and wasm filters as callbacks: each run of consecutive
    ones one callback (one exchange with pandoc), or with ``separate`` each
    its own."""
    listed: list[Any] = []
    for f in filters:
        ep = available.get(f["path"]) if f.get("type") == "json" else None
        listed.append(ep.load() if ep is not None else dict(f))
    callbacks, entries = _callbacks(listed, None, args, separate=separate)
    return entries, callbacks


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        # (as text: a file name that isn't UTF-8 is U+FFFD here, which
        # matters only if it names a filter; pandoc_main gets the bytes)
        parsed = query("parse-args", args=[_text(a) for a in args])
    except PandocError:
        parsed = {}  # pandoc_main reports it, as pandoc does
    wanted = [f for f in parsed.get("filters", ()) if f.get("type") == "json"]
    help_ = parsed.get("informational") == "Help"
    available = installed_filters() if wanted or help_ else {}
    filters_json, callbacks = None, ()
    if "filters" in parsed:
        try:
            separate = _run.separate(None) if parsed["filters"] else False
        except ValueError as e:
            print(f"{PROG}: {e}", file=sys.stderr)
            return 1
        try:
            entries, cbs = plan(parsed["filters"], available, args, separate)
        except (OSError, ImportError, ValueError) as e:  # a wasm filter
            print(f"{PROG}: {e}", file=sys.stderr)
            return FILTER_FAILED
        if cbs:
            filters_json = json.dumps(entries).encode()
            callbacks = tuple(cbs)
    sys.stdout.flush()
    sys.stderr.flush()
    # the bytes of each argument, as the file system's encoding gives them
    # (pandoc_main decodes them so): any file name works
    command = tuple(map(os.fsencode, (PROG, *args)))
    try:
        dirs = _sandbox.from_env()
        with _default_sigint():
            if dirs is not None:
                status = _sandbox.main(command, filters_json, callbacks, dirs)
            else:
                status = _core.main(command, filters_json, callbacks)
    except WasmFilterError as e:
        # (and its notes: which filter it was)
        print(f"{PROG}: {e}", *getattr(e, "__notes__", ()), sep="\n", file=sys.stderr)
        return FILTER_FAILED
    except Exception:  # noqa: BLE001  a filter's exception, whatever it is
        traceback.print_exc()
        return FILTER_FAILED
    info = parsed.get("informational")
    if info == "Help":
        names = ", ".join(sorted(available)) or "none"
        print(f"\nInstalled Python filters, run in process by -F NAME: {names}")
    elif info == "VersionInfo":
        try:
            ours = version("libpandoc")
        except PackageNotFoundError:
            ours = "(development)"
        print(f"{PROG}: pandoc in process, through libpandoc {ours}")
    return status


@contextlib.contextmanager
def _default_sigint() -> Iterator[None]:
    """SIGINT's default action meanwhile (on the main thread; elsewhere
    nothing changes)."""
    try:
        before = signal.signal(signal.SIGINT, signal.SIG_DFL)
    except ValueError:  # (not the main thread)
        before = None
    try:
        yield
    finally:
        if before is not None:
            signal.signal(signal.SIGINT, before)


def _text(arg: str) -> str:
    """An argument as UTF-8 text, bytes that aren't text as U+FFFD."""
    return os.fsencode(arg).decode("utf-8", "replace")


def run_main() -> None:
    """The console script: main(), quiet if its output is cut short (as by
    ``| head``), as pandoc is."""
    try:
        status = main()
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        status = 0
    sys.exit(status)


if __name__ == "__main__":
    run_main()
