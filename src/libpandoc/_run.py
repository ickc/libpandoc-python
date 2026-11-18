"""Runs of filters: consecutive filters of this process, one exchange with
pandoc.

pandoc hands a filter in this process (a callback) the whole document as
JSON and reads back the JSON it returns: an exchange. Consecutive filters
of this process are one callback, a ``Run``, by default; one of pandoc's
own (Lua, a JSON program, citeproc) ends a run. From one member to the next:

- from a Python filter (a ``Filter``, a function) to another: the same
  ``Pandoc``, as it is;
- from a Python filter to one on bytes (a wasm filter, a script reading
  its standard input, a script run as a subprocess): written once;
- from one on bytes to a Python filter: parsed once;
- from one on bytes to another: the bytes exactly as written, unchecked.

A script run in this process is either: given the document as the member
before left it, it reads its standard input (then made, if the document is
parsed) or hands its ``Filter``s over, which run on the document parsed.

``"separate-filters": True`` (or ``LIBPANDOC_SEPARATE_FILTERS=1``) makes
each filter its own run, so that pandoc reads and checks the document
between any two.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Mapping
from typing import Any

import panir
from panir import Conversion, Filter, Pandoc

from ._scripts import Script

VAR = "LIBPANDOC_SEPARATE_FILTERS"
KEY = "separate-filters"

_HINT = f" (its input is what filters[{{}}] wrote, unchecked: {VAR}=1 has pandoc check it)"


def separate(given: Any) -> bool:
    """Whether each filter is its own exchange: ``given`` (the options'
    ``"separate-filters"``) if not None, else ``LIBPANDOC_SEPARATE_FILTERS``:
    ``1`` yes; unset, empty or ``0`` no; anything else an error, as a safety
    switch is not to be ignored."""
    if given is not None:
        if not isinstance(given, bool):
            from . import _error

            raise _error("PandocOptionError", f'"{KEY}": True or False, not {given!r}')
        return given
    v = os.environ.get(VAR)
    if v in (None, "", "0"):
        return False
    if v == "1":
        return True
    raise ValueError(f"{VAR}: 1 (each filter its own exchange with pandoc), or 0 or empty, "
                     f"not {json.dumps(v, ensure_ascii=False)}")


class Typed:
    """A Python filter: a ``panir.Filter``, or a function taking the
    document (and optionally the conversion) that changes it or returns a
    new one."""

    def __init__(self, f: Any) -> None:
        self.f = f
        self.conversion = isinstance(f, Filter) or _takes_conversion(f)

    def __call__(self, doc: Pandoc, conversion: Conversion) -> Pandoc:
        f = self.f
        if isinstance(f, Filter):
            doc = f(doc, conversion=conversion)
        else:
            result = f(doc, conversion) if self.conversion else f(doc)
            doc = doc if result is None else result
        if not isinstance(doc, Pandoc):
            raise TypeError(f"the filter {getattr(f, '__qualname__', f)!r} returned "
                            f"{type(doc).__name__}, not a Pandoc")
        return doc


def _takes_conversion(fn: Any) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2


class Run:
    """One callback of libpandoc's, one exchange with pandoc: consecutive
    filters of this process, each with its place in the list given. A
    member is a ``Typed`` (on the parsed document), a ``Script`` run in
    this process (either), or a callable on bytes ``(doc, context) ->
    doc`` (a ``WasmFilter``, a script run as a subprocess).

    A failing member fails the run, the later ones not run; its exception
    is raised as it is, with a note naming it, ``filters[N]: ...``, and,
    when what it failed on went unchecked from a member on bytes before it,
    that one too. ``token``, the call's ``CancelToken`` (set for each
    call): a cancelled one stops the run before the next member."""

    def __init__(self, members: list[tuple[int, Any]], options: Mapping[str, Any] | None) -> None:
        self.members = members
        self.options = None if options is None else {k: v for k, v in options.items()
                                                     if k != "filters"}
        self.token: Any = None

    def __repr__(self) -> str:
        return f"Run({self.members!r})"

    def __call__(self, data: bytes, context: bytes) -> bytes:
        from . import PandocCancelledError

        # the document: parsed (`doc`), or bytes (`data`), pandoc's, written
        # from `doc`, or what the member at place `wrote` wrote, unchecked
        doc: Pandoc | None = None
        wrote: int | None = None

        def conversion() -> Conversion:
            # (one for each: one filter's changes to it don't reach the next)
            return Conversion.from_context(json.loads(context), self.options)

        def parsed() -> Pandoc:
            nonlocal doc
            if doc is None:
                try:
                    doc = panir.loads(data)
                except Exception as e:
                    e.add_note(f"filters[{i}]: what it was given could not be read"
                               + ("" if wrote is None else _HINT.format(wrote)))
                    raise
            return doc

        def written() -> bytes:
            nonlocal data
            if data is None:
                data = panir.dumps(doc).encode()
            return data

        for k, (i, m) in enumerate(self.members):
            if k and self.token is not None and self.token.cancelled:
                # (pandoc stops a cancelled conversion only when the callback returns)
                raise PandocCancelledError("Cancelled", "the call was cancelled")
            unchecked = None  # the member that wrote what this one fails on, if so
            try:
                if isinstance(m, Typed):
                    out = m(parsed(), conversion())
                    doc, data, wrote = out, None, None
                    continue
                if isinstance(m, Script):
                    unchecked = wrote
                    answer = m(written, context)
                    if isinstance(answer, bytes):
                        doc, data, wrote = None, answer, i
                        continue
                    unchecked = None
                    d = parsed()
                    try:
                        for f in answer:
                            d = f(d, conversion=conversion())
                    except Exception as e:  # (what a filter it handed over raised)
                        raise m.failed(e) from e
                    doc, data, wrote = d, None, None
                    continue
                unchecked = wrote
                answer = m(written(), context)
                doc, data, wrote = None, answer, i
            except Exception as e:
                if not any(n.startswith(f"filters[{i}]:") for n in getattr(e, "__notes__", ())):
                    e.add_note(f"filters[{i}]: this filter failed"
                               + ("" if unchecked is None else _HINT.format(unchecked)))
                raise
        return written()

