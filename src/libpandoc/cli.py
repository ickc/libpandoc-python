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

Consecutive installed Python filters share one pass over the document.

Python filter scripts (``-F foo.py``) run in this process too, unless they or
the user say otherwise (``# pandocpy: subprocess``, ``$PANDOCPY_SUBPROCESS``):
see ``libpandoc._scripts``. So do wasm filters (``-F foo.wasm``, with
``libpandoc[wasm]``), sandboxed: see ``libpandoc.WasmFilter``. Other filters
run as with pandoc.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, distributions, version
from typing import Any

from . import PandocError, _callbacks, _core, query
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
    args: Sequence[str] = (),
) -> tuple[list[Any], tuple[Callable[..., Any], ...]]:
    """pandoc's filter list with installed Python filters (loaded), Python
    filter scripts and wasm filters as callbacks: each run of consecutive
    installed ones is one callback (one pass of JSON), each script or wasm
    filter one."""
    listed: list[Any] = []
    for f in filters:
        ep = available.get(f["path"]) if f.get("type") == "json" else None
        listed.append(ep.load() if ep is not None else dict(f))
    callbacks, entries = _callbacks(listed, None, args)
    return entries, callbacks


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        parsed = query("parse-args", args=args)
    except PandocError:
        parsed = {}  # pandoc_main reports it, as pandoc does
    wanted = [f for f in parsed.get("filters", ()) if f.get("type") == "json"]
    help_ = parsed.get("informational") == "Help"
    available = installed_filters() if wanted or help_ else {}
    filters_json, callbacks = None, ()
    if "filters" in parsed:
        try:
            entries, cbs = plan(parsed["filters"], available, args)
        except (OSError, ImportError, ValueError) as e:  # a wasm filter
            print(f"{PROG}: {e}", file=sys.stderr)
            return FILTER_FAILED
        if cbs:
            filters_json = json.dumps(entries).encode()
            callbacks = tuple(cbs)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        status = _core.main(tuple(a.encode() for a in (PROG, *args)), filters_json, callbacks)
    except WasmFilterError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
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
