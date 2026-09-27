"""pandocpy: pandoc's command line, in process, through libpandoc.

    pandocpy [pandoc's arguments]

It is the pandoc command (libpandoc's ``pandoc_main``: pandoc parses the
arguments, reads and writes, and reports errors itself, with its exit
status), with one difference: a filter named with ``-F``/``--filter``, on
the command line or in a defaults file, that is an installed Python filter,
an entry point in the ``pandom.filters`` group, runs in this process, inside
the conversion, instead of as a separate program:

    # pyproject.toml of a filter package
    [project.entry-points."pandom.filters"]
    pantable = "pantable:filter"      # a pandom.Filter, or a function

    $ pandocpy -F pantable input.md -o output.html

Consecutive installed Python filters share one pass over the document.

Python filter scripts (``-F foo.py``) run in this process too, unless they or
the user say otherwise (``# pandocpy: subprocess``, ``$PANDOCPY_SUBPROCESS``):
see ``libpandoc._scripts``. Other filters run as with pandoc.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, distributions, version
from typing import Any

from . import (
    PandocError,
    _callback,
    _conversion,
    _core,
    _scripts,
    pandoc_version,
    query,
)

GROUP = "pandom.filters"
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
    filters: Sequence[Mapping[str, Any]], available: Mapping[str, Any]
) -> tuple[list[Any], list[Callable[..., Any]]]:
    """pandoc's filter list with the installed Python filters as callbacks:
    each run of consecutive ones becomes one callback (one pass of JSON)."""
    entries: list[Any] = []
    callbacks: list[Callable[..., Any]] = []
    group: list[Any] = []

    def flush() -> None:
        if group:
            entries.append({"type": "callback", "index": len(callbacks)})
            callbacks.append(_callback(tuple(group), None))
            group.clear()

    for f in filters:
        path = f["path"] if f.get("type") == "json" else None
        ep = available.get(path) if path is not None else None
        if ep is not None:
            group.append(ep.load())
            continue
        flush()
        if path is not None and _scripts.is_python_script(path):
            # with this Python (and its packages), in process unless opted out
            in_process = _scripts.opted_out(path) is None
            entries.append({"type": "callback", "index": len(callbacks)})
            callbacks.append(
                _scripts.callback(path, pandoc_version(), _conversion, in_process=in_process)
            )
        else:
            entries.append(dict(f))
    flush()
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
        entries, cbs = plan(parsed["filters"], available)
        if cbs:
            filters_json = json.dumps(entries).encode()
            callbacks = tuple(cbs)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        status = _core.main(tuple(a.encode() for a in (PROG, *args)), filters_json, callbacks)
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
