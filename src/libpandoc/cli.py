"""pandocpy: pandoc's command line, in process, through libpandoc.

    pandocpy [pandoc's arguments]

It takes pandoc's arguments and does what pandoc does, with one difference:
a filter named with ``-F``/``--filter`` that is an installed Python filter,
an entry point in the ``pandom.filters`` group, runs in this process,
inside the conversion, instead of as a separate program:

    # pyproject.toml of a filter package
    [project.entry-points."pandom.filters"]
    pantable = "pantable:filter"      # a pandom.Filter, or a function

    $ pandocpy -F pantable input.md -o output.html

The filter then knows how the document is being read (``ctx.read``) and
calls pandoc in the same process. Other filters run as with pandoc.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, entry_points, version
from typing import Any

from . import (
    PandocError,
    PandocWarning,
    _callback,
    _check,
    _core,
    default_template,
    extensions,
    input_formats,
    output_formats,
    pandoc_version,
    query,
)

GROUP = "pandom.filters"

USAGE = """\
usage: pandocpy [pandoc's options] [input files]

pandoc's command line, run in process through libpandoc: the options are
pandoc's (https://pandoc.org/MANUAL.html). A filter named with -F/--filter
that is an installed Python filter (an entry point in the "pandom.filters"
group) runs in this process, inside the conversion.

Installed Python filters: {filters}
"""


def installed_filters() -> dict[str, Any]:
    """The installed Python filters, by name (not yet loaded)."""
    return {ep.name: ep for ep in entry_points(group=GROUP)}


def with_python_filters(
    args: Sequence[str], available: dict[str, Any]
) -> tuple[list[str], list[Callable[..., Any]]]:
    """Replace each -F NAME naming an installed Python filter by the Lua
    filter path libpandoc answers with a callback (and load the filter)."""
    out: list[str] = []
    filters: list[Callable[..., Any]] = []

    def python(name: str) -> str | None:
        ep = available.get(name)
        if ep is None:
            return None
        filters.append(ep.load())
        return f"--lua-filter=libpandoc:callback/{len(filters) - 1}"

    it = iter(args)
    for a in it:
        if a in ("-F", "--filter"):
            name = next(it, None)
            if name is None:
                out.append(a)
                break
            path = python(name)
            out.extend([path] if path else [a, name])
        elif a.startswith("--filter="):
            out.append(python(a[len("--filter=") :]) or a)
        elif a.startswith("-F") and len(a) > 2:
            out.append(python(a[2:]) or a)
        else:
            out.append(a)
    return out, filters


def run(args: Sequence[str], input: bytes | None = None) -> bytes:
    """Run pandoc with command-line arguments, installed Python filters in
    process. Returns what pandoc writes to standard output."""
    argv, filters = with_python_filters(args, installed_filters())
    callbacks = tuple(_callback((f,), {}) for f in filters)
    encoded = tuple(a.encode() for a in argv)
    if callbacks:
        return _check(*_core.convert_args_filters(encoded, input, callbacks))
    return _check(*_core.convert_args(encoded, input))


def _informational(args: Sequence[str]) -> str | None:
    """Answers to pandoc's informational options, from libpandoc's queries."""
    if not args:
        return None
    a, rest = args[0], args[1:]
    if a in ("-v", "--version"):
        try:
            ours = version("libpandoc")
        except PackageNotFoundError:
            ours = "(development)"
        return f"pandocpy (libpandoc {ours})\npandoc {pandoc_version()}, in process\n"
    if a in ("-h", "--help"):
        names = ", ".join(sorted(installed_filters())) or "none"
        return USAGE.format(filters=names)
    if a == "--list-input-formats":
        return "".join(f"{f}\n" for f in input_formats())
    if a == "--list-output-formats":
        return "".join(f"{f}\n" for f in output_formats())
    if a == "--list-extensions" or a.startswith("--list-extensions="):
        fmt = a.partition("=")[2] or "markdown"
        exts = extensions(fmt)
        return "".join(f"{'+' if on else '-'}{name}\n" for name, on in exts.items())
    if a == "--list-highlight-languages":
        return "".join(f"{x}\n" for x in query("highlight-languages"))
    if a == "--list-highlight-styles":
        return "".join(f"{x}\n" for x in query("highlight-styles"))
    if a in ("-D", "--print-default-template") or a.startswith("--print-default-template="):
        fmt = a.partition("=")[2] or (rest[0] if rest else "")
        return default_template(fmt)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        info = _informational(args)
        if info is not None:
            sys.stdout.write(info)
            return 0
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", PandocWarning)
            out = run(args)
        for w in caught:
            print(f"[WARNING] {w.message}", file=sys.stderr)
    except PandocError as e:
        print(e.message, file=sys.stderr)
        return 1
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
